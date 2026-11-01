"""FlopPolicy: allowlists, ledger-backed caps across restarts, kill switch, blocklist, signing gate."""

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from flopstar.flop.config import ConfigError, GenesisMismatch
from flopstar.flop.ledger import Ledger
from flopstar.flop.policy import ActionKind, FlopPolicy, KillSwitchTripped
from flopstar.signer import PolicyViolation
from tests.flop_helpers import BLOCKED, CENT, GENESIS, MINER, MODEL, config_text, make, sha


def ask(policy, n=1, **kw):
    args = {"route": "direct", "model": MODEL, "miner": MINER, "max_flop": CENT,
            "purpose": "research-notes", "request_sha256": sha(n)}
    return policy.authorize_inference(**{**args, **kw})


class FakeSubstrate:
    def __init__(self, genesis=GENESIS, fail=None, success=True):
        self.genesis, self.fail, self.success, self.submitted = genesis, fail, success, []

    def get_block_hash(self, n):
        assert n == 0
        return self.genesis

    def compose_call(self, call_module, call_function, call_params):
        return (call_module, call_function, call_params)

    def create_signed_extrinsic(self, call, keypair):
        if self.fail:
            raise self.fail
        return ("signed", call)

    def submit_extrinsic(self, extrinsic, wait_for_inclusion):
        self.submitted.append(extrinsic)
        return SimpleNamespace(extrinsic_hash="0xfeed", is_success=self.success,
                               error_message=None if self.success else "Module error")


KEYPAIR = SimpleNamespace(ss58_address=MINER, private_key=b"S3CRET-KEY-BYTES")


def test_allowed_request_is_recorded_prepared(tmp_path, monkeypatch):
    cfg, ledger, policy, _ = make(tmp_path, monkeypatch)
    sid = ask(policy)
    assert ledger.get(sid)["status"] == "prepared"
    assert not cfg.kill_path.exists()
    log = [json.loads(line) for line in cfg.signatures_log.read_text().splitlines()]
    assert log[-1]["action"] == "inference" and log[-1]["spend_id"] == sid


@pytest.mark.parametrize("override, reason", [
    ({"model": "gpt-oss"}, "allowed_models"),
    ({"miner": BLOCKED}, "blocklist"),
    ({"max_flop": CENT + 1}, "max_flop_per_request"),
    ({"route": "smoke-signal"}, "route"),
    ({"route": "direct", "miner": None}, "name its miner"),
    ({"max_flop": 0.01}, "int base units"),
])
def test_violations_raise_and_trip(tmp_path, monkeypatch, override, reason):
    cfg, ledger, policy, _ = make(tmp_path, monkeypatch)
    with pytest.raises(PolicyViolation, match=reason):
        ask(policy, **override)
    assert reason in cfg.kill_path.read_text()
    assert ledger.requests_since("devnet", policy.now().replace(year=2000)) == 0


def test_blocklisted_miner_refused_even_as_only_offer(tmp_path, monkeypatch):
    _, _, policy, _ = make(tmp_path, monkeypatch)
    assert any("blocklist" in p for p in policy.check_inference("direct", MODEL, BLOCKED, CENT))


def test_daily_cap_holds_across_restarts(tmp_path, monkeypatch):
    cfg, ledger, policy, clock = make(tmp_path, monkeypatch)
    for i in range(4):
        ask(policy, i)
        clock.advance(minutes=20)
    ledger.close()
    # A new process: fresh Ledger and FlopPolicy over the same database.
    ledger2 = Ledger(cfg.db_path, now=clock)
    policy2 = FlopPolicy(cfg, ledger2, now=clock)
    clock.advance(hours=2)
    assert policy2.budget().spent_today == 4 * CENT
    ask(policy2, 5)  # 0.05 reached exactly
    with pytest.raises(PolicyViolation, match="remaining"):
        ask(policy2, 6)


def test_hourly_request_cap_and_window(tmp_path, monkeypatch):
    _, ledger, policy, clock = make(tmp_path, monkeypatch)
    for i in range(4):
        sid = ask(policy, i, max_flop=1)
        ledger.transition(sid, "failed")  # even failures count as requests
    assert policy.check_inference("direct", MODEL, MINER, 1)
    clock.advance(minutes=61)
    assert policy.check_inference("direct", MODEL, MINER, 1) == []


