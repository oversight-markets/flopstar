"""
Report completed FLOP requests to Touchstone (our verification service, on another server).

One module holds the wire schema (SCHEMA_VERSION, TURN_FIELDS, AGENT_FIELDS) so it can be diffed
against Touchstone's /openapi.json (`flopstar flop touchstone check`).

Rules:
- Never block inference: `report` never raises; failures go to data/touchstone-buffer.jsonl and are
  replayed in order (oldest first) before anything new is sent.
- A ledger row is marked `touchstone_reported_at` only on a 2xx.
- Request/response bytes leave this server only encrypted with the per-agent streak key.
- Plain http:// is refused unless the host resolves only to private / loopback / Tailscale (CGNAT)
  addresses; redirects are never followed. The API should be reachable only over the private
  network (docs/FLOP.md §10).
- The bearer token and streak key come from owner-only files; neither is ever logged.
"""

import base64
import fcntl
import hashlib
import ipaddress
import json
import os
import socket
import stat
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# SPEC: Touchstone's API is being updated in parallel; bump this with any wire change and run
# `flopstar flop touchstone check` against its /openapi.json.
SCHEMA_VERSION = "flopstar.touchstone.v0"
TURNS_PATH = "/v1/streak/turns"
AGENTS_PATH = "/v1/agents"

# Our payloads as JSON-schema type names (a list means any of them). The `check` command diffs these
# against Touchstone's published request schemas.
TURN_FIELDS: dict[str, str | list[str]] = {
    "schema_version": "string",
    "agent_did": "string",
    "channel_id": ["string", "null"],  # the gateway route has no channel
    "turn_index": "integer",
    "route": "string",
    "model": "string",
    "miner": ["string", "null"],
    "request_enc": "string",
    "response_enc": "string",
    "request_sha256": "string",
    "response_sha256": "string",
    "token_ids": ["array", "null"],
    "timings": "object",
    "receipt": ["object", "null"],
    "sdk_verification": "string",
    "sdk_version": "string",
    "cli_version": ["string", "null"],
    "network": "string",
    "client_digest": "string",
    "recorded_at": "string",
}
AGENT_FIELDS: dict[str, str | list[str]] = {
    "agent_did": "string",
    "chain_account": "string",
    "label": "string",
}
# Excluded from client_digest: ciphertexts use a fresh nonce each time, and the digest itself.
_UNDIGESTED = ("request_enc", "response_enc", "client_digest")

# Explicit, not ipaddress.is_private: that also admits documentation/benchmark/reserved ranges.
PRIVATE_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",  # RFC 1918 (WireGuard, VPC)
    "100.64.0.0/10",  # CGNAT: Tailscale IPv4
    "127.0.0.0/8", "::1/128",  # loopback
    "fc00::/7",  # IPv6 ULA, incl. Tailscale fd7a:115c:a1e0::/48
))


class TouchstoneConfigError(Exception):
    """Touchstone settings missing or unsafe."""


class UnsafeURL(TouchstoneConfigError):
    """A Touchstone URL that would send data over a public plain-HTTP path."""


# ---- settings ---------------------------------------------------------------------------------

def read_secret_file(path: Path, what: str) -> bytes:
    """Read an owner-only (no group/other bits) secret file, refusing anything looser."""
    try:
        st = path.stat()
    except FileNotFoundError:
        raise TouchstoneConfigError(f"{what} file {path} does not exist") from None
    if not stat.S_ISREG(st.st_mode):
        raise TouchstoneConfigError(f"{what} file {path} is not a regular file")
    if st.st_mode & 0o077:
        raise TouchstoneConfigError(f"{what} file {path} must be mode 0600 (is {st.st_mode & 0o777:o})")
    if st.st_uid != os.getuid():
        raise TouchstoneConfigError(f"{what} file {path} is not owned by this user")
    return path.read_bytes()


