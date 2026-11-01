"""Every command that signs, spends or sends does nothing without --post."""

import httpx
import pytest

from flopstar.flop import cli
from flopstar.flop.wallet import create_spend_keystore
from tests.flop_helpers import BLOCKED, MODEL, make_full


def explode(*a, **k):
    raise AssertionError("dry run touched the network, the chain or the signing CLI")


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    cfg, _ledger, _policy, _ = make_full(tmp_path, monkeypatch)
    monkeypatch.setenv("FLOPSTAR_FLOP_CONFIG", str(cfg.config_path))
    monkeypatch.setitem(cli.HOOKS, "chain", explode)
    monkeypatch.setitem(cli.HOOKS, "http", explode)
    monkeypatch.setitem(cli.HOOKS, "runner", explode)
    monkeypatch.setitem(cli.HOOKS, "transport", httpx.MockTransport(explode))
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
    monkeypatch.setitem(cli.HOOKS, "runner",
                        lambda argv, env, **kw: subprocess.CompletedProcess(argv, 0, stdout, ""))
    cli.run_flop(["ask", "--model", MODEL, "--prompt", "hi", "--purpose", "research-notes",
                  "--miner", "5ED68Tgj9G6xtPnWKxzBkr7cij6nPR3kVXUYQ2RT27igsrH2", "--post"])
    out = capsys.readouterr().out
    assert "status: settled" in out and "untrusted model output" in out
    assert "touchstone: skipped" in out  # not configured in tests
