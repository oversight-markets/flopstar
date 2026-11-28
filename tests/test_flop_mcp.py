"""Local read-only / plan-only FastMCP tool handlers and safety invariants."""

import hashlib
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from flopstar.flop import cli, inference, mcp_server
from flopstar.flop.client import ChannelView
from flopstar.flop.wallet import create_spend_keystore
from tests.flop_helpers import (
    GENESIS,
    MINER,
    MODEL,
    SERVING,
    catalog_transport,
    make_full,
)


class _StubChain:
    def __init__(self, balance: int = 10 * 10**18, channels: list[ChannelView] | None = None):
        self._balance = balance
        self._channels = channels or []

    def genesis(self) -> str:
        return GENESIS

    def spec_version(self) -> int:
        return 1014038

    def finalized(self) -> tuple[str, int]:
        return "0x" + "ab" * 32, 123_456

    def free_balance(self, ss58: str, block_hash: str | None = None) -> int:
        return self._balance

    def channels_by_agent(self, agent: str, block_hash: str | None = None) -> list[ChannelView]:
        return list(self._channels)

    def channel(self, channel_id: str, block_hash: str | None = None) -> ChannelView | None:
        for c in self._channels:
            if c.channel_id == channel_id:
                return c
        return None


def _roomy(cfg):
    text = cfg.config_path.read_text()
    text = text.replace('max_flop_per_request = "0.01"', 'max_flop_per_request = "1"')
    text = text.replace('max_flop_per_day = "0.05"', 'max_flop_per_day = "5"')
    cfg.config_path.write_text(text)
    from flopstar.flop.config import load_config
    return load_config(cfg.config_path, env={
        "FLOPSTAR_DATA_DIR": str(cfg.data_dir),
        "FLOPSTAR_FLOP_KEYSTORE": str(cfg.keystore_path),
    })


def test_tool_flop_status_read_only(tmp_path, monkeypatch):
    cfg, _, _, _ = make_full(tmp_path, monkeypatch)
    addr = create_spend_keystore(cfg, "correct horse battery staple")
    view = mcp_server.tool_flop_status(cfg, chain=_StubChain())
    assert view["network"] == "devnet"
    assert view["spend_account"] == addr
    assert view["kill_switch"] == "clear"
    assert view["chain"]["status"] == "ok"
    assert view["chain"]["genesis_matches_pin"] is True
    assert view["chain"]["spend_balance"] == "10"


def test_tool_flop_plan_inference_dry_run_secret_and_dedupe(tmp_path, monkeypatch):
    cfg, _ledger, policy, _ = make_full(tmp_path, monkeypatch)
    cfg = _roomy(cfg)
    create_spend_keystore(cfg, "correct horse battery staple")
    monkeypatch.setitem(cli.HOOKS, "transport", catalog_transport())
    monkeypatch.setitem(cli.HOOKS, "which", lambda n: "/usr/bin/docker" if n == "docker" else None)

    # 1. Refuses secrets without tripping kill switch.
    bad = mcp_server.tool_flop_plan_inference(
        model=MODEL,
        prompt="leak: -----BEGIN PRIVATE" + " KEY-----",
        cfg=cfg,
    )
    assert bad["ok"] is False and bad["dry_run"] is True
    assert any("secret refused" in p for p in bad["problems"])
    assert not cfg.kill_path.exists()

    # 2. Valid dry-run plan succeeds and estimates the 0.05 floor.
    prompt = "Compare TOPLOC verification overheads across Qwen3 models."
    ok = mcp_server.tool_flop_plan_inference(
        model=MODEL, prompt=prompt, miner=SERVING, max_tokens=128, cfg=cfg,
    )
    assert ok["ok"] is True and ok["dry_run"] is True
    assert ok["max_flop"] == "0.05"
    assert ok["problems"] == []

    # 3. Duplicate prompt already in ledger is flagged (anti-spam guard).
    req_bytes = inference.build_request(MODEL, prompt, 128)
    req_hash = hashlib.sha256(req_bytes).hexdigest()
    policy.authorize_inference(
        route="direct", model=MODEL, miner=SERVING, max_flop=10**16,
        purpose="research-notes", request_sha256=req_hash,
    )
    dup = mcp_server.tool_flop_plan_inference(
        model=MODEL, prompt=prompt, miner=SERVING, max_tokens=128, cfg=cfg,
    )
    assert dup["ok"] is False
    assert "duplicate_warning" in dup
    assert any("duplicate prompt" in p for p in dup["problems"])


