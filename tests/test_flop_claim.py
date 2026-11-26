"""Testnet agent claims: exact message, pre-checks, the signing gate, and unknown-outcome reconcile."""

import json

import httpx
import pytest
from substrateinterface import Keypair, KeypairType

from flopstar.flop import cli, faucet
from flopstar.flop.config import GenesisMismatch
from flopstar.flop.policy import agent_claim_message
from flopstar.signer import PolicyViolation
from tests.flop_helpers import config_text, make

TESTNET = "0x918abd58fb16306b30ef3886514cfaebffecb4de43e82b6f8ec4795c43cfd800"
TX = "0x" + "ab" * 32


def keypair():
    return Keypair.create_from_seed("0x" + "07" * 32, ss58_format=42,
                                    crypto_type=KeypairType.SR25519)


KP = keypair()
ADDRESS = KP.ss58_address


class Chain:
    def __init__(self, genesis=TESTNET):
        self._g = genesis

    def genesis(self):
        return self._g


def faucet_client(claim_status=200, claim_body=None, network="testnet", genesis=TESTNET,
                  status="open", calls=None):
    def handle(request):
        if calls is not None:
            calls.append((request.method, request.url.path, request.content))
        if request.url.path == "/chain":
            return httpx.Response(200, json={"network": network, "genesisHash": genesis})
        if request.url.path == "/limits":
            return httpx.Response(200, json={"faucet": {
                "status": status, "agent": {"grantFlop": 10, "claimPath": faucet.AGENT_CLAIM_PATH}}})
        if request.url.path == faucet.AGENT_CLAIM_PATH:
            body = claim_body if claim_body is not None else json.dumps(
                {"success": True, "amount": 10, "txHash": TX, "status": "finalized"}).encode()
            return httpx.Response(claim_status, content=body)
        raise AssertionError(f"unexpected {request.method} {request.url}")
    return httpx.Client(transport=httpx.MockTransport(handle))


@pytest.fixture
def testnet(tmp_path, monkeypatch):
    return make(tmp_path, monkeypatch, text=config_text("testnet"))


def posts(calls):
    return [json.loads(c[2]) for c in calls if c[1] == faucet.AGENT_CLAIM_PATH]


def test_message_layout_matches_the_published_sdk():
    # npm @flop-labs/sdk 0.12.0: ['flop-faucet-agent/v1', D10_FAUCET_GENESIS_HASH, address, nonce]
    assert agent_claim_message(TESTNET, ADDRESS, "1795697227527") == (
        f"flop-faucet-agent/v1|{TESTNET}|{ADDRESS}|1795697227527")
    for bad in ("", "01", "1.5", "-1", "1" * 20, " 1"):
        with pytest.raises(PolicyViolation):
            agent_claim_message(TESTNET, ADDRESS, bad)
    with pytest.raises(PolicyViolation):
        agent_claim_message(TESTNET.upper(), ADDRESS, "1")
    with pytest.raises(PolicyViolation):
        agent_claim_message(TESTNET, ADDRESS + "|x", "1")


def test_funded_claim_signs_raw_message_and_records_it(testnet):
    cfg, ledger, policy, _ = testnet
    calls = []
    out = faucet.claim(cfg, policy, Chain(), faucet_client(calls=calls), ADDRESS, keypair)
    assert out["status"] == "funded" and out["amount"] == "10" and out["txHash"] == TX
    assert [c[1] for c in calls] == ["/chain", "/limits", faucet.AGENT_CLAIM_PATH]
    (body,) = posts(calls)
    assert set(body) == {"address", "nonce", "signature"} and body["address"] == ADDRESS
    message = agent_claim_message(TESTNET, ADDRESS, body["nonce"])
    public = Keypair(ss58_address=ADDRESS, crypto_type=KeypairType.SR25519)
    assert public.verify(message.encode(), bytes.fromhex(body["signature"][2:]))
    row = ledger.conn.execute("SELECT * FROM flop_faucet_draws").fetchone()
    assert row["status"] == "funded" and row["amount"] == str(10 * 10**18)
    assert json.loads(row["detail"])["nonce"] == body["nonce"]
    log = [json.loads(line) for line in cfg.signatures_log.read_text().splitlines()]
    assert any(e["kind"] == "signature" and e["message"] == message for e in log)
    assert "private" not in cfg.signatures_log.read_text().lower()
    with pytest.raises(PolicyViolation, match="max_faucet_draws_per_day"):
        faucet.claim(cfg, policy, Chain(), faucet_client(), ADDRESS, keypair)


def test_closed_faucet_refuses_before_the_key_is_loaded(testnet):
    cfg, ledger, policy, _ = testnet

    def no_key():
        raise AssertionError("key loaded")
    with pytest.raises(faucet.ClaimRefused, match="not open"):
        faucet.claim(cfg, policy, Chain(), faucet_client(status="paused"), ADDRESS, no_key)
    assert ledger.conn.execute("SELECT COUNT(*) FROM flop_faucet_draws").fetchone()[0] == 0
    assert not cfg.kill_path.exists()


@pytest.mark.parametrize("kw, error", [
    ({"network": "devnet"}, faucet.FaucetError),
    ({"genesis": "0x" + "22" * 32}, GenesisMismatch),
])
def test_wrong_faucet_chain_trips_and_sends_nothing(testnet, kw, error):
    cfg, _, policy, _ = testnet
    calls = []
    with pytest.raises(error):
        faucet.claim(cfg, policy, Chain(), faucet_client(calls=calls, **kw), ADDRESS, keypair)
    assert posts(calls) == [] and cfg.kill_path.exists()


