"""Engine 2 (Red/Blue Debate Arena & GEAR Study Coach): spec, context guard, CLI, ADK LoopAgent."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from flopstar.flop import adk_debate, cli, debate
from flopstar.flop.tasks import SecretInInput
from flopstar.flop.wallet import create_spend_keystore
from tests.flop_helpers import MODEL, SERVING, catalog_transport, make_full


def _roomy(cfg, per_request="1", per_day="5", per_hour=10):
    text = cfg.config_path.read_text()
    text = text.replace('max_flop_per_request = "0.01"', f'max_flop_per_request = "{per_request}"')
    text = text.replace('max_flop_per_day = "0.05"', f'max_flop_per_day = "{per_day}"')
    text = text.replace("max_requests_per_hour = 4", f"max_requests_per_hour = {per_hour}")
    cfg.config_path.write_text(text)


def test_assign_models_rotates_preferred_and_falls_back():
    multi = (
        "qwen/qwen3-8b",
        "qwen/qwen3-14b",
        "qwen/qwen3-30b-a3b-fp8",
        "qwen/qwen2.5-14b-instruct",
    )
    roles = debate.assign_models(multi, "decision")
    assert roles == {
        "advocate": "qwen/qwen3-14b",
        "skeptic": "qwen/qwen3-30b-a3b-fp8",
        "synthesizer": "qwen/qwen2.5-14b-instruct",
    }

    single = debate.assign_models((MODEL,), "gear-coach")
    assert single == {"author": MODEL, "reviewer": MODEL, "synthesizer": MODEL}

    with pytest.raises(ValueError, match="allowed_models"):
        debate.assign_models((MODEL,), "decision", {"skeptic": "unlisted/model"})


def test_load_context_files_blocks_secrets_and_truncates(tmp_path):
    clean = tmp_path / "notes.md"
    clean.write_text("FLOP uses 18-decimal integer amounts.\n" * 20)
    text, sources = debate.load_context_files([clean], max_bytes=120)
    assert sources == ("notes.md",)
    assert "...[truncated]" in text

    for bad_name in ("flopstar.pem", "spend.json", ".env", "tree.seed", "token.cred"):
        p = tmp_path / bad_name
        p.write_text("dummy")
        with pytest.raises(SecretInInput, match="secret-like"):
            debate.load_context_files([p])

    leaked = tmp_path / "readme.txt"
    leaked.write_text("oops: -----BEGIN PRIVATE" + " KEY-----")
    with pytest.raises(SecretInInput, match="PEM"):
        debate.load_context_files([leaked])


def test_prompt_snowballs_with_untrusted_framing_and_convergence(tmp_path, monkeypatch):
    cfg, _, _, _ = make_full(tmp_path, monkeypatch)
    spec = debate.build_spec(cfg, topic="Should we enable qwen3-14b?", mode="decision", rounds=2)

    p1 = debate.build_turn_prompt(spec, round_num=1, role="advocate", history=[])
    p2 = debate.build_turn_prompt(
        spec,
        round_num=1,
        role="skeptic",
        history=[(1, "advocate", MODEL, "Use qwen3-14b for 83k tokens per 0.05 FLOP.")],
    )
    assert len(p2) > len(p1)
    assert '<prior_turn round="1" role="advocate"' in p2
    assert "untrusted model output" in p2

    assert debate.is_converged("All risks resolved.\nCONVERGED: true\n") is True
    assert debate.is_converged('{"converged": true}') is True
    assert debate.is_converged("Still open.\nCONVERGED: false\n") is False
    assert debate.is_converged(None) is False


def test_cli_debate_dry_run_touches_nothing(tmp_path, monkeypatch, capsys):
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

    cli.run_flop([
        "debate", "--topic", "Evaluate Touchstone retention strategy",
        "--mode", "decision", "--rounds", "1", "--max-tokens", "256", "--json",
    ])
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is True
    assert out["problems"] == []
    assert out["spec"]["planned_turns"] == 3
    assert out["worst_case_escrow"] == "0.15"  # 3 turns x 0.05 floor
    assert not (cfg.data_dir / "debates").exists()


def test_cli_debate_post_early_convergence_and_gear_coach(tmp_path, monkeypatch, capsys):
    cfg, _, _, _ = make_full(tmp_path, monkeypatch)
    _roomy(cfg)
    create_spend_keystore(cfg, "correct horse battery staple")
    monkeypatch.setenv("FLOPSTAR_FLOP_CONFIG", str(cfg.config_path))
    monkeypatch.setitem(cli.HOOKS, "transport", catalog_transport())
    monkeypatch.setitem(cli.HOOKS, "which", lambda n: "/usr/bin/docker" if n == "docker" else None)
    monkeypatch.setitem(cli.HOOKS, "passphrase", "correct horse battery staple")

    call_n = 0

    def fake_runner(argv, env, **kw):
        nonlocal call_n
        call_n += 1
        prompt = argv[argv.index("--prompt") + 1]
        if "SKEPTIC" in prompt or "REVIEWER" in prompt:
            reply = "Critique complete; no unresolved blockers.\nCONVERGED: true"
        elif "JUDGE" in prompt or "STUDY GUIDE" in prompt:
            reply = "## Final Synthesis\nRecommendation: proceed with guarded rollout."
        else:
            reply = "Structured thesis backed by exact 18-decimal cap math."
        payload = {
            "reply": reply,
            "miner": SERVING,
            "channel": f"0x{call_n:064x}",
            "settle_tx": f"0x{call_n + 100:064x}",
            "cooperative": True,
            "verified_turns": 1,
            "finish_reason": "stop",
            "completion_tokens": "64",
        }
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    monkeypatch.setitem(cli.HOOKS, "runner", fake_runner)

    # 1. Decision mode with rounds=2 converges early on round 1 -> 3 turns instead of 5.
    cli.run_flop([
        "debate", "--topic", "Raise testnet daily cap to 10 FLOP once S3 opens?",
        "--mode", "decision", "--rounds", "2", "--miner", SERVING,
        "--max-tokens", "128", "--json", "--post",
    ])
    out = json.loads(capsys.readouterr().out)
    assert out["completed"] is True
    assert out["converged_early"] is True
    assert len(out["turns"]) == 3
    assert out["total_escrow_committed"] == "0.15"
    report = Path(out["report_path"])
    assert report.exists()
    md = report.read_text()
    assert "Decision Memo:" in md and "untrusted data" in md

    # 2. GEAR Coach mode with an attached context file.
    ctx = tmp_path / "adk-notes.md"
    ctx.write_text("LoopAgent exits when tool_context.actions.escalate = True.\n")
    cli.run_flop([
        "debate", "--topic", "ADK LoopAgent termination & callback governance",
        "--mode", "gear-coach", "--rounds", "1", "--context-file", str(ctx),
        "--miner", SERVING, "--max-tokens", "128", "--json", "--post",
    ])
    out2 = json.loads(capsys.readouterr().out)
    assert out2["completed"] is True
    assert len(out2["turns"]) == 3
    assert "GEAR Study Drill:" in Path(out2["report_path"]).read_text()
    assert not cfg.kill_path.exists()


def test_adk_exit_loop_and_missing_optional_dep(monkeypatch):
    ctx = SimpleNamespace(actions=SimpleNamespace(escalate=False))
    res = adk_debate.exit_loop("all risks resolved", ctx)
    assert res == {"exited": True, "reason": "all risks resolved"}
    assert ctx.actions.escalate is True

    with pytest.raises(ValueError, match="unknown debate mode"):
        adk_debate.build_debate_agent(mode="invalid")

    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("google.adk"):
            raise ImportError("no adk")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(RuntimeError, match="uv sync --extra adk"):
        adk_debate.build_debate_agent(mode="decision")
