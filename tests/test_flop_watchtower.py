"""Engine 3 (FLOP Living Changelog & Doc-vs-Code Watchtower): snapshots, diffs, CLI, ADK."""

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from flopstar.flop import adk_watchtower, cli, scout, watchtower
from flopstar.flop.config import CLI_IMAGE
from flopstar.flop.wallet import create_spend_keystore
from tests.flop_helpers import (
    GENESIS,
    MODEL,
    SERVING,
    catalog_body,
    config_text,
    make,
    make_full,
)

TESTNET_GENESIS = "0x918abd58fb16306b30ef3886514cfaebffecb4de43e82b6f8ec4795c43cfd800"


def _roomy(cfg, per_request="1", per_day="5", per_hour=10):
    text = cfg.config_path.read_text()
    text = text.replace('max_flop_per_request = "0.01"', f'max_flop_per_request = "{per_request}"')
    text = text.replace('max_flop_per_day = "0.05"', f'max_flop_per_day = "{per_day}"')
    text = text.replace("max_requests_per_hour = 4", f"max_requests_per_hour = {per_hour}")
    cfg.config_path.write_text(text)


def _watch_transport(
    *,
    spec_version: int = 1014039,
    genesis: str = TESTNET_GENESIS,
    cli_image: str = CLI_IMAGE,
    pypi_ver: str = "0.4.1",
    npm_cli_ver: str = "0.9.1",
    npm_sdk_ver: str = "0.12.1",
    llms_txt: str = "# FLOP Network\nTestnet status: live.\n",
    openapi_paths: list[str] | None = None,
    catalog: bytes | dict | None = None,
) -> httpx.MockTransport:
    oa_paths = {p: {} for p in (openapi_paths or ["/v1/models", "/v1/chat/completions"])}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "POST":
            body = json.loads(request.content)
            if body["method"] == "state_getRuntimeVersion":
                return httpx.Response(200, json={
                    "jsonrpc": "2.0", "id": 1,
                    "result": {"specName": "flop-testnet", "specVersion": spec_version},
                })
            if body["method"] == "chain_getBlockHash":
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": genesis})
        if url == scout.RELEASES_URL:
            return httpx.Response(200, json={"cli": {"image": cli_image, "version": "0.1.505"}})
        if url == watchtower.PYPI_FLOP_SDK_URL:
            return httpx.Response(200, json={"info": {"version": pypi_ver}})
        if url == watchtower.NPM_CLI_URL:
            return httpx.Response(200, json={"version": npm_cli_ver})
        if url == watchtower.NPM_SDK_URL:
            return httpx.Response(200, json={"version": npm_sdk_ver})
        if url == watchtower.LLMS_TXT_URL:
            return httpx.Response(200, text=llms_txt)
        if url.endswith("/openapi.json"):
            return httpx.Response(200, json={"openapi": "3.1.0", "paths": oa_paths})
        if url.endswith("/models"):
            if isinstance(catalog, bytes):
                return httpx.Response(200, content=catalog)
            if isinstance(catalog, dict):
                return httpx.Response(200, json=catalog)
            return httpx.Response(200, content=catalog_body())
        if url.endswith("/limits"):
            return httpx.Response(200, json={"faucet": {"status": "open"}})
        return httpx.Response(404, json={"error": "not found"})

    return httpx.MockTransport(handler)


def test_snapshot_diff_and_contradiction_detection(tmp_path, monkeypatch):
    cfg, _, policy, _ = make(tmp_path, monkeypatch, text=config_text("testnet"))
    now = datetime(2026, 11, 28, 12, 0, tzinfo=UTC)

    t1 = _watch_transport(
        llms_txt=(
            "There is no registered Testnet model yet.\n"
            "No released CLI command sends this claim yet.\n"
        ),
    )
    snap1 = watchtower.collect_snapshot(cfg, policy, transport=t1, now=now)

    # Baseline has no prior snapshot.
    d0 = watchtower.diff_snapshots(None, snap1)
    assert d0.is_baseline is True and d0.has_changes is False

    # Contradictions are caught deterministically.
    contras = watchtower.detect_contradictions(snap1)
    kinds = {c["kind"] for c in contras}
    assert "DOC_VS_CATALOG_CONTRADICTION" in kinds
    assert "DOC_VS_CLI_CONTRADICTION" in kinds

    # Identical second snapshot -> no diff.
    snap1_again = watchtower.collect_snapshot(cfg, policy, transport=t1, now=now)
    d_same = watchtower.diff_snapshots(snap1, snap1_again)
    assert d_same.is_baseline is False and d_same.has_changes is False

    # Mutated snapshot -> catches runtime, PyPI, npm, OpenAPI, llms.txt, and model deltas.
    cat2 = json.loads(catalog_body())
    cat2["data"][0]["pricing"]["prompt"] = "0.000002"
    cat2["data"].append({
        "id": "qwen/qwen3-14b",
        "pricing": {"prompt": "0.0000006", "completion": "0.0000006"},
        "x-flop": {"price_sources": []},
    })
    t2 = _watch_transport(
        spec_version=1014040,
        pypi_ver="0.4.2",
        npm_cli_ver="0.9.2",
        openapi_paths=["/v1/models", "/v1/chat/completions", "/v1/miner-pin"],
        llms_txt="# FLOP Network\nTestnet inference is now live!\n",
        catalog=cat2,
    )
    snap2 = watchtower.collect_snapshot(cfg, policy, transport=t2, now=now)
    d_chg = watchtower.diff_snapshots(snap1, snap2)
    chg_kinds = {c["kind"] for c in d_chg.changes}
    assert d_chg.has_changes is True
    assert "runtime_spec_version" in chg_kinds
    assert "package_pypi_flop_sdk" in chg_kinds
    assert "package_npm_cli" in chg_kinds
    assert "catalog_models_added" in chg_kinds
    assert "catalog_prices_changed" in chg_kinds
    assert "openapi_paths_added" in chg_kinds
    assert "llms_txt_changed" in chg_kinds
    assert "Testnet inference is now live!" in d_chg.llms_unified_diff

    contras2 = watchtower.detect_contradictions(snap2)
    assert any(c["kind"] == "RUNTIME_VS_SDK_DRIFT" for c in contras2)


