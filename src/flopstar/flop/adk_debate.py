"""
Optional Google ADK `LoopAgent` + `SequentialAgent` wrapper for Engine 2
(Adversarial Red/Blue Debate Arena & GEAR Study Coach).

Demonstrates key Google GEAR / Professional Agentic Architect patterns:
- `LoopAgent` (`max_iterations=3`) with explicit `exit_loop` escalation tool
- `SequentialAgent` (`DebateLoop` -> `Synthesizer`)
- Shared `before_tool_callback` (`before_tool_guard`) and `after_tool_callback`
  (`after_tool_incident_logger`) from `adk_auditor.py`
- Read-only / plan-only local `McpToolset` (`flopstar-mcp`)

Imports `google.adk` lazily inside `build_debate_agent()` so the core 961 MiB droplet stays
lightweight and callbacks/exit-loop logic can be unit-tested without `google-adk`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .adk_auditor import after_tool_incident_logger, before_tool_guard
from .debate import DEFAULT_ROUNDS, MAX_ROUNDS, MODES


def exit_loop(reason: str, tool_context: Any) -> dict[str, Any]:
    """
    Canonical Google ADK `LoopAgent` termination tool.

    Sets `tool_context.actions.escalate = True` so the enclosing `LoopAgent` exits cleanly once
    the Skeptic/Reviewer determines the debate or study drill has converged.
    """
    actions = getattr(tool_context, "actions", None)
    if actions is not None:
        actions.escalate = True
    return {"exited": True, "reason": str(reason).strip() or "converged"}


def build_debate_agent(
    *,
    mode: str = "decision",
    model: str = "gemini-2.5-flash",
    max_iterations: int = DEFAULT_ROUNDS,
    incidents_path: Path | None = None,
):
    """
    Construct the ADK `SequentialAgent` wrapping a `LoopAgent` (`max_iterations`) + Synthesizer.

    Requires optional `google-adk` + `mcp` (`uv sync --extra adk`).
    All MCP tools are strictly read-only / plan-only; live `--post` spends stay on the droplet.
    """
    if mode not in MODES:
        raise ValueError(f"unknown debate mode {mode!r}; choose from {MODES}")
    if type(max_iterations) is not int or not 1 <= max_iterations <= MAX_ROUNDS:
        raise ValueError(f"max_iterations must be an int between 1 and {MAX_ROUNDS}")

    try:
        from google.adk.agents import LlmAgent, LoopAgent, SequentialAgent
        from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
        from google.adk.tools.mcp_tool.mcp_toolset import McpToolset
        from mcp import StdioServerParameters
    except ImportError as e:
        raise RuntimeError(
            "Optional ADK dependencies are not installed. Run `uv sync --extra adk` on your "
            "dev machine or GEAR sandbox (keep the 961 MiB droplet minimal)."
        ) from e

    sink = incidents_path or Path("data/incidents.jsonl")

    def _before_cb(tool, args, tool_context):
        return before_tool_guard(getattr(tool, "name", str(tool)), args or {})

    def _after_cb(tool, args, tool_context, tool_response):
        after_tool_incident_logger(getattr(tool, "name", str(tool)), tool_response, sink)

    local_mcp = McpToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command="uv",
                args=["run", "python", "-m", "flopstar.flop.mcp_server"],
            )
        )
    )

    if mode == "decision":
        proposer_name, critic_name, synth_name = (
            "RedBlueAdvocate",
            "RedBlueSkeptic",
            "DecisionSynthesizer",
        )
        proposer_instr = (
            "You are the ADVOCATE. Build or refine a concrete, evidence-backed thesis and "
            "action plan for the user's decision topic. Address any objections in {critique?}."
        )
        critic_instr = (
            "You are the SKEPTIC running a pre-mortem on {proposal}. Attack unstated "
            "assumptions, security/economic failure modes, and operational gaps. If all material "
            "risks are addressed and further rounds add no value, call `exit_loop(reason=...)`."
        )
        synth_instr = (
            "Synthesize {proposal} and {critique} into an executive Decision Memo (recommendation, "
            "confidence, supporting case, unresolved risks, falsifiable assumptions, next 3 steps). "
            "Optionally call `flop_plan_debate` to preview a FLOP-paid run (dry-run only)."
        )
    else:
        proposer_name, critic_name, synth_name = (
            "GearExamAuthor",
            "GearAdversarialReviewer",
            "GearStudySynthesizer",
        )
        proposer_instr = (
            "You are the Google Cloud Professional Agentic Architect (GEAR) exam author. Draft "
            "or refine 3 hard scenario MCQs (ADK workflows, MCP/A2A, callbacks, evals, Cloud Run) "
            "plus 1 hands-on debugging lab drill. Address all feedback in {critique?}."
        )
        critic_instr = (
            "You are the adversarial technical reviewer auditing {proposal}. Flag any inaccurate "
            "ADK APIs, weak distractors, or security anti-patterns. When every item is exam-ready, "
            "call `exit_loop(reason=...)`."
        )
        synth_instr = (
            "Compile {proposal} and {critique} into a clean Markdown GEAR Study Drill & Answer "
            "Key with rubric explanations. Optionally call `flop_plan_debate` to preview a "
            "FLOP-paid run (dry-run only)."
        )

    proposer = LlmAgent(
        name=proposer_name,
        model=model,
        description=f"Proposer/Author for Engine 2 ({mode}).",
        instruction=proposer_instr,
        before_tool_callback=_before_cb,
        after_tool_callback=_after_cb,
        output_key="proposal",
    )

    critic = LlmAgent(
        name=critic_name,
        model=model,
        description=f"Skeptic/Reviewer for Engine 2 ({mode}) with exit_loop convergence control.",
        instruction=critic_instr,
        tools=[exit_loop],
        before_tool_callback=_before_cb,
        after_tool_callback=_after_cb,
        output_key="critique",
    )

    debate_loop = LoopAgent(
        name="DebateRefinementLoop",
        sub_agents=[proposer, critic],
        max_iterations=max_iterations,
    )

    synthesizer = LlmAgent(
        name=synth_name,
        model=model,
        description=f"Final synthesizer for Engine 2 ({mode}).",
        instruction=synth_instr,
        tools=[local_mcp],
        before_tool_callback=_before_cb,
        after_tool_callback=_after_cb,
        output_key="final_memo",
    )

    return SequentialAgent(
        name=f"Engine2_{mode.replace('-', '_')}_Pipeline",
        description=f"Engine 2 LoopAgent + Synthesizer pipeline ({mode}).",
        sub_agents=[debate_loop, synthesizer],
    )


__all__ = ["build_debate_agent", "exit_loop"]
