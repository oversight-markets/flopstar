"""Shared fixtures for FLOP tests: temp config/data, a fixed clock, no network, no real keys."""

from datetime import UTC, datetime, timedelta

from flopstar.flop.config import load_config
from flopstar.flop.ledger import Ledger
from flopstar.flop.policy import FlopPolicy

GENESIS = "0x99c172c095340ace980631f9123544c6bebd4839f3437e463533422d8be2d631"
MODEL = "qwen/qwen2.5-0.5b-instruct"
MINER = "5FpcgGhyP73JvTsRutafYQf7ocuAxLjAt8L6aYrbUmrtZc5p"
BLOCKED = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
CENT = 10**16  # 0.01 FLOP

def config_text(network: str = "devnet") -> str:
    return f"""
network = "{network}"
[{network}.caps]
max_flop_per_request = "0.01"
max_flop_per_day = "0.05"
max_requests_per_hour = 4
max_faucet_draws_per_day = 1
allowed_models = ["{MODEL}"]
same_control_miners = ["{BLOCKED}"]
"""


CONFIG = config_text()


class Clock:
    def __init__(self):
        self.t = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


def make(tmp_path, monkeypatch, clock=None, text=CONFIG):
    monkeypatch.setenv("FLOPSTAR_DATA_DIR", str(tmp_path / "data"))
    path = tmp_path / "flop.toml"
    path.write_text(text)
    cfg = load_config(path, env={})
    clock = clock or Clock()
    ledger = Ledger(cfg.db_path, now=clock)
    return cfg, ledger, FlopPolicy(cfg, ledger, now=clock), clock


def sha(n: int) -> str:
    return f"{n:064x}"


SERVING = "5ED68Tgj9G6xtPnWKxzBkr7cij6nPR3kVXUYQ2RT27igsrH2"  # miner in the synthetic fixtures
FIXTURES = __import__("pathlib").Path(__file__).parent / "fixtures" / "flop"


def config_with_miners(network: str = "devnet") -> str:
    return config_text(network) + f"""
[{network}.miners]
"{SERVING}" = "https://serving.example/session"
"{MINER}" = "https://other.example/session"
"{BLOCKED}" = "https://ours.example/session"
"""


def make_full(tmp_path, monkeypatch, clock=None):
    """Config with miners, a keystore path and passphrase file under tmp_path."""
    import os
    pw = tmp_path / "pw"
    pw.write_text("correct horse battery staple\n")
    os.chmod(pw, 0o600)
    monkeypatch.setenv("FLOPSTAR_FLOP_PASSPHRASE_FILE", str(pw))
    monkeypatch.setenv("FLOPSTAR_FLOP_KEYSTORE", str(tmp_path / "keys" / "spend.json"))
    monkeypatch.setenv("FLOPSTAR_DATA_DIR", str(tmp_path / "data"))
    from flopstar.flop.config import load_config
    from flopstar.flop.ledger import Ledger
    from flopstar.flop.policy import FlopPolicy
    path = tmp_path / "flop.toml"
    path.write_text(config_with_miners())
    cfg = load_config(path)
    clock = clock or Clock()
    ledger = Ledger(cfg.db_path, now=clock)
    return cfg, ledger, FlopPolicy(cfg, ledger, now=clock), clock
