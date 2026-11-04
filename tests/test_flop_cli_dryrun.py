"""Every command that signs, spends or sends does nothing without --post."""

import pytest

from flopstar.flop import cli
from flopstar.flop.wallet import create_spend_keystore
from tests.flop_helpers import BLOCKED, MODEL, SERVING, catalog_body, catalog_transport, make_full


def explode(*a, **k):
    raise AssertionError("dry run touched the network, the chain or the signing CLI")


def roomy_caps(cfg, per_request="1", per_day="5"):
    """Rewrite the test config with devnet-sized ceilings (the estimate floor is 0.05 FLOP)."""
    text = cfg.config_path.read_text()
    text = text.replace('max_flop_per_request = "0.01"', f'max_flop_per_request = "{per_request}"')
    text = text.replace('max_flop_per_day = "0.05"', f'max_flop_per_day = "{per_day}"')
    cfg.config_path.write_text(text)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    cfg, _ledger, _policy, _ = make_full(tmp_path, monkeypatch)
    monkeypatch.setenv("FLOPSTAR_FLOP_CONFIG", str(cfg.config_path))
    monkeypatch.setitem(cli.HOOKS, "chain", explode)
    monkeypatch.setitem(cli.HOOKS, "http", explode)
    monkeypatch.setitem(cli.HOOKS, "runner", explode)
    # The only network a dry run may touch: GET of the public model catalog (no key).
    monkeypatch.setitem(cli.HOOKS, "transport", catalog_transport(also=explode))
    monkeypatch.setitem(cli.HOOKS, "which", lambda n: "/usr/bin/docker" if n == "docker" else None)
    monkeypatch.setitem(cli.HOOKS, "passphrase", "correct horse battery staple")
    return cfg


@pytest.mark.parametrize("args", [
    ["faucet"],
    ["faucet", "--amount", "1"],
    ["ask", "--model", MODEL, "--prompt", "hi", "--purpose", "research-notes"],
    ["ask", "--model", MODEL, "--prompt", "hi", "--purpose", "t", "--route", "gateway"],
    ["run"],
])
def test_dry_runs_touch_nothing(isolated, capsys, args):
    create_spend_keystore(isolated, "correct horse battery staple")
    (isolated.data_dir / "research-queue.txt").write_text("What is TOPLOC?\n")
    cli.run_flop(args)
    out = capsys.readouterr().out
    assert "dry run" in out
    assert not isolated.kill_path.exists()
    research = isolated.data_dir / "research"
    assert not research.exists() or list(research.glob("*.md")) == []


def test_wallet_setup_dry_run_creates_no_key(isolated, capsys):
    cli.run_flop(["wallet", "setup"])
    assert "no key generated" in capsys.readouterr().out
    assert not isolated.keystore_path.exists()
    cli.run_flop(["wallet", "setup", "--post"])
    assert isolated.keystore_path.exists()
    with pytest.raises(SystemExit, match="already exists"):
        cli.run_flop(["wallet", "setup", "--post"])


def test_wallet_fund_is_refused_under_design_a(isolated):
    with pytest.raises(SystemExit, match="design A"):
        cli.run_flop(["wallet", "fund", "--amount", "0.1", "--post"])
    with pytest.raises(SystemExit, match="refused"):
        cli.run_flop(["wallet", "fund", "--amount", "100"])


def test_ask_refuses_blocklisted_miner_and_secrets(isolated):
    create_spend_keystore(isolated, "correct horse battery staple")
    with pytest.raises(SystemExit, match="blocklist"):
        cli.run_flop(["ask", "--model", MODEL, "--prompt", "hi", "--purpose", "t",
                      "--miner", BLOCKED, "--post"])
    with pytest.raises(SystemExit, match="PEM"):
        cli.run_flop(["ask", "--model", MODEL, "--purpose", "t", "--prompt",
                      "-----BEGIN PRIVATE" + " KEY-----", "--post"])
    assert not isolated.kill_path.exists()  # pre-checks refuse without tripping


def test_ask_post_runs_through_the_fake_cli(isolated, monkeypatch, capsys):
    import subprocess

    from tests.flop_helpers import FIXTURES
    create_spend_keystore(isolated, "correct horse battery staple")
    stdout = (FIXTURES / "session-run-success.SYNTHETIC.json").read_text()
    roomy_caps(isolated)
    seen = []
    monkeypatch.setitem(cli.HOOKS, "runner", lambda argv, env, **kw: (
        seen.append(argv), subprocess.CompletedProcess(argv, 0, stdout, ""))[1])
    cli.run_flop(["ask", "--model", MODEL, "--prompt", "hi", "--purpose", "research-notes",
                  "--miner", SERVING, "--max-tokens", "64", "--post"])
    out = capsys.readouterr().out
    assert "status: settled" in out and "untrusted model output" in out
    assert "touchstone: skipped" in out  # not configured in tests
    # (2 prompt bytes + 40 template + 64 output) x 0.006 = 0.636 FLOP: the estimate, not the cap
    argv = seen[0]
    assert argv[argv.index("--max-flop") + 1] == "0.636"


def test_ask_defaults_to_the_estimate_not_the_ceiling(isolated, capsys):
    create_spend_keystore(isolated, "correct horse battery staple")
    roomy_caps(isolated)
    cli.run_flop(["ask", "--model", MODEL, "--prompt", "hi", "--purpose", "t",
                  "--miner", SERVING, "--max-tokens", "64"])
    out = capsys.readouterr().out
    assert "max_flop: 0.636" in out and "max_flop_source: estimate" in out
    assert "problems: []" in out


