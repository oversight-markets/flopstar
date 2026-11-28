"""
FlopPolicy: the only path to any FLOP-signed or FLOP-spending action.

- Action kinds are allowlisted (ActionKind); anything else raises PolicyViolation.
- Caps are checked against the ledger inside the same IMMEDIATE transaction that records the
  spend, so they hold across restarts and concurrent runs.
- The kill switch (data/KILL-flop) is checked before every action and tripped, with a reason, on
  any cap breach, unexpected error, verification failure or mismatch. Only a human removes it.
- Every signature is appended to data/flop-signatures.log (call, params, signer address, tx hash;
  never key material).
- Only this module calls substrate signing methods (tests/test_flop_policy.py enforces that).

Check methods (`check_*`) are pure: callers use them for dry runs and budget pre-checks, and they
never trip the switch. `authorize_*` methods are the real gate and do trip it.
"""

import json
import re
import subprocess
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from urllib.parse import urlparse

from ..signer import PolicyViolation
from .amounts import format_flop
from .config import FlopConfig
from .ledger import DAY, HOUR, ROUTES, Ledger


class CliTimeout(Exception):
    """The signing CLI ran past its timeout and was killed; escrow may be locked."""


class KillSwitchTripped(PolicyViolation):
    """data/KILL-flop exists: FLOP spending and signing are stopped until a human deletes it."""


class ActionKind(StrEnum):
    FAUCET_DRAW = "faucet_draw"
    FAUCET_CLAIM = "faucet_claim"
    GATEWAY_ENROLL = "gateway_enroll"
    INFERENCE = "inference"
    RECOVERY = "recovery"
    WALLET_FUND = "wallet_fund"
    SESSION_KEY = "session_key"


RECOVERY_CALLS = ("expire_force_open", "timeout", "finalize")

# Testnet agent claim (npm @flop-labs/sdk 0.12.0 faucet.js `d10AgentClaimMessage`, used by
# @flop-labs/cli 0.9.1 `faucet request` as `pair.sign(message)`: the raw UTF-8 text, no <Bytes>
# wrapper). The recipient key signs `flop-faucet-agent/v1|<testnet genesis>|<ss58>|<nonce>`.
AGENT_CLAIM_PREFIX = "flop-faucet-agent/v1"
AGENT_CLAIM_NETWORK = "testnet"
NONCE_RE = re.compile(r"(0|[1-9][0-9]{0,18})")


def agent_claim_message(genesis: str, address: str, nonce: str) -> str:
    """The exact text a testnet agent claim signs. Refuses anything that could break the layout."""
    if not isinstance(nonce, str) or not NONCE_RE.fullmatch(nonce):
        raise PolicyViolation(f"claim nonce {nonce!r} is not a canonical decimal of 1-19 digits")
    if not re.fullmatch(r"0x[0-9a-f]{64}", genesis):
        raise PolicyViolation(f"claim genesis {genesis!r} is not lowercase 32-byte hex")
    if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{46,48}", address):
        raise PolicyViolation(f"claim address {address!r} is not an SS58 address")
    return f"{AGENT_CLAIM_PREFIX}|{genesis}|{address}|{nonce}"


_ENROLL_NONCE_RE = re.compile(r"^Nonce: ([0-9a-f]{64})$")
_ENROLL_MAX_TTL = timedelta(minutes=5)
_ENROLL_MAX_SKEW = timedelta(seconds=30)


