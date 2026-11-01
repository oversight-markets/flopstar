"""Inference routes: the direct CLI (via synthetic fixtures, never run), gateway, native."""

import json
import subprocess

import httpx
import pytest

from flopstar.flop import inference
from flopstar.flop.inference import (
    CliOutputError,
    RouteUnavailable,
    choose_miner,
    parse_cli_failure,
    parse_cli_success,
)
from flopstar.flop.wallet import create_spend_keystore
from tests.flop_helpers import BLOCKED, CENT, FIXTURES, MINER, MODEL, SERVING, make_full

SUCCESS = (FIXTURES / "session-run-success.SYNTHETIC.json").read_text()
UNVERIFIED = (FIXTURES / "session-run-unverified.SYNTHETIC.json").read_text()
DSE = (FIXTURES / "session-run-direct-session-error.SYNTHETIC.stderr").read_text()
RECOVERY_FAILED = (FIXTURES / "session-run-recovery-failed.SYNTHETIC.stderr").read_text()
PW = "correct horse battery staple"


def docker(name):
    return "/usr/bin/docker" if name == "docker" else None


class FakeCli:
    def __init__(self, code=0, stdout="", stderr="", timeout=False):
        self.code, self.stdout, self.stderr, self.timeout, self.calls = code, stdout, stderr, timeout, []

    def __call__(self, argv, env, **kw):
        self.calls.append((argv, env, kw))
        if self.timeout:
            raise subprocess.TimeoutExpired(argv, kw["timeout"])
        return subprocess.CompletedProcess(argv, self.code, self.stdout, self.stderr)


def direct(cfg, policy, cli, miner=SERVING, prompt="Summarise the day"):
    create_spend_keystore(cfg, PW)
    return inference.execute(cfg, policy, route="direct", model=MODEL, prompt=prompt,
                             purpose="contest-digest", max_flop=CENT, miner=miner,
                             runner=cli, which=docker)


# ---- parsing (synthetic fixtures) ---------------------------------------------------------------

def test_parse_success_fixture():
    data = parse_cli_success(SUCCESS)
    assert data["verified_turns"] == 1 and data["miner"] == SERVING


@pytest.mark.parametrize("mutate", [
    lambda d: d.pop("channel"),
    lambda d: d.update(channel="0x1234"),
    lambda d: d.update(verified_turns="1"),
    lambda d: d.update(surprise=True),
    lambda d: d.update(wallet="/root/.flop/session-wallets/x.json"),
])
def test_parse_success_is_strict(mutate):
    data = json.loads(SUCCESS)
    mutate(data)
    with pytest.raises(CliOutputError):
        parse_cli_success(json.dumps(data))
    with pytest.raises(CliOutputError):
        parse_cli_success("Banner\n" + SUCCESS)


def test_parse_failure_fixtures():
    f = parse_cli_failure(DSE)
    assert f["channel_id"] == "0xf45ef2bb28803a41ec18a93782a34daabafffab9f2a8bc6cfbe8129dda5b6afc"
    assert f["code"] == "FLOP_ERROR" and not f["recovery_failed"]
    assert parse_cli_failure(RECOVERY_FAILED)["recovery_failed"]
    assert parse_cli_failure("Error: boom")["channel_id"] is None


# ---- miner selection and CLI invocation ---------------------------------------------------------

def test_choose_miner_never_picks_blocklisted_and_rotates(tmp_path, monkeypatch):
    cfg, ledger, policy, clock = make_full(tmp_path, monkeypatch)
    with pytest.raises(RouteUnavailable, match="blocklist"):
        choose_miner(cfg, ledger, BLOCKED)
    first, _ = choose_miner(cfg, ledger)
    assert first != BLOCKED
    policy.authorize_inference(route="direct", model=MODEL, miner=first, max_flop=1,
                               purpose="t", request_sha256="0" * 64)
    clock.advance(minutes=1)
    second, _ = choose_miner(cfg, ledger)
    assert second not in (first, BLOCKED)


