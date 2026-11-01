"""
Key design A (docs/FLOP.md §7): the FLOP spend account.

A fresh sr25519 key in an encrypted Polkadot JSON keystore (v3: scrypt + xsalsa20-poly1305, the
format `flop --keystore` reads), mode 0600 in a 0700 directory. Its small faucet-funded balance plus
FlopPolicy's caps bound what a bug can spend. The owner key (flopstar.pem) is never loaded here.

This module creates and loads keys; it never signs (FlopPolicy does). It never passes a secret to
flop_sdk's SubstrateClient, which would derive from the public dev phrase (FLOP.md §2.3).
"""

import getpass
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import base58
from scalecodec.utils.ss58 import ss58_encode
from substrateinterface import Keypair, KeypairType

from ..signer import expected_did
from .config import FlopConfig
from .secretfiles import SecretFileError, read_secret_file

KEYSTORE_NAME = "flopstar-spend"


class WalletError(Exception):
    """Missing, unsafe or unreadable spend keystore."""


def identity_account(did: str | None = None) -> str:
    """
    The SS58 (format 42) of our did:key's Ed25519 public key. Public information; under design A
    this account is unused on FLOP until FLOP publishes the DID-signed flow (SPEC S1).
    """
    did = did or expected_did()
    raw = base58.b58decode(did.removeprefix("did:key:z"))
    if raw[:2] != b"\xed\x01" or len(raw) != 34:
        raise WalletError(f"{did} is not an Ed25519 did:key")
    return ss58_encode(raw[2:], 42)


def read_passphrase(cfg: FlopConfig, confirm: bool = False) -> str:
    """Passphrase from FLOPSTAR_FLOP_PASSPHRASE_FILE (owner-only or a systemd credential), else a prompt."""
    if cfg.passphrase_file:
        try:
            raw = read_secret_file(cfg.passphrase_file, "spend keystore passphrase")
        except SecretFileError as e:
            raise WalletError(str(e)) from None
        value = raw.decode().rstrip("\r\n")
    else:
        value = getpass.getpass("FLOP spend keystore passphrase: ")
        if confirm and getpass.getpass("Repeat passphrase: ") != value:
            raise WalletError("passphrases differ")
    if len(value) < 12:
        raise WalletError("spend keystore passphrase must be at least 12 characters")
    return value


def _check_keystore_file(path: Path) -> None:
    st = path.stat()
    if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077:
        raise WalletError(f"{path} must be a regular file with mode 0600")
    if st.st_uid != os.getuid():
        raise WalletError(f"{path} is not owned by this user")


def keystore_public(cfg: FlopConfig) -> dict | None:
    """Public fields of the keystore (address, encoding), without decrypting. None if absent."""
    try:
        data = json.loads(cfg.keystore_path.read_text())
    except FileNotFoundError:
        return None
    except ValueError as e:
        raise WalletError(f"{cfg.keystore_path} is not JSON: {e}") from e
    if not isinstance(data, dict) or "address" not in data or "encoding" not in data:
        raise WalletError(f"{cfg.keystore_path} is not a Polkadot JSON keystore")
    if "scrypt" not in data["encoding"].get("type", []):
        raise WalletError(f"{cfg.keystore_path} is not encrypted with scrypt")
    return {"address": data["address"], "encoding": data["encoding"],
            "name": data.get("meta", {}).get("name")}


def spend_address(cfg: FlopConfig) -> str | None:
    info = keystore_public(cfg)
    return info["address"] if info else None


def plan_setup(cfg: FlopConfig) -> dict:
    """What `wallet setup --post` would do. No key is generated."""
    return {
        "design": "A: separate sr25519 spend account; the owner key does nothing on FLOP",
        "keystore": str(cfg.keystore_path),
        "exists": cfg.keystore_path.exists(),
        "format": "Polkadot JSON v3 (scrypt, xsalsa20-poly1305), mode 0600",
        "on_chain": "nothing is signed; fund it with `flopstar flop faucet --post`",
        "bounds": {
            "balance": "whatever the faucet grants (spend account holds nothing else)",
            "max_flop_per_request": cfg.caps.max_flop_per_request,
            "max_flop_per_day": cfg.caps.max_flop_per_day,
            "max_requests_per_hour": cfg.caps.max_requests_per_hour,
            "allowed_models": list(cfg.caps.allowed_models),
            "expiry": "none on chain (design A has no session key); revoke by draining the account",
        },
    }


def create_spend_keystore(cfg: FlopConfig, passphrase: str) -> str:
    """Generate the spend key and write its encrypted keystore. Refuses to overwrite. Returns SS58."""
    path = cfg.keystore_path
    if path.exists():
        raise WalletError(f"{path} already exists; refusing to overwrite the spend keystore")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.stat().st_mode & 0o077:
        raise WalletError(f"{path.parent} must be mode 0700")
    keypair = Keypair.create_from_seed(os.urandom(32).hex(), ss58_format=42,
                                       crypto_type=KeypairType.SR25519)
    data = keypair.export_to_encrypted_json(passphrase, name=KEYSTORE_NAME)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    record = {"address": keypair.ss58_address, "public_key": "0x" + keypair.public_key.hex(),
              "crypto_type": "sr25519", "keystore": str(path),
              "created_at": datetime.now(UTC).isoformat()}
    (cfg.data_dir / "flop-wallet.json").write_text(json.dumps(record, indent=2) + "\n")
    return keypair.ss58_address


def load_spend_keypair(cfg: FlopConfig, passphrase: str | None = None) -> Keypair:
    """Decrypt the spend keystore. Only FlopPolicy-gated callers (recovery --post) use this."""
    if not cfg.keystore_path.exists():
        raise WalletError(f"no spend keystore at {cfg.keystore_path}; run `flop wallet setup --post`")
    _check_keystore_file(cfg.keystore_path)
    data = json.loads(cfg.keystore_path.read_text())
    try:
        return Keypair.create_from_encrypted_json(data, passphrase or read_passphrase(cfg),
                                                  ss58_format=42)
    except Exception as e:  # noqa: BLE001 - nacl raises CryptoError; never echo the passphrase
        raise WalletError(f"cannot decrypt {cfg.keystore_path}: {type(e).__name__}") from None


def check_keystore_for_cli(cfg: FlopConfig) -> Path:
    """The keystore the pinned CLI will read: must exist, be ours and 0600."""
    if not cfg.keystore_path.exists():
        raise WalletError(f"no spend keystore at {cfg.keystore_path}; run `flop wallet setup --post`")
    _check_keystore_file(cfg.keystore_path)
    return cfg.keystore_path
