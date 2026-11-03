"""
Buy inference: one interface, three routes (docs/FLOP.md §8).

direct   The pinned `flop session run --json` CLI (TypeScript runDirectSession: finalized channel,
         verified receipts before payment), launched only through FlopPolicy.run_signing_cli with
         the spend keystore. Preferred: the only route whose evidence the SDK verifies.
gateway  `POST {gateway}/chat/completions` with a bounded API key. Convenient, unverified.
native   Not available: flop-sdk has no inference-request or DID-signed flow (SPEC S1).

Everything a route returns is untrusted data: replies are stored and shown, never executed, posted
or used to sign. Every request is authorized and recorded by FlopPolicy before any network call.
"""

import hashlib
import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

from .amounts import format_flop
from .config import ConfigError, FlopConfig, ss58_pubkey
from .ledger import Ledger
from .policy import ActionKind, CliTimeout, FlopPolicy
from .touchstone import TouchstoneConfigError, read_secret_file

ROUTES = ("direct", "gateway", "native")
DIRECT_TIMEOUT_S = 900  # turn deadline 60 s + ack window + settlement waits (FLOP.md §3.3)
GATEWAY_TIMEOUT_S = 120
DEFAULT_MAX_TOKENS = 512
# Container limits for the CLI. A runaway or oversized CLI is OOM-killed inside its cgroup instead
# of exhausting the host: the 1 GB droplet hard-reset when an uncapped CLI container ran
# (docs/FLOP.md §11).
DOCKER_LIMITS = ("--memory", "640m", "--memory-swap", "1g", "--pids-limit", "256")
_CHANNEL = re.compile(r"\(channel (0x[0-9a-fA-F]{64})\)")
_HEX32 = re.compile(r"^0x[0-9a-fA-F]{64}$")


class RouteUnavailable(Exception):
    """A route that cannot run here (no CLI runtime, no endpoint, no key, not implemented)."""


class CliOutputError(Exception):
    """The CLI printed something other than the documented JSON."""


@dataclass
class Result:
    spend_id: int | None
    route: str
    model: str
    status: str  # settled | failed | dry-run
    miner: str | None = None
    channel_id: str | None = None
    reply: str | None = None  # untrusted model output
    settle_tx: str | None = None
    verification: str | None = None
    max_flop: int = 0
    settled_flop: int | None = None
    request_bytes: bytes = b""
    response_bytes: bytes = b""
    timings: dict = field(default_factory=dict)
    receipt: dict | None = None
    error: str | None = None
    needs_recovery: bool = False
    touchstone: str | None = None


