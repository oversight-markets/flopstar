"""
Append-only FLOP spend ledger in data/flopstar.db (new tables; the monitor's `messages` is untouched).

flop_spend         one row per inference request. Core columns never change; result columns are
                   fill-once (NULL -> value, never rewritten); `status` mirrors the latest event.
flop_spend_events  every status change, with detail. Never updated or deleted.
flop_faucet_draws  every faucet draw attempt; status moves once from `requested`.

SQLite triggers enforce all of this, so a bug elsewhere cannot rewrite history. Amounts are base-unit
digit strings.

`settled_flop` is the agent's real outflow for the request, escrow - refund, taken from the chain's
settle/refund events or the spend account's balance change. It is never miner_paid or the claimed
amount: the 1% audit earmark also comes out of the escrow, and the devnet settle at block 2796872
refunded nothing, so a claim below the escrow can still mean the whole escrow was spent. Nothing
writes it yet (the CLI does not report an amount, SPEC S6), so a settled request counts its full
escrow. Cap checks and inserts share one BEGIN IMMEDIATE transaction (see `transaction`), so
concurrent runs cannot both pass a cap.
"""

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .amounts import AmountError, check_raw, raw_from_str

ROUTES = ("direct", "gateway", "native")
STATUSES = ("prepared", "sent", "settled", "refunded", "failed", "recovering")
VERIFICATIONS = ("sdk_verified", "unverified", "failed")
TRANSITIONS = {
    "prepared": {"sent", "failed"},
    "sent": {"settled", "refunded", "failed", "recovering"},
    "failed": {"recovering"},  # a failed session can still hold escrow
    "recovering": {"refunded", "settled", "failed"},
    "settled": set(),
    "refunded": set(),
}
CORE = ("id", "ts", "network", "route", "model", "max_flop", "purpose", "request_sha256")
FILL_ONCE = ("miner", "channel_id", "settled_flop", "settlement_tx", "response_sha256",
             "receipt_json", "verification", "touchstone_reported_at")

_changed = " OR ".join(f"NEW.{c} IS NOT OLD.{c}" for c in CORE)
_refilled = " OR ".join(f"(OLD.{c} IS NOT NULL AND NEW.{c} IS NOT OLD.{c})" for c in FILL_ONCE)

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS flop_spend (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    network TEXT NOT NULL,
    route TEXT NOT NULL CHECK (route IN {ROUTES}),
    model TEXT NOT NULL,
    miner TEXT,
    channel_id TEXT,
    max_flop TEXT NOT NULL,
    settled_flop TEXT,
    settlement_tx TEXT,
    status TEXT NOT NULL CHECK (status IN {STATUSES}),
    purpose TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_sha256 TEXT,
    receipt_json TEXT,
    verification TEXT CHECK (verification IS NULL OR verification IN {VERIFICATIONS}),
    touchstone_reported_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_flop_spend_net_ts ON flop_spend(network, ts);
CREATE INDEX IF NOT EXISTS idx_flop_spend_channel ON flop_spend(network, channel_id);

CREATE TABLE IF NOT EXISTS flop_spend_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    spend_id INTEGER NOT NULL REFERENCES flop_spend(id),
    ts TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN {STATUSES}),
    detail TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_flop_events_spend ON flop_spend_events(spend_id, id);

CREATE TABLE IF NOT EXISTS flop_faucet_draws (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    network TEXT NOT NULL,
    address TEXT NOT NULL,
    amount TEXT,
    status TEXT NOT NULL CHECK (status IN ('requested', 'funded', 'failed')),
    detail TEXT
);

CREATE TRIGGER IF NOT EXISTS flop_spend_no_delete BEFORE DELETE ON flop_spend
BEGIN SELECT RAISE(ABORT, 'flop_spend is append-only'); END;

CREATE TRIGGER IF NOT EXISTS flop_spend_core BEFORE UPDATE ON flop_spend
WHEN {_changed}
BEGIN SELECT RAISE(ABORT, 'flop_spend core columns are immutable'); END;

CREATE TRIGGER IF NOT EXISTS flop_spend_fill_once BEFORE UPDATE ON flop_spend
WHEN {_refilled}
BEGIN SELECT RAISE(ABORT, 'flop_spend result columns are fill-once'); END;

CREATE TRIGGER IF NOT EXISTS flop_spend_status BEFORE UPDATE OF status ON flop_spend
WHEN NEW.status IS NOT (SELECT status FROM flop_spend_events
                        WHERE spend_id = NEW.id ORDER BY id DESC LIMIT 1)
BEGIN SELECT RAISE(ABORT, 'flop_spend status must follow flop_spend_events'); END;

CREATE TRIGGER IF NOT EXISTS flop_events_no_update BEFORE UPDATE ON flop_spend_events
BEGIN SELECT RAISE(ABORT, 'flop_spend_events is append-only'); END;

