"""Spend keystore (key design A): encrypted, 0600, never overwritten; a throwaway key per test."""

import json
import os
import stat

import pytest

from flopstar.flop.wallet import (
    WalletError,
    check_keystore_for_cli,
    create_spend_keystore,
    identity_account,
    keystore_public,
    load_spend_keypair,
    read_passphrase,
    spend_address,
)
from tests.flop_helpers import make_full

PW = "correct horse battery staple"


def test_identity_account_is_the_dids_ed25519_key():
    assert identity_account() == "5Dhx2pBYBJe7DeWueVRgpQjKMpcva46qGVKyHRo9PwT81wzF"


def test_create_load_and_refuse_overwrite(tmp_path, monkeypatch):
    cfg, *_ = make_full(tmp_path, monkeypatch)
    assert spend_address(cfg) is None
    address = create_spend_keystore(cfg, PW)
    mode = stat.S_IMODE(cfg.keystore_path.stat().st_mode)
    assert mode == 0o600 and stat.S_IMODE(cfg.keystore_path.parent.stat().st_mode) == 0o700
    public = keystore_public(cfg)
    assert public["address"] == address and "scrypt" in public["encoding"]["type"]
    assert PW not in cfg.keystore_path.read_text()
    assert json.loads((cfg.data_dir / "flop-wallet.json").read_text())["address"] == address
    assert load_spend_keypair(cfg).ss58_address == address  # passphrase from the 0600 file
    assert check_keystore_for_cli(cfg) == cfg.keystore_path
    with pytest.raises(WalletError, match="refusing to overwrite"):
        create_spend_keystore(cfg, PW)


def test_wrong_passphrase_and_loose_modes_refused(tmp_path, monkeypatch):
    cfg, *_ = make_full(tmp_path, monkeypatch)
    create_spend_keystore(cfg, PW)
    with pytest.raises(WalletError, match="cannot decrypt"):
        load_spend_keypair(cfg, "not the passphrase")
    os.chmod(cfg.keystore_path, 0o644)
    with pytest.raises(WalletError, match="0600"):
        load_spend_keypair(cfg)
    with pytest.raises(WalletError, match="0600"):
        check_keystore_for_cli(cfg)


def test_passphrase_rules(tmp_path, monkeypatch):
    cfg, *_ = make_full(tmp_path, monkeypatch)
    assert read_passphrase(cfg) == PW
    os.chmod(cfg.passphrase_file, 0o640)
    with pytest.raises(WalletError, match="0600"):
        read_passphrase(cfg)
    os.chmod(cfg.passphrase_file, 0o600)
    cfg.passphrase_file.write_text("short\n")
    with pytest.raises(WalletError, match="12 characters"):
        read_passphrase(cfg)
