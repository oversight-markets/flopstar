"""Engine 1 (Miner Truth Serum): probes, deterministic verifier, divergence detection, CLI & ADK."""

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from flopstar.flop import adk_auditor, auditor, cli
from flopstar.flop.inference import Result
from flopstar.flop.wallet import create_spend_keystore
from tests.flop_helpers import (
    BLOCKED,
    MINER,
    MODEL,
    SERVING,
    catalog_transport,
    make_full,
)


def _roomy(cfg, per_request="1", per_day="5"):
    text = cfg.config_path.read_text()
    text = text.replace('max_flop_per_request = "0.01"', f'max_flop_per_request = "{per_request}"')
    text = text.replace('max_flop_per_day = "0.05"', f'max_flop_per_day = "{per_day}"')
    cfg.config_path.write_text(text)


@pytest.mark.parametrize("family", auditor.FAMILIES)
def test_all_probe_families_deterministic_and_verifiable(family):
    p1 = auditor.generate_probe(family, seed="fixed-seed-42", context_kb=4)
    p2 = auditor.generate_probe(family, seed="fixed-seed-42", context_kb=4)
    p3 = auditor.generate_probe(family, seed="other-seed-99", context_kb=4)

    assert p1 == p2
    assert p1.prompt != p3.prompt
    assert p1.describe()["prompt_sha256"] != p3.describe()["prompt_sha256"]
    assert p1.context_bytes >= 1_000

    # Exact JSON passes.
    ok = auditor.verify_reply(p1, json.dumps(p1.expected))
    assert ok.passed is True and ok.schema_valid is True and ok.score == 1.0

    # Fenced markdown JSON also passes.
    fenced = f"Here is the verified output:\n```json\n{json.dumps(p1.expected)}\n```"
    assert auditor.verify_reply(p1, fenced).passed is True

    # Corrupted / wrong / empty replies fail cleanly without raising.
    assert auditor.verify_reply(p1, "").passed is False
    assert auditor.verify_reply(p1, "just prose without json").passed is False
    bad_obj = {**p1.expected, "unexpected_extra": 1}
    bad = auditor.verify_reply(p1, json.dumps(bad_obj))
    assert bad.passed is False and any("extra keys" in m for m in bad.mismatches)


def test_select_audit_miners_excludes_blocklist(tmp_path, monkeypatch):
    cfg, ledger, _, _ = make_full(tmp_path, monkeypatch)
    picked = auditor.select_audit_miners(cfg, ledger, count=3)
    assert BLOCKED not in picked
    assert set(picked) == {SERVING, MINER}
    with pytest.raises(ValueError, match="blocklist"):
        auditor.select_audit_miners(cfg, ledger, count=2, requested=[BLOCKED])


def test_analyze_batch_detects_divergence_latency_and_faults(tmp_path, monkeypatch):
    cfg, _, _, _ = make_full(tmp_path, monkeypatch)
    probe = auditor.generate_probe("needle-haystack", seed="div-check", context_kb=2)
    good_json = json.dumps(probe.expected)
    wrong_json = json.dumps({**probe.expected, "checksum_sum": -1})

    miner_c = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"
    runs = [
        auditor.MinerRun(
            miner=SERVING,
            result=Result(
                spend_id=1, route="direct", model=MODEL, status="settled", miner=SERVING,
                channel_id="0x" + "11" * 32, reply=good_json, settle_tx="0xaa",
                verification="sdk_verified", max_flop=5 * 10**16,
                request_bytes=b"req", response_bytes=good_json.encode(),
                timings={"total_ms": 800},
            ),
            verification=auditor.verify_reply(probe, good_json),
        ),
        auditor.MinerRun(
            miner=MINER,
            result=Result(
                spend_id=2, route="direct", model=MODEL, status="settled", miner=MINER,
                channel_id="0x" + "22" * 32, reply=good_json, settle_tx="0xbb",
                verification="sdk_verified", max_flop=5 * 10**16,
                request_bytes=b"req", response_bytes=good_json.encode(),
                timings={"total_ms": 900},
            ),
            verification=auditor.verify_reply(probe, good_json),
        ),
        auditor.MinerRun(
            miner=miner_c,
            result=Result(
                spend_id=3, route="direct", model=MODEL, status="settled", miner=miner_c,
                channel_id="0x" + "33" * 32, reply=wrong_json, settle_tx="0xcc",
                verification="sdk_verified", max_flop=5 * 10**16,
                request_bytes=b"req", response_bytes=wrong_json.encode(),
                timings={"total_ms": 9_500},
            ),
            verification=auditor.verify_reply(probe, wrong_json),
        ),
    ]

    analysis = auditor.analyze_batch(
        cfg, MODEL, probe, runs, now=datetime(2026, 11, 28, 12, 0, tzinfo=UTC)
    )
    kinds = {a["kind"] for a in analysis.anomalies}
    assert "ACCURACY_DIVERGENCE" in kinds
    assert "LATENCY_OUTLIER" in kinds
    assert analysis.summary["passed_ground_truth"] == 2

    inc_path = auditor.record_incidents(cfg, analysis)
    assert inc_path is not None and inc_path.exists()
    lines = [json.loads(line) for line in inc_path.read_text().splitlines()]
    assert len(lines) == 2

    report_path = auditor.write_audit_report(cfg, analysis)
    assert report_path.exists()
    md = report_path.read_text()
    assert "untrusted data" in md and "ACCURACY_DIVERGENCE" in md


