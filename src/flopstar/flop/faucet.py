"""
Faucet draws for the spend account, through FlopPolicy (sdk:faucet.js, docs/FLOP.md §3.2).

`POST {faucet}/fund {"address"[, "amount"]}` is unauthenticated and keyed by SS58 (SPEC S1: FLOP's
testnet faucet may instead want a DID-signed request; not published). Before drawing we require the
faucet's `/chain` genesis to equal both the pinned genesis and the RPC's, as the CLI does.

Faucet responses are untrusted data: only `amount` (as an exact decimal) and `txHash` (as a string)
are kept. Expected refusals (4xx such as rate limits, 5xx, network errors) are recorded as failed
draws without tripping the kill switch; a genesis mismatch or a malformed 2xx does trip it.
"""

import json
from decimal import Decimal

import httpx

from .amounts import AmountError, format_flop, parse_flop
from .config import FlopConfig
from .policy import ActionKind, FlopPolicy


class FaucetError(Exception):
    """The faucet answered in a way we will not act on."""


def plan(cfg: FlopConfig, policy: FlopPolicy, address: str | None, amount: int | None) -> dict:
    return {
        "network": cfg.network.name,
        "faucet": cfg.network.faucet_url,
        "address": address or "<no spend keystore: run `flop wallet setup --post` first>",
        "amount": format_flop(amount) if amount else "faucet default",
        "draws_left_today": policy.budget().faucet_draws_left_today,
        "problems": policy.check_faucet() + ([] if address else ["no spend account"]),
        "earns": "nothing (devnet)" if cfg.network.name == "devnet" else "see FLOP's testnet rules",
    }


def _json(response: httpx.Response) -> dict:
    try:
        data = json.loads(response.content, parse_float=Decimal)
    except ValueError as e:
        raise FaucetError(f"faucet returned non-JSON: {e}") from e
    if not isinstance(data, dict):
        raise FaucetError("faucet returned a non-object")
    return data


def check_faucet_chain(cfg: FlopConfig, client: httpx.Client, rpc_genesis: str) -> None:
    r = client.get(f"{cfg.network.faucet_url}/chain")
    r.raise_for_status()
    genesis = _json(r).get("genesisHash")
    cfg.check_genesis(genesis if isinstance(genesis, str) else repr(genesis))
    cfg.check_genesis(rpc_genesis)


def draw(cfg: FlopConfig, policy: FlopPolicy, chain, client: httpx.Client, address: str,
         amount: int | None = None) -> dict:
    """Draw once. Returns the recorded outcome. Caller has already decided to --post."""
    with policy.guarded(f"{ActionKind.FAUCET_DRAW} chain identity"):
        check_faucet_chain(cfg, client, chain.genesis())
    draw_id = policy.authorize_faucet(address, amount)
    body = {"address": address}
    if amount is not None:
        body["amount"] = format_flop(amount)  # SPEC(S7): the faucet's amount unit/format
    try:
        r = client.post(f"{cfg.network.faucet_url}/fund", json=body)
    except httpx.HTTPError as e:
        detail = {"error": f"{type(e).__name__}: {e}"[:300]}
        policy.ledger.finish_faucet(draw_id, "failed", detail)
        return {"draw_id": draw_id, "status": "failed", **detail}
    if not r.is_success:
        try:
            error = str(_json(r).get("error", ""))[:300]
        except FaucetError:
            error = r.text[:300]
        detail = {"http": r.status_code, "error": error,
                  "retry_after": r.headers.get("retry-after")}
        policy.ledger.finish_faucet(draw_id, "failed", detail)
        return {"draw_id": draw_id, "status": "failed", **detail}
    with policy.guarded(f"{ActionKind.FAUCET_DRAW} response"):
        data = _json(r)
        granted = data.get("amount")
        try:
            granted_raw = None if granted is None else parse_flop(
                granted if isinstance(granted, (str, Decimal)) else Decimal(granted))
        except (AmountError, TypeError) as e:
            policy.ledger.finish_faucet(draw_id, "failed", {"error": f"bad amount {granted!r}"})
            raise FaucetError(f"faucet granted an unparseable amount {granted!r}") from e
        tx = data.get("txHash")
        detail = {"http": r.status_code, "txHash": tx if isinstance(tx, str) else None}
        policy.ledger.finish_faucet(draw_id, "funded", detail, amount=granted_raw)
    return {"draw_id": draw_id, "status": "funded",
            "amount": format_flop(granted_raw) if granted_raw is not None else None, **detail}