def build_request(model: str, prompt: str, max_tokens: int) -> bytes:
    """The exact request bytes we hash, ledger and report (OpenAI-style chat body)."""
    if not prompt.strip():
        raise ValueError("empty prompt")
    if type(max_tokens) is not int or max_tokens <= 0:
        raise ValueError("max_tokens must be a positive int")
    body = {"model": model, "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


# ---- miner selection ----------------------------------------------------------------------------

def choose_miner(cfg: FlopConfig, ledger: Ledger, requested: str | None = None) -> tuple[str, str]:
    """
    (miner SS58, endpoint). Only configured miners (SPEC S3: no discovery), never a blocklisted one
    even if it is the only option, preferring those we have used least recently.
    """
    if requested:
        if cfg.caps.is_blocked_miner(requested):
            raise RouteUnavailable(f"miner {requested} is on the same_control_miners blocklist")
        for ss58, url in cfg.miners.items():
            if ss58_pubkey(ss58) == ss58_pubkey(requested):
                return ss58, url
        raise RouteUnavailable(f"miner {requested} has no endpoint in [{cfg.network.name}.miners]")
    candidates = [m for m in cfg.miners if not cfg.caps.is_blocked_miner(m)]
    if not candidates:
        raise RouteUnavailable(f"no usable miner in [{cfg.network.name}.miners] (SPEC S3)")
    last = ledger.last_used_miners(cfg.network.name)
    miner = min(candidates, key=lambda m: (last.get(m, ""), m))
    return miner, cfg.miners[miner]


# ---- the CLI ------------------------------------------------------------------------------------

def cli_prefix(cfg: FlopConfig, which=shutil.which) -> tuple[list[str], str, str]:
    """(argv prefix, keystore path as the CLI sees it, cli version label) for the pinned CLI."""
    runner = cfg.cli_runner or ("docker" if which("docker") else None)
    keystore = cfg.keystore_path
    if runner == "docker":
        if not which("docker"):
            raise RouteUnavailable("FLOPSTAR_FLOP_CLI=docker but docker is not installed")
        if not cfg.network.cli_image:
            raise RouteUnavailable(f"no pinned cli_image for {cfg.network.name} (SPEC S5)")
        prefix = ["docker", "run", "--rm", *DOCKER_LIMITS, "--network", "host",
                  "-e", "FLOP_KEY_PASSWORD", "-e", "FLOP_INDEXER_URL",
                  "-v", f"{keystore.parent}:/keys:ro", cfg.network.cli_image]
        version = cfg.network.cli_image.split("@")[0].rsplit(":", 1)[-1]
        return prefix, f"/keys/{keystore.name}", version
    if runner == "npx":
        if not which("npx") or not cfg.network.cli_npm_version:
            raise RouteUnavailable("FLOPSTAR_FLOP_CLI=npx needs Node 22+ and cli_npm_version")
        version = cfg.network.cli_npm_version
        return (["npx", "--yes", "--package", f"@flop-labs/cli@{version}", "flop"],
                str(keystore), version)
    if runner and os.path.isabs(runner) and os.access(runner, os.X_OK):
        return [runner], str(keystore), cfg.network.cli_npm_version or "unknown"
    raise RouteUnavailable("no way to run the flop CLI here: install Docker (preferred, pinned by "
                           "digest) or Node 22+, or set FLOPSTAR_FLOP_CLI")


def direct_argv(cfg: FlopConfig, prefix: list[str], keystore_arg: str, *, miner: str,
                endpoint: str, model: str, prompt: str, max_flop: int,
                max_tokens: int) -> list[str]:
    """`flop session run` argv (cli.js:5519). The keystore password goes in FLOP_KEY_PASSWORD only."""
    return [*prefix,
            "--network", cfg.network.name, "--rpc", cfg.network.rpc_url,
            "--keystore", keystore_arg, "--password-env", "FLOP_KEY_PASSWORD",
            "--output", "json",
            "session", "run", "--miner", miner, "--model", model, "--prompt", prompt,
            "--max-flop", format_flop(max_flop), "--max-tokens", str(max_tokens),
            "--via", "auto", "--endpoint", endpoint, "--json"]


SUCCESS_FIELDS = {"reply": str, "miner": str, "channel": str, "settle_tx": str,
                  "cooperative": bool, "verified_turns": int}
OPTIONAL_FIELDS = {"finish_reason": str, "completion_tokens": str, "wallet": str, "ok": bool}


def parse_cli_success(stdout: str) -> dict:
    """Strictly parse `session run --json` output (cli.js:5571-5580). Raises CliOutputError."""
    try:
        data = json.loads(stdout)
    except ValueError as e:
        raise CliOutputError(f"session run printed non-JSON: {e}") from e
    if not isinstance(data, dict):
        raise CliOutputError("session run printed a non-object")
    unknown = set(data) - set(SUCCESS_FIELDS) - set(OPTIONAL_FIELDS)
    if unknown:
        raise CliOutputError(f"session run printed unexpected fields {sorted(unknown)}")
    for key, typ in {**SUCCESS_FIELDS, **OPTIONAL_FIELDS}.items():
        if key in data and data[key] is not None and type(data[key]) is not typ:
            raise CliOutputError(f"{key} must be {typ.__name__}")
        if key in SUCCESS_FIELDS and key not in data:
            raise CliOutputError(f"session run output lacks {key}")
    if not _HEX32.match(data["channel"]):
        raise CliOutputError("channel is not a 32-byte hex id")
    if data.get("ok") is False:
        raise CliOutputError("session run printed ok:false on exit 0")
    if "wallet" in data:
        raise CliOutputError("session run used --new-wallet; Flopstar never does")
    return data


def parse_cli_failure(stderr: str) -> dict:
    """The last `{ok:false, code, message, ...}` object on stderr (cli.js:5045), plus channel id."""
    decoder, found = json.JSONDecoder(), None
    for i, ch in enumerate(stderr):
        if ch == "{":
            try:
                obj, _ = decoder.raw_decode(stderr, i)
            except ValueError:
                continue
            if isinstance(obj, dict) and obj.get("ok") is False:
                found = obj
    message = str(found.get("message", "")) if found else stderr.strip()[-500:]
    m = _CHANNEL.search(message) or _CHANNEL.search(stderr)
    return {
        "code": str(found.get("code")) if found else None,
        "message": message[:500],
        "retryable": found.get("retryable") if found else None,
        "channel_id": m.group(1).lower() if m else None,
        "recovery_failed": "on-chain recovery failed" in message,
    }


# ---- execution ----------------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def run_direct(cfg: FlopConfig, policy: FlopPolicy, *, model: str, prompt: str, purpose: str,
               max_flop: int, max_tokens: int, miner: str, endpoint: str, passphrase: str,
               signer: str, runner=None, which=shutil.which) -> Result:
    prefix, keystore_arg, _ = cli_prefix(cfg, which)
    request = build_request(model, prompt, max_tokens)
    argv = direct_argv(cfg, prefix, keystore_arg, miner=miner, endpoint=endpoint, model=model,
                       prompt=prompt, max_flop=max_flop, max_tokens=max_tokens)
    ledger = policy.ledger
    spend_id = policy.authorize_inference(route="direct", model=model, miner=miner,
                                          max_flop=max_flop, purpose=purpose,
                                          request_sha256=hashlib.sha256(request).hexdigest())
    ledger.transition(spend_id, "sent", {"endpoint": endpoint})
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/"),
           "FLOP_KEY_PASSWORD": passphrase}
    if cfg.network.indexer_url:
        env["FLOP_INDEXER_URL"] = cfg.network.indexer_url
    result = Result(spend_id=spend_id, route="direct", model=model, status="failed", miner=miner,
                    max_flop=max_flop, request_bytes=request)
    started, t0 = _now(), time.monotonic()
    try:
        proc = policy.run_signing_cli(ActionKind.INFERENCE, argv, env, signer=signer,
                                      spend_id=spend_id, timeout=DIRECT_TIMEOUT_S, runner=runner)
    except CliTimeout:
        result.error, result.needs_recovery = f"CLI timed out after {DIRECT_TIMEOUT_S}s", True
        ledger.transition(spend_id, "failed", {"error": result.error})
        policy.log_cli_result(spend_id, {"timeout": True})
        return result
    result.timings = {"started_at": started, "total_ms": int((time.monotonic() - t0) * 1000)}

    if proc.returncode != 0:
        failure = parse_cli_failure(proc.stderr or "")
        policy.log_cli_result(spend_id, {"exit": proc.returncode, **failure})
        result.error = f"{failure['code']}: {failure['message']}"
        result.channel_id = failure["channel_id"]
        result.needs_recovery = failure["channel_id"] is not None or failure["recovery_failed"]
        fill = {"channel_id": failure["channel_id"]} if failure["channel_id"] else {}
        ledger.transition(spend_id, "failed", {"exit": proc.returncode, **failure}, **fill)
        if failure["recovery_failed"]:
            policy.trip(f"spend {spend_id}: direct session failed and on-chain recovery failed "
                        f"(channel {failure['channel_id']}); run `flopstar flop recovery`")
        return result

    stdout = proc.stdout or ""
    try:
        data = parse_cli_success(stdout)
        if ss58_pubkey(data["miner"]) != ss58_pubkey(miner):
            raise CliOutputError(f"served by {data['miner']}, requested {miner}")
        if data["verified_turns"] < 1:
            raise CliOutputError("no verified turn")
    except (CliOutputError, ConfigError) as e:
        result.error, result.verification = str(e), "failed"
        ledger.transition(spend_id, "failed", {"error": str(e)[:500]}, verification="failed",
                          response_sha256=hashlib.sha256(stdout.encode()).hexdigest())
        policy.trip(f"spend {spend_id}: direct session verification failed: {e}")
        return result

    response = stdout.encode()
    result.status, result.verification = "settled", "sdk_verified"
    result.channel_id, result.settle_tx = data["channel"].lower(), data["settle_tx"]
    result.reply, result.response_bytes, result.receipt = data["reply"], response, data
    ledger.transition(
        spend_id, "settled", {"cooperative": data["cooperative"]},
        channel_id=result.channel_id, settlement_tx=data["settle_tx"],
        response_sha256=hashlib.sha256(response).hexdigest(),
        receipt_json=json.dumps(data, sort_keys=True), verification="sdk_verified",
        # SPEC(S6): the CLI does not report the settled amount; the ledger keeps max_flop.
    )
    policy.log_cli_result(spend_id, {"exit": 0, "channel": result.channel_id,
                                     "settle_tx": data["settle_tx"],
                                     "verified_turns": data["verified_turns"]})
    return result


