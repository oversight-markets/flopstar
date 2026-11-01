"""`flopstar flop ...`. Read-only commands need no flag; anything that signs or spends needs --post."""

import json
import sys

from .amounts import format_flop
from .config import ConfigError, FlopConfig, load_config

USAGE = """Usage: flopstar flop <command>

Commands:
  config [--json]   Show the resolved network profile, caps and paths (read-only)
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


def run_flop(args: list[str]) -> None:
    command = args[0] if args else ""
    if command == "config":
        try:
            cfg = load_config()
        except ConfigError as e:
            raise SystemExit(f"config error: {e}")
        view = describe(cfg)
        if "--json" in args[1:]:
            print(json.dumps(view, indent=2, sort_keys=True))
            return
        for key, value in view.items():
            if isinstance(value, dict):
                print(f"{key}:")
                for k, v in value.items():
                    print(f"  {k}: {v}")
            else:
                print(f"{key}: {value}")
        if not cfg.network.genesis_hash:
            print(f"WARNING: {cfg.network.name} genesis is not pinned; FLOP actions will refuse")
        if not cfg.caps.allowed_models:
            print("WARNING: allowed_models is empty; every inference request will be refused")
    else:
        print(USAGE, end="", file=sys.stderr)
        sys.exit(1)