@dataclass(frozen=True)
class TouchstoneSettings:
    api_url: str
    token: str
    streak_key: bytes

    def __repr__(self) -> str:  # never show the token or key
        return f"TouchstoneSettings(api_url={self.api_url!r})"

    @classmethod
    def from_env(cls, env=None) -> "TouchstoneSettings":
        env = os.environ if env is None else env
        if env.get("TOUCHSTONE_API_TOKEN"):
            raise TouchstoneConfigError(
                "TOUCHSTONE_API_TOKEN is set in the environment; put the token in a 0600 file "
                "and set TOUCHSTONE_API_TOKEN_FILE instead")
        missing = [k for k in ("TOUCHSTONE_API_URL", "TOUCHSTONE_API_TOKEN_FILE",
                               "TOUCHSTONE_STREAK_KEY_FILE") if not env.get(k)]
        if missing:
            raise TouchstoneConfigError(f"not configured: {', '.join(missing)} unset")
        token = read_secret_file(Path(env["TOUCHSTONE_API_TOKEN_FILE"]).expanduser(),
                                 "Touchstone token").decode().strip()
        if not token:
            raise TouchstoneConfigError("Touchstone token file is empty")
        key = parse_streak_key(read_secret_file(
            Path(env["TOUCHSTONE_STREAK_KEY_FILE"]).expanduser(), "streak key"))
        return cls(api_url=env["TOUCHSTONE_API_URL"].rstrip("/"), token=token, streak_key=key)


def parse_streak_key(data: bytes) -> bytes:
    """SPEC: key file format is ours to choose until Touchstone says otherwise: 64 hex chars or 32 raw bytes."""
    text = data.strip()
    if len(text) == 64:
        try:
            return bytes.fromhex(text.decode())
        except (ValueError, UnicodeDecodeError):
            pass
    if len(data) == 32:
        return data
    raise TouchstoneConfigError("streak key must be 32 bytes (raw) or 64 hex characters")


# ---- network path -----------------------------------------------------------------------------

def _private(addr: str) -> bool:
    ip = ipaddress.ip_address(addr.split("%")[0])
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip.version == n.version and ip in n for n in PRIVATE_NETWORKS)


def check_url(url: str, resolve: Callable = socket.getaddrinfo) -> list[str]:
    """
    Raise UnsafeURL for a plain-http URL whose host is not private. Returns warnings (e.g. https to
    a public address, which works but means Touchstone is exposed publicly).
    """
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise UnsafeURL(f"Touchstone URL must be http(s)://host...: {url!r}")
    host = parts.hostname
    try:
        addrs = [ipaddress.ip_address(host).compressed]
    except ValueError:
        try:
            addrs = sorted({info[4][0] for info in resolve(host, parts.port or 443)})
        except OSError as e:
            raise UnsafeURL(f"cannot resolve {host}: {e}") from e
    if not addrs:
        raise UnsafeURL(f"{host} resolves to nothing")
    public = [a for a in addrs if not _private(a)]
    if public and parts.scheme == "http":
        raise UnsafeURL(f"refusing plain http to non-private address(es) {public} for {host}")
    warning = (f"{host} resolves to public address(es) {public}; keep Touchstone on the private "
               "network")
    return [warning] if public else []


# ---- encryption -------------------------------------------------------------------------------

class StreakCipher:
    """
    SPEC: algorithm and associated data must match Touchstone's decryptor. Provisional choice:
    AES-256-GCM, 12-byte random nonce, output nonce || ciphertext || tag, base64; the AAD binds the
    ciphertext to its agent, channel, turn and field so it cannot be replayed as another record.
    """

    SCHEME = "aes-256-gcm/v0"

    def __init__(self, key: bytes):
        if len(key) != 32:
            raise TouchstoneConfigError("streak key must be 32 bytes")
        self._aead = AESGCM(key)

    @staticmethod
    def aad(agent_did: str, channel_id: str | None, turn_index: int, field: str) -> bytes:
        return f"{SCHEMA_VERSION}|{agent_did}|{channel_id or ''}|{turn_index}|{field}".encode()

    def encrypt(self, data: bytes, aad: bytes) -> str:
        nonce = os.urandom(12)
        return base64.b64encode(nonce + self._aead.encrypt(nonce, data, aad)).decode()

    def decrypt(self, blob: str, aad: bytes) -> bytes:
        raw = base64.b64decode(blob)
        return self._aead.decrypt(raw[:12], raw[12:], aad)


# ---- payloads ---------------------------------------------------------------------------------

def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def client_digest(payload: dict) -> str:
    """sha256 over the canonical record minus ciphertexts: identical for every rebuild of a turn."""
    return hashlib.sha256(canonical({k: v for k, v in payload.items()
                                     if k not in _UNDIGESTED})).hexdigest()


