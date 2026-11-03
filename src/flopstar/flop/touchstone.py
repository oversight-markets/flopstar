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
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .secretfiles import SecretFileError
from .secretfiles import read_secret_file as _read_secret_file

# SPEC: Touchstone's API is being updated in parallel; bump this with any wire change and run
# `flopstar flop touchstone check` against its /openapi.json.
SCHEMA_VERSION = "flopstar.touchstone.v1"
TURNS_PATH = "/v1/streak/turns"
AGENTS_PATH = "/v1/agents"

# Our payloads as JSON-schema type names (a list means any of them). The `check` command diffs these
# against Touchstone's published request schemas.
# Exactly Touchstone's TurnRecord (web/openapi.json at c00367e), minus fields we have no value for
# (model_identity needs registry hashes; prompt_token_ids, settlement_tx). Touchstone silently
# drops undeclared fields, so model/miner/network live only in our ledger; the channel ties a turn
# to its miner on chain.
TURN_FIELDS: dict[str, str | list[str]] = {
    "agent_did": "string",
    "channel_id": ["string", "null"],  # the gateway route has no channel
    "turn_index": "integer",
    "route": "string",
    "enc_scheme": "string",
    "request_enc": "string",
    "response_enc": "string",
    "request_sha256": "string",
    "response_sha256": "string",
    "token_ids": ["array", "null"],
    "timings": "object",  # values must be ints
    "receipt": ["object", "null"],
    "sdk": "object",  # {name, version}
    "sdk_verification": ["object", "null"],  # {ok, message?, error_kind?}; null = not SDK-checked
    "recorded_at": "string",
    "client_digest": "string",
}
# Touchstone's AgentCreate (main @ c00367e): {did, account_ss58, label}. Registering a DID twice
# returns 400 "already exists", which `register` treats as success.
AGENT_FIELDS: dict[str, str | list[str]] = {
    "did": "string",
    "account_ss58": "string",
    "label": "string",
}

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
    """An owner-only file or a systemd credential (secretfiles.py); refuses anything looser."""
    try:
        return _read_secret_file(path, what)
    except SecretFileError as e:
        raise TouchstoneConfigError(str(e)) from None


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
    """32 bytes as base64 (Touchstone's TOUCHSTONE_STREAK_KEY_FILE format), 64 hex chars, or raw."""
    text = data.strip()
    try:
        key = base64.b64decode(text, validate=True)
        if len(key) == 32:
            return key
    except ValueError:
        pass
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
    Touchstone's `aes-256-gcm/v1` (touchstone_streak/crypto.py, streak-bridge/src/streak-key.ts):
    base64 of 0x01 || 12-byte random nonce || ciphertext || 16-byte tag, AAD = UTF-8 of
    `agent_did + "\n" + client_digest`. Request and response share the AAD (Touchstone's helpers add
    no direction tag); a swap is caught by request_sha256/response_sha256, which we always send.
    """

    SCHEME = "aes-256-gcm/v1"
    VERSION = b"\x01"

    def __init__(self, key: bytes):
        if len(key) != 32:
            raise TouchstoneConfigError("streak key must be 32 bytes")
        self._aead = AESGCM(key)

    @staticmethod
    def aad(agent_did: str, digest: str) -> bytes:
        return f"{agent_did}\n{digest}".encode()

    def encrypt(self, data: bytes, aad: bytes) -> str:
        nonce = os.urandom(12)
        return base64.b64encode(self.VERSION + nonce + self._aead.encrypt(nonce, data, aad)).decode()

    def decrypt(self, blob: str, aad: bytes) -> bytes:
        raw = base64.b64decode(blob)
        if raw[:1] != self.VERSION:
            raise ValueError(f"not an {self.SCHEME} blob")
        return self._aead.decrypt(raw[1:13], raw[13:], aad)


# ---- payloads ---------------------------------------------------------------------------------

def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def client_digest(route: str, channel_id: str | None, turn_index: int | None,
                  request_sha256: str) -> str:
    """Touchstone's SDK digest: sha256 hex of `route|channel_id or ""|turn_index or ""|req sha256`.

    Touchstone dedupes on it, so two turns with the same route, channel, index and request bytes are
    one record; callers must give each turn a distinct (channel_id, turn_index).
    """
    index = "" if turn_index is None else str(turn_index)
    return hashlib.sha256(
        f"{route}|{channel_id or ''}|{index}|{request_sha256}".encode()).hexdigest()


def build_turn(*, cipher: StreakCipher, agent_did: str, spend: dict, turn_index: int,
               request_bytes: bytes, response_bytes: bytes, token_ids: list[int] | None,
               timings: dict, receipt: dict | None, sdk_version: str, recorded_at: str) -> dict:
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
    if spend["route"] == "direct" and not channel:
        raise ValueError(f"spend {spend['id']}: direct route needs a channel_id")
    digest = client_digest(spend["route"], channel, turn_index, request_sha)
    aad = cipher.aad(agent_did, digest)
    payload = {
        "agent_did": agent_did,
        "channel_id": channel,
        "turn_index": turn_index,
        "route": spend["route"],
        "enc_scheme": cipher.SCHEME,
        "request_enc": cipher.encrypt(request_bytes, aad),
        "response_enc": cipher.encrypt(response_bytes, aad),
        "request_sha256": request_sha,
        "response_sha256": response_sha,
        "token_ids": token_ids,
        "timings": int_timings(timings),
        "receipt": receipt,
        "sdk": {"name": "flop-sdk", "version": sdk_version},
        "sdk_verification": sdk_verdict(spend.get("verification")),
        "recorded_at": recorded_at,
        "client_digest": digest,
    }
    validate(payload, TURN_FIELDS)
    return payload


def int_timings(timings: dict) -> dict[str, int]:
    """Touchstone's timings are {str: int}: an ISO `started_at` becomes `started_at_ms` (epoch ms)."""
    from datetime import datetime
    out = {}
    for key, value in timings.items():
        if key == "started_at" and isinstance(value, str):
            out["started_at_ms"] = int(datetime.fromisoformat(value).timestamp() * 1000)
        elif type(value) is int:
            out[key] = value
        else:
            raise ValueError(f"timing {key!r} is not an int")
    return out


def sdk_verdict(verification: str | None) -> dict | None:
    """The FLOP SDK's own verdict; null when the SDK never checked the turn (gateway route)."""
    if verification == "sdk_verified":
        return {"ok": True}
    if verification == "failed":
        return {"ok": False, "error_kind": "verification_failed"}
    return None


def agent_payload(agent_did: str, chain_account: str) -> dict:
    """`chain_account` is the spend account: Touchstone's indexer links channels by their opener."""
    payload = {"did": agent_did, "account_ss58": chain_account, "label": "flopstar"}
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


def already_registered(status_code: int, body: str) -> bool:
    """Touchstone answers a repeat registration with 400 "... already exists"; that is success."""
    return status_code == 400 and "already exists" in body.lower()


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
