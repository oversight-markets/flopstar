"""
FLOP network profiles, spend caps, blocklist and paths. Read-only: nothing here signs or calls out.

Network: FLOPSTAR_FLOP_NETWORK (devnet | testnet), else `network` in the config file, else devnet.
Other env: FLOPSTAR_FLOP_KEYSTORE (spend keystore), FLOPSTAR_FLOP_PASSPHRASE_FILE (its passphrase,
else prompt), FLOPSTAR_FLOP_GATEWAY_KEY_FILE (gateway API key, 0600), FLOPSTAR_FLOP_CLI (how to run
the pinned CLI: "docker", "npx", or a path to a `flop` executable; default: docker if present).
Config file: FLOPSTAR_FLOP_CONFIG, default ~/.config/flopstar/flop.toml (example:
deploy/flop.example.toml). Every cap must be set explicitly per network; amounts are decimal
strings, never TOML floats. Sources for the built-in profiles are in docs/FLOP.md §3.1 and §4.
"""

import os
import re
import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from urllib.parse import urlparse

from scalecodec.utils.ss58 import ss58_decode

from .. import config as base_config
from .amounts import AmountError, parse_flop

NETWORKS = ("devnet", "testnet")
DEFAULT_CONFIG_PATH = "~/.config/flopstar/flop.toml"
DEFAULT_KEYSTORE_PATH = "~/.config/flopstar/flop/spend.json"


class ConfigError(Exception):
    """Missing, malformed or unsafe FLOP configuration."""


class GenesisMismatch(ConfigError):
    """The chain reports a genesis hash other than the pinned one."""


@dataclass(frozen=True)
class NetworkProfile:
    name: str
    rpc_url: str | None  # FLOP's canonical RPC (wss); what the CLI is given
    rpc_http_url: str | None  # JSON-RPC over HTTPS for our own reads
    faucet_url: str | None
    gateway_url: str | None
    indexer_url: str | None  # session-offer indexer (explorer)
    mcp_url: str | None
    genesis_hash: str | None  # None: not pinned, so nothing may run against this network
    cli_npm_version: str | None
    cli_image: str | None  # image@sha256 digest from flop.network/releases.json
    # Escrow floor in base units (computeChannel.minForceOpenEscrowForFailedAck); config key
    # `min_escrow` as a decimal FLOP string. None: unknown, so no escrow estimate (fail closed).
    min_escrow: int | None = None


# sdk:chain.js FLOP_NETWORK_PROFILES; devnet genesis and HTTPS RPC observed 2026-11-01 (FLOP.md §4);
# CLI image pin from https://flop.network/releases.json on 2026-11-01.
BUILTIN_PROFILES = {
    "devnet": NetworkProfile(
        name="devnet",
        rpc_url="wss://rpc.devnet.flop.network",
        rpc_http_url="https://rpc.devnet.flop.network",
        faucet_url="https://faucet.flop.network",
        gateway_url="https://api.flop.network/v1",
        indexer_url="https://explorer.devnet.flop.network",
        mcp_url="https://mcp.flop.network/mcp",
        genesis_hash="0x99c172c095340ace980631f9123544c6bebd4839f3437e463533422d8be2d631",
        cli_npm_version="0.4.0",
        cli_image=(
            "ghcr.io/flop-labs/flop-core/cli:v0.1.220"
            "@sha256:c8f3456c48d41cec9d0229d1e1dd9eb8d6247a22f350c51078d85f129601bbbd"
        ),
        # Read from the devnet runtime (spec_version 1014003) on 2026-11-04.
        min_escrow=50_000_000_000_000_000,  # 0.05 FLOP
    ),
    # Status "planned", genesis policy "pin-before-live"; none of these hosts resolve yet.
    "testnet": NetworkProfile(
        name="testnet",
        rpc_url="wss://testnet.flop.network",
        # SPEC(S2): FLOP publishes only the wss URL; HTTPS JSON-RPC on the same host is unconfirmed.
        rpc_http_url=None,
        faucet_url="https://faucet.testnet.flop.network",
        gateway_url="https://gateway.testnet.flop.network/v1",
        # SPEC(S3): no testnet offer indexer is named anywhere.
        indexer_url=None,
        mcp_url=None,
        # SPEC(S2): pin before use; refuse to run until set in the config file.
        genesis_hash=None,
        cli_npm_version="0.4.0",
        cli_image=None,  # SPEC(S5): which CLI build FLOP supports on testnet
        min_escrow=None,  # read it from the testnet runtime once live
    ),
}

