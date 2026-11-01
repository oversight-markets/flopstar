"""Faucet draws: genesis checks, exact amounts, caps, and which failures trip the switch."""

import json

import httpx
import pytest

from flopstar.flop import faucet
from flopstar.flop.config import GenesisMismatch
from tests.flop_helpers import GENESIS, MINER, make


class Chain:
    def __init__(self, genesis=GENESIS):
        self._g = genesis

    def genesis(self):
        return self._g


def client(fund_status=200, fund_body=None, chain_genesis=GENESIS, calls=None):
    def handle(request):
        if calls is not None:
            calls.append((request.method, request.url.path, request.content))
        if request.url.path == "/chain":
            return httpx.Response(200, json={"network": "devnet", "genesisHash": chain_genesis})
        body = fund_body if fund_body is not None else b'{"amount": 25, "txHash": "0xabc"}'
        return httpx.Response(fund_status, content=body, headers={"retry-after": "3600"})
    return httpx.Client(transport=httpx.MockTransport(handle))


def test_funded_draw_is_exact_and_recorded(tmp_path, monkeypatch):
    cfg, ledger, policy, _ = make(tmp_path, monkeypatch)
    calls = []
    out = faucet.draw(cfg, policy, Chain(), client(fund_body=b'{"amount": 0.1, "txHash": "0x1"}',
                                                   calls=calls), MINER)
    assert out["status"] == "funded" and out["amount"] == "0.1"
    row = ledger.conn.execute("SELECT * FROM flop_faucet_draws").fetchone()
    assert row["amount"] == str(10**17) and row["status"] == "funded"
    assert calls[-1][1] == "/fund" and json.loads(calls[-1][2]) == {"address": MINER}
    with pytest.raises(Exception, match="max_faucet_draws_per_day"):
        faucet.draw(cfg, policy, Chain(), client(), MINER)


@pytest.mark.parametrize("chain, faucet_genesis", [
    (Chain("0x" + "11" * 32), GENESIS),
    (Chain(), "0x" + "22" * 32),
])
def test_genesis_mismatch_refuses_and_trips(tmp_path, monkeypatch, chain, faucet_genesis):
    cfg, _ledger, policy, _ = make(tmp_path, monkeypatch)
    calls = []
    with pytest.raises(GenesisMismatch):
        faucet.draw(cfg, policy, chain, client(chain_genesis=faucet_genesis, calls=calls), MINER)
    assert [c[1] for c in calls] == ["/chain"]  # never reached /fund
    assert cfg.kill_path.exists()


def test_rate_limit_is_recorded_without_tripping(tmp_path, monkeypatch):
    cfg, _ledger, policy, _ = make(tmp_path, monkeypatch)
    out = faucet.draw(cfg, policy, Chain(), client(429, b'{"error": "too many draws"}'), MINER)
    assert out["status"] == "failed" and out["retry_after"] == "3600"
    assert not cfg.kill_path.exists()
    assert policy.budget().faucet_draws_left_today == 1  # failures don't use the allowance


def test_malformed_success_trips(tmp_path, monkeypatch):
    cfg, _, policy, _ = make(tmp_path, monkeypatch)
    with pytest.raises(faucet.FaucetError):
        faucet.draw(cfg, policy, Chain(), client(fund_body=b'{"amount": "lots"}'), MINER)
    assert cfg.kill_path.exists()