def test_ask_refuses_when_estimate_exceeds_the_ceiling(isolated):
    create_spend_keystore(isolated, "correct horse battery staple")
    roomy_caps(isolated)
    # 512 default output tokens x 0.006 = over 3 FLOP: refused, never clipped to the ceiling.
    with pytest.raises(SystemExit, match="exceeds max_flop_per_request"):
        cli.run_flop(["ask", "--model", MODEL, "--prompt", "hi", "--purpose", "t",
                      "--miner", SERVING, "--post"])


@pytest.mark.parametrize("body", [b"not json", catalog_body(price="-1"), catalog_body(price="0"),
                                  catalog_body(miners=[]),
                                  catalog_body(expires_at="2000-01-01T00:00:00Z"),
                                  catalog_body(model="other/model")])
def test_ask_without_estimate_refuses_instead_of_escrowing_the_ceiling(isolated, monkeypatch,
                                                                       body):
    create_spend_keystore(isolated, "correct horse battery staple")
    roomy_caps(isolated)
    monkeypatch.setitem(cli.HOOKS, "transport", catalog_transport(body, also=explode))
    with pytest.raises(SystemExit, match="no escrow estimate"):
        cli.run_flop(["ask", "--model", MODEL, "--prompt", "hi", "--purpose", "t",
                      "--miner", SERVING, "--max-tokens", "64", "--post"])


def test_explicit_max_flop_still_bounded_by_ceiling(isolated, monkeypatch, capsys):
    import subprocess

    from tests.flop_helpers import FIXTURES
    create_spend_keystore(isolated, "correct horse battery staple")
    roomy_caps(isolated)
    with pytest.raises(SystemExit, match="exceeds max_flop_per_request"):
        cli.run_flop(["ask", "--model", MODEL, "--prompt", "hi", "--purpose", "t",
                      "--miner", SERVING, "--max-tokens", "64", "--max-flop", "2", "--post"])
    stdout = (FIXTURES / "session-run-success.SYNTHETIC.json").read_text()
    seen = []
    monkeypatch.setitem(cli.HOOKS, "runner", lambda argv, env, **kw: (
        seen.append(argv), subprocess.CompletedProcess(argv, 0, stdout, ""))[1])
    cli.run_flop(["ask", "--model", MODEL, "--prompt", "hi", "--purpose", "t",
                  "--miner", SERVING, "--max-tokens", "64", "--max-flop", "0.7", "--post"])
    assert seen[0][seen[0].index("--max-flop") + 1] == "0.7"


def test_plan_and_execute_use_the_miner_that_was_priced(isolated, monkeypatch, capsys):
    # Even if choose_miner gave a different answer on a second unrequested call, the plan (and
    # so execute, via view["miner"]) must use the miner the estimate priced.
    from flopstar.flop import inference
    from tests.flop_helpers import MINER
    create_spend_keystore(isolated, "correct horse battery staple")
    roomy_caps(isolated)
    real, unrequested = inference.choose_miner, iter([SERVING, MINER, MINER])

    def flaky(cfg, ledger, requested=None):
        return real(cfg, ledger, requested or next(unrequested))
    monkeypatch.setattr(inference, "choose_miner", flaky)
    # The catalog prices only SERVING, so pricing one miner and planning another would show up.
    monkeypatch.setitem(cli.HOOKS, "transport",
                        catalog_transport(catalog_body(miners=[SERVING]), also=explode))
    cli.run_flop(["ask", "--model", MODEL, "--prompt", "hi", "--purpose", "t",
                  "--max-tokens", "64"])
    out = capsys.readouterr().out
    assert f"miner: {SERVING}" in out and MINER not in out
    assert "problems: []" in out


def test_no_estimate_still_reports_the_spent_day_budget(isolated, monkeypatch):
    from flopstar.flop.ledger import Ledger
    create_spend_keystore(isolated, "correct horse battery staple")
    ledger = Ledger(isolated.db_path)
    for i in range(5):  # 5 x 0.01 = the test config's 0.05 day cap
        ledger.prepare(network="devnet", route="direct", model=MODEL, miner=SERVING,
                       max_flop=10**16, purpose="t", request_sha256=f"{i:064x}")
    ledger.close()
    monkeypatch.setitem(cli.HOOKS, "transport", catalog_transport(b"not json", also=explode))
    with pytest.raises(SystemExit) as e:
        cli.run_flop(["ask", "--model", MODEL, "--prompt", "hi", "--purpose", "t",
                      "--miner", SERVING, "--max-tokens", "64", "--post"])
    assert "no escrow estimate" in str(e.value) and "max_flop_per_day" in str(e.value)


@pytest.mark.parametrize("amount", ["0", "0.0", "-1"])
def test_explicit_max_flop_must_be_positive(isolated, amount):
    create_spend_keystore(isolated, "correct horse battery staple")
    with pytest.raises(SystemExit, match="bad amount"):
        cli.run_flop(["ask", "--model", MODEL, "--prompt", "hi", "--purpose", "t",
                      "--miner", SERVING, "--max-flop", amount, "--post"])
    assert not isolated.kill_path.exists()