_PROFILE_KEYS = {f.name for f in fields(NetworkProfile)} - {"name"}
_URL_KEYS = {"rpc_url", "rpc_http_url", "faucet_url", "gateway_url", "indexer_url", "mcp_url"}
_GENESIS = re.compile(r"^0x[0-9a-f]{64}$")
_IMAGE = re.compile(r"^[a-z0-9./_-]+(:[A-Za-z0-9._-]+)?@sha256:[0-9a-f]{64}$")
_CAP_KEYS = {
    "max_flop_per_request",
    "max_flop_per_day",
    "max_requests_per_hour",
    "max_faucet_draws_per_day",
    "allowed_models",
    "same_control_miners",
}


@dataclass(frozen=True)
class Caps:
    max_flop_per_request: int  # base units
    max_flop_per_day: int  # base units
    max_requests_per_hour: int
    max_faucet_draws_per_day: int
    allowed_models: tuple[str, ...]
    # Blocklisted miners as SS58 strings, and their public keys (so any SS58 prefix matches).
    same_control_miners: tuple[str, ...]
    same_control_pubkeys: frozenset[str]

    def is_blocked_miner(self, ss58: str) -> bool:
        """True if `ss58` is on the same-control blocklist. Unparseable addresses count as blocked."""
        try:
            return ss58_pubkey(ss58) in self.same_control_pubkeys
        except ConfigError:
            return True


@dataclass(frozen=True)
class FlopConfig:
    network: NetworkProfile
    caps: Caps
    config_path: Path
    keystore_path: Path
    data_dir: Path
    miners: dict[str, str] = field(default_factory=dict)
    passphrase_file: Path | None = None
    gateway_key_file: Path | None = None
    cli_runner: str | None = None

    def subdir(self, name: str) -> Path:
        path = self.data_dir / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def kill_path(self) -> Path:
        return self.data_dir / "KILL-flop"

    @property
    def signatures_log(self) -> Path:
        return self.data_dir / "flop-signatures.log"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "flopstar.db"

    @property
    def touchstone_buffer(self) -> Path:
        return self.data_dir / "touchstone-buffer.jsonl"

    def require_pinned_genesis(self) -> str:
        """The pinned genesis hash, or ConfigError: never act on an unpinned network."""
        if not self.network.genesis_hash:
            raise ConfigError(
                f"{self.network.name} has no pinned genesis hash; set "
                f"[{self.network.name}].genesis_hash in {self.config_path} before using it"
            )
        return self.network.genesis_hash

    def check_genesis(self, reported: str) -> None:
        """Raise GenesisMismatch unless the chain's genesis equals the pinned one."""
        pinned = self.require_pinned_genesis()
        if not isinstance(reported, str) or reported.lower() != pinned:
            raise GenesisMismatch(
                f"{self.network.name} chain reports genesis {reported!r}, pinned {pinned}"
            )


def ss58_pubkey(address: str) -> str:
    """SS58 address -> lowercase hex public key, validating its checksum."""
    try:
        return ss58_decode(address).lower()
    except Exception as e:  # scalecodec raises ValueError and friends on bad input
        raise ConfigError(f"invalid SS58 address {address!r}: {e}") from e


def _profile(name: str, overrides: dict) -> NetworkProfile:
    unknown = set(overrides) - _PROFILE_KEYS - {"caps", "miners"}
    if unknown:
        raise ConfigError(f"[{name}] has unknown keys: {sorted(unknown)}")
    values = {k: v for k, v in overrides.items() if k not in ("caps", "miners")}
    if "min_escrow" in values:
        values["min_escrow"] = _flop(f"[{name}]", "min_escrow", values["min_escrow"])
    for key, value in values.items():
        if key == "min_escrow":
            continue
        if not isinstance(value, str):
            raise ConfigError(f"[{name}].{key} must be a string")
        if key in _URL_KEYS and urlparse(value).scheme not in ("https", "wss"):
            raise ConfigError(f"[{name}].{key} must be an https:// or wss:// URL: {value!r}")
        if key == "genesis_hash" and not _GENESIS.match(value):
            raise ConfigError(f"[{name}].genesis_hash must be 0x + 64 lowercase hex")
        if key == "cli_image" and not _IMAGE.match(value):
            raise ConfigError(f"[{name}].cli_image must be pinned by @sha256 digest")
    return replace(BUILTIN_PROFILES[name], **values)