CREATE TRIGGER IF NOT EXISTS flop_events_no_delete BEFORE DELETE ON flop_spend_events
BEGIN SELECT RAISE(ABORT, 'flop_spend_events is append-only'); END;

CREATE TRIGGER IF NOT EXISTS flop_faucet_no_delete BEFORE DELETE ON flop_faucet_draws
BEGIN SELECT RAISE(ABORT, 'flop_faucet_draws is append-only'); END;

CREATE TRIGGER IF NOT EXISTS flop_faucet_once BEFORE UPDATE ON flop_faucet_draws
WHEN OLD.status IS NOT 'requested' OR NEW.id IS NOT OLD.id OR NEW.ts IS NOT OLD.ts
  OR NEW.network IS NOT OLD.network OR NEW.address IS NOT OLD.address
  OR (OLD.amount IS NOT NULL AND NEW.amount IS NOT OLD.amount)
BEGIN SELECT RAISE(ABORT, 'flop_faucet_draws rows settle once'); END;
"""


class LedgerError(Exception):
    """A ledger write that would break its rules."""


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).isoformat(timespec="microseconds")


class Ledger:
    def __init__(self, db_path: Path, now: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self.now = now
        self.conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._depth = 0

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE (re-entrant): reads and writes inside see and hold one consistent state."""
        if self._depth:
            self._depth += 1
            try:
                yield self.conn
            finally:
                self._depth -= 1
            return
        self.conn.execute("BEGIN IMMEDIATE")
        self._depth = 1
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")
        finally:
            self._depth = 0

    # ---- spends -------------------------------------------------------------------------------

    def prepare(self, *, network: str, route: str, model: str, max_flop: int, purpose: str,
                request_sha256: str, miner: str | None = None) -> int:
        if route not in ROUTES:
            raise LedgerError(f"unknown route {route!r}")
        if not purpose:
            raise LedgerError("every spend needs a purpose")
        if len(request_sha256) != 64:
            raise LedgerError("request_sha256 must be 64 hex characters")
        ts = _iso(self.now())
        with self.transaction() as c:
            cur = c.execute(
                "INSERT INTO flop_spend (ts, network, route, model, miner, max_flop, status,"
                " purpose, request_sha256) VALUES (?, ?, ?, ?, ?, ?, 'prepared', ?, ?)",
                (ts, network, route, model, miner, str(check_raw(max_flop)), purpose,
                 request_sha256),
            )
            spend_id = cur.lastrowid
            c.execute(
                "INSERT INTO flop_spend_events (spend_id, ts, status, detail) VALUES (?, ?, ?, ?)",
                (spend_id, ts, "prepared", "{}"),
            )
        return spend_id

    def transition(self, spend_id: int, status: str, detail: dict | None = None, **fill) -> None:
        """Append a status event and set fill-once result columns in the same transaction."""
        unknown = set(fill) - set(FILL_ONCE) - {"touchstone_reported_at"}
        if unknown:
            raise LedgerError(f"cannot set {sorted(unknown)}")
        if "settled_flop" in fill and fill["settled_flop"] is not None:
            try:
                fill["settled_flop"] = str(check_raw(fill["settled_flop"]))
            except AmountError as e:
                raise LedgerError(f"spend {spend_id}: bad outflow: {e}") from None
        if fill.get("verification") not in (None, *VERIFICATIONS):
            raise LedgerError(f"bad verification {fill['verification']!r}")
        with self.transaction() as c:
            row = self.get(spend_id)
            if fill.get("settled_flop") is not None and (
                    int(fill["settled_flop"]) > raw_from_str(row["max_flop"])):
                raise LedgerError(f"spend {spend_id}: outflow {fill['settled_flop']} exceeds "
                                  f"its escrow {row['max_flop']}")
            if status not in TRANSITIONS.get(row["status"], set()):
                raise LedgerError(f"spend {spend_id}: {row['status']} -> {status} not allowed")
            c.execute(
                "INSERT INTO flop_spend_events (spend_id, ts, status, detail) VALUES (?, ?, ?, ?)",
                (spend_id, _iso(self.now()), status, json.dumps(detail or {}, sort_keys=True)),
            )
            sets = ", ".join(f"{k} = ?" for k in fill)
            c.execute(
                f"UPDATE flop_spend SET status = ?{', ' + sets if sets else ''} WHERE id = ?",
                (status, *fill.values(), spend_id),
            )

    def mark_reported(self, spend_id: int) -> None:
        with self.transaction() as c:
            c.execute(
                "UPDATE flop_spend SET touchstone_reported_at = ? WHERE id = ?"
                " AND touchstone_reported_at IS NULL",
                (_iso(self.now()), spend_id),
            )

    def get(self, spend_id: int) -> dict:
        row = self.conn.execute("SELECT * FROM flop_spend WHERE id = ?", (spend_id,)).fetchone()
        if row is None:
            raise LedgerError(f"no spend {spend_id}")
        return dict(row)

    def events(self, spend_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM flop_spend_events WHERE spend_id = ? ORDER BY id", (spend_id,)
        )
        return [dict(r) for r in rows]

    def find_by_channel(self, network: str, channel_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM flop_spend WHERE network = ? AND lower(channel_id) = lower(?)",
            (network, channel_id),
        ).fetchone()
        return dict(row) if row else None

    def open_channels(self, network: str) -> list[dict]:
        """Our channels that may still hold escrow (not settled or refunded)."""
        rows = self.conn.execute(
            "SELECT * FROM flop_spend WHERE network = ? AND channel_id IS NOT NULL"
            " AND status IN ('sent', 'failed', 'recovering') ORDER BY id",
            (network,),
        )
        return [dict(r) for r in rows]

    def last_used_miners(self, network: str) -> dict[str, str]:
        """miner -> timestamp of our latest request to it."""
        rows = self.conn.execute(
            "SELECT miner, MAX(ts) AS ts FROM flop_spend WHERE network = ? AND miner IS NOT NULL"
            " GROUP BY miner", (network,))
        return {r["miner"]: r["ts"] for r in rows}

    def unchanneled_failures(self, network: str, since: datetime) -> list[dict]:
        """Direct requests that were sent but never learned their channel (e.g. a CLI timeout)."""
        rows = self.conn.execute(
            "SELECT * FROM flop_spend WHERE network = ? AND route = 'direct' AND ts >= ?"
            " AND channel_id IS NULL AND status IN ('sent', 'failed')"
            " AND EXISTS (SELECT 1 FROM flop_spend_events e WHERE e.spend_id = flop_spend.id"
            " AND e.status = 'sent') ORDER BY id", (network, _iso(since)))
        return [dict(r) for r in rows]

    # ---- caps ---------------------------------------------------------------------------------

    def spent_since(self, network: str, since: datetime) -> int:
        """
        Base units committed since `since`. Conservative: escrow counts in full from authorize
        until it is settled with a recorded outflow (`settled_flop` = escrow - refund, never the
        claimed amount) or refunded; a request that failed before it was sent counts 0.
        Transaction fees are not included (they are not in max_flop).
        """
        rows = self.conn.execute(
            "SELECT s.max_flop, s.settled_flop, s.status, EXISTS (SELECT 1 FROM flop_spend_events e"
            " WHERE e.spend_id = s.id AND e.status = 'sent') AS was_sent"
            " FROM flop_spend s WHERE s.network = ? AND s.ts >= ?",
            (network, _iso(since)),
        )
        total = 0
        for r in rows:
            if r["status"] == "refunded" or (r["status"] == "failed" and not r["was_sent"]):
                continue
            if r["status"] == "settled" and r["settled_flop"] is not None:
                total += raw_from_str(r["settled_flop"])
            else:
                total += raw_from_str(r["max_flop"])
        return total

    def requests_since(self, network: str, since: datetime) -> int:
        """Every request attempt counts, whatever its outcome."""
        return self.conn.execute(
            "SELECT COUNT(*) FROM flop_spend WHERE network = ? AND ts >= ?",
            (network, _iso(since)),
        ).fetchone()[0]

    # ---- faucet -------------------------------------------------------------------------------

    def record_faucet(self, network: str, address: str, amount: int | None) -> int:
        with self.transaction() as c:
            return c.execute(
                "INSERT INTO flop_faucet_draws (ts, network, address, amount, status)"
                " VALUES (?, ?, ?, ?, 'requested')",
                (_iso(self.now()), network, address,
                 None if amount is None else str(check_raw(amount))),
            ).lastrowid

    def finish_faucet(self, draw_id: int, status: str, detail: dict,
                      amount: int | None = None) -> None:
        if status not in ("funded", "failed"):
            raise LedgerError(f"bad faucet status {status!r}")
        with self.transaction() as c:
            c.execute(
                "UPDATE flop_faucet_draws SET status = ?, detail = ?, amount = COALESCE(amount, ?)"
                " WHERE id = ?",
                (status, json.dumps(detail, sort_keys=True),
                 None if amount is None else str(check_raw(amount)), draw_id),
            )

    def faucet_draws_since(self, network: str, since: datetime) -> int:
        """Draws that may have counted against FLOP's per-identity limit (all but failures)."""
        return self.conn.execute(
            "SELECT COUNT(*) FROM flop_faucet_draws WHERE network = ? AND ts >= ?"
            " AND status != 'failed'",
            (network, _iso(since)),
        ).fetchone()[0]


DAY = timedelta(days=1)
HOUR = timedelta(hours=1)
