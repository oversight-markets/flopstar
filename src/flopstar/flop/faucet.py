"""
Faucet draws for the spend account, through FlopPolicy (sdk:faucet.js, docs/FLOP.md §3.2).

`POST {faucet}/fund {"address"[, "amount"]}` is unauthenticated and keyed by SS58 (SPEC S1: FLOP's
testnet faucet may instead want a DID-signed request; not published). Before drawing we require the
faucet's `/chain` genesis to equal both the pinned genesis and the RPC's, as the CLI does.

Faucet responses are untrusted data: only `amount` (as an exact decimal) and `txHash` (as a string)
are kept. Expected refusals (4xx such as rate limits, 5xx, network errors) are recorded as failed
draws without tripping the kill switch; a genesis mismatch or a malformed 2xx does trip it.

Testnet has no `/fund`. There the spend account makes an **agent claim** instead (`claim`): the
spend key signs `flop-faucet-agent/v1|<genesis>|<ss58>|<nonce>` (FlopPolicy.sign_agent_claim) and
we POST `{address, nonce, signature}` to `/v1/testnet/agent-claims` (npm @flop-labs/sdk 0.12.0
`claimTestnetAgentFunds`). The nonce and signature are written to the ledger before sending. If
the outcome is unknown (network error, timeout, 408, 5xx) the draw stays `requested` and is
resent unchanged with `reconcile`, never re-signed: FLOP's rule, so one claim can't be granted
twice. A 4xx is a refusal, recorded as failed without tripping the switch.
"""

import json
import re
from collections.abc import Callable
from decimal import Decimal

import httpx

from ..signer import PolicyViolation
from .amounts import AmountError, format_flop, parse_flop
from .config import FlopConfig
from .policy import (
    AGENT_CLAIM_NETWORK,
    ActionKind,
    FlopPolicy,
    agent_claim_message,
)

AGENT_CLAIM_PATH = "/v1/testnet/agent-claims"
TX_HASH_RE = re.compile(r"0x[0-9a-fA-F]{64}")
SIGNATURE_RE = re.compile(r"0x[0-9a-f]{128}")


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


# ---- testnet agent claims -------------------------------------------------------------------------

class ClaimRefused(Exception):
    """A pre-check before signing failed (faucet closed, open claim pending): nothing was signed."""


def is_claim_network(cfg: FlopConfig) -> bool:
    return cfg.network.name == AGENT_CLAIM_NETWORK


def plan_claim(cfg: FlopConfig, policy: FlopPolicy, address: str | None, nonce: str) -> dict:
    """What `faucet --post` would do on testnet. Offline: reads only the config and ledger."""
    problems = policy.check_faucet() + ([] if address else ["no spend account"])
    pending = policy.ledger.open_claims(cfg.network.name, address) if address else []
    if pending:
        ids = ", ".join(str(r["id"]) for r in pending)
        problems.append(f"claim(s) {ids} have an unknown outcome; reconcile with "
                        f"`flop faucet --reconcile <id> --post` before signing a new one")
    try:
        genesis = cfg.require_pinned_genesis().lower()
        message = agent_claim_message(genesis, address, nonce) if address else None
    except Exception as e:  # noqa: BLE001 - shown as a problem; nothing is signed in a plan
        message = None
        problems.append(str(e))
    return {
        "network": cfg.network.name,
        "route": f"agent claim: POST {cfg.network.faucet_url}{AGENT_CLAIM_PATH}",
        "address": address or "<no spend keystore: run `flop wallet setup --post` first>",
        "signer": "spend key (sr25519), raw UTF-8 message",
        "message": message,
        "nonce": f"{nonce} (the millisecond clock at --post, kept above earlier claims)",
        "grant": "fixed by the faucet (10 FLOP per rolling 24 h on 26 Nov)",
        "checks before signing": "faucet /chain is testnet on the pinned genesis (= RPC's); "
                                 "/limits faucet.status open and claimPath matches",
        "draws_left_today": policy.budget().faucet_draws_left_today,
        "problems": problems,
    }


def next_nonce(policy: FlopPolicy, address: str) -> str:
    clock = int(policy.now().timestamp() * 1000)
    last = policy.ledger.last_claim_nonce(policy.network, address)
    return str(clock if last is None or clock > last else last + 1)


def check_claim_faucet(cfg: FlopConfig, policy: FlopPolicy, client: httpx.Client,
                       rpc_genesis: str) -> None:
    """The faucet is testnet on our genesis (trips on a mismatch). Raises ClaimRefused if closed."""
    with policy.guarded(f"{ActionKind.FAUCET_CLAIM} chain identity"):
        r = client.get(f"{cfg.network.faucet_url}/chain")
        r.raise_for_status()
        info = _json(r)
        if info.get("network") != AGENT_CLAIM_NETWORK:
            raise FaucetError(f"faucet /chain reports network {info.get('network')!r}")
        genesis = info.get("genesisHash")
        cfg.check_genesis(genesis if isinstance(genesis, str) else repr(genesis))
        cfg.check_genesis(rpc_genesis)
    r = client.get(f"{cfg.network.faucet_url}/limits")
    if not r.is_success:
        raise ClaimRefused(f"faucet /limits returned HTTP {r.status_code}")
    faucet_info = _json(r).get("faucet")
    faucet_info = faucet_info if isinstance(faucet_info, dict) else {}
    agent = faucet_info.get("agent") if isinstance(faucet_info.get("agent"), dict) else {}
    if faucet_info.get("status") != "open":
        raise ClaimRefused(f"faucet status is {faucet_info.get('status')!r}, not open")
    if agent.get("claimPath") != AGENT_CLAIM_PATH:
        raise ClaimRefused(f"faucet offers no agent claim at {AGENT_CLAIM_PATH} "
                           f"(claimPath {agent.get('claimPath')!r})")