def test_unknown_outcome_is_resent_unchanged_never_resigned(testnet):
    cfg, ledger, policy, clock = testnet
    calls = []
    out = faucet.claim(cfg, policy, Chain(), faucet_client(503, b"busy", calls=calls),
                       ADDRESS, keypair)
    assert out["status"] == "unknown" and "--reconcile" in out["next"]
    assert not cfg.kill_path.exists()
    with pytest.raises(faucet.ClaimRefused, match="unknown outcome"):
        faucet.claim(cfg, policy, Chain(), faucet_client(), ADDRESS, keypair)
    clock.advance(days=2)  # even after the cap window, an open claim blocks a new signature
    with pytest.raises(faucet.ClaimRefused):
        faucet.claim(cfg, policy, Chain(), faucet_client(), ADDRESS, keypair)
    out2 = faucet.reconcile(cfg, policy, Chain(), faucet_client(calls=calls), out["draw_id"])
    assert out2["status"] == "funded"
    first, again = posts(calls)
    assert first == again
    assert ledger.get_faucet(out["draw_id"])["status"] == "funded"
    with pytest.raises(faucet.ClaimRefused):
        faucet.reconcile(cfg, policy, Chain(), faucet_client(), out["draw_id"])


def test_network_error_is_unknown(testnet):
    cfg, ledger, policy, _ = testnet

    def handle(request):
        if request.url.path == faucet.AGENT_CLAIM_PATH:
            raise httpx.ReadTimeout("slow", request=request)
        return faucet_client().send(request)
    client = httpx.Client(transport=httpx.MockTransport(handle))
    out = faucet.claim(cfg, policy, Chain(), client, ADDRESS, keypair)
    assert out["status"] == "unknown"
    assert ledger.get_faucet(out["draw_id"])["status"] == "requested"


def test_refusal_is_recorded_and_the_next_nonce_moves_up(testnet):
    cfg, _ledger, policy, _ = testnet
    body = json.dumps({"error": "Sign the D10 versioned claim with the recipient key",
                       "reason": "claim_signature_invalid", "action": "fix_request"}).encode()
    calls = []
    out = faucet.claim(cfg, policy, Chain(), faucet_client(401, body, calls=calls),
                       ADDRESS, keypair)
    assert out["status"] == "failed" and out["reason"] == "claim_signature_invalid"
    assert not cfg.kill_path.exists()
    assert policy.budget().faucet_draws_left_today == 1
    # same clock: the next nonce must still be above the one already signed
    faucet.claim(cfg, policy, Chain(), faucet_client(calls=calls), ADDRESS, keypair)
    n1, n2 = (int(b["nonce"]) for b in posts(calls))
    assert n2 == n1 + 1


def test_incomplete_success_trips_and_stays_open(testnet):
    cfg, ledger, policy, _ = testnet
    with pytest.raises(faucet.FaucetError):
        faucet.claim(cfg, policy, Chain(), faucet_client(claim_body=b'{"success": true}'),
                     ADDRESS, keypair)
    assert cfg.kill_path.exists()
    assert ledger.conn.execute("SELECT status FROM flop_faucet_draws").fetchone()[0] == "requested"


def test_signing_gate_refuses_other_keys_and_networks(testnet, tmp_path, monkeypatch):
    cfg, _ledger, policy, _ = testnet
    other = Keypair.create_from_seed("0x" + "08" * 32, ss58_format=42,
                                     crypto_type=KeypairType.SR25519)
    draw_id = policy.authorize_faucet(ADDRESS)
    with pytest.raises(PolicyViolation, match="not the recipient"):
        policy.sign_agent_claim(draw_id, other, "1")
    assert cfg.kill_path.exists()

    (tmp_path / "dev").mkdir()
    dcfg, _, dpolicy, _ = make(tmp_path / "dev", monkeypatch)
    with pytest.raises(PolicyViolation, match="only on testnet"):
        faucet.claim(dcfg, dpolicy, Chain(), faucet_client(), ADDRESS, keypair)


def test_cli_testnet_dry_run_signs_and_sends_nothing(tmp_path, monkeypatch, capsys):
    from flopstar.flop.wallet import create_spend_keystore

    _, _, _, _ = make(tmp_path, monkeypatch, text=config_text("testnet"))
    monkeypatch.setenv("FLOPSTAR_FLOP_CONFIG", str(tmp_path / "flop.toml"))
    monkeypatch.setenv("FLOPSTAR_FLOP_KEYSTORE", str(tmp_path / "keys" / "spend.json"))

    def explode(*a, **k):
        raise AssertionError("dry run touched the network or the chain")
    monkeypatch.setitem(cli.HOOKS, "http", explode)
    monkeypatch.setitem(cli.HOOKS, "chain", explode)
    cfg = cli._load()
    address = create_spend_keystore(cfg, "correct horse battery staple")
    cli.run_flop(["faucet"])
    out = capsys.readouterr().out
    assert "dry run: nothing signed" in out
    assert f"flop-faucet-agent/v1|{TESTNET}|{address}|" in out
    assert not cfg.signatures_log.exists() or "signature" not in cfg.signatures_log.read_text()
    with pytest.raises(SystemExit, match="fixed amount"):
        cli.run_flop(["faucet", "--amount", "1"])
    cli.run_flop(["faucet", "--reconcile", "1"])
    assert "nothing sent" in capsys.readouterr().out