def validate_gateway_enrollment_challenge(
    message: str,
    *,
    address: str,
    gateway_url: str,
    genesis: str,
    network: str,
    now: datetime,
) -> str:
    """
    Strictly validate the 8-line gateway wallet-enrollment challenge (@flop-labs/cli 0.10.4
    `validateWalletEnrollmentChallenge`). Returns the 64-hex nonce; raises PolicyViolation
    on any structural, host, genesis, address, or timestamp mismatch.
    """
    if not isinstance(message, str):
        raise PolicyViolation("enrollment challenge must be a string")
    if not re.fullmatch(r"0x[0-9a-f]{64}", genesis):
        raise PolicyViolation(f"pinned genesis {genesis!r} is not lowercase 32-byte hex")
    host = urlparse(gateway_url).netloc
    if not host:
        raise PolicyViolation(f"gateway_url {gateway_url!r} has no host")
    lines = message.split("\n")
    expected_header = (
        f"{host} wants you to enroll a FLOP {network} gateway key with your account:"
    )
    if (
        len(lines) != 8
        or lines[0] != expected_header
        or lines[1] != address
        or lines[2] != ""
        or lines[3] != f"Action: flop-{network}-enroll/v1"
        or lines[4] != f"Genesis: {genesis}"
    ):
        raise PolicyViolation(f"{network} gateway returned an invalid enrollment challenge")
    nonce_match = _ENROLL_NONCE_RE.fullmatch(lines[5])
    if not nonce_match:
        raise PolicyViolation(f"{network} gateway enrollment challenge has an invalid nonce")
    if not lines[6].startswith("Issued At: ") or not lines[7].startswith("Expiration Time: "):
        raise PolicyViolation(f"{network} gateway enrollment challenge lacks timestamps")
    try:
        issued = datetime.fromisoformat(lines[6].removeprefix("Issued At: "))
        expires = datetime.fromisoformat(lines[7].removeprefix("Expiration Time: "))
    except ValueError as e:
        raise PolicyViolation(f"enrollment challenge timestamp invalid: {e}") from None
    if issued.tzinfo is None or expires.tzinfo is None:
        raise PolicyViolation("enrollment challenge timestamps must include timezone")
    now_utc = now.astimezone(UTC)
    if issued > now_utc + _ENROLL_MAX_SKEW:
        raise PolicyViolation("enrollment challenge Issued At is in the future")
    if expires <= now_utc:
        raise PolicyViolation("enrollment challenge has already expired")
    if expires <= issued or (expires - issued) > _ENROLL_MAX_TTL:
        raise PolicyViolation("enrollment challenge lifetime exceeds 5 minutes")
    return nonce_match.group(1)

# Extrinsics FlopPolicy will sign, per action kind. Names checked against the devnet runtime
# metadata (spec 1014003, docs/FLOP.md §2.4/§6). Kinds without an entry have no extrinsic path yet
# (the faucet is HTTP; inference goes through the CLI subprocess; funding under key design A is
# from the faucet, not from the owner key).
EXTRINSICS: dict[ActionKind, frozenset[tuple[str, str]]] = {
    ActionKind.RECOVERY: frozenset(("ComputeChannel", c) for c in RECOVERY_CALLS),
}


@dataclass(frozen=True)
class Budget:
    spent_today: int
    remaining_today: int
    requests_last_hour: int
    requests_left_this_hour: int
    faucet_draws_today: int
    faucet_draws_left_today: int

    def describe(self) -> dict:
        return {
            "spent_today": format_flop(self.spent_today),
            "remaining_today": format_flop(self.remaining_today),
            "requests_last_hour": self.requests_last_hour,
            "requests_left_this_hour": self.requests_left_this_hour,
            "faucet_draws_today": self.faucet_draws_today,
            "faucet_draws_left_today": self.faucet_draws_left_today,
        }


def redact_argv(argv: list[str]) -> list[str]:
    """argv for logs: the prompt (data we may not want in a long-lived log) becomes its length."""
    out, hide = [], False
    for a in argv:
        out.append(f"<{len(a)} chars>" if hide else a)
        hide = a == "--prompt"
    return out