def test_cli_audit_dry_run_touches_nothing(tmp_path, monkeypatch, capsys):
    cfg, _, _, _ = make_full(tmp_path, monkeypatch)
    _roomy(cfg)
    create_spend_keystore(cfg, "correct horse battery staple")
    monkeypatch.setenv("FLOPSTAR_FLOP_CONFIG", str(cfg.config_path))

    def explode(*a, **k):
        raise AssertionError("dry run must never touch runner or chain")

    monkeypatch.setitem(cli.HOOKS, "chain", explode)
    monkeypatch.setitem(cli.HOOKS, "runner", explode)
    monkeypatch.setitem(cli.HOOKS, "transport", catalog_transport(also=explode))
    monkeypatch.setitem(cli.HOOKS, "which", lambda n: "/usr/bin/docker" if n == "docker" else None)

    cli.run_flop(["audit", "--family", "state-trace", "--seed", "s1", "--context-kb", "2",
                  "--miners", "2", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is True
    assert out["problems"] == []
    assert out["total_escrow"] == "0.1"
    assert set(out["miners"]) == {SERVING, MINER}
    assert not (cfg.data_dir / "incidents.jsonl").exists()


def test_cli_audit_post_catches_cheating_miner_end_to_end(tmp_path, monkeypatch, capsys):
    cfg, _, _, _ = make_full(tmp_path, monkeypatch)
    _roomy(cfg)
    create_spend_keystore(cfg, "correct horse battery staple")
    monkeypatch.setenv("FLOPSTAR_FLOP_CONFIG", str(cfg.config_path))
    monkeypatch.setitem(cli.HOOKS, "transport", catalog_transport())
    monkeypatch.setitem(cli.HOOKS, "which", lambda n: "/usr/bin/docker" if n == "docker" else None)
    monkeypatch.setitem(cli.HOOKS, "passphrase", "correct horse battery staple")

    probe = auditor.generate_probe("state-trace", seed="live-sim-1", context_kb=2)

    def fake_runner(argv, env, **kw):
        miner = argv[argv.index("--miner") + 1]
        # SERVING passes ground truth; MINER simulates a lazy/quantized cheating miner.
        reply = json.dumps(probe.expected) if miner == SERVING else '{"escrow_remaining": 0}'
        payload = {
            "reply": reply,
            "miner": miner,
            "channel": "0x" + ("aa" if miner == SERVING else "bb") * 32,
            "settle_tx": "0x" + ("11" if miner == SERVING else "22") * 32,
            "cooperative": True,
            "verified_turns": 1,
            "finish_reason": "stop",
            "completion_tokens": "42",
        }
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    monkeypatch.setitem(cli.HOOKS, "runner", fake_runner)

    cli.run_flop(["audit", "--family", "state-trace", "--seed", "live-sim-1", "--context-kb", "2",
                  "--miners", "2", "--json", "--post"])
    out = json.loads(capsys.readouterr().out)
    assert out["summary"]["miners_tested"] == 2
    assert out["summary"]["settled"] == 2
    assert out["summary"]["passed_ground_truth"] == 1
    assert any(a["kind"] == "ACCURACY_DIVERGENCE" and a["miner"] == MINER
               for a in out["anomalies"])
    assert Path(out["report_path"]).exists()
    assert Path(out["incidents_path"]).exists()
    assert not cfg.kill_path.exists()


def test_adk_callbacks_block_secrets_and_log_incidents(tmp_path, monkeypatch):
    # Forbidden server-side secret tool blocked.
    refused = adk_auditor.before_tool_guard("flop_generate_secrets", {})
    assert refused is not None and refused["refused"] is True

    # Secret inside prompt arg blocked.
    pem_refused = adk_auditor.before_tool_guard(
        "flop_plan_inference", {"prompt": "-----BEGIN PRIVATE" + " KEY-----"}
    )
    assert pem_refused is not None and pem_refused["refused"] is True

    # Clean args allowed.
    assert adk_auditor.before_tool_guard("flop_scout", {"network": "testnet"}) is None

    # Incident sink writes only on real alerts / anomalies / recovery needs.
    sink = tmp_path / "incidents.jsonl"
    adk_auditor.after_tool_incident_logger("flop_scout", {"alerts": ["S3_BLOCKED: empty"]}, sink)
    assert not sink.exists()

    adk_auditor.after_tool_incident_logger(
        "flop_scout",
        {"network": "testnet", "alerts": ["RUNTIME_DRIFT: spec 1014040 unsupported"]},
        sink,
    )
    assert sink.exists()
    row = json.loads(sink.read_text().strip())
    assert row["tool"] == "flop_scout"
    assert any("RUNTIME_DRIFT:" in f for f in row["flags"])


def test_adk_builder_clear_error_without_optional_extra(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("google.adk"):
            raise ImportError("no adk")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(RuntimeError, match="uv sync --extra adk"):
        adk_auditor.build_truth_serum_agent()