def test_only_blocklisted_miner_means_no_miner(tmp_path, monkeypatch):
    cfg, ledger, *_ = make_full(tmp_path, monkeypatch)
    object.__setattr__(cfg, "miners", {BLOCKED: "https://ours.example"})
    with pytest.raises(RouteUnavailable):
        choose_miner(cfg, ledger)


def test_cli_prefix_needs_a_runtime(tmp_path, monkeypatch):
    cfg, *_ = make_full(tmp_path, monkeypatch)
    with pytest.raises(RouteUnavailable, match="Docker"):
        inference.cli_prefix(cfg, which=lambda _: None)
    prefix, keystore, version = inference.cli_prefix(cfg, which=docker)
    assert prefix[-1].endswith("@sha256:c8f3456c48d41cec9d0229d1e1dd9eb8d6247a22f350c51078d85f129601bbbd")
    assert keystore == "/keys/spend.json" and version == "v0.1.220"
    assert f"{cfg.keystore_path.parent}:/keys:ro" in prefix
    # Memory, swap and process caps come before the image name.
    limits = prefix[:prefix.index(prefix[-1])]
    for flag, value in (("--memory", "640m"), ("--memory-swap", "1g"), ("--pids-limit", "256")):
        assert limits[limits.index(flag) + 1] == value


# ---- direct route end to end with a fake CLI ----------------------------------------------------

def test_direct_success_records_verified_spend(tmp_path, monkeypatch):
    cfg, ledger, policy, _ = make_full(tmp_path, monkeypatch)
    cli = FakeCli(stdout=SUCCESS)
    r = direct(cfg, policy, cli)
    assert r.status == "settled" and r.verification == "sdk_verified"
    row = ledger.get(r.spend_id)
    assert row["status"] == "settled" and row["channel_id"] == json.loads(SUCCESS)["channel"]
    assert row["settled_flop"] is None  # SPEC S6: CLI does not report it; max_flop stays counted
    argv, env, _kw = cli.calls[0]
    assert env["FLOP_KEY_PASSWORD"] == PW and PW not in " ".join(argv)
    assert set(env) <= {"PATH", "HOME", "FLOP_KEY_PASSWORD", "FLOP_INDEXER_URL"}
    assert argv[argv.index("--max-flop") + 1] == "0.01" and "--json" in argv
    log = cfg.signatures_log.read_text()
    assert "Summarise the day" not in log and "<17 chars>" in log and PW not in log
    assert [e["status"] for e in ledger.events(r.spend_id)] == ["prepared", "sent", "settled"]


@pytest.mark.parametrize("stdout, reason", [
    (UNVERIFIED, "no verified turn"),
    (SUCCESS.replace(SERVING, MINER), "served by"),
    ("not json", "non-JSON"),
])
def test_direct_verification_failures_trip(tmp_path, monkeypatch, stdout, reason):
    cfg, ledger, policy, _ = make_full(tmp_path, monkeypatch)
    r = direct(cfg, policy, FakeCli(stdout=stdout))
    assert r.status == "failed" and reason in r.error and r.verification == "failed"
    assert ledger.get(r.spend_id)["verification"] == "failed"
    assert reason in cfg.kill_path.read_text()


def test_direct_session_error_hands_channel_to_recovery(tmp_path, monkeypatch):
    cfg, ledger, policy, _ = make_full(tmp_path, monkeypatch)
    r = direct(cfg, policy, FakeCli(code=1, stderr=DSE))
    assert r.status == "failed" and r.needs_recovery and r.channel_id.startswith("0xf45e")
    assert ledger.get(r.spend_id)["channel_id"] == r.channel_id
    assert not cfg.kill_path.exists()  # the SDK refunded; recovery double-checks on chain


def test_failed_on_chain_recovery_trips(tmp_path, monkeypatch):
    cfg, _, policy, _ = make_full(tmp_path, monkeypatch)
    r = direct(cfg, policy, FakeCli(code=1, stderr=RECOVERY_FAILED))
    assert r.needs_recovery and "recovery failed" in cfg.kill_path.read_text()