def _positive_int(table: str, key: str, value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{table}.{key} must be a positive integer")
    return value


def _flop(table: str, key: str, value) -> int:
    if not isinstance(value, str):
        raise ConfigError(f'{table}.{key} must be a decimal string like "0.01" (no floats)')
    try:
        raw = parse_flop(value)
    except AmountError as e:
        raise ConfigError(f"{table}.{key}: {e}") from e
    if raw <= 0:
        raise ConfigError(f"{table}.{key} must be positive")
    return raw


def _caps(name: str, table: dict | None) -> Caps:
    where = f"[{name}.caps]"
    if not isinstance(table, dict):
        raise ConfigError(f"{where} is required: every cap must be set explicitly")
    missing = _CAP_KEYS - set(table)
    unknown = set(table) - _CAP_KEYS
    if missing:
        raise ConfigError(f"{where} is missing {sorted(missing)}")
    if unknown:
        raise ConfigError(f"{where} has unknown keys: {sorted(unknown)}")
    per_request = _flop(where, "max_flop_per_request", table["max_flop_per_request"])
    per_day = _flop(where, "max_flop_per_day", table["max_flop_per_day"])
    if per_request > per_day:
        raise ConfigError(f"{where} max_flop_per_request exceeds max_flop_per_day")
    models = table["allowed_models"]
    if not isinstance(models, list) or not all(isinstance(m, str) and m for m in models):
        raise ConfigError(f"{where}.allowed_models must be a list of model slugs")
    miners = table["same_control_miners"]
    if not isinstance(miners, list) or not all(isinstance(m, str) for m in miners):
        raise ConfigError(f"{where}.same_control_miners must be a list of SS58 addresses")
    return Caps(
        max_flop_per_request=per_request,
        max_flop_per_day=per_day,
        max_requests_per_hour=_positive_int(where, "max_requests_per_hour",
                                            table["max_requests_per_hour"]),
        max_faucet_draws_per_day=_positive_int(where, "max_faucet_draws_per_day",
                                               table["max_faucet_draws_per_day"]),
        allowed_models=tuple(dict.fromkeys(models)),
        same_control_miners=tuple(miners),
        same_control_pubkeys=frozenset(ss58_pubkey(m) for m in miners),
    )


def _miners(name: str, table) -> dict[str, str]:
    """
    [<net>.miners]: SS58 -> session endpoint. SPEC(S3): FLOP publishes no endpoint discovery, so
    the operator lists miners whose public HTTPS session endpoint they know.
    """
    if table is None:
        return {}
    if not isinstance(table, dict):
        raise ConfigError(f"[{name}.miners] must be a table of SS58 = \"https://...\"")
    out = {}
    for ss58, url in table.items():
        ss58_pubkey(ss58)
        if not isinstance(url, str) or urlparse(url).scheme != "https" or not urlparse(url).hostname:
            raise ConfigError(f"[{name}.miners] {ss58}: endpoint must be an https:// URL")
        out[ss58] = url.rstrip("/")
    return out


def get_config_path(env=None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get("FLOPSTAR_FLOP_CONFIG", DEFAULT_CONFIG_PATH)).expanduser()


def load_config(path: Path | None = None, network: str | None = None, env=None) -> FlopConfig:
    """Load and validate the FLOP config. Raises ConfigError with a precise reason."""
    env = os.environ if env is None else env
    path = path or get_config_path(env)
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(
            f"no FLOP config at {path}; copy deploy/flop.example.toml there (caps are required)"
        ) from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e

    unknown = set(data) - {"network", *NETWORKS}
    if unknown:
        raise ConfigError(f"{path} has unknown top-level keys: {sorted(unknown)}")
    name = network or env.get("FLOPSTAR_FLOP_NETWORK") or data.get("network") or "devnet"
    if name not in NETWORKS:
        raise ConfigError(f"network must be one of {NETWORKS}, got {name!r}")
    table = data.get(name, {})
    if not isinstance(table, dict):
        raise ConfigError(f"[{name}] must be a table")
    return FlopConfig(
        network=_profile(name, table),
        caps=_caps(name, table.get("caps")),
        config_path=path,
        keystore_path=Path(env.get("FLOPSTAR_FLOP_KEYSTORE", DEFAULT_KEYSTORE_PATH)).expanduser(),
        data_dir=base_config.get_data_dir(),
        miners=_miners(name, table.get("miners")),
        passphrase_file=_path(env.get("FLOPSTAR_FLOP_PASSPHRASE_FILE")),
        gateway_key_file=_path(env.get("FLOPSTAR_FLOP_GATEWAY_KEY_FILE")),
        cli_runner=env.get("FLOPSTAR_FLOP_CLI") or None,
    )


def _path(value: str | None) -> Path | None:
    return Path(value).expanduser() if value else None
