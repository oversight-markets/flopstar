"""`flopstar flop ...`. Read-only commands need no flag; anything that signs, spends or sends needs --post."""

import json
import sys

from .amounts import format_flop
from .config import ConfigError, FlopConfig, load_config, ss58_pubkey

USAGE = """Usage: flopstar flop <command>

Commands:
  config [--json]                      Resolved network profile, caps and paths (read-only)
  touchstone status                    Touchstone settings and buffer backlog (read-only)
  touchstone register [--post] [--chain-account SS58]
                                       Register our DID and chain account with Touchstone
  touchstone check                     Diff our payload schema against Touchstone's /openapi.json
  touchstone replay [--post]           Send buffered reports, oldest first
"""


def describe(cfg: FlopConfig) -> dict:
    """Public, non-secret view of the config (no key material exists in it)."""
    n, c = cfg.network, cfg.caps
    return {
        "network": n.name,
        "genesis_pinned": n.genesis_hash,
        "rpc_url": n.rpc_url,
        "rpc_http_url": n.rpc_http_url,
        "faucet_url": n.faucet_url,
        "gateway_url": n.gateway_url,
        "indexer_url": n.indexer_url,
        "mcp_url": n.mcp_url,
        "cli_npm_version": n.cli_npm_version,
        "cli_image": n.cli_image,
        "caps": {
            "max_flop_per_request": format_flop(c.max_flop_per_request),
            "max_flop_per_day": format_flop(c.max_flop_per_day),
            "max_requests_per_hour": c.max_requests_per_hour,
            "max_faucet_draws_per_day": c.max_faucet_draws_per_day,
            "allowed_models": list(c.allowed_models),
            "same_control_miners": list(c.same_control_miners),
        },
        "config_path": str(cfg.config_path),
        "keystore_path": str(cfg.keystore_path),
        "kill_switch": {"path": str(cfg.kill_path), "tripped": cfg.kill_path.exists()},
        "earns": "nothing (devnet)" if n.name == "devnet" else "testnet credit (per FLOP's rules)",
    }


def _load() -> FlopConfig:
    try:
        return load_config()
    except ConfigError as e:
        raise SystemExit(f"config error: {e}") from None


def _print(view: dict, indent: str = "") -> None:
    for key, value in view.items():
        if isinstance(value, dict):
            print(f"{indent}{key}:")
            _print(value, indent + "  ")
        else:
            print(f"{indent}{key}: {value}")


def run_config(args: list[str]) -> None:
    cfg = _load()
    view = describe(cfg)
    if "--json" in args:
        print(json.dumps(view, indent=2, sort_keys=True))
        return
    _print(view)
    if not cfg.network.genesis_hash:
        print(f"WARNING: {cfg.network.name} genesis is not pinned; FLOP actions will refuse")
    if not cfg.caps.allowed_models:
        print("WARNING: allowed_models is empty; every inference request will be refused")


def _option(args: list[str], name: str) -> str | None:
    if name in args:
        i = args.index(name)
        if i + 1 >= len(args):
            raise SystemExit(f"{name} needs a value")
        return args[i + 1]
    return None


def chain_account(cfg: FlopConfig, override: str | None) -> str:
    """Our spend account's SS58: --chain-account, else the public `address` of the keystore JSON."""
    if override:
        address = override
    else:
        try:
            address = json.loads(cfg.keystore_path.read_text())["address"]
        except (FileNotFoundError, KeyError, ValueError):
            raise SystemExit(f"no spend keystore address at {cfg.keystore_path}; create the "
                             "wallet first or pass --chain-account SS58") from None
    try:
        ss58_pubkey(address)
    except ConfigError as e:
        raise SystemExit(str(e)) from None
    return address


def run_touchstone(args: list[str], transport=None) -> None:
    from ..signer import expected_did
    from . import touchstone as ts

    command = args[0] if args else ""
    post = "--post" in args[1:]
    cfg = _load()
    try:
        settings = ts.TouchstoneSettings.from_env()
        problem = None
    except ts.TouchstoneConfigError as e:
        settings, problem = None, str(e)

    def reporter():
        if settings is None:
            raise SystemExit(f"Touchstone not configured: {problem}")
        return ts.TouchstoneReporter(settings, cfg.touchstone_buffer, transport=transport)

    if command == "status":
        backlog = ts.TouchstoneReporter(None, cfg.touchstone_buffer, transport=transport)
        print(f"schema_version: {ts.SCHEMA_VERSION}")
        print(f"configured: {settings is not None}" + (f" ({problem})" if problem else ""))
        if settings is not None:
            print(f"api_url: {settings.api_url}")
            try:
                for warning in ts.check_url(settings.api_url):
                    print(f"WARNING: {warning}")
                print("network path: ok")
            except ts.UnsafeURL as e:
                print(f"network path: REFUSED ({e})")
        print(f"buffered reports: {len(backlog.buffered())} in {cfg.touchstone_buffer}")
    elif command == "register":
        payload = ts.agent_payload(expected_did(), chain_account(cfg, _option(args, "--chain-account")))
        url = (settings.api_url if settings else "<TOUCHSTONE_API_URL unset>") + ts.AGENTS_PATH
        print(f"POST {url}")
        print(json.dumps(payload, indent=2, sort_keys=True))
        if not post:
            print("dry run: nothing sent (add --post to send)")
            return
        r = reporter().post(ts.AGENTS_PATH, payload)
        print(f"HTTP {r.status_code}")
        if not 200 <= r.status_code < 300:
            sys.exit(1)
    elif command == "check":
        rep = reporter()
        r = rep.get("/openapi.json")
        if r.status_code != 200:
            raise SystemExit(f"GET /openapi.json: HTTP {r.status_code}")
        spec = r.json()
        problems = (ts.diff_against_openapi(spec, ts.TURNS_PATH, ts.TURN_FIELDS)
                    + ts.diff_against_openapi(spec, ts.AGENTS_PATH, ts.AGENT_FIELDS))
        for p in problems:
            print(f"MISMATCH {p}")
        print(f"{len(problems)} mismatches against Touchstone {spec.get('info', {}).get('version')}")
        if problems:
            sys.exit(1)
    elif command == "replay":
        backlog = ts.TouchstoneReporter(None, cfg.touchstone_buffer, transport=transport).buffered()
        print(f"{len(backlog)} buffered reports")
        if not post:
            print("dry run: nothing sent (add --post to send)")
            return
        from .ledger import Ledger
        rep = reporter()
        rep.ledger = Ledger(cfg.db_path)
        remaining = rep.replay()
        print(f"{remaining} remaining" + (f" (last error: {rep.last_error})" if remaining else ""))
        if remaining:
            sys.exit(1)
    else:
        print(USAGE, end="", file=sys.stderr)
        sys.exit(1)


def run_flop(args: list[str]) -> None:
    command = args[0] if args else ""
    if command == "config":
        run_config(args[1:])
    elif command == "touchstone":
        run_touchstone(args[1:])
    else:
        print(USAGE, end="", file=sys.stderr)
        sys.exit(1)
