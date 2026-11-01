"""Secret files: owner-only files, or systemd credentials (0440 root:root in CREDENTIALS_DIRECTORY)."""

import os

import pytest

from flopstar.flop import secretfiles
from flopstar.flop.secretfiles import SecretFileError, read_secret_file


def write(path, mode):
    path.write_text("s3cret-value\n")
    os.chmod(path, mode)
    return path


@pytest.fixture
def creds(tmp_path, monkeypatch):
    """A stand-in credentials dir; tests run as root on the droplet, so uid 0 matches systemd's."""
    d = tmp_path / "credentials"
    d.mkdir(mode=0o550)
    os.chmod(d, 0o550)
    if os.getuid() != 0:
        pytest.skip("credentials-dir trust needs a root-owned directory")
    return d


def test_owner_only_file_is_accepted_and_loose_one_refused(tmp_path):
    assert read_secret_file(write(tmp_path / "a", 0o600), "x", env={}) == b"s3cret-value\n"
    with pytest.raises(SecretFileError, match="0600"):
        read_secret_file(write(tmp_path / "b", 0o440), "x", env={})
    with pytest.raises(SecretFileError, match="does not exist"):
        read_secret_file(tmp_path / "missing", "x", env={})


def test_systemd_credential_0440_is_accepted(creds):
    os.chmod(creds, 0o750)
    f = write(creds / "flop-spend-passphrase", 0o440)
    os.chmod(creds, 0o550)
    assert read_secret_file(f, "x", env={"CREDENTIALS_DIRECTORY": str(creds)})


def test_0440_outside_the_credentials_dir_is_refused(creds, tmp_path):
    f = write(tmp_path / "elsewhere", 0o440)
    with pytest.raises(SecretFileError, match="0600"):
        read_secret_file(f, "x", env={"CREDENTIALS_DIRECTORY": str(creds)})


def test_symlink_in_credentials_dir_pointing_out_gets_no_exemption(creds, tmp_path):
    target = write(tmp_path / "loose", 0o444)
    os.chmod(creds, 0o750)
    (creds / "link").symlink_to(target)
    os.chmod(creds, 0o550)
    with pytest.raises(SecretFileError, match="0600"):
        read_secret_file(creds / "link", "x", env={"CREDENTIALS_DIRECTORY": str(creds)})


def test_writable_credentials_dir_is_not_trusted(creds):
    os.chmod(creds, 0o770)
    assert secretfiles.credentials_dir({"CREDENTIALS_DIRECTORY": str(creds)}) is None
    assert secretfiles.credentials_dir({"CREDENTIALS_DIRECTORY": str(creds / "nope")}) is None


def test_wallet_passphrase_reads_a_systemd_credential(creds, tmp_path, monkeypatch):
    from flopstar.flop.wallet import read_passphrase
    from tests.flop_helpers import make_full
    cfg, *_ = make_full(tmp_path, monkeypatch)
    os.chmod(creds, 0o750)
    f = creds / "flop-spend-passphrase"
    f.write_text("a-long-random-passphrase")
    os.chmod(f, 0o440)
    os.chmod(creds, 0o550)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(creds))
    object.__setattr__(cfg, "passphrase_file", f)
    assert read_passphrase(cfg) == "a-long-random-passphrase"