def test_cli_watchtower_skips_unchanged_and_runs_3_stages(tmp_path, monkeypatch, capsys):
    cfg, _, policy, _ = make_full(tmp_path, monkeypatch)
    _roomy(cfg)
    create_spend_keystore(cfg, "correct horse battery staple")
    monkeypatch.setenv("FLOPSTAR_FLOP_CONFIG", str(cfg.config_path))
    monkeypatch.setitem(cli.HOOKS, "which", lambda n: "/usr/bin/docker" if n == "docker" else None)
    monkeypatch.setitem(cli.HOOKS, "passphrase", "correct horse battery staple")

    base_t = _watch_transport(spec_version=1014038, genesis=GENESIS)
    monkeypatch.setitem(cli.HOOKS, "transport", base_t)

    # 1. Baseline --post with no prior snapshot & no contradictions -> 0 FLOP spent, saves baseline.
    def explode(*a, **k):
        raise AssertionError("must not call signing CLI when no changes exist")

    monkeypatch.setitem(cli.HOOKS, "runner", explode)
    cli.run_flop(["watchtower", "--json", "--post"])
    out0 = json.loads(capsys.readouterr().out)
    assert out0["skipped"] is True
    assert Path(out0["snapshot_saved"]).exists()

    # 2. Now simulate a PyPI SDK update + llms.txt change -> dry run plans 3 stages (0.15 FLOP).
    changed_t = _watch_transport(
        spec_version=1014038,
        genesis=GENESIS,
        pypi_ver="0.4.2",
        llms_txt="# FLOP Network\nNew routing guide published.\n",
    )
    monkeypatch.setitem(cli.HOOKS, "transport", changed_t)
    cli.run_flop(["watchtower", "--miner", SERVING, "--max-tokens", "128", "--json"])
    plan = json.loads(capsys.readouterr().out)
    assert plan["dry_run"] is True
    assert plan["will_run_inference"] is True
    assert plan["worst_case_escrow"] == "0.15"
    assert len(plan["stages"]) == 3

    # 3. Execute --post with fake CLI -> 3 settled stages, digest written, incident logged.
    call_n = 0

    def fake_runner(argv, env, **kw):
        nonlocal call_n
        call_n += 1
        payload = {
            "reply": f"Stage {call_n} verified analysis output for {MODEL}.",
            "miner": SERVING,
            "channel": f"0x{call_n:064x}",
            "settle_tx": f"0x{call_n + 200:064x}",
            "cooperative": True,
            "verified_turns": 1,
            "finish_reason": "stop",
            "completion_tokens": "48",
        }
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    monkeypatch.setitem(cli.HOOKS, "runner", fake_runner)
    cli.run_flop(["watchtower", "--miner", SERVING, "--max-tokens", "128", "--json", "--post"])
    res = json.loads(capsys.readouterr().out)
    assert res["completed"] is True
    assert len(res["stages"]) == 3
    assert res["total_escrow_committed"] == "0.15"
    assert Path(res["digest_path"]).exists()
    assert "untrusted data" in Path(res["digest_path"]).read_text()
    assert Path(res["incidents_path"]).exists()
    assert not cfg.kill_path.exists()
    assert policy.budget().spent_today == 15 * 10**16  # 0.15 FLOP


def test_adk_watchtower_skip_gate_and_missing_dep(monkeypatch):
    assert adk_watchtower.should_skip_inference(
        {"diff": {"is_baseline": True, "has_changes": False}, "contradictions": []}
    )["skip"] is True
    assert adk_watchtower.should_skip_inference(
        {"diff": {"is_baseline": False, "has_changes": False}, "contradictions": []}
    )["skip"] is True
    assert adk_watchtower.should_skip_inference(
        {"diff": {"has_changes": True, "changes": [{"kind": "runtime_spec_version"}]}}
    )["skip"] is False
    assert adk_watchtower.should_skip_inference(
        {"diff": {"has_changes": False}, "contradictions": [{"kind": "DOC_VS_CLI_CONTRADICTION"}]}
    )["skip"] is False
    assert adk_watchtower.should_skip_inference(
        {"diff": {"has_changes": False}}, force=True
    )["skip"] is False

    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("google.adk"):
            raise ImportError("no adk")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(RuntimeError, match="uv sync --extra adk"):
        adk_watchtower.build_watchtower_agent()
