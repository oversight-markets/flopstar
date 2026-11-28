"""
Optional Google ADK orchestration wrapper for Engine 1 ("Miner Truth Serum").

Designed to run in a dev / GEAR sandbox / Cloud Run environment (`uv sync --extra adk`) while
connecting ONLY to read-only / plan-only Flopstar tools (`mcp_server.py`) and FLOP's hosted
read-only MCP (`https://mcp.flop.network/mcp`, explicitly excluding `flop_generate_secrets`).

Demonstrates core Google GEAR / Professional Agentic Architect patterns:
- `ParallelAgent` (concurrent read-only scouting + multi-miner probe planning)
- `SequentialAgent` (Scout -> Plan -> Synthesize & Triage)
- `before_tool_callback` (secret guard + blocklist enforcement)
- `after_tool_callback` (structured incident-trace sink to `data/incidents.jsonl`)

Imports `google.adk` lazily inside `build_truth_serum_agent()` so the core 961 MiB droplet stays
lightweight and tests can verify the callbacks without installing `google-adk`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .tasks import SecretInInput, assert_no_secrets

HOSTED_FLOP_MCP_URL = "https://mcp.flop.network/mcp"
# Explicitly allowlisted read-only tools from mcp.flop.network (NEVER include flop_generate_secrets).
HOSTED_MCP_TOOL_ALLOWLIST = (
    "flop_list_session_offers",
    "flop_find_best_miner",
    "flop_get_compute_channel",
    "flop_get_inference_receipt",
)
FORBIDDEN_MCP_TOOLS = frozenset({"flop_generate_secrets"})


def before_tool_guard(tool_name: str, args: dict[str, Any]) -> dict[str, Any] | None:
    """
    ADK `before_tool_callback` defense-in-depth guard.

    Returns `None` to allow the tool call, or a refusal dict to short-circuit execution:
    - Refuses forbidden server-side secret generators (`flop_generate_secrets`).
    - Scans all string arguments with `tasks.assert_no_secrets()` so key material never leaves.
    """
    if tool_name in FORBIDDEN_MCP_TOOLS:
        return {"refused": True, "reason": f"tool {tool_name!r} is forbidden by Flopstar policy"}
    for key, val in args.items():
        if isinstance(val, str):
            try:
                assert_no_secrets(val)
            except SecretInInput as e:
                return {"refused": True, "reason": f"argument {key!r} refused: {e}"}
    return None


def after_tool_incident_logger(
    tool_name: str,
    response: Any,
    incidents_path: Path,
    *,
    now: datetime | None = None,
) -> None:
    """
    ADK `after_tool_callback` sink: appends structured incident traces when a tool reports
    recovery needs, runtime/CLI drift, or audit anomalies.
    """
    if not isinstance(response, dict):
        return
    flags: list[str] = []
    if response.get("needs_recovery") is True:
        flags.append("needs_recovery")
    for alert in response.get("alerts") or []:
        if isinstance(alert, str) and alert.startswith(
            ("RUNTIME_DRIFT:", "CLI_DRIFT:", "GENESIS_MISMATCH:", "S3_SIGNAL:")
        ):
            flags.append(alert)
    if response.get("anomalies"):
        flags.append(f"audit_anomalies:{len(response['anomalies'])}")
    if not flags:
        return
    record = {
        "recorded_at": (now or datetime.now(UTC)).isoformat(timespec="seconds"),
        "source": "adk_after_tool_callback",
        "tool": tool_name,
        "flags": flags,
        "network": response.get("network"),
    }
    incidents_path.parent.mkdir(parents=True, exist_ok=True)
    with incidents_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")


def build_truth_serum_agent(
    *,
    model: str = "gemini-2.5-flash",
    incidents_path: Path | None = None,
    include_hosted_mcp: bool = False,
):
    """
    Construct the ADK `SequentialAgent` + `ParallelAgent` workflow for Engine 1.

    Requires optional `google-adk` + `mcp` (`uv sync --extra adk`).
    All tools are strictly read-only / plan-only; live `--post` spends stay on the droplet vault.
    """
    try:
        from google.adk.agents import LlmAgent, ParallelAgent, SequentialAgent
        from google.adk.tools.mcp_tool.mcp_session_manager import (
            StdioConnectionParams,
            StreamableHTTPConnectionParams,
        )
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
    scout_tools = [local_mcp]
    if include_hosted_mcp:
        scout_tools.append(
            McpToolset(
                connection_params=StreamableHTTPConnectionParams(url=HOSTED_FLOP_MCP_URL),
                tool_filter=list(HOSTED_MCP_TOOL_ALLOWLIST),
            )
        )

    radar_agent = LlmAgent(
        name="FlopRadarScout",
        model=model,
        description="Runs read-only FLOP runtime, CLI, catalog, and faucet checks.",
        instruction=(
            "Call `flop_scout` and `flop_status` to gather live network state, compute density "
            "per 0.05 FLOP floor, and any RUNTIME_DRIFT / CLI_DRIFT / S3_SIGNAL alerts. "
            "Summarize concisely in JSON."
        ),
        tools=scout_tools,
        before_tool_callback=_before_cb,
        after_tool_callback=_after_cb,
        output_key="radar_report",
    )

    recovery_agent = LlmAgent(
        name="RecoveryWatcher",
        model=model,
        description="Checks whether any FLOP compute channels hold recoverable escrow.",
        instruction="Call `flop_check_recovery` and report any channels holding escrow.",
        tools=[local_mcp],
        before_tool_callback=_before_cb,
        after_tool_callback=_after_cb,
        output_key="recovery_report",
    )

    parallel_preflight = ParallelAgent(
        name="PreflightParallel",
        sub_agents=[radar_agent, recovery_agent],
    )

    audit_planner = LlmAgent(
        name="TruthSerumPlanner",
        model=model,
        description="Plans a zero-repetition cross-miner Truth Serum audit batch (dry-run only).",
        instruction=(
            "Given {radar_report} and {recovery_report}, call `flop_plan_audit` with the best "
            "allowed high-density model (preferring qwen/qwen3-14b if allowed, otherwise the "
            "first allowed model) and report the planned probe, miners, total escrow, and any "
            "blockers. Never ask for or output secrets."
        ),
        tools=[local_mcp],
        before_tool_callback=_before_cb,
        after_tool_callback=_after_cb,
        output_key="audit_plan",
    )

    return SequentialAgent(
        name="MinerTruthSerumPipeline",
        description="Read-only preflight + dry-run cross-miner Truth Serum audit planner.",
        sub_agents=[parallel_preflight, audit_planner],
    )