def gateway_key(cfg: FlopConfig) -> str:
    if not cfg.gateway_key_file:
        raise RouteUnavailable("FLOPSTAR_FLOP_GATEWAY_KEY_FILE unset (SPEC S4: no capped key yet)")
    key = read_secret_file(cfg.gateway_key_file, "gateway API key").decode().strip()
    if not key:
        raise RouteUnavailable("gateway API key file is empty")
    return key


def parse_gateway(content: bytes) -> dict:
    """Strict OpenAI-style chat completion: choices[0].message.content must be a string."""
    try:
        data = json.loads(content)
    except ValueError as e:
        raise CliOutputError(f"gateway returned non-JSON: {e}") from e
    if not isinstance(data, dict) or not isinstance(data.get("choices"), list) or not data["choices"]:
        raise CliOutputError("gateway response has no choices")
    message = data["choices"][0].get("message") if isinstance(data["choices"][0], dict) else None
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise CliOutputError("gateway response has no message content")
    return data


def run_gateway(cfg: FlopConfig, policy: FlopPolicy, *, model: str, prompt: str, purpose: str,
                max_flop: int, max_tokens: int,
                transport: httpx.BaseTransport | None = None) -> Result:
    key = gateway_key(cfg)
    if not cfg.network.gateway_url:
        raise RouteUnavailable(f"{cfg.network.name} has no gateway")
    request = build_request(model, prompt, max_tokens)
    ledger = policy.ledger
    spend_id = policy.authorize_inference(route="gateway", model=model, miner=None,
                                          max_flop=max_flop, purpose=purpose,
                                          request_sha256=hashlib.sha256(request).hexdigest())
    ledger.transition(spend_id, "sent", {"gateway": cfg.network.gateway_url})
    result = Result(spend_id=spend_id, route="gateway", model=model, status="failed",
                    max_flop=max_flop, request_bytes=request)
    started, t0 = _now(), time.monotonic()
    try:
        with httpx.Client(transport=transport, timeout=GATEWAY_TIMEOUT_S,
                          follow_redirects=False) as client:
            r = client.post(f"{cfg.network.gateway_url}/chat/completions", content=request,
                            headers={"Authorization": f"Bearer {key}",
                                     "Content-Type": "application/json"})
    except httpx.HTTPError as e:
        result.error = f"{type(e).__name__}: {e}"[:300]
        ledger.transition(spend_id, "failed", {"error": result.error})
        return result
    result.timings = {"started_at": started, "total_ms": int((time.monotonic() - t0) * 1000)}
    if not r.is_success:
        # A 402 names a payment destination: we never pay an address taken from a response.
        result.error = f"HTTP {r.status_code}: {r.text[:300]}"
        ledger.transition(spend_id, "failed", {"http": r.status_code, "error": r.text[:300]})
        return result
    try:
        data = parse_gateway(r.content)
    except CliOutputError as e:
        result.error = str(e)
        ledger.transition(spend_id, "failed", {"error": str(e)},
                          response_sha256=hashlib.sha256(r.content).hexdigest())
        policy.trip(f"spend {spend_id}: malformed gateway response: {e}")
        return result
    evidence = {k: data[k] for k in ("id", "model", "usage", "created", "receipt") if k in data}
    result.status, result.verification = "settled", "unverified"
    result.reply, result.response_bytes = data["choices"][0]["message"]["content"], r.content
    result.receipt = evidence
    ledger.transition(spend_id, "settled", {"http": r.status_code},
                      response_sha256=hashlib.sha256(r.content).hexdigest(),
                      receipt_json=json.dumps(evidence, sort_keys=True, default=str),
                      verification="unverified")
    return result