def test_tool_flop_check_recovery_read_only(tmp_path, monkeypatch):
    cfg, ledger, policy, _ = make_full(tmp_path, monkeypatch)
    addr = create_spend_keystore(cfg, "correct horse battery staple")
    cid = "0x" + "12" * 32
    sid = policy.authorize_inference(
        route="direct", model=MODEL, miner=MINER, max_flop=10**16,
        purpose="research-notes", request_sha256="a" * 64,
    )
    ledger.transition(sid, "sent")
    ledger.transition(sid, "failed", {"error": "timeout"}, channel_id=cid)
    ch = ChannelView(
        channel_id=cid,
        agent=addr,
        miner=MINER,
        escrow=5 * 10**16,
        state="PendingAck",
        ack_deadline=100_000,
        opened_at=99_400,
        max_duration=600,
        challenge_end=None,
    )
    res = mcp_server.tool_flop_check_recovery(cfg, chain=_StubChain(channels=[ch]))
    assert res["count"] == 1
    assert res["due_count"] == 1
    assert res["channels"][0]["call"] == "expire_force_open"
    assert res["channels"][0]["escrow"] == "0.05"


def test_tool_flop_enqueue_research_sanitizes_and_dedupes(tmp_path, monkeypatch):
    cfg, _, _, _ = make_full(tmp_path, monkeypatch)
    q = "What changes between FLOP runtime 1014035 and 1014039?"

    # Rejects empty, comment, secret.
    assert mcp_server.tool_flop_enqueue_research("   ", cfg=cfg)["enqueued"] is False
    assert mcp_server.tool_flop_enqueue_research("# comment", cfg=cfg)["enqueued"] is False
    sec = mcp_server.tool_flop_enqueue_research(
        "-----BEGIN PRIVATE" + " KEY-----", cfg=cfg,
    )
    assert sec["enqueued"] is False and "secret refused" in sec["reason"]

    # Enqueues once, rejects duplicate in queue.
    first = mcp_server.tool_flop_enqueue_research(q, cfg=cfg)
    assert first["enqueued"] is True
    assert Path(first["queue_path"]).read_text().strip() == q

    second = mcp_server.tool_flop_enqueue_research(q, cfg=cfg)
    assert second["enqueued"] is False and "already present" in second["reason"]

    # Rejects question whose answer already exists in data/research/.
    q2 = "How does FLOP AuditFeeEarmarked interact with min_escrow?"
    digest2 = hashlib.sha256(q2.encode()).hexdigest()[:12]
    (cfg.subdir("research") / f"{digest2}.md").write_text("# already answered\n")
    answered = mcp_server.tool_flop_enqueue_research(q2, cfg=cfg)
    assert answered["enqueued"] is False and "already answered" in answered["reason"]


def test_tool_flop_scout_wrapper(tmp_path, monkeypatch):
    cfg, _, _, _ = make_full(tmp_path, monkeypatch)
    transport = httpx.MockTransport(lambda req: httpx.Response(503, json={"error": "down"}))
    view = mcp_server.tool_flop_scout(cfg, transport=transport)
    assert view["network"] == "devnet"
    assert isinstance(view["alerts"], list)


def test_mcp_server_never_exposes_signing_or_spending():
    src = Path(mcp_server.__file__).read_text()
    forbidden = (
        "inference.execute(",
        "run_direct(",
        "run_gateway(",
        "faucet.claim(",
        "faucet.draw(",
        "faucet.reconcile(",
        "recovery.recover(",
        "authorize_inference(",
        "authorize_faucet(",
        "load_spend_keypair(",
        "create_spend_keystore(",
    )
    for token in forbidden:
        assert token not in src, f"mcp_server.py must remain read-only/plan-only; found {token}"


def test_build_server_clear_error_when_mcp_missing(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "mcp.server.fastmcp":
            raise ImportError("no mcp")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(RuntimeError, match="uv sync --extra adk"):
        mcp_server.build_server()
    assert SimpleNamespace() is not None