def test_timeout_marks_failed_for_recovery_scan(tmp_path, monkeypatch):
    cfg, ledger, policy, clock = make_full(tmp_path, monkeypatch)
    r = direct(cfg, policy, FakeCli(timeout=True))
    assert r.needs_recovery and r.channel_id is None
    assert ledger.unchanneled_failures("devnet", clock() - __import__("datetime").timedelta(days=1))


def test_kill_switch_stops_direct_before_the_cli(tmp_path, monkeypatch):
    cfg, _, policy, _ = make_full(tmp_path, monkeypatch)
    policy.trip("test")
    cli = FakeCli(stdout=SUCCESS)
    with pytest.raises(Exception, match="KILL-flop"):
        direct(cfg, policy, cli)
    assert cli.calls == []


# ---- gateway and native -------------------------------------------------------------------------

def gateway_setup(tmp_path, monkeypatch):
    import os
    key = tmp_path / "gw"
    key.write_text("gw-key-SECRET\n")
    os.chmod(key, 0o600)
    monkeypatch.setenv("FLOPSTAR_FLOP_GATEWAY_KEY_FILE", str(key))
    return make_full(tmp_path, monkeypatch)


def gateway(cfg, policy, handler):
    return inference.execute(cfg, policy, route="gateway", model=MODEL, prompt="hi",
                             purpose="research-notes", max_flop=CENT,
                             transport=httpx.MockTransport(handler))


def test_gateway_success_is_unverified(tmp_path, monkeypatch):
    cfg, ledger, policy, _ = gateway_setup(tmp_path, monkeypatch)
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"id": "c1", "model": MODEL, "usage": {"total_tokens": 5},
                                         "choices": [{"message": {"content": "hello"}}]})
    r = gateway(cfg, policy, handler)
    assert r.status == "settled" and r.verification == "unverified" and r.reply == "hello"
    assert seen[0].headers["authorization"] == "Bearer gw-key-SECRET"
    assert seen[0].url.path == "/v1/chat/completions"
    assert ledger.get(r.spend_id)["verification"] == "unverified"


def test_gateway_402_fails_without_paying_or_tripping(tmp_path, monkeypatch):
    cfg, _ledger, policy, _ = gateway_setup(tmp_path, monkeypatch)
    r = gateway(cfg, policy, lambda req: httpx.Response(402, json={"destination": BLOCKED}))
    assert r.status == "failed" and "402" in r.error and not cfg.kill_path.exists()


def test_gateway_malformed_trips(tmp_path, monkeypatch):
    cfg, _, policy, _ = gateway_setup(tmp_path, monkeypatch)
    r = gateway(cfg, policy, lambda req: httpx.Response(200, json={"choices": []}))
    assert r.status == "failed" and cfg.kill_path.exists()


def test_native_is_unavailable(tmp_path, monkeypatch):
    cfg, _, policy, _ = make_full(tmp_path, monkeypatch)
    with pytest.raises(RouteUnavailable, match="S1"):
        inference.execute(cfg, policy, route="native", model=MODEL, prompt="x", purpose="t",
                          max_flop=1)


# ---- Touchstone hand-off ------------------------------------------------------------------------

def test_settled_direct_result_reports_to_touchstone(tmp_path, monkeypatch):
    from flopstar.flop import touchstone as ts
    cfg, ledger, policy, _ = make_full(tmp_path, monkeypatch)
    r = direct(cfg, policy, FakeCli(stdout=SUCCESS))
    got = []
    settings = ts.TouchstoneSettings("http://10.0.0.5:8080", "tok", bytes(32))
    status = inference.report(cfg, ledger, r, agent_did="did:key:z6Mktest", cli_version="v0.1.220",
                              settings=settings, transport=httpx.MockTransport(
                                  lambda req: got.append(json.loads(req.content)) or httpx.Response(201)))
    assert status == "sent" and ledger.get(r.spend_id)["touchstone_reported_at"]
    assert got[0]["channel_id"] == r.channel_id and got[0]["sdk_verification"] == "sdk_verified"
    assert "Summarise" not in json.dumps(got[0])  # request content only as ciphertext