def run_native(**_) -> Result:
    raise RouteUnavailable("native route: flop-sdk 0.2.0 has no inference-request or DID-signed "
                           "flow (docs/FLOP.md S1)")


# ---- Touchstone ---------------------------------------------------------------------------------

def report(cfg: FlopConfig, ledger: Ledger, result: Result, *, agent_did: str,
           cli_version: str | None, settings=None, transport=None) -> str:
    """Report a settled request to Touchstone; never raises. Returns sent|buffered|skipped: reason."""
    from flop_sdk import __version__ as sdk_version

    from . import touchstone as ts
    if result.status != "settled" or result.spend_id is None:
        return "skipped: not settled"
    try:
        settings = settings or ts.TouchstoneSettings.from_env()
    except ts.TouchstoneConfigError as e:
        return f"skipped: {e}"
    try:
        spend = ledger.get(result.spend_id)
        payload = ts.build_turn(
            cipher=ts.StreakCipher(settings.streak_key), agent_did=agent_did,
            spend=spend, turn_index=_turn_index(spend), request_bytes=result.request_bytes,
            response_bytes=result.response_bytes, token_ids=None,  # SPEC(S6): CLI omits ids
            timings=result.timings, receipt=result.receipt, sdk_version=sdk_version,
            recorded_at=_now())
    except Exception as e:  # noqa: BLE001 - reporting never breaks inference
        return f"skipped: cannot build report: {e}"
    reporter = ts.TouchstoneReporter(settings, cfg.touchstone_buffer, ledger=ledger,
                                     transport=transport)
    try:
        return reporter.report(result.spend_id, payload)
    finally:
        reporter.close()