def test_refunds_and_unsent_failures_free_the_budget(tmp_path, monkeypatch):
    _, ledger, policy, _ = make(tmp_path, monkeypatch)
    a, b, c = ask(policy, 1), ask(policy, 2), ask(policy, 3)
    ledger.transition(a, "sent")
    ledger.transition(a, "refunded")
    ledger.transition(b, "failed")  # never sent
    ledger.transition(c, "sent")
    ledger.transition(c, "settled", settled_flop=CENT // 4)
    assert policy.budget().spent_today == CENT // 4


def test_kill_switch_blocks_everything(tmp_path, monkeypatch):
    cfg, ledger, policy, _ = make(tmp_path, monkeypatch)
    sid = ask(policy)
    ledger.transition(sid, "sent", channel_id="0x" + "ab" * 32)
    policy.trip("operator test")
    calls = [
        lambda: ask(policy, 2),
        lambda: policy.authorize_faucet(MINER),
        lambda: policy.authorize_recovery("timeout", "0x" + "ab" * 32),
        lambda: policy.authorize_wallet_fund(CENT),
        lambda: policy.authorize_session_key(CENT, 100),
        lambda: policy.submit_extrinsic(ActionKind.RECOVERY, "ComputeChannel", "timeout",
                                        {"channel_id": "0x" + "ab" * 32},
                                        substrate=FakeSubstrate(), keypair=KEYPAIR),
    ]
    for call in calls:
        with pytest.raises(KillSwitchTripped):
            call()
    assert "operator test" in cfg.kill_path.read_text()


def test_faucet_cap(tmp_path, monkeypatch):
    cfg, ledger, policy, clock = make(tmp_path, monkeypatch)
    did = policy.authorize_faucet(MINER)
    ledger.finish_faucet(did, "funded", {})
    with pytest.raises(PolicyViolation, match="faucet"):
        policy.authorize_faucet(MINER)
    clock.advance(days=1, seconds=1)
    cfg.kill_path.unlink()  # a human clears the trip
    policy.authorize_faucet(MINER)


def test_testnet_without_genesis_refuses(tmp_path, monkeypatch):
    _, _, policy, _ = make(tmp_path, monkeypatch, text=config_text("testnet"))
    with pytest.raises(ConfigError, match="no pinned genesis"):
        ask(policy)
    with pytest.raises(ConfigError, match="no pinned genesis"):
        policy.authorize_faucet(MINER)


def test_recovery_only_for_our_channels_and_allowed_calls(tmp_path, monkeypatch):
    cfg, ledger, policy, _ = make(tmp_path, monkeypatch)
    chan = "0x" + "cd" * 32
    sid = ask(policy)
    ledger.transition(sid, "sent", channel_id=chan)
    assert policy.authorize_recovery("expire_force_open", chan.upper().replace("0X", "0x"))["id"] == sid
    with pytest.raises(PolicyViolation):
        policy.authorize_recovery("force_settle", chan)
    with pytest.raises(PolicyViolation, match="not in our ledger"):
        policy.authorize_recovery("timeout", "0x" + "00" * 32)
    assert cfg.kill_path.exists()


def test_funding_and_session_caps(tmp_path, monkeypatch):
    _, _, policy, _ = make(tmp_path, monkeypatch)
    policy.authorize_wallet_fund(15 * CENT)  # 3 x 0.05
    with pytest.raises(PolicyViolation):
        policy.authorize_wallet_fund(15 * CENT + 1)


def test_signing_gate_allowlist_success_and_log(tmp_path, monkeypatch):
    cfg, _, policy, _ = make(tmp_path, monkeypatch)
    sub = FakeSubstrate()
    with pytest.raises(PolicyViolation):
        policy.submit_extrinsic(ActionKind.RECOVERY, "Balances", "transfer_keep_alive", {},
                                substrate=sub, keypair=KEYPAIR)
    with pytest.raises(PolicyViolation):
        policy.submit_extrinsic(ActionKind.INFERENCE, "ComputeChannel", "force_open", {},
                                substrate=sub, keypair=KEYPAIR)
    assert sub.submitted == [] and not cfg.kill_path.exists()
    tx = policy.submit_extrinsic(ActionKind.RECOVERY, "ComputeChannel", "finalize",
                                 {"channel_id": "0x01"}, substrate=sub, keypair=KEYPAIR)
    assert tx == "0xfeed"
    log = cfg.signatures_log.read_text()
    assert "ComputeChannel.finalize" in log and "S3CRET" not in log


@pytest.mark.parametrize("sub, error, reason", [
    (FakeSubstrate(genesis="0x" + "11" * 32), GenesisMismatch, "GenesisMismatch"),
    (FakeSubstrate(fail=ConnectionError("rpc down")), ConnectionError, "rpc down"),
    (FakeSubstrate(success=False), RuntimeError, "failed on chain"),
])
def test_signing_failures_trip_the_switch(tmp_path, monkeypatch, sub, error, reason):
    cfg, _, policy, _ = make(tmp_path, monkeypatch)
    with pytest.raises(error):
        policy.submit_extrinsic(ActionKind.RECOVERY, "ComputeChannel", "timeout",
                                {"channel_id": "0x01"}, substrate=sub, keypair=KEYPAIR)
    assert reason in cfg.kill_path.read_text()


def test_only_policy_calls_signing_methods():
    pkg = Path(__file__).parents[1] / "src" / "flopstar" / "flop"
    # Calling the gate (`policy.submit_extrinsic(`) is the intended path; anything else is not.
    signing = re.compile(r"create_signed_extrinsic|(?<!policy\.)submit_extrinsic\(|\.sign\(|"
                         r"SubstrateClient\(|subprocess|create_from_uri|create_from_mnemonic")
    key_loading = re.compile(r"create_from_encrypted_json|export_to_encrypted_json|create_from_seed")
    sources = {p.name: p.read_text() for p in pkg.glob("*.py")}
    # client.py defines CheckedSubstrate.submit_extrinsic (a runtime check), it never calls signing.
    sources["client.py"] = sources["client.py"].replace("def submit_extrinsic(self", "")
    sources["client.py"] = sources["client.py"].replace("super().submit_extrinsic(", "")
    assert [n for n, src in sources.items() if n != "policy.py" and signing.search(src)] == []
    assert [n for n, src in sources.items() if n != "wallet.py" and key_loading.search(src)] == []
