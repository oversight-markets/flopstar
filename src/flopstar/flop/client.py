"""
Thin wrapper over flop-sdk / substrate-interface for chain reads. Never signs (FlopPolicy does).

All amounts come back as int base units straight from SCALE decoding; the SDK's float helpers
(`get_balance`, `raw_to_flop`) are never used. Every connection checks the runtime spec version
against flop-sdk's qualified list, as the SDK's own clients do (docs/FLOP.md §2.1, §11.6).
"""

from dataclasses import dataclass

from flop_sdk import FlopClient
from flop_sdk.client import FLOPError, assert_supported_runtime
from flop_sdk.types import FlopClientOptions
from substrateinterface import SubstrateInterface

from .config import ConfigError, FlopConfig


class CheckedSubstrate(SubstrateInterface):
    """SubstrateInterface that refuses runtimes flop-sdk has not qualified (before refresh/submit)."""

    def check_runtime(self) -> None:
        assert_supported_runtime(self.rpc_request("state_getRuntimeVersion", [])["result"])

    def init_runtime(self, block_hash=None, block_id=None):
        self.check_runtime()
        return super().init_runtime(block_hash=block_hash, block_id=block_id)

    def submit_extrinsic(self, *args, **kwargs):
        self.check_runtime()
        return super().submit_extrinsic(*args, **kwargs)


def rpc_http_url(cfg: FlopConfig) -> str:
    url = cfg.network.rpc_http_url
    if not url:
        raise ConfigError(f"{cfg.network.name} has no rpc_http_url (SPEC S2); set it in the config")
    return url


@dataclass(frozen=True)
class ChannelView:
    """The parts of ComputeChannel.Channels we act on (layout read from devnet metadata, FLOP.md S8)."""

    channel_id: str
    agent: str
    miner: str
    escrow: int
    state: str  # PendingAck | Open | Settling | Closed
    ack_deadline: int | None
    opened_at: int
    max_duration: int
    challenge_end: int | None

    @classmethod
    def from_storage(cls, channel_id: str, value: dict) -> "ChannelView":
        state = value["state"]
        if isinstance(state, str):
            name, fields = state, {}
        elif isinstance(state, dict) and len(state) == 1:
            name, fields = next(iter(state.items()))
        else:
            raise ValueError(f"unrecognised channel state {state!r}")
        if name not in ("PendingAck", "Open", "Settling", "Closed"):
            raise ValueError(f"unknown channel state {name!r}")
        escrow = value["escrow"]
        if type(escrow) is not int:
            raise ValueError("escrow must decode as int")
        return cls(
            channel_id=channel_id.lower(),
            agent=value["agent"],
            miner=value["miner"],
            escrow=escrow,
            state=name,
            ack_deadline=fields.get("ack_deadline"),
            opened_at=int(value["opened_at"]),
            max_duration=int(value["sla"]["max_duration"]),
            challenge_end=fields.get("challenge_end"),
        )


class Chain:
    """Read-only chain access. Construct with a substrate for tests; otherwise connects lazily."""

    def __init__(self, cfg: FlopConfig, substrate=None):
        self.cfg = cfg
        self._substrate = substrate

    @property
    def substrate(self):
        if self._substrate is None:
            sub = CheckedSubstrate(url=rpc_http_url(self.cfg), ss58_format=42)
            sub.check_runtime()
            self._substrate = sub
        return self._substrate

    def genesis(self) -> str:
        return self.substrate.get_block_hash(0)

    def verify_genesis(self) -> str:
        """Raise GenesisMismatch unless the chain is the pinned one; returns the genesis."""
        genesis = self.genesis()
        self.cfg.check_genesis(genesis)
        return genesis

    def spec_version(self) -> int:
        return self.substrate.rpc_request("state_getRuntimeVersion", [])["result"]["specVersion"]

    def finalized(self) -> tuple[str, int]:
        head = self.substrate.get_chain_finalised_head()
        return head, self.substrate.get_block_number(head)

    def free_balance(self, ss58: str, block_hash: str | None = None) -> int:
        info = self.substrate.query("System", "Account", [ss58], block_hash=block_hash).value
        free = (info or {}).get("data", {}).get("free", 0)
        if type(free) is not int:
            raise ValueError("free balance must decode as int")
        return free

    def channel(self, channel_id: str, block_hash: str | None = None) -> ChannelView | None:
        value = self.substrate.query("ComputeChannel", "Channels", [channel_id],
                                     block_hash=block_hash).value
        return ChannelView.from_storage(channel_id, value) if value else None

    def channels_by_agent(self, agent: str, block_hash: str | None = None,
                          max_results: int = 5000) -> list[ChannelView]:
        """Scan Channels for ones we pay for (used when a run died before learning its channel)."""
        out = []
        for key, value in self.substrate.query_map("ComputeChannel", "Channels", page_size=200,
                                                   max_results=max_results, block_hash=block_hash):
            if value.value and value.value.get("agent") == agent:
                out.append(ChannelView.from_storage(key.value, value.value))
        return out


async def health(cfg: FlopConfig) -> dict:
    """FlopClient.health() (also checks the runtime spec version on connect)."""
    async with FlopClient(FlopClientOptions(rpc_url=rpc_http_url(cfg))) as client:
        h = await client.health()
    return {"syncing": h.is_syncing, "peers": h.peers, "should_have_peers": h.should_have_peers}


__all__ = ["Chain", "ChannelView", "CheckedSubstrate", "FLOPError", "health", "rpc_http_url"]