def claim(cfg: FlopConfig, policy: FlopPolicy, chain, client: httpx.Client, address: str,
          load_keypair: Callable[[], object]) -> dict:
    """Sign and send one agent claim. Caller has already decided to --post."""
    if not is_claim_network(cfg):
        raise PolicyViolation(f"agent claims exist only on {AGENT_CLAIM_NETWORK}")
    policy.ensure_alive()
    if policy.ledger.open_claims(cfg.network.name, address):
        raise ClaimRefused("a signed claim has an unknown outcome; reconcile it first")
    check_claim_faucet(cfg, policy, client, chain.genesis())
    keypair = load_keypair()
    if keypair.ss58_address != address:
        raise PolicyViolation(f"spend keystore holds {keypair.ss58_address}, not {address}")
    draw_id = policy.authorize_faucet(address)
    nonce = next_nonce(policy, address)
    message, signature = policy.sign_agent_claim(draw_id, keypair, nonce)
    claim_detail = {"route": "agent-claim", "nonce": nonce, "signature": signature,
                    "message": message}
    policy.ledger.note_faucet(draw_id, claim_detail)
    return _send_claim(cfg, policy, client, draw_id, address, claim_detail)


def reconcile(cfg: FlopConfig, policy: FlopPolicy, chain, client: httpx.Client,
              draw_id: int) -> dict:
    """Resend an earlier claim byte-for-byte (same address, nonce, signature). Signs nothing."""
    policy.ensure_alive()
    row = policy.ledger.get_faucet(draw_id)
    if row is None or row["network"] != cfg.network.name or row["status"] != "requested":
        raise ClaimRefused(f"faucet draw {draw_id} is not an open {cfg.network.name} claim")
    try:
        detail = json.loads(row["detail"] or "")
    except ValueError:
        detail = {}
    if not isinstance(detail, dict) or not isinstance(detail.get("nonce"), str) \
            or not isinstance(detail.get("signature"), str):
        raise ClaimRefused(f"faucet draw {draw_id} has no signed claim to resend")
    with policy.guarded(f"{ActionKind.FAUCET_CLAIM} reconcile chain identity"):
        check_faucet_chain(cfg, client, chain.genesis())
    keep = {k: detail[k] for k in ("route", "nonce", "signature", "message") if k in detail}
    return _send_claim(cfg, policy, client, draw_id, row["address"], keep)


def _send_claim(cfg: FlopConfig, policy: FlopPolicy, client: httpx.Client, draw_id: int,
                address: str, claim_detail: dict) -> dict:
    body = {"address": address, "nonce": claim_detail["nonce"],
            "signature": claim_detail["signature"]}
    if not SIGNATURE_RE.fullmatch(body["signature"]):
        raise PolicyViolation("refusing to send a malformed claim signature")
    reconcile_hint = f"flopstar flop faucet --reconcile {draw_id} --post"
    base = {"draw_id": draw_id, "nonce": body["nonce"]}

    def unknown(detail: dict) -> dict:
        policy.ledger.note_faucet(draw_id, {**claim_detail, "last": detail})
        return {**base, "status": "unknown", **detail,
                "next": f"do not sign a new claim; resend this one: {reconcile_hint}"}

    try:
        r = client.post(f"{cfg.network.faucet_url}{AGENT_CLAIM_PATH}", json=body)
    except httpx.HTTPError as e:
        return unknown({"error": f"{type(e).__name__}: {e}"[:300]})
    if r.status_code >= 500 or r.status_code == 408:
        return unknown({"http": r.status_code, "error": r.text[:300],
                        "retry_after": r.headers.get("retry-after")})
    if not r.is_success:
        try:
            data = _json(r)
        except FaucetError:
            data = {"error": r.text}
        detail = {"http": r.status_code,
                  "error": str(data.get("error", ""))[:300],
                  "reason": data.get("reason") if isinstance(data.get("reason"), str) else None,
                  "action": data.get("action") if isinstance(data.get("action"), str) else None,
                  "retry_after": r.headers.get("retry-after")}
        policy.ledger.finish_faucet(draw_id, "failed", {**claim_detail, **detail})
        return {**base, "status": "failed", **detail}
    with policy.guarded(f"{ActionKind.FAUCET_CLAIM} response"):
        data = _json(r)
        granted, tx = data.get("amount"), data.get("txHash")
        ok = (data.get("success") is True and isinstance(granted, int)
              and not isinstance(granted, bool) and granted > 0
              and isinstance(tx, str) and TX_HASH_RE.fullmatch(tx))
        if not ok:
            policy.ledger.note_faucet(draw_id, {**claim_detail,
                                                "last": {"http": r.status_code,
                                                         "error": "incomplete grant response"}})
            raise FaucetError(f"faucet returned an incomplete grant response; draw {draw_id} "
                              f"stays open for `--reconcile`")
        granted_raw = parse_flop(granted)
        transfer = data.get("status") if data.get("status") in ("included", "finalized") else None
        detail = {"http": r.status_code, "txHash": tx, "transfer": transfer}
        policy.ledger.finish_faucet(draw_id, "funded", {**claim_detail, **detail},
                                    amount=granted_raw)
    return {**base, "status": "funded", "amount": format_flop(granted_raw), **detail}