def build_turn(*, cipher: StreakCipher, agent_did: str, spend: dict, turn_index: int,
               request_bytes: bytes, response_bytes: bytes, token_ids: list[int] | None,
               timings: dict, receipt: dict | None, sdk_version: str, cli_version: str | None,
               recorded_at: str) -> dict:
    """One `POST /v1/streak/turns` body from a ledger row and the exact bytes exchanged."""
    request_sha = hashlib.sha256(request_bytes).hexdigest()
    response_sha = hashlib.sha256(response_bytes).hexdigest()
    if spend["request_sha256"] != request_sha:
        raise ValueError(f"spend {spend['id']}: request bytes do not match the ledger hash")
    if spend.get("response_sha256") not in (None, response_sha):
        raise ValueError(f"spend {spend['id']}: response bytes do not match the ledger hash")
    if token_ids is not None and not all(type(t) is int and t >= 0 for t in token_ids):
        raise ValueError("token_ids must be non-negative ints")
    channel = spend.get("channel_id")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "agent_did": agent_did,
        "channel_id": channel,
        "turn_index": turn_index,
        "route": spend["route"],
        "model": spend["model"],
        "miner": spend.get("miner"),
        "request_enc": cipher.encrypt(request_bytes,
                                      cipher.aad(agent_did, channel, turn_index, "request")),
        "response_enc": cipher.encrypt(response_bytes,
                                       cipher.aad(agent_did, channel, turn_index, "response")),
        "request_sha256": request_sha,
        "response_sha256": response_sha,
        "token_ids": token_ids,
        "timings": timings,
        "receipt": receipt,
        "sdk_verification": spend.get("verification") or "unverified",
        "sdk_version": sdk_version,
        "cli_version": cli_version,
        "network": spend["network"],
        "recorded_at": recorded_at,
    }
    payload["client_digest"] = client_digest(payload)
    validate(payload, TURN_FIELDS)
    return payload


def agent_payload(agent_did: str, chain_account: str) -> dict:
    payload = {"agent_did": agent_did, "chain_account": chain_account, "label": "flopstar"}
    validate(payload, AGENT_FIELDS)
    return payload


_PY_TYPES = {"string": str, "integer": int, "object": dict, "array": list, "null": type(None)}


def validate(payload: dict, schema: dict) -> None:
    """Exact key set and JSON types, so we never send a shape we haven't declared."""
    if set(payload) != set(schema):
        raise ValueError(f"payload keys differ from schema: missing {set(schema) - set(payload)}, "
                         f"extra {set(payload) - set(schema)}")
    for key, types in schema.items():
        allowed = [types] if isinstance(types, str) else types
        value = payload[key]
        if not any(type(value) is _PY_TYPES[t] for t in allowed):
            raise ValueError(f"{key} must be {allowed}, got {type(value).__name__}")


# ---- schema check against Touchstone's OpenAPI --------------------------------------------------

def _resolve(spec: dict, node: dict) -> dict:
    seen = 0
    while "$ref" in node and seen < 20:
        ref = node["$ref"]
        if not ref.startswith("#/"):
            raise ValueError(f"external $ref not supported: {ref}")
        node = spec
        for part in ref[2:].split("/"):
            node = node[part]
        seen += 1
    return node


def _types(spec: dict, prop: dict) -> set[str]:
    prop = _resolve(spec, prop)
    out = set()
    t = prop.get("type")
    if isinstance(t, list):
        out |= set(t)
    elif t:
        out.add(t)
    if prop.get("nullable"):
        out.add("null")
    for alt in prop.get("anyOf", []) + prop.get("oneOf", []):
        out |= _types(spec, alt)
    if out & {"number"}:
        out.add("integer")
    return out


def diff_against_openapi(spec: dict, path: str, ours: dict) -> list[str]:
    """Differences between our payload fields and the request schema for `POST path`."""
    try:
        body = spec["paths"][path]["post"]["requestBody"]["content"]["application/json"]["schema"]
    except KeyError:
        return [f"{path}: no POST application/json request schema in Touchstone's OpenAPI"]
    schema = _resolve(spec, body)
    props = schema.get("properties", {})
    required = set(schema.get("required", []))
    problems = []
    for key in sorted(required - set(ours)):
        problems.append(f"{path}: Touchstone requires {key!r}, we do not send it")
    for key in sorted(set(ours) - set(props)):
        if props:
            problems.append(f"{path}: we send {key!r}, Touchstone's schema does not declare it")
    for key in sorted(set(ours) & set(props)):
        theirs = _types(spec, props[key])
        mine = {ours[key]} if isinstance(ours[key], str) else set(ours[key])
        if theirs and not mine <= theirs:
            problems.append(f"{path}: {key!r} is {sorted(mine)} for us, {sorted(theirs)} for them")
    return problems


