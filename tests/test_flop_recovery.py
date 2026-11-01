"""Channel recovery: due-call decisions, policy-gated submission, ledger events, orphans."""

from types import SimpleNamespace

import pytest

from flopstar.flop import recovery
from flopstar.flop.client import ChannelView
from tests.flop_helpers import CENT, GENESIS, MINER, MODEL, SERVING, make, sha

AGENT = "5FpcgGhyP73JvTsRutafYQf7ocuAxLjAt8L6aYrbUmrtZc5p"
CHAN = "0x" + "ab" * 32


def view(state, **kw):
    base = {"agent": AGENT, "miner": MINER, "escrow": CENT, "opened_at": 100,
            "sla": {"max_duration": 1200}, "state": state}
    return ChannelView.from_storage(kw.pop("cid", CHAN), {**base, **kw})


@pytest.mark.parametrize("state, block, call, deadline", [
    ({"PendingAck": {"ack_deadline": 700}}, 700, None, 700),
    ({"PendingAck": {"ack_deadline": 700}}, 701, "expire_force_open", 700),
    ("Open", 1300, None, 1300),
    ("Open", 1301, "timeout", 1300),
    ({"Settling": {"final_root": "0x00", "claimed_gn": 5, "challenge_end": 2000}}, 2001,
     "finalize", 2000),
    ("Closed", 9999, None, None),
])
def test_decide(state, block, call, deadline):
    assert recovery.decide(view(state), block)[:2] == (call, deadline)


def test_unknown_state_is_an_error():
    with pytest.raises(ValueError):
        view("Exploded")


class FakeSubstrate:
    def __init__(self):
        self.submitted = []

    def get_block_hash(self, n):
        return GENESIS

    def compose_call(self, call_module, call_function, call_params):
        return (call_module, call_function, call_params)

    def create_signed_extrinsic(self, call, keypair):
        return call

    def submit_extrinsic(self, extrinsic, wait_for_inclusion):
        self.submitted.append(extrinsic)
        return SimpleNamespace(extrinsic_hash="0xrefund", is_success=True, error_message=None)


class FakeChain:
    def __init__(self, views, block=1301):
        self.views, self.block, self.substrate = {v.channel_id: v for v in views}, block, FakeSubstrate()

    def finalized(self):
        return "0xhead", self.block

    def channel(self, cid, head):
        return self.views.get(cid.lower())

    def channels_by_agent(self, agent, head):
        return [v for v in self.views.values() if v.agent == agent]


def sent_spend(ledger, policy, channel=CHAN, status="failed"):
    sid = policy.authorize_inference(route="direct", model=MODEL, miner=MINER, max_flop=CENT,
                                     purpose="t", request_sha256=sha(1))
    ledger.transition(sid, "sent", **({"channel_id": channel} if channel else {}))
    ledger.transition(sid, status)
    return sid


def test_due_timeout_is_submitted_through_policy(tmp_path, monkeypatch):
    cfg, ledger, policy, _ = make(tmp_path, monkeypatch)
    sid = sent_spend(ledger, policy)
    chain = FakeChain([view("Open")])
    items = recovery.scan(cfg, policy, chain, AGENT)
    assert [(i.spend_id, i.call) for i in items] == [(sid, "timeout")]
    assert "flopstar flop recovery --post" in recovery.human_command(cfg, items[0])
    done = recovery.recover(cfg, policy, chain, SimpleNamespace(ss58_address=AGENT), items)
    assert done[0]["status"] == "refunded" and chain.substrate.submitted == [
        ("ComputeChannel", "timeout", {"channel_id": CHAN})]
    assert [e["status"] for e in ledger.events(sid)][-2:] == ["recovering", "refunded"]
    assert "ComputeChannel.timeout" in cfg.signatures_log.read_text()


def test_not_yet_due_and_finalize_command(tmp_path, monkeypatch):
    cfg, ledger, policy, _ = make(tmp_path, monkeypatch)
    sent_spend(ledger, policy)
    settling = view({"Settling": {"final_root": "0x0", "claimed_gn": 1, "challenge_end": 5000}})
    item = recovery.scan(cfg, policy, FakeChain([settling]), AGENT)[0]
    assert item.call is None and item.blocks_left == 5000 + 1 - 1301
    item.call = "finalize"
    assert "channel finalize " + CHAN in recovery.human_command(cfg, item)


def test_foreign_channel_is_flagged_not_recovered(tmp_path, monkeypatch):
    cfg, ledger, policy, _ = make(tmp_path, monkeypatch)
    sent_spend(ledger, policy)
    item = recovery.scan(cfg, policy, FakeChain([view("Open", agent=SERVING)]), AGENT)[0]
    assert item.call is None and item.note.startswith("MISMATCH")


def test_orphan_channel_attaches_to_the_single_unchanneled_failure(tmp_path, monkeypatch):
    cfg, ledger, policy, _ = make(tmp_path, monkeypatch)
    sid = sent_spend(ledger, policy, channel=None)
    chain = FakeChain([view({"PendingAck": {"ack_deadline": 700}})])
    items = recovery.attach_orphans(policy, recovery.scan(cfg, policy, chain, AGENT))
    assert items[0].spend_id == sid and ledger.get(sid)["channel_id"] == CHAN
    done = recovery.recover(cfg, policy, chain, SimpleNamespace(ss58_address=AGENT), items)
    assert done[0]["call"] == "expire_force_open"


def test_recovery_refused_when_killed(tmp_path, monkeypatch):
    cfg, ledger, policy, _ = make(tmp_path, monkeypatch)
    sent_spend(ledger, policy)
    chain = FakeChain([view("Open")])
    items = recovery.scan(cfg, policy, chain, AGENT)
    policy.trip("stop")
    with pytest.raises(Exception, match="KILL-flop"):
        recovery.recover(cfg, policy, chain, SimpleNamespace(ss58_address=AGENT), items)
    assert chain.substrate.submitted == []
