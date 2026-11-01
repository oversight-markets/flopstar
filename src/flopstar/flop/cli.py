"""`flopstar flop ...`. Read-only commands need no flag; anything that signs, spends or sends needs --post."""

import json
import sys

from .amounts import format_flop
from .config import ConfigError, FlopConfig, load_config, ss58_pubkey

USAGE = """Usage: flopstar flop <command>

Read-only:
  config [--json]                      Resolved network profile, caps and paths
  status                               Chain health, genesis check, accounts, balances, budget
  wallet address                       Our identity and spend accounts (public)
  touchstone status | check            Touchstone settings, backlog, schema diff

Dry run unless --post:
  wallet setup [--post]                Create the encrypted spend keystore (key design A)
  wallet fund --amount FLOP [--post]   Owner-funded top-up (refused under key design A)
  faucet [--amount FLOP] [--post]      Draw from the network faucet into the spend account
  ask --model M --prompt TEXT --purpose NAME [--route direct|gateway] [--miner SS58]
      [--max-flop FLOP] [--max-tokens N] [--post]
  run [--route direct|gateway] [--post]          Run the useful-work tasks (contest-digest,
                                                 ops-review, research-notes) within budget
  recovery [--post]                    Find our channels with escrow to recover; submit the calls
  touchstone register [--post] [--chain-account SS58] | replay [--post]
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


# ---- hooks (tests replace these to prove dry runs touch nothing) ---------------------------------

def _chain(cfg):
    from .client import Chain
    return Chain(cfg)


def _http_client():
    import httpx
    return httpx.Client(timeout=30, follow_redirects=False)


HOOKS = {"chain": _chain, "http": _http_client, "which": None, "runner": None,
         "transport": None, "passphrase": None}


def _which():
    import shutil
    return HOOKS["which"] or shutil.which


def _policy(cfg):
    from .ledger import Ledger
    from .policy import FlopPolicy
    return FlopPolicy(cfg, Ledger(cfg.db_path))


def _flop_amount(text: str) -> int:
    from .amounts import AmountError, parse_flop
    try:
        return parse_flop(text)
    except AmountError as e:
        raise SystemExit(f"bad amount: {e}") from None


# ---- status and wallet --------------------------------------------------------------------------

def run_status(args: list[str]) -> None:
    import asyncio

    from ..signer import expected_did
    from .client import health
    from .wallet import identity_account, spend_address

    cfg = _load()
    policy = _policy(cfg)
    spend = spend_address(cfg)
    view = {"network": cfg.network.name,
            "earns": "nothing (devnet)" if cfg.network.name == "devnet" else "testnet credit",
            "did": expected_did(),
            "identity_account": f"{identity_account()} (unused on FLOP under key design A)",
            "spend_account": spend or "none (run `flop wallet setup --post`)"}
    try:
        chain = HOOKS["chain"](cfg)
        genesis = chain.genesis()
        pinned = cfg.network.genesis_hash
        view["genesis"] = (f"{genesis} " + ("matches pin" if pinned and genesis.lower() == pinned
                                             else f"MISMATCH (pinned {pinned})"))
        view["spec_version"] = chain.spec_version()
        view["health"] = asyncio.run(health(cfg))
        head, block = chain.finalized()
        view["finalized_block"] = block
        if spend:
            view["spend_balance"] = format_flop(chain.free_balance(spend, head))
    except Exception as e:  # noqa: BLE001 - status reports whatever it can
        view["chain"] = f"unavailable: {type(e).__name__}: {e}"
    c = cfg.caps
    view["caps"] = {"per_request": format_flop(c.max_flop_per_request),
                    "per_day": format_flop(c.max_flop_per_day),
                    "requests_per_hour": c.max_requests_per_hour,
                    "faucet_draws_per_day": c.max_faucet_draws_per_day,
                    "allowed_models": list(c.allowed_models),
                    "miners": list(cfg.miners), "blocklist": list(c.same_control_miners)}
    view["budget"] = policy.budget().describe()
    view["kill_switch"] = policy.kill_reason() or "clear"
    _print(view)


def run_wallet(args: list[str]) -> None:
    from ..signer import PolicyViolation
    from .policy import KillSwitchTripped
    from .wallet import (
        WalletError,
        create_spend_keystore,
        identity_account,
        plan_setup,
        read_passphrase,
        spend_address,
    )

    command = args[0] if args else ""
    post = "--post" in args
    cfg = _load()
    if command == "address":
        print(f"identity account (did:key, unused under design A): {identity_account()}")
        print(f"spend account: {spend_address(cfg) or 'none'}")
    elif command == "setup":
        view = plan_setup(cfg)
        b = view["bounds"]
        for k in ("max_flop_per_request", "max_flop_per_day"):
            b[k] = format_flop(b[k])
        _print(view)
        if view["exists"]:
            raise SystemExit("a spend keystore already exists; refusing to replace it")
        if not post:
            print("dry run: no key generated (add --post)")
            return
        try:
            address = create_spend_keystore(cfg, HOOKS["passphrase"] or read_passphrase(cfg, True))
        except WalletError as e:
            raise SystemExit(str(e)) from None
        print(f"created spend account {address}; keystore {cfg.keystore_path} (0600)")
        print("next: flopstar flop faucet --post")
    elif command == "fund":
        amount_text = _option(args, "--amount")
        if not amount_text:
            raise SystemExit("wallet fund needs --amount FLOP")
        amount = _flop_amount(amount_text)
        policy = _policy(cfg)
        print(f"fund spend account with {format_flop(amount)} FLOP from the owner key "
              f"(limit {format_flop(policy.funding_limit())})")
        try:
            policy.authorize_wallet_fund(amount)
        except (PolicyViolation, KillSwitchTripped) as e:
            raise SystemExit(f"refused: {e}") from None
        raise SystemExit("refused: key design A keeps the owner key off FLOP; fund the spend "
                         "account from the faucet (`flopstar flop faucet --post`)")
    else:
        print(USAGE, end="", file=sys.stderr)
        sys.exit(1)


def run_faucet(args: list[str]) -> None:
    from . import faucet
    from .wallet import spend_address

    cfg = _load()
    policy = _policy(cfg)
    amount_text = _option(args, "--amount")
    amount = _flop_amount(amount_text) if amount_text else None
    address = spend_address(cfg)
    view = faucet.plan(cfg, policy, address, amount)
    _print(view)
    if "--post" not in args:
        print("dry run: nothing requested (add --post)")
        return
    if view["problems"]:
        raise SystemExit("refused: " + "; ".join(view["problems"]))
    with HOOKS["http"]() as client:
        outcome = faucet.draw(cfg, policy, HOOKS["chain"](cfg), client, address, amount)
    _print(outcome)
    if outcome["status"] != "funded":
        sys.exit(1)


# ---- inference ----------------------------------------------------------------------------------

def _report_and_print(cfg, policy, result, cli_version) -> None:
    from ..signer import expected_did
    from . import inference

    print(f"status: {result.status}  spend: {result.spend_id}  verification: {result.verification}")
    if result.channel_id:
        print(f"miner: {result.miner}  channel: {result.channel_id}  settle tx: {result.settle_tx}")
    if result.error:
        print(f"error: {result.error}")
    if result.needs_recovery:
        print("escrow may be locked: run `flopstar flop recovery`")
    if result.reply is not None:
        print("---- reply (untrusted model output) ----")
        print(result.reply)
        print("---- end reply ----")
    result.touchstone = inference.report(cfg, policy.ledger, result, agent_did=expected_did(),
                                         cli_version=cli_version, transport=HOOKS["transport"])
    print(f"touchstone: {result.touchstone}")


def run_ask(args: list[str]) -> None:
    import argparse

    from . import inference
    from .tasks import SecretInInput, assert_no_secrets

    ap = argparse.ArgumentParser(prog="flopstar flop ask")
    ap.add_argument("--model", required=True)
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--purpose", required=True)
    ap.add_argument("--route", choices=inference.ROUTES, default="direct")
    ap.add_argument("--miner")
    ap.add_argument("--max-flop")
    ap.add_argument("--max-tokens", type=int, default=inference.DEFAULT_MAX_TOKENS)
    ap.add_argument("--post", action="store_true")
    a = ap.parse_args(args)
    cfg = _load()
    policy = _policy(cfg)
    max_flop = _flop_amount(a.max_flop) if a.max_flop else cfg.caps.max_flop_per_request
    try:
        assert_no_secrets(a.prompt)
    except SecretInInput as e:
        raise SystemExit(f"refused: {e}") from None
    kw = {"route": a.route, "model": a.model, "prompt": a.prompt, "purpose": a.purpose,
          "max_flop": max_flop, "max_tokens": a.max_tokens, "miner": a.miner}
    view = inference.plan(cfg, policy, which=_which(), **kw)
    _print(view)
    if not a.post:
        print("dry run: nothing sent (add --post)")
        return
    if view["problems"]:
        raise SystemExit("refused: " + "; ".join(view["problems"]))
    result = inference.execute(cfg, policy, which=_which(), runner=HOOKS["runner"],
                               transport=HOOKS["transport"], passphrase=HOOKS["passphrase"], **kw)
    _report_and_print(cfg, policy, result, view.get("cli"))
    if result.status != "settled":
        sys.exit(1)


def run_tasks(args: list[str]) -> None:
    from datetime import UTC, datetime

    from ..signer import expected_did
    from . import inference, tasks

    route = _option(args, "--route") or "direct"
    post = "--post" in args
    cfg = _load()
    policy = _policy(cfg)
    if not cfg.caps.allowed_models:
        raise SystemExit("allowed_models is empty; nothing may be bought")
    model = cfg.caps.allowed_models[0]
    jobs, outcomes = tasks.collect(cfg, policy.ledger, datetime.now(UTC))
    max_flop = cfg.caps.max_flop_per_request
    passphrase = HOOKS["passphrase"]
    for job in jobs:
        try:
            tasks.assert_no_secrets(job.prompt)
        except tasks.SecretInInput as e:
            outcomes.append({"task": job.task, "outcome": "refused", "reason": str(e)})
            continue
        view = inference.plan(cfg, policy, route=route, model=model, prompt=job.prompt,
                              purpose=job.task, max_flop=max_flop, which=_which())
        if view["problems"]:
            outcomes.append({"task": job.task, "outcome": "stopped",
                             "reason": "; ".join(view["problems"])})
            break  # budget, kill switch or setup: later jobs would hit the same wall
        if not post:
            outcomes.append({"task": job.task, "outcome": "planned", "output": str(job.output),
                             "miner": view.get("miner"), "max_flop": view["max_flop"]})
            continue
        if route == "direct" and passphrase is None:
            from .wallet import read_passphrase
            passphrase = read_passphrase(cfg)
        result = inference.execute(cfg, policy, route=route, model=model, prompt=job.prompt,
                                   purpose=job.task, max_flop=max_flop, passphrase=passphrase,
                                   runner=HOOKS["runner"], which=_which(),
                                   transport=HOOKS["transport"], miner=view.get("miner"))
        if result.status == "settled":
            tasks.write_output(job, result)
        result.touchstone = inference.report(
            cfg, policy.ledger, result, agent_did=expected_did(),
            cli_version=view.get("cli"), transport=HOOKS["transport"])
        outcomes.append({"task": job.task, "outcome": result.status, "spend": result.spend_id,
                         "output": str(job.output) if result.status == "settled" else None,
                         "error": result.error, "touchstone": result.touchstone})
        if result.status != "settled":
            break
    for o in outcomes:
        print(json.dumps(o, sort_keys=True))
    if not post:
        print("dry run: nothing sent (add --post)")


# ---- recovery -----------------------------------------------------------------------------------

def run_recovery(args: list[str]) -> None:
    from . import recovery
    from .wallet import load_spend_keypair, spend_address

    post = "--post" in args
    cfg = _load()
    policy = _policy(cfg)
    chain = HOOKS["chain"](cfg)
    agent = spend_address(cfg)
    items = recovery.scan(cfg, policy, chain, agent)
    if post:
        items = recovery.attach_orphans(policy, items)
    if not items:
        print("no channels of ours hold escrow")
        return
    for it in items:
        due = f"DUE {it.call}" if it.call else (
            f"wait {it.blocks_left} blocks (deadline {it.deadline})" if it.deadline else "-")
        escrow = format_flop(it.escrow) if it.escrow is not None else "?"
        print(f"spend {it.spend_id} channel {it.channel_id} {it.state} escrow {escrow}: {due}"
              + (f"  [{it.note}]" if it.note else ""))
        if it.call and not post:
            print(f"    {recovery.human_command(cfg, it)}")
    mismatches = [it for it in items if it.note.startswith("MISMATCH")]
    if post and mismatches:
        policy.trip("recovery: ledger channel paid by another account: "
                    + ", ".join(it.channel_id for it in mismatches))
        raise SystemExit("kill switch tripped: channel/account mismatch")
    due = [it for it in items if it.call and it.spend_id is not None]
    if not post:
        print("dry run: nothing submitted (add --post)" if due else "nothing due yet")
        return
    if not due:
        print("nothing due yet")
        return
    keypair = load_spend_keypair(cfg, HOOKS["passphrase"])
    for done in recovery.recover(cfg, policy, chain, keypair, due):
        print(json.dumps(done, sort_keys=True))


def run_flop(args: list[str]) -> None:
    command, rest = (args[0] if args else ""), args[1:]
    handlers = {"config": run_config, "touchstone": run_touchstone, "status": run_status,
                "wallet": run_wallet, "faucet": run_faucet, "ask": run_ask, "run": run_tasks,
                "recovery": run_recovery}
    if command not in handlers:
        print(USAGE, end="", file=sys.stderr)
        sys.exit(1)
    handlers[command](rest)