# ---- the reporter -----------------------------------------------------------------------------

class TouchstoneReporter:
    """Sends turns, buffering on any failure. `report` and `replay` never raise."""

    def __init__(self, settings: TouchstoneSettings | None, buffer_path: Path, ledger=None,
                 transport: httpx.BaseTransport | None = None,
                 resolve: Callable = socket.getaddrinfo, timeout: float = 10.0):
        self.settings = settings
        self.buffer_path = buffer_path
        self.ledger = ledger
        self.resolve = resolve
        self._client = httpx.Client(transport=transport, timeout=timeout, follow_redirects=False)
        self.last_error: str | None = None

    def close(self) -> None:
        self._client.close()

    # buffer -----------------------------------------------------------------------------------

    @contextmanager
    def _locked(self):
        lock = self.buffer_path.with_suffix(".lock")
        with lock.open("a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def buffered(self) -> list[dict]:
        if not self.buffer_path.exists():
            return []
        out = []
        for line in self.buffer_path.read_text().splitlines():
            if line.strip():
                out.append(json.loads(line))
        return out

    def _append(self, entry: dict) -> None:
        with self.buffer_path.open("a") as f:
            f.write(json.dumps(entry, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def _rewrite(self, entries: list[dict]) -> None:
        tmp = self.buffer_path.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(e, sort_keys=True) + "\n" for e in entries))
        os.replace(tmp, self.buffer_path)

    # sending ----------------------------------------------------------------------------------

    def post(self, path: str, payload: dict) -> httpx.Response:
        """One authenticated POST over a checked URL. Raises on refusal or transport errors."""
        if self.settings is None:
            raise TouchstoneConfigError("Touchstone is not configured")
        check_url(self.settings.api_url, self.resolve)
        return self._client.post(self.settings.api_url + path, json=payload,
                                 headers={"Authorization": f"Bearer {self.settings.token}"})

    def get(self, path: str) -> httpx.Response:
        if self.settings is None:
            raise TouchstoneConfigError("Touchstone is not configured")
        check_url(self.settings.api_url, self.resolve)
        return self._client.get(self.settings.api_url + path,
                                headers={"Authorization": f"Bearer {self.settings.token}"})

    def _send(self, entry: dict) -> bool:
        try:
            r = self.post(entry["path"], entry["payload"])
        except Exception as e:  # noqa: BLE001 - reporting must never break inference
            self.last_error = f"{type(e).__name__}: {e}"
            return False
        if not 200 <= r.status_code < 300:
            self.last_error = f"HTTP {r.status_code}"
            return False
        if self.ledger is not None and entry.get("spend_id") is not None:
            try:
                self.ledger.mark_reported(entry["spend_id"])
            except Exception as e:  # noqa: BLE001 - sent is sent; record why the mark failed
                self.last_error = f"sent, but ledger mark failed: {e}"
        return True

    def replay(self) -> int:
        """Send buffered entries oldest first, stopping at the first failure. Returns how many remain."""
        try:
            with self._locked():
                return self._replay_locked()
        except Exception as e:  # noqa: BLE001 - replay must never raise
            self.last_error = f"replay: {type(e).__name__}: {e}"
            return -1

    def _replay_locked(self) -> int:
        entries = self.buffered()
        sent = 0
        for entry in entries:
            if not self._send(entry):
                break
            sent += 1
        if sent:
            self._rewrite(entries[sent:])
        return len(entries) - sent

    def report(self, spend_id: int | None, payload: dict, path: str = TURNS_PATH) -> str:
        """Send one record after any backlog; buffer it if that fails. Returns 'sent' or 'buffered'."""
        entry = {"spend_id": spend_id, "path": path, "payload": payload}
        try:
            with self._locked():
                if self._replay_locked() == 0 and self._send(entry):
                    return "sent"
                self._append(entry)
                return "buffered"
        except Exception as e:  # noqa: BLE001 - last resort: keep the record even if locking failed
            self.last_error = f"report: {type(e).__name__}: {e}"
            try:
                self._append(entry)
            except Exception as e:  # noqa: BLE001 - nothing left to do but say so
                self.last_error += f"; buffering also failed: {e}"
            return "buffered"
