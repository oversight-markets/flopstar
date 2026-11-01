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
import subprocess
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

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
    INFERENCE = "inference"
    RECOVERY = "recovery"
    WALLET_FUND = "wallet_fund"
    SESSION_KEY = "session_key"


RECOVERY_CALLS = ("expire_force_open", "timeout", "finalize")

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
