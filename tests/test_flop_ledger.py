"""The spend ledger is append-only: core columns frozen, results fill-once, history in events."""

import sqlite3

import pytest

from flopstar.flop.amounts import AmountError
from flopstar.flop.ledger import Ledger, LedgerError
from tests.flop_helpers import CENT, MINER, MODEL, sha


@pytest.fixture
def ledger(tmp_path):
    return Ledger(tmp_path / "flopstar.db")


def prepare(ledger, **kw):
    args = {"network": "devnet", "route": "direct", "model": MODEL, "max_flop": CENT,
            "purpose": "research-notes", "request_sha256": sha(1), "miner": MINER}
    return ledger.prepare(**{**args, **kw})


def test_lifecycle_keeps_full_history(ledger):
    sid = prepare(ledger)
    ledger.transition(sid, "sent", {"argv": "session run"}, channel_id="0x" + "aa" * 32)
    ledger.transition(sid, "settled", settled_flop=7 * 10**15, settlement_tx="0xbeef",
                      response_sha256=sha(2), verification="sdk_verified", receipt_json="{}")
    row = ledger.get(sid)
    assert row["status"] == "settled" and row["settled_flop"] == str(7 * 10**15)
    assert row["max_flop"] == str(CENT)
    assert [e["status"] for e in ledger.events(sid)] == ["prepared", "sent", "settled"]


def test_disallowed_transitions_raise(ledger):
    sid = prepare(ledger)
    with pytest.raises(LedgerError):
        ledger.transition(sid, "settled")
    ledger.transition(sid, "sent")
    ledger.transition(sid, "refunded")
    with pytest.raises(LedgerError):
        ledger.transition(sid, "sent")
    assert [e["status"] for e in ledger.events(sid)] == ["prepared", "sent", "refunded"]


@pytest.mark.parametrize("sql", [
    "UPDATE flop_spend SET max_flop = '1' WHERE id = 1",
    "UPDATE flop_spend SET model = 'other' WHERE id = 1",
    "UPDATE flop_spend SET purpose = 'x' WHERE id = 1",
    "UPDATE flop_spend SET status = 'settled' WHERE id = 1",
    "UPDATE flop_spend SET miner = 'someone-else' WHERE id = 1",
    "DELETE FROM flop_spend WHERE id = 1",
    "UPDATE flop_spend_events SET status = 'settled' WHERE id = 1",
    "DELETE FROM flop_spend_events",
])
def test_triggers_block_rewriting_history(ledger, sql):
    prepare(ledger)
    with pytest.raises(sqlite3.DatabaseError):
        ledger.conn.execute(sql)


def test_result_columns_fill_once(ledger):
    sid = prepare(ledger)
    ledger.transition(sid, "sent", channel_id="0x01")
    with pytest.raises(sqlite3.DatabaseError):
        ledger.transition(sid, "failed", channel_id="0x02")
    assert ledger.get(sid)["status"] == "sent"  # the failed write rolled back entirely
    ledger.mark_reported(sid)
    first = ledger.get(sid)["touchstone_reported_at"]
    ledger.mark_reported(sid)
    assert ledger.get(sid)["touchstone_reported_at"] == first


def test_bad_inputs_are_refused(ledger):
    with pytest.raises(LedgerError):
        prepare(ledger, route="carrier-pigeon")
    with pytest.raises(LedgerError):
        prepare(ledger, purpose="")
    with pytest.raises(AmountError):
        prepare(ledger, max_flop=0.01)
    sid = prepare(ledger)
    with pytest.raises(LedgerError):
        ledger.transition(sid, "sent", max_flop=1)


def test_faucet_draws_settle_once(ledger):
    did = ledger.record_faucet("devnet", MINER, None)
    ledger.finish_faucet(did, "funded", {"txHash": "0x1"}, amount=25 * 10**18)
    with pytest.raises(sqlite3.DatabaseError):
        ledger.finish_faucet(did, "failed", {})
    with pytest.raises(sqlite3.DatabaseError):
        ledger.conn.execute("DELETE FROM flop_faucet_draws")


def test_monitor_messages_table_untouched(tmp_path):
    from flopstar.store import MessageStore
    db = tmp_path / "flopstar.db"
    MessageStore(db).store_message("r", 1, {"seq": 1})
    Ledger(db)
    assert MessageStore(db).get_message("r", 1) == {"seq": 1}