class FlopPolicy:
    def __init__(self, cfg: FlopConfig, ledger: Ledger,
                 now: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self.cfg = cfg
        self.ledger = ledger
        self.now = now
        self.network = cfg.network.name

    # ---- kill switch --------------------------------------------------------------------------

    def kill_reason(self) -> str | None:
        path = self.cfg.kill_path
        if not path.exists():
            return None
        return path.read_text().strip() or "(no reason recorded)"

    def ensure_alive(self) -> None:
        reason = self.kill_reason()
        if reason is not None:
            raise KillSwitchTripped(f"{self.cfg.kill_path} exists: {reason}")

    def trip(self, reason: str) -> None:
        """Write (append to) the kill file. Never removes it."""
        line = f"{self.now().isoformat()} {' '.join(reason.split())}\n"
        with self.cfg.kill_path.open("a") as f:
            f.write(line)
        self._log({"kind": "kill", "reason": line.strip()})

    @contextmanager
    def guarded(self, what: str):
        """Run an action: refuse if killed; trip on any unexpected error, then re-raise."""
        self.ensure_alive()
        try:
            yield
        except KillSwitchTripped:
            raise
        except Exception as e:
            self.trip(f"{what}: {type(e).__name__}: {e}")
            raise

    # ---- budget -------------------------------------------------------------------------------

    def budget(self) -> Budget:
        caps, now = self.cfg.caps, self.now()
        spent = self.ledger.spent_since(self.network, now - DAY)
        requests = self.ledger.requests_since(self.network, now - HOUR)
        draws = self.ledger.faucet_draws_since(self.network, now - DAY)
        return Budget(
            spent_today=spent,
            remaining_today=max(0, caps.max_flop_per_day - spent),
            requests_last_hour=requests,
            requests_left_this_hour=max(0, caps.max_requests_per_hour - requests),
            faucet_draws_today=draws,
            faucet_draws_left_today=max(0, caps.max_faucet_draws_per_day - draws),
        )

    # ---- inference ----------------------------------------------------------------------------

    def check_inference(self, route: str, model: str, miner: str | None, max_flop: int) -> list[str]:
        """Every reason this request would be refused (empty list: allowed). Never trips."""
        caps, problems = self.cfg.caps, self.check_request(route, model, miner)
        if not isinstance(max_flop, int) or isinstance(max_flop, bool) or max_flop <= 0:
            problems.append("max_flop must be positive int base units")
            return problems
        if max_flop > caps.max_flop_per_request:
            problems.append(f"max_flop {format_flop(max_flop)} exceeds max_flop_per_request "
                            f"{format_flop(caps.max_flop_per_request)}")
        b = self.budget()
        if max_flop > b.remaining_today:
            problems.append(f"max_flop {format_flop(max_flop)} exceeds today's remaining "
                            f"{format_flop(b.remaining_today)}")
        return problems

    def check_request(self, route: str, model: str, miner: str | None) -> list[str]:
        """
        The checks that do not depend on the amount (a plan with no escrow yet still shows them,
        including a spent day budget). Never authorizes anything: authorize_inference always runs
        the full check_inference. Never trips.
        """
        caps, problems = self.cfg.caps, []
        if self.kill_reason() is not None:
            problems.append("kill switch is tripped")
        if route not in ROUTES:
            problems.append(f"route {route!r} is not one of {ROUTES}")
        if model not in caps.allowed_models:
            problems.append(f"model {model!r} is not in allowed_models")
        if route == "direct" and not miner:
            problems.append("a direct session must name its miner")
        if miner is not None and caps.is_blocked_miner(miner):
            problems.append(f"miner {miner} is on the same_control_miners blocklist")
        b = self.budget()
        if b.remaining_today <= 0:
            problems.append(f"max_flop_per_day ({format_flop(caps.max_flop_per_day)}) spent")
        if b.requests_left_this_hour <= 0:
            problems.append(f"max_requests_per_hour ({caps.max_requests_per_hour}) reached")
        return problems

    def authorize_inference(self, *, route: str, model: str, miner: str | None, max_flop: int,
                            purpose: str, request_sha256: str) -> int:
        """Check caps and record the spend as `prepared`, atomically. Returns the ledger id."""
        self.ensure_alive()
        self.cfg.require_pinned_genesis()
        with self.ledger.transaction():
            problems = self.check_inference(route, model, miner, max_flop)
            if problems:
                self._refuse(ActionKind.INFERENCE, problems)
            spend_id = self.ledger.prepare(
                network=self.network, route=route, model=model, miner=miner, max_flop=max_flop,
                purpose=purpose, request_sha256=request_sha256,
            )
        self._log({"kind": "authorize", "action": ActionKind.INFERENCE, "spend_id": spend_id,
                   "route": route, "model": model, "miner": miner,
                   "max_flop": format_flop(max_flop), "purpose": purpose})
        return spend_id

    # ---- faucet -------------------------------------------------------------------------------

    def check_faucet(self) -> list[str]:
        problems = []
        if self.kill_reason() is not None:
            problems.append("kill switch is tripped")
        if not self.cfg.network.faucet_url:
            problems.append(f"{self.network} has no faucet")
        if self.budget().faucet_draws_left_today <= 0:
            problems.append(f"max_faucet_draws_per_day ({self.cfg.caps.max_faucet_draws_per_day})"
                            " reached")
        return problems

    def authorize_faucet(self, address: str, amount: int | None = None) -> int:
        self.ensure_alive()
        self.cfg.require_pinned_genesis()
        with self.ledger.transaction():
            problems = self.check_faucet()
            if problems:
                self._refuse(ActionKind.FAUCET_DRAW, problems)
            draw_id = self.ledger.record_faucet(self.network, address, amount)
        self._log({"kind": "authorize", "action": ActionKind.FAUCET_DRAW, "draw_id": draw_id,
                   "address": address})
        return draw_id

    def sign_agent_claim(self, draw_id: int, keypair, nonce: str) -> tuple[str, str]:
        """
        Sign one testnet agent claim for an authorized draw with the recipient (spend) key.
        Returns (message, 0x-hex signature). The draw must be ours, still `requested`, on testnet,
        for this key's address, and the nonce must be above every earlier claim nonce for it.
        """
        kind = ActionKind.FAUCET_CLAIM
        with self.guarded(f"{kind} signature"):
            if self.network != AGENT_CLAIM_NETWORK:
                raise PolicyViolation(f"agent claims exist only on {AGENT_CLAIM_NETWORK}")
            row = self.ledger.get_faucet(draw_id)
            if row is None or row["status"] != "requested" or row["network"] != self.network:
                raise PolicyViolation(f"faucet draw {draw_id} is not an open {self.network} draw")
            address = row["address"]
            if keypair.ss58_address != address:
                raise PolicyViolation(f"spend key {keypair.ss58_address} is not the recipient "
                                      f"{address}")
            last = self.ledger.last_claim_nonce(self.network, address)
            if last is not None and int(nonce) <= last:
                raise PolicyViolation(f"claim nonce {nonce} is not above the last one ({last})")
            message = agent_claim_message(self.cfg.require_pinned_genesis().lower(), address,
                                          nonce)
            signature = keypair.sign(message.encode())
            if len(signature) != 64 or not keypair.verify(message.encode(), signature):
                raise RuntimeError("claim signature does not verify against the spend key")
            sig_hex = "0x" + signature.hex()
            self._log({"kind": "signature", "action": kind, "draw_id": draw_id,
                       "signer": address, "message": message, "signature": sig_hex})
            return message, sig_hex

    def sign_gateway_enrollment(self, keypair, message: str) -> str:
        """
        Sign a strictly validated 8-line gateway wallet-enrollment challenge with the spend key.
        Returns `0x`-hex sr25519 signature. Never logs any resulting bearer key.
        """
        kind = ActionKind.GATEWAY_ENROLL
        with self.guarded(f"{kind} signature"):
            gateway_url = self.cfg.network.gateway_url
            if not gateway_url:
                raise PolicyViolation(f"{self.network} has no gateway_url configured")
            genesis = self.cfg.require_pinned_genesis().lower()
            address = keypair.ss58_address
            nonce = validate_gateway_enrollment_challenge(
                message,
                address=address,
                gateway_url=gateway_url,
                genesis=genesis,
                network=self.network,
                now=self.now(),
            )
            payload = message.encode("utf-8")
            signature = keypair.sign(payload)
            if len(signature) != 64 or not keypair.verify(payload, signature):
                raise RuntimeError("enrollment signature does not verify against the spend key")
            sig_hex = "0x" + signature.hex()
            self._log({
                "kind": "signature",
                "action": kind,
                "signer": address,
                "gateway": urlparse(gateway_url).netloc,
                "nonce": nonce,
                "signature": sig_hex,
            })
            return sig_hex

    # ---- recovery -----------------------------------------------------------------------------

    def authorize_recovery(self, call: str, channel_id: str) -> dict:
        """Recovery calls are permissionless, but we only make them for our own channels."""
        self.ensure_alive()
        if call not in RECOVERY_CALLS:
            raise PolicyViolation(f"recovery call {call!r} is not allowed")
        row = self.ledger.find_by_channel(self.network, channel_id)
        if row is None:
            self._refuse(ActionKind.RECOVERY, [f"channel {channel_id} is not in our ledger"])
        return row

    # ---- funding and session keys (owner-approved; unused under key design A) -------------------

    def funding_limit(self) -> int:
        return self.cfg.caps.max_flop_per_day * 3

    def authorize_wallet_fund(self, amount: int) -> None:
        self.ensure_alive()
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            raise PolicyViolation("funding amount must be positive int base units")
        if amount > self.funding_limit():
            limit = format_flop(self.funding_limit())
            self._refuse(ActionKind.WALLET_FUND,
                         [f"{format_flop(amount)} exceeds 3 x max_flop_per_day ({limit})"])

    def authorize_session_key(self, spending_cap: int, duration_blocks: int) -> None:
        self.ensure_alive()
        problems = []
        if spending_cap <= 0 or spending_cap > self.funding_limit():
            problems.append(f"session spending_cap must be in (0, {format_flop(self.funding_limit())}]")
        if duration_blocks <= 0:
            problems.append("session duration must be positive")
        if problems:
            self._refuse(ActionKind.SESSION_KEY, problems)

    # ---- the signing gate ---------------------------------------------------------------------

    def submit_extrinsic(self, kind: ActionKind, pallet: str, call: str, params: dict, *,
                         substrate, keypair) -> str:
        """
        Sign and submit one allowlisted extrinsic, after the kill-switch and genesis checks.
        Waits for inclusion and requires success. Any failure trips the kill switch.
        """
        if (pallet, call) not in EXTRINSICS.get(kind, frozenset()):
            raise PolicyViolation(f"{kind} may not sign {pallet}.{call}")
        with self.guarded(f"{kind} {pallet}.{call}"):
            self.cfg.check_genesis(substrate.get_block_hash(0))  # GenesisMismatch trips too
            composed = substrate.compose_call(call_module=pallet, call_function=call,
                                              call_params=params)
            extrinsic = substrate.create_signed_extrinsic(call=composed, keypair=keypair)
            receipt = substrate.submit_extrinsic(extrinsic, wait_for_inclusion=True)
            tx_hash = receipt.extrinsic_hash
            tx_hash = tx_hash if isinstance(tx_hash, str) else tx_hash.value
            self._log({"kind": "extrinsic", "action": kind, "call": f"{pallet}.{call}",
                       "params": params, "signer": keypair.ss58_address, "tx": tx_hash,
                       "success": bool(receipt.is_success)})
            if not receipt.is_success:
                raise RuntimeError(f"{pallet}.{call} failed on chain: {receipt.error_message}")
            return tx_hash

    def run_signing_cli(self, kind: ActionKind, argv: list[str], env: dict[str, str], *,
                        signer: str, spend_id: int, timeout: float,
                        runner: Callable | None = None) -> subprocess.CompletedProcess:
        """
        Launch the pinned `flop` CLI, which signs with the spend keystore (route=direct). This is the
        only place the package starts it. The spend must already be authorized; secrets travel only
        in `env` (never argv), and the logged argv has the prompt redacted.
        """
        if kind is not ActionKind.INFERENCE:
            raise PolicyViolation(f"{kind} may not run the signing CLI")
        self.ensure_alive()
        row = self.ledger.get(spend_id)
        if row["status"] != "sent":
            raise PolicyViolation(f"spend {spend_id} is {row['status']}, not sent")
        if any("PASSWORD" in a.upper() and "=" in a for a in argv):
            raise PolicyViolation("refusing a secret on the command line")
        self._log({"kind": "cli", "action": kind, "spend_id": spend_id, "signer": signer,
                   "argv": redact_argv(argv)})
        try:
            return (runner or subprocess.run)(argv, env=env, capture_output=True, text=True,
                                              timeout=timeout, check=False)
        except subprocess.TimeoutExpired as e:
            raise CliTimeout(f"signing CLI exceeded {timeout}s") from e

    def log_cli_result(self, spend_id: int, result: dict) -> None:
        self._log({"kind": "cli-result", "spend_id": spend_id, "result": result})

    # ---- internals ----------------------------------------------------------------------------

    def _refuse(self, kind: ActionKind, problems: list[str]):
        reason = f"{kind} refused: " + "; ".join(problems)
        if "kill switch is tripped" not in problems:
            self.trip(reason)
        raise PolicyViolation(reason)

    def _log(self, entry: dict) -> None:
        entry = {"at": self.now().isoformat(), "network": self.network, **entry}
        with self.cfg.signatures_log.open("a") as f:
            f.write(json.dumps(entry, sort_keys=True, default=str) + "\n")
