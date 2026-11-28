"""
Local read-only and plan-only FastMCP server wrapping `flopstar.flop`.

Designed as the safe boundary between Google ADK (or any MCP-capable agent) and Flopstar's
deterministic vault:
- NEVER decrypts keys, signs transactions, claims faucets, or runs `--post` inference.
- Exposes read-only inspection (`flop_status`, `flop_scout`, `flop_check_recovery`), dry-run
  escrow/policy planning with duplicate-prompt detection (`flop_plan_inference`), and sanitized
  research queue insertion (`flop_enqueue_research`) for Flopstar's deterministic `flop run`
  runner to execute under `FlopPolicy`.

Requires optional dependency `mcp` (`uv sync --extra adk`) only when starting the MCP server;
the underlying tool handler functions are pure Python and import cleanly on the core 1 GiB droplet.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict
from typing import Any

from . import auditor, debate, inference, recovery, scout, watchtower
from . import cli as flop_cli
from .amounts import AmountError, format_flop, parse_flop
from .config import ConfigError, FlopConfig, load_config
from .ledger import Ledger
from .policy import FlopPolicy
from .tasks import SecretInInput, assert_no_secrets
from .wallet import spend_address

MAX_ENQUEUE_CHARS = 2_000


def _context(cfg: FlopConfig | None = None) -> tuple[FlopConfig, FlopPolicy]:
    resolved = cfg or load_config()
    return resolved, FlopPolicy(resolved, Ledger(resolved.db_path))


def tool_flop_status(cfg: FlopConfig | None = None, *, chain=None) -> dict[str, Any]:
    """Read-only vault & chain summary: spend address, balances, caps, budget, kill switch."""
    resolved, policy = _context(cfg)
    spend = spend_address(resolved)
    out: dict[str, Any] = {
        "network": resolved.network.name,
        "spend_account": spend,
        "kill_switch": policy.kill_reason() or "clear",
        "budget": policy.budget().describe(),
        "caps": {
            "max_flop_per_request": format_flop(resolved.caps.max_flop_per_request),
            "max_flop_per_day": format_flop(resolved.caps.max_flop_per_day),
            "max_requests_per_hour": resolved.caps.max_requests_per_hour,
            "max_faucet_draws_per_day": resolved.caps.max_faucet_draws_per_day,
            "allowed_models": list(resolved.caps.allowed_models),
            "configured_miners": len(resolved.miners),
        },
    }
    try:
        ch = chain or flop_cli.HOOKS["chain"](resolved)
        genesis = ch.genesis()
        pinned = resolved.network.genesis_hash
        head, block = ch.finalized()
        out["chain"] = {
            "status": "ok",
            "genesis": genesis,
            "genesis_matches_pin": bool(pinned and genesis.lower() == pinned.lower()),
            "spec_version": ch.spec_version(),
            "finalized_block": block,
            "spend_balance": format_flop(ch.free_balance(spend, head)) if spend else None,
        }
    except Exception as e:  # noqa: BLE001 - read-only status degrades cleanly
        out["chain"] = {"status": "unavailable", "error": f"{type(e).__name__}: {e}"}
    return out


def tool_flop_scout(cfg: FlopConfig | None = None, *, transport=None) -> dict[str, Any]:
    """Read-only drift & opportunity radar: runtime spec, CLI pin, catalog density, faucet."""
    resolved, policy = _context(cfg)
    return scout.scout(resolved, policy, transport=transport)


def tool_flop_plan_inference(
    *,
    model: str,
    prompt: str,
    purpose: str = "research-notes",
    route: str = "direct",
    miner: str | None = None,
    max_tokens: int = inference.DEFAULT_MAX_TOKENS,
    max_flop: str | None = None,
    cfg: FlopConfig | None = None,
) -> dict[str, Any]:
    """Dry-run inference plan: secret check, duplicate-prompt check, escrow sizing, policy check."""
    resolved, policy = _context(cfg)
    try:
        assert_no_secrets(prompt)
    except SecretInInput as e:
        return {"ok": False, "dry_run": True, "problems": [f"secret refused: {e}"]}
    explicit: int | None = None
    if max_flop is not None:
        try:
            explicit = parse_flop(max_flop)
        except AmountError as e:
            return {"ok": False, "dry_run": True, "problems": [f"bad max_flop: {e}"]}
        if explicit <= 0:
            return {"ok": False, "dry_run": True, "problems": ["max_flop must be positive"]}
    try:
        req_bytes = inference.build_request(model, prompt, max_tokens)
    except ValueError as e:
        return {"ok": False, "dry_run": True, "problems": [str(e)]}
    req_hash = hashlib.sha256(req_bytes).hexdigest()
    dup = policy.ledger.conn.execute(
        "SELECT id, ts, status FROM flop_spend WHERE network = ? AND request_sha256 = ? "
        "ORDER BY id DESC LIMIT 1",
        (resolved.network.name, req_hash),
    ).fetchone()
    view = flop_cli._sized_plan(
        resolved,
        policy,
        route=route,
        model=model,
        prompt=prompt,
        purpose=purpose,
        max_tokens=max_tokens,
        miner=miner,
        explicit=explicit,
    )
    view.pop("_max_flop_raw", None)
    view["dry_run"] = True
    view["request_sha256"] = req_hash
    if dup is not None:
        view["duplicate_warning"] = {
            "prior_spend_id": dup["id"],
            "prior_ts": dup["ts"],
            "prior_status": dup["status"],
        }
        view["problems"].append(
            f"duplicate prompt already recorded in spend {dup['id']} ({dup['status']})"
        )
    view["ok"] = len(view["problems"]) == 0
    return view


def tool_flop_check_recovery(cfg: FlopConfig | None = None, *, chain=None) -> dict[str, Any]:
    """Read-only scan for channels holding recoverable escrow. Never submits extrinsics."""
    resolved, policy = _context(cfg)
    ch = chain or flop_cli.HOOKS["chain"](resolved)
    agent = spend_address(resolved)
    items = recovery.scan(resolved, policy, ch, agent)
    serialized = []
    for it in items:
        row = asdict(it)
        row["escrow"] = format_flop(it.escrow) if it.escrow is not None else None
        serialized.append(row)
    return {
        "network": resolved.network.name,
        "spend_account": agent,
        "count": len(serialized),
        "due_count": sum(1 for it in items if it.call),
        "channels": serialized,
    }


def tool_flop_plan_audit(
    *,
    family: str = "precision-audit",
    model: str | None = None,
    miners: int = auditor.DEFAULT_MINER_COUNT,
    context_kb: int = auditor.DEFAULT_CONTEXT_KB,
    seed: str | None = None,
    route: str = "direct",
    cfg: FlopConfig | None = None,
) -> dict[str, Any]:
    """Dry-run cross-miner Truth Serum audit plan: probe preview, miners, and total escrow."""
    resolved, policy = _context(cfg)
    chosen_model = model or (
        resolved.caps.allowed_models[0] if resolved.caps.allowed_models else None
    )
    if not chosen_model:
        return {
            "ok": False,
            "dry_run": True,
            "problems": ["no model specified and allowed_models is empty"],
        }
    try:
        probe = auditor.generate_probe(family, seed=seed, context_kb=context_kb)
    except ValueError as e:
        return {"ok": False, "dry_run": True, "problems": [str(e)]}

    problems: list[str] = []
    selected: list[str] = []
    if route == "direct":
        try:
            selected = auditor.select_audit_miners(resolved, policy.ledger, count=miners)
        except ValueError as e:
            problems.append(str(e))
        if not selected and not problems:
            problems.append(f"no usable miners in [{resolved.network.name}.miners] (SPEC S3)")
    else:
        selected = [None]  # type: ignore[list-item]

    plans: list[dict[str, Any]] = []
    total_escrow = 0
    for m in selected:
        p = flop_cli._sized_plan(
            resolved, policy, route=route, model=chosen_model, prompt=probe.prompt,
            purpose=f"audit:{probe.family}", max_tokens=probe.recommended_max_tokens,
            miner=m, explicit=None,
        )
        raw = p.pop("_max_flop_raw", None)
        if raw is not None:
            total_escrow += raw
        plans.append(p)

    all_problems = problems + [
        f"{p.get('miner') or route}: {msg}" for p in plans for msg in p.get("problems", [])
    ]
    return {
        "ok": len(all_problems) == 0,
        "dry_run": True,
        "network": resolved.network.name,
        "model": chosen_model,
        "route": route,
        "probe": probe.describe(),
        "miners": selected,
        "total_escrow": format_flop(total_escrow),
        "problems": all_problems,
        "plans": plans,
    }


def tool_flop_plan_debate(
    *,
    topic: str,
    mode: str = "decision",
    rounds: int = debate.DEFAULT_ROUNDS,
    context_files: list[str] | None = None,
    route: str = "direct",
    max_tokens: int = debate.DEFAULT_TURN_MAX_TOKENS,
    cfg: FlopConfig | None = None,
) -> dict[str, Any]:
    """Dry-run multi-model Red/Blue Debate or GEAR Study Coach plan (secret-checked)."""
    resolved, policy = _context(cfg)
    try:
        spec = debate.build_spec(
            resolved,
            topic=topic,
            mode=mode,
            rounds=rounds,
            context_files=context_files,
            max_tokens=max_tokens,
        )
    except (ValueError, SecretInInput) as e:
        return {"ok": False, "dry_run": True, "problems": [str(e)]}

    seq = debate.planned_turn_sequence(spec)
    synthetic_history: list[tuple[int, str, str, str]] = []
    plans: list[dict[str, Any]] = []
    total_escrow = 0
    for rnum, role, model in seq:
        prompt = debate.build_turn_prompt(spec, round_num=rnum, role=role, history=synthetic_history)
        p = flop_cli._sized_plan(
            resolved, policy, route=route, model=model, prompt=prompt,
            purpose=f"debate:{spec.mode}:{role}", max_tokens=spec.max_tokens,
            miner=None, explicit=None,
        )
        raw = p.pop("_max_flop_raw", None)
        if raw is not None:
            total_escrow += raw
        p["round_num"] = rnum
        p["role"] = role
        plans.append(p)
        synthetic_history.append((rnum, role, model, "x" * (spec.max_tokens * 4)))

    problems = [
        f"turn {i + 1} ({p['role']}): {msg}"
        for i, p in enumerate(plans) for msg in p.get("problems", [])
    ]
    b = policy.budget()
    if total_escrow > b.remaining_today:
        problems.append(
            f"worst-case escrow {format_flop(total_escrow)} exceeds remaining "
            f"{format_flop(b.remaining_today)}"
        )
    if len(plans) > b.requests_left_this_hour:
        problems.append(
            f"debate needs up to {len(plans)} requests but only {b.requests_left_this_hour} "
            "left this hour"
        )
    return {
        "ok": len(problems) == 0,
        "dry_run": True,
        "network": resolved.network.name,
        "route": route,
        "spec": spec.describe(),
        "worst_case_escrow": format_flop(total_escrow),
        "problems": problems,
        "turns": plans,
    }


def tool_flop_watchtower_diff(
    cfg: FlopConfig | None = None,
    *,
    transport=None,
) -> dict[str, Any]:
    """Read-only Watchtower snapshot delta and deterministic doc-vs-code contradiction check."""
    resolved, policy = _context(cfg)
    prev = watchtower.load_previous_snapshot(resolved)
    curr = watchtower.collect_snapshot(resolved, policy, transport=transport)
    diff = watchtower.diff_snapshots(prev, curr)
    contradictions = watchtower.detect_contradictions(curr)
    return {
        "dry_run": True,
        "network": resolved.network.name,
        "collected_at": curr["collected_at"],
        "packages": curr["packages"],
        "diff": diff.describe(),
        "contradictions": contradictions,
        "alerts": (curr.get("scout") or {}).get("alerts") or [],
    }


def tool_flop_enqueue_research(question: str, cfg: FlopConfig | None = None) -> dict[str, Any]:
    """Validate and enqueue a single non-duplicate research question into data/research-queue.txt."""
    resolved, _ = _context(cfg)
    cleaned = " ".join(question.splitlines()).strip()
    if not cleaned:
        return {"enqueued": False, "reason": "question is empty"}
    if cleaned.startswith("#"):
        return {"enqueued": False, "reason": "question cannot start with '#'"}
    if len(cleaned) > MAX_ENQUEUE_CHARS:
        return {"enqueued": False, "reason": f"question exceeds {MAX_ENQUEUE_CHARS} chars"}
    try:
        assert_no_secrets(cleaned)
    except SecretInInput as e:
        return {"enqueued": False, "reason": f"secret refused: {e}"}

    digest = hashlib.sha256(cleaned.encode()).hexdigest()[:12]
    out_file = resolved.subdir("research") / f"{digest}.md"
    if out_file.exists():
        return {"enqueued": False, "reason": f"already answered in {out_file.name}", "id": digest}

    queue = resolved.data_dir / "research-queue.txt"
    existing = []
    if queue.exists():
        existing = [line.strip() for line in queue.read_text().splitlines() if line.strip()]
    if cleaned in existing:
        return {"enqueued": False, "reason": "already present in research-queue.txt", "id": digest}

    with queue.open("a", encoding="utf-8") as f:
        f.write(cleaned + "\n")
    return {
        "enqueued": True,
        "id": digest,
        "queue_path": str(queue),
        "expected_output": str(out_file),
    }


def build_server():
    """Construct the read-only/plan-only FastMCP server (requires optional `mcp` package)."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as e:
        raise RuntimeError(
            "The optional `mcp` package is not installed. Run `uv sync --extra adk` in a "
            "dev/ADK environment (keep the 1 GiB droplet minimal)."
        ) from e

    server = FastMCP("flopstar")

    @server.tool()
    def flop_status() -> dict[str, Any]:
        """Read-only FLOP vault status: spend address, balances, caps, budget, and kill switch."""
        try:
            return tool_flop_status()
        except ConfigError as e:
            return {"error": str(e)}

    @server.tool()
    def flop_scout() -> dict[str, Any]:
        """Read-only drift & opportunity radar: runtime spec, CLI pin, catalog density, faucet."""
        try:
            return tool_flop_scout()
        except ConfigError as e:
            return {"error": str(e)}

    @server.tool()
    def flop_plan_inference(
        model: str,
        prompt: str,
        purpose: str = "research-notes",
        route: str = "direct",
        miner: str | None = None,
        max_tokens: int = inference.DEFAULT_MAX_TOKENS,
        max_flop: str | None = None,
    ) -> dict[str, Any]:
        """Dry-run inference plan (secret check, duplicate check, escrow estimate, policy check)."""
        try:
            return tool_flop_plan_inference(
                model=model,
                prompt=prompt,
                purpose=purpose,
                route=route,
                miner=miner,
                max_tokens=max_tokens,
                max_flop=max_flop,
            )
        except ConfigError as e:
            return {"ok": False, "dry_run": True, "problems": [str(e)]}

    @server.tool()
    def flop_check_recovery() -> dict[str, Any]:
        """Read-only scan for FLOP compute channels holding recoverable escrow."""
        try:
            return tool_flop_check_recovery()
        except ConfigError as e:
            return {"error": str(e)}

    @server.tool()
    def flop_plan_audit(
        family: str = "precision-audit",
        model: str | None = None,
        miners: int = auditor.DEFAULT_MINER_COUNT,
        context_kb: int = auditor.DEFAULT_CONTEXT_KB,
        seed: str | None = None,
        route: str = "direct",
    ) -> dict[str, Any]:
        """Dry-run cross-miner Truth Serum audit plan (probe preview, miners, total escrow)."""
        try:
            return tool_flop_plan_audit(
                family=family,
                model=model,
                miners=miners,
                context_kb=context_kb,
                seed=seed,
                route=route,
            )
        except ConfigError as e:
            return {"ok": False, "dry_run": True, "problems": [str(e)]}

    @server.tool()
    def flop_plan_debate(
        topic: str,
        mode: str = "decision",
        rounds: int = debate.DEFAULT_ROUNDS,
        context_files: list[str] | None = None,
        route: str = "direct",
        max_tokens: int = debate.DEFAULT_TURN_MAX_TOKENS,
    ) -> dict[str, Any]:
        """Dry-run multi-model Red/Blue Debate or GEAR Study Coach plan."""
        try:
            return tool_flop_plan_debate(
                topic=topic,
                mode=mode,
                rounds=rounds,
                context_files=context_files,
                route=route,
                max_tokens=max_tokens,
            )
        except ConfigError as e:
            return {"ok": False, "dry_run": True, "problems": [str(e)]}

    @server.tool()
    def flop_watchtower_diff() -> dict[str, Any]:
        """Read-only Watchtower snapshot delta and doc-vs-code contradiction check."""
        try:
            return tool_flop_watchtower_diff()
        except ConfigError as e:
            return {"dry_run": True, "error": str(e)}

    @server.tool()
    def flop_enqueue_research(question: str) -> dict[str, Any]:
        """Sanitize, deduplicate, and enqueue a research question for the deterministic runner."""
        try:
            return tool_flop_enqueue_research(question)
        except ConfigError as e:
            return {"enqueued": False, "reason": str(e)}

    return server


def main() -> None:
    build_server().run()


if __name__ == "__main__":
    main()