def _turn_index(spend: dict) -> int:
    """A direct session is one turn on its own channel (index 0). Gateway turns have no channel, so
    the ledger id keeps each one's client_digest distinct (Touchstone dedupes on it)."""
    return 0 if spend["route"] == "direct" else spend["id"]


# ---- one interface ------------------------------------------------------------------------------

def plan(cfg: FlopConfig, policy: FlopPolicy, *, route: str, model: str, prompt: str,
         purpose: str, max_flop: int, max_tokens: int = DEFAULT_MAX_TOKENS,
         miner: str | None = None, which=shutil.which) -> dict:
    """What `ask --post` would do. Reads only local state: no network, no key, no ledger write."""
    from .wallet import spend_address
    out = {"route": route, "model": model, "purpose": purpose, "max_flop": format_flop(max_flop),
           "max_tokens": max_tokens, "prompt_chars": len(prompt),
           "budget": policy.budget().describe(), "problems": []}
    if route not in ROUTES:
        out["problems"].append(f"unknown route {route!r}")
        return out
    chosen = None
    if route == "direct":
        try:
            chosen, endpoint = choose_miner(cfg, policy.ledger, miner)
            out.update(miner=chosen, endpoint=endpoint)
        except RouteUnavailable as e:
            out["problems"].append(str(e))
        try:
            prefix, keystore_arg, version = cli_prefix(cfg, which)
            out["cli"] = version
            if chosen:
                from .policy import redact_argv
                out["argv"] = redact_argv(direct_argv(
                    cfg, prefix, keystore_arg, miner=chosen, endpoint=endpoint, model=model,
                    prompt=prompt, max_flop=max_flop, max_tokens=max_tokens))
        except RouteUnavailable as e:
            out["problems"].append(str(e))
        if not spend_address(cfg):
            out["problems"].append("no spend keystore (run `flop wallet setup --post`)")
    elif route == "gateway":
        try:
            gateway_key(cfg)
        except (RouteUnavailable, TouchstoneConfigError) as e:
            out["problems"].append(str(e))
    else:
        out["problems"].append("native route not available (SPEC S1)")
    out["problems"] += policy.check_inference(route, model, chosen, max_flop)
    if not cfg.network.genesis_hash:
        out["problems"].append(f"{cfg.network.name} genesis not pinned")
    return out


def execute(cfg: FlopConfig, policy: FlopPolicy, *, route: str, model: str, prompt: str,
            purpose: str, max_flop: int, max_tokens: int = DEFAULT_MAX_TOKENS,
            miner: str | None = None, passphrase: str | None = None, runner=None,
            which=shutil.which, transport=None) -> Result:
    """Run one request for real. Callers check `plan(...)["problems"]` first."""
    if route == "direct":
        from .wallet import check_keystore_for_cli, read_passphrase, spend_address
        chosen, endpoint = choose_miner(cfg, policy.ledger, miner)
        check_keystore_for_cli(cfg)
        return run_direct(cfg, policy, model=model, prompt=prompt, purpose=purpose,
                          max_flop=max_flop, max_tokens=max_tokens, miner=chosen,
                          endpoint=endpoint, passphrase=passphrase or read_passphrase(cfg),
                          signer=spend_address(cfg), runner=runner, which=which)
    if route == "gateway":
        return run_gateway(cfg, policy, model=model, prompt=prompt, purpose=purpose,
                           max_flop=max_flop, max_tokens=max_tokens, transport=transport)
    return run_native()
