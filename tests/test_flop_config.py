"""FLOP config: explicit caps, no float amounts, pinned genesis, SS58 blocklist."""

from pathlib import Path

import pytest
from scalecodec.utils.ss58 import ss58_decode, ss58_encode

from flopstar.flop.config import (
    BUILTIN_PROFILES,
    ConfigError,
    GenesisMismatch,
    load_config,
)

EXAMPLE = Path(__file__).parents[1] / "deploy" / "flop.example.toml"
ALICE = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"  # public dev account
DEVNET_GENESIS = BUILTIN_PROFILES["devnet"].genesis_hash

CAPS = """
[{net}.caps]
max_flop_per_request = "0.01"
max_flop_per_day = "0.5"
max_requests_per_hour = 6
max_faucet_draws_per_day = 1
allowed_models = ["qwen/qwen2.5-0.5b-instruct"]
same_control_miners = [{miners}]
"""


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOPSTAR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("FLOPSTAR_FLOP_NETWORK", raising=False)


def write(tmp_path, text):
    path = tmp_path / "flop.toml"
    path.write_text(text)
    return path


def test_example_config_loads_with_starting_caps():
    cfg = load_config(EXAMPLE, env={})
    assert cfg.network.name == "devnet"
    assert cfg.caps.max_flop_per_request == 10**16
    assert cfg.caps.max_flop_per_day == 5 * 10**17
    assert cfg.network.genesis_hash == DEVNET_GENESIS
    assert cfg.kill_path.name == "KILL-flop"


def test_testnet_is_selectable_by_env_and_refuses_until_genesis_pinned():
    cfg = load_config(EXAMPLE, env={"FLOPSTAR_FLOP_NETWORK": "testnet"})
    assert cfg.network.name == "testnet"
    with pytest.raises(ConfigError, match="no pinned genesis"):
        cfg.require_pinned_genesis()
    with pytest.raises(ConfigError):
        cfg.check_genesis(DEVNET_GENESIS)


def test_testnet_genesis_pin_from_config(tmp_path):
    pin = "0x" + "ab" * 32
    path = write(tmp_path, f'[testnet]\ngenesis_hash = "{pin}"\n' + CAPS.format(net="testnet", miners=""))
    cfg = load_config(path, network="testnet", env={})
    cfg.check_genesis(pin.upper().replace("0X", "0x"))
    with pytest.raises(GenesisMismatch):
        cfg.check_genesis(DEVNET_GENESIS)


def test_missing_file_and_missing_caps_are_errors(tmp_path):
    with pytest.raises(ConfigError, match="no FLOP config"):
        load_config(tmp_path / "absent.toml", env={})
    with pytest.raises(ConfigError, match="caps.*required"):
        load_config(write(tmp_path, "[devnet]\n"), env={})
    text = CAPS.format(net="devnet", miners="").replace('max_flop_per_day = "0.5"\n', "")
    with pytest.raises(ConfigError, match="missing"):
        load_config(write(tmp_path, text), env={})


def test_float_amounts_are_refused(tmp_path):
    text = CAPS.format(net="devnet", miners="").replace('"0.01"', "0.01")
    with pytest.raises(ConfigError, match="no floats"):
        load_config(write(tmp_path, text), env={})


def test_per_request_cannot_exceed_per_day(tmp_path):
    text = CAPS.format(net="devnet", miners="").replace('"0.01"', '"1"')
    with pytest.raises(ConfigError, match="exceeds"):
        load_config(write(tmp_path, text), env={})


@pytest.mark.parametrize(
    "override",
    [
        'rpc_url = "http://rpc.example"',
        'genesis_hash = "0x1234"',
        'cli_image = "ghcr.io/flop-labs/flop-core/cli:stable"',
        'typo_url = "https://x"',
    ],
)
def test_unsafe_or_unknown_profile_overrides_are_refused(tmp_path, override):
    text = f"[devnet]\n{override}\n" + CAPS.format(net="devnet", miners="")
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, text), env={})


def test_blocklist_matches_any_ss58_prefix_and_rejects_bad_checksums(tmp_path):
    cfg = load_config(write(tmp_path, CAPS.format(net="devnet", miners=f'"{ALICE}"')), env={})
    assert cfg.caps.is_blocked_miner(ALICE)
    polkadot_alice = ss58_encode(ss58_decode(ALICE), 0)
    assert polkadot_alice != ALICE and cfg.caps.is_blocked_miner(polkadot_alice)
    assert not cfg.caps.is_blocked_miner("5FpcgGhyP73JvTsRutafYQf7ocuAxLjAt8L6aYrbUmrtZc5p")
    assert cfg.caps.is_blocked_miner("not-an-address")  # unparseable counts as blocked
    bad = ALICE[:-1] + ("Z" if ALICE[-1] != "Z" else "Y")
    with pytest.raises(ConfigError, match="invalid SS58"):
        load_config(write(tmp_path, CAPS.format(net="devnet", miners=f'"{bad}"')), env={})


def test_unknown_network_is_refused():
    with pytest.raises(ConfigError, match="network must be"):
        load_config(EXAMPLE, network="mainnet", env={})
