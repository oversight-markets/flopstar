"""
Optional Google ADK `SequentialAgent` wrapper for Engine 3
(FLOP Living Changelog & Doc-vs-Code Watchtower).

Demonstrates Google GEAR / Professional Agentic Architect patterns:
- `SequentialAgent` (`WatchtowerDiffCollector` -> `ContradictionHunter` -> `BuilderDigestWriter`)
- Zero-spend gating (`should_skip_inference`) when no protocol/package/catalog deltas exist
- Shared `before_tool_callback` (`before_tool_guard`) and `after_tool_callback`
  (`after_tool_incident_logger`)
- Read-only / plan-only `McpToolset` (`flopstar-mcp`)

Imports `google.adk` lazily inside `build_watchtower_agent()` so the 961 MiB droplet stays lean.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .adk_auditor import after_tool_incident_logger, before_tool_guard


def should_skip_inference(diff_payload: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
    """Deterministic zero-spend gate: skip paid analysis when no ecosystem delta exists."""
    if force:
        return {"skip": False, "reason": "forced run"}
    diff = diff_payload.get("diff") if isinstance(diff_payload.get("diff"), dict) else {}
    contradictions = diff_payload.get("contradictions") or []
    if diff.get("has_changes") is True:
        return {"skip": False, "reason": f"{len(diff.get('changes') or [])} snapshot delta(s)"}
    if contradictions:
        return {"skip": False, "reason": f"{len(contradictions)} doc-vs-code contradiction(s)"}
    if diff.get("is_baseline") is True:
        return {"skip": True, "reason": "baseline snapshot only; no prior snapshot to diff"}
    return {"skip": True, "reason": "no snapshot changes or contradictions detected"}


def build_watchtower_agent(
    *,
    model: str = "gemini-2.5-flash",
    incidents_path: Path | None = None,
):
    """
    Construct the ADK `SequentialAgent` pipeline for Engine 3 (Watchtower).

    Requires optional `google-adk` + `mcp` (`uv sync --extra adk`).
    All MCP tools are strictly read-only / plan-only; live `--post` spends stay on the droplet.
    """
    try:
        from google.adk.agents import LlmAgent, SequentialAgent
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

    collector = LlmAgent(
        name="WatchtowerDiffCollector",
        model=model,
        description="Calls read-only `flop_watchtower_diff` to inspect live FLOP deltas.",
        instruction=(
            "Call `flop_watchtower_diff` to collect the live snapshot delta and deterministic "
            "doc-vs-code contradictions. Return the concise JSON summary."
        ),
        tools=[local_mcp],
        before_tool_callback=_before_cb,
        after_tool_callback=_after_cb,
        output_key="watchtower_diff",
    )

    analyst = LlmAgent(
        name="ContradictionHunter",
        model=model,
        description="Analyzes breaking changes and drafts reproducible FLOP Labs issue reports.",
        instruction=(
            "Given {watchtower_diff}, if there are no changes and no contradictions, say "
            "'NO_CHANGES' concisely. Otherwise, produce: (1) a breaking-change migration "
            "checklist for Flopstar and (2) reproducible GitHub issue drafts for each verified "
            "doc-vs-code contradiction."
        ),
        before_tool_callback=_before_cb,
        after_tool_callback=_after_cb,
        output_key="watchtower_analysis",
    )

    publisher = LlmAgent(
        name="BuilderDigestWriter",
        model=model,
        description="Compiles the weekly FLOP Living Changelog digest for builders.",
        instruction=(
            "Given {watchtower_diff} and {watchtower_analysis}, if the analysis is 'NO_CHANGES', "
            "output a 2-line all-clear status. Otherwise, write a crisp, factual Markdown FLOP "
            "Living Changelog for agent builders. Never invent unverified claims or secrets."
        ),
        tools=[local_mcp],
        before_tool_callback=_before_cb,
        after_tool_callback=_after_cb,
        output_key="builder_digest",
    )

    return SequentialAgent(
        name="Engine3WatchtowerPipeline",
        description="Sequential Watchtower diff -> contradiction hunt -> builder digest pipeline.",
        sub_agents=[collector, analyst, publisher],
    )


__all__ = ["build_watchtower_agent", "should_skip_inference"]
