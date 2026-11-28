"""
Engine 1 — "Miner Truth Serum": blind cross-miner consistency & quantization auditor.

Sends the same parameterized, zero-repetition, deterministically verifiable long-context probe to
multiple independent miners serving the same model, verifies every untrusted reply against exact
ground truth (never executing model output), scores miner reliability, and logs reproducible
anomaly traces to `data/incidents.jsonl` (never containing secrets).

- Dry-run by default: `plan_audit()` previews the probe, selected independent miners, and exact
  escrow estimates without signing or spending.
- Live execution runs every inference turn strictly through `inference.execute()` -> `FlopPolicy`
  and reports every settled turn to Touchstone (`inference.report()`).
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import secrets
import statistics
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .amounts import format_flop
from .config import FlopConfig, ss58_pubkey
from .inference import DEFAULT_MAX_TOKENS, Result
from .ledger import Ledger
from .tasks import BANNER, assert_no_secrets

FAMILIES = ("precision-audit", "needle-haystack", "state-trace", "schema-extract")
DEFAULT_MINER_COUNT = 3
DEFAULT_CONTEXT_KB = 6
MAX_CONTEXT_KB = 48
LATENCY_OUTLIER_RATIO = 3.0
LATENCY_OUTLIER_MIN_MS = 5_000
_FENCED_JSON = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)


@dataclass(frozen=True)
class Probe:
    probe_id: str
    family: str
    seed: str
    prompt: str
    expected: dict[str, Any]
    context_bytes: int
    recommended_max_tokens: int = 512

    def describe(self) -> dict[str, Any]:
        return {
            "probe_id": self.probe_id,
            "family": self.family,
            "seed": self.seed,
            "prompt_bytes": len(self.prompt.encode("utf-8")),
            "context_bytes": self.context_bytes,
            "recommended_max_tokens": self.recommended_max_tokens,
            "prompt_sha256": hashlib.sha256(self.prompt.encode("utf-8")).hexdigest(),
        }


@dataclass(frozen=True)
class Verification:
    passed: bool
    schema_valid: bool
    score: float  # 0.0 .. 1.0
    parsed: dict[str, Any] | None
    mismatches: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class MinerRun:
    miner: str
    result: Result
    verification: Verification


@dataclass(frozen=True)
class BatchAnalysis:
    batch_id: str
    checked_at: str
    network: str
    model: str
    probe: dict[str, Any]
    runs: list[dict[str, Any]]
    anomalies: list[dict[str, Any]]
    summary: dict[str, Any]


# ---- deterministic probe generators -------------------------------------------------------------

def _filler_line(rng: random.Random, idx: int) -> str:
    states = ("Open", "Settling", "Closed", "PendingAck")
    models = ("qwen3-8b", "qwen3-14b", "qwen3-30b-a3b-fp8", "qwen2.5-14b-instruct")
    return (
        f"[rec#{idx:04d}] blk={180_000 + idx} mdl={rng.choice(models)} "
        f"st={rng.choice(states)} gn={rng.randint(120, 9_800)} "
        f"lat_ms={rng.randint(210, 2_400)} seq={rng.randint(10**8, 10**9 - 1)}"
    )


def _gen_precision_audit(rng: random.Random, seed: str, target_bytes: int) -> Probe:
    """18-decimal integer settlement ledger with 2 planted precision/fee bugs."""
    rows: list[str] = []
    # Build enough rows to approach target_bytes cleanly (~115 bytes/row).
    n_rows = max(12, min(320, target_bytes // 115))
    bug_indices = sorted(rng.sample(range(1, n_rows + 1), 2))
    bug_ids: list[str] = []
    total_delta = 0

    for i in range(1, n_rows + 1):
        row_id = f"CH-{i:04d}"
        escrow = rng.randint(50, 950) * 10**15  # 0.05 .. 0.95 FLOP
        audit_fee = escrow // 100  # exact 1% audit fee earmark
        miner_paid = escrow - audit_fee
        if i == bug_indices[0]:
            # Planted float-truncation discrepancy (like docs/FLOP.md §2.5)
            delta = rng.randint(111, 999)
            miner_paid -= delta
            total_delta += delta
            bug_ids.append(row_id)
        elif i == bug_indices[1]:
            # Planted fee miscalculation discrepancy
            delta = rng.randint(1_000, 9_999)
            audit_fee += delta
            total_delta += delta
            bug_ids.append(row_id)
        rows.append(
            f"{row_id} | escrow_raw={escrow} | miner_paid_raw={miner_paid} "
            f"| audit_fee_raw={audit_fee}"
        )

    corpus = "\n".join(rows)
    prompt = (
        "You are auditing an exact 18-decimal integer settlement ledger. "
        "For every valid row, `audit_fee_raw == escrow_raw // 100` and "
        "`miner_paid_raw + audit_fee_raw == escrow_raw`. Exactly two rows violate "
        "these invariants.\n\n"
        "Return ONLY a single JSON object (no extra prose) with exact keys:\n"
        '  {"bug_rows": ["CH-XXXX", "CH-YYYY"], "total_abs_discrepancy_raw": "<int as string>"}\n'
        "where `bug_rows` is sorted lexicographically and `total_abs_discrepancy_raw` is the "
        "exact sum of `abs(escrow_raw - (miner_paid_raw + audit_fee_raw))` across the buggy "
        "rows.\n\n"
        f"<ledger>\n{corpus}\n</ledger>"
    )
    expected = {
        "bug_rows": sorted(bug_ids),
        "total_abs_discrepancy_raw": str(total_delta),
    }
    pid = f"precision-audit-{hashlib.sha256(seed.encode()).hexdigest()[:10]}"
    return Probe(pid, "precision-audit", seed, prompt, expected, len(corpus.encode("utf-8")), 256)


def _gen_needle_haystack(rng: random.Random, seed: str, target_bytes: int) -> Probe:
    """Multi-KB telemetry haystack with 3 buried structured checkpoint needles."""
    n_lines = max(30, min(500, target_bytes // 95))
    anchor_code = f"ANC-{rng.randint(1000, 9999)}-{rng.choice(['KILO', 'NOVA', 'VEX', 'ZETA'])}"
    fee_bps = rng.randint(15, 250)
    block_height = rng.randint(250_000, 950_000)

    pos = [max(1, int(n_lines * 0.15)), max(2, int(n_lines * 0.52)), max(3, int(n_lines * 0.88))]
    lines = [_filler_line(rng, i) for i in range(1, n_lines + 1)]
    lines[pos[0]] = f"[CHECKPOINT-A] verified anchor_code={anchor_code} status=ratified"
    lines[pos[1]] = f"[CHECKPOINT-B] verified protocol_fee_bps={fee_bps} source=onchain"
    lines[pos[2]] = f"[CHECKPOINT-C] verified voucher_block_height={block_height} finality=hard"

    corpus = "\n".join(lines)
    prompt = (
        "Read the telemetry log below and extract the three `[CHECKPOINT-*]` values.\n"
        "Return ONLY a single JSON object with exact keys:\n"
        '  {"anchor_code": "<str>", "protocol_fee_bps": <int>, '
        '"voucher_block_height": <int>, "checksum_sum": <int>}\n'
        "where `checksum_sum` equals `protocol_fee_bps + voucher_block_height`.\n\n"
        f"<telemetry>\n{corpus}\n</telemetry>"
    )
    expected = {
        "anchor_code": anchor_code,
        "protocol_fee_bps": fee_bps,
        "voucher_block_height": block_height,
        "checksum_sum": fee_bps + block_height,
    }
    pid = f"needle-haystack-{hashlib.sha256(seed.encode()).hexdigest()[:10]}"
    return Probe(pid, "needle-haystack", seed, prompt, expected, len(corpus.encode("utf-8")), 256)


def _gen_state_trace(rng: random.Random, seed: str, target_bytes: int) -> Probe:
    """Deterministic escrow channel state-machine trace with integer accounting."""
    initial = rng.randint(50_000, 200_000)
    escrow = initial
    settled_turns = 0
    state = "PendingAck"
    events: list[str] = [f"00. EVT_INIT escrow_units={initial} st=PendingAck"]
    step = 1

    # Always ack into Open, then perform randomized turn debits + reattestations + close.
    events.append(f"{step:02d}. EVT_ACK -> st=Open")
    state = "Open"
    step += 1

    n_turns = rng.randint(6, 11)
    for _ in range(n_turns):
        if rng.random() < 0.25:
            events.append(f"{step:02d}. EVT_REATTEST (noop)")
        else:
            debit = rng.randint(500, 4_500)
            if debit <= escrow:
                escrow -= debit
                settled_turns += 1
                events.append(f"{step:02d}. EVT_DEBIT amt={debit}")
            else:
                events.append(f"{step:02d}. EVT_REJECTED amt={debit} (insufficient_escrow)")
        step += 1

    final_action = rng.choice(("EVT_COOP_CLOSE", "EVT_TIMEOUT_REFUND"))
    if final_action == "EVT_COOP_CLOSE":
        state = "Closed"
        events.append(f"{step:02d}. EVT_COOP_CLOSE -> st=Closed")
    else:
        state = "Refunded"
        events.append(f"{step:02d}. EVT_TIMEOUT_REFUND -> st=Refunded")

    # Pad with clearly demarcated background archive notes to hit desired context size.
    n_bg = max(0, (target_bytes - sum(len(e) for e in events)) // 95)
    bg = "\n".join(_filler_line(rng, i) for i in range(1, n_bg + 1))
    trace_block = "\n".join(events)
    corpus = (f"<archive_noise>\n{bg}\n</archive_noise>\n\n" if bg else "") + (
        f"<channel_trace>\n{trace_block}\n</channel_trace>"
    )
    prompt = (
        "Trace ONLY the `<channel_trace>` events (ignore `<archive_noise>`). "
        "Start from `EVT_INIT`. Each `EVT_DEBIT` subtracts `amt` from `escrow_units` and "
        "increments `settled_turns` by 1. `EVT_REATTEST` and `EVT_REJECTED` leave both "
        "unchanged.\n"
        "Return ONLY a single JSON object with exact keys:\n"
        '  {"escrow_remaining": <int>, "settled_turns": <int>, "final_state": "<str>"}\n\n'
        f"{corpus}"
    )
    expected = {
        "escrow_remaining": escrow,
        "settled_turns": settled_turns,
        "final_state": state,
    }
    pid = f"state-trace-{hashlib.sha256(seed.encode()).hexdigest()[:10]}"
    return Probe(pid, "state-trace", seed, prompt, expected, len(corpus.encode("utf-8")), 256)


def _gen_schema_extract(rng: random.Random, seed: str, target_bytes: int) -> Probe:
    """Extract exact normalized records from noisy prose into strict sorted JSON."""
    regions = ("us-east", "eu-central", "ap-south", "us-west")
    n_records = rng.randint(4, 6)
    expected_records = []
    paragraphs: list[str] = []

    for i in range(1, n_records + 1):
        mid = f"M-{rng.randint(100, 999)}-{i}"
        region = rng.choice(regions)
        gpus = rng.choice((1, 2, 4, 8))
        active = rng.choice((True, False))
        expected_records.append({"miner_id": mid, "region": region, "gpus": gpus, "active": active})
        status_word = "ACTIVE" if active else "PAUSED"
        paragraphs.append(
            f"Operator note for {mid}: deployed in region {region} with {gpus} H100-class "
            f"accelerators; current serving status is {status_word}."
        )

    n_noise = max(10, (target_bytes - sum(len(p) for p in paragraphs)) // 95)
    noise = [_filler_line(rng, i) for i in range(1, n_noise + 1)]
    mixed = noise[:]
    for idx, para in enumerate(paragraphs):
        insert_at = min(len(mixed), int((idx + 1) * len(mixed) / (len(paragraphs) + 1)))
        mixed.insert(insert_at, para)
    corpus = "\n".join(mixed)

    prompt = (
        "Extract every `Operator note for M-...` from `<notes>` into strict JSON.\n"
        "Return ONLY a single JSON object with exact key `miners` containing a list sorted "
        "ascending by `miner_id`, where each item has exact keys "
        '`{"miner_id": str, "region": str, "gpus": int, "active": bool}` '
        "(ACTIVE -> true, PAUSED -> false).\n\n"
        f"<notes>\n{corpus}\n</notes>"
    )
    expected = {"miners": sorted(expected_records, key=lambda r: r["miner_id"])}
    pid = f"schema-extract-{hashlib.sha256(seed.encode()).hexdigest()[:10]}"
    return Probe(pid, "schema-extract", seed, prompt, expected, len(corpus.encode("utf-8")), 512)


def generate_probe(
    family: str = "precision-audit",
    *,
    seed: str | None = None,
    context_kb: int = DEFAULT_CONTEXT_KB,
) -> Probe:
    """Create a fresh, deterministic-given-seed, zero-repetition audit probe."""
    if family not in FAMILIES:
        raise ValueError(f"unknown probe family {family!r}; choose from {FAMILIES}")
    if type(context_kb) is not int or not 1 <= context_kb <= MAX_CONTEXT_KB:
        raise ValueError(f"context_kb must be an int between 1 and {MAX_CONTEXT_KB}")
    resolved_seed = seed if seed is not None else secrets.token_hex(8)
    rng = random.Random(f"flopstar-truth-serum:{family}:{resolved_seed}")
    target_bytes = context_kb * 1024
    generators = {
        "precision-audit": _gen_precision_audit,
        "needle-haystack": _gen_needle_haystack,
        "state-trace": _gen_state_trace,
        "schema-extract": _gen_schema_extract,
    }
    probe = generators[family](rng, resolved_seed, target_bytes)
    assert_no_secrets(probe.prompt)
    return probe


# ---- deterministic verification (untrusted reply -> exact comparison) ---------------------------

def _extract_json_object(reply: str) -> dict[str, Any] | None:
    """Extract a JSON dict from untrusted model output without ever executing code."""
    text = reply.strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except ValueError:
        pass
    matches = _FENCED_JSON.findall(text)
    for candidate in reversed(matches):
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except ValueError:
            continue
    decoder = json.JSONDecoder()
    last_obj: dict[str, Any] | None = None
    for i, ch in enumerate(text):
        if ch == "{":
            try:
                obj, _ = decoder.raw_decode(text, i)
            except ValueError:
                continue
            if isinstance(obj, dict):
                last_obj = obj
    return last_obj


def verify_reply(probe: Probe, reply: str | None) -> Verification:
    """Deterministically verify an untrusted reply against `probe.expected`."""
    if not reply or not reply.strip():
        return Verification(False, False, 0.0, None, ["empty reply"])
    parsed = _extract_json_object(reply)
    if parsed is None:
        return Verification(False, False, 0.0, None, ["reply did not contain a valid JSON object"])

    expected = probe.expected
    mismatches: list[str] = []
    matched_keys = 0
    for key, want in expected.items():
        if key not in parsed:
            mismatches.append(f"missing key {key!r}")
            continue
        got = parsed[key]
        if type(got) is not type(want) or got != want:
            mismatches.append(f"{key}: expected {want!r}, got {got!r}")
        else:
            matched_keys += 1
    extra = sorted(set(parsed) - set(expected))
    if extra:
        mismatches.append(f"unexpected extra keys: {extra}")

    score = matched_keys / len(expected) if expected else 1.0
    passed = len(mismatches) == 0
    return Verification(
        passed=passed,
        schema_valid=True,
        score=round(score, 4),
        parsed=parsed,
        mismatches=mismatches,
    )


# ---- miner selection & batch analysis -----------------------------------------------------------

def select_audit_miners(
    cfg: FlopConfig,
    ledger: Ledger,
    count: int = DEFAULT_MINER_COUNT,
    requested: list[str] | None = None,
) -> list[str]:
    """Choose up to `count` independent, non-blocklisted configured miners (LRU order)."""
    if type(count) is not int or count < 1:
        raise ValueError("miner count must be >= 1")
    eligible = [m for m in cfg.miners if not cfg.caps.is_blocked_miner(m)]
    if requested:
        picked: list[str] = []
        seen_pubkeys: set[str] = set()
        for req in requested:
            if cfg.caps.is_blocked_miner(req):
                raise ValueError(f"miner {req} is on the same_control_miners blocklist")
            pk = ss58_pubkey(req)
            if pk in seen_pubkeys:
                continue
            match = next((m for m in eligible if ss58_pubkey(m) == pk), None)
            if match is None:
                raise ValueError(f"miner {req} has no endpoint in [{cfg.network.name}.miners]")
            seen_pubkeys.add(pk)
            picked.append(match)
        return picked[:count]
    last = ledger.last_used_miners(cfg.network.name)
    ordered = sorted(eligible, key=lambda m: (last.get(m, ""), m))
    return ordered[:count]


def analyze_batch(
    cfg: FlopConfig,
    model: str,
    probe: Probe,
    runs: list[MinerRun],
    *,
    now: datetime | None = None,
) -> BatchAnalysis:
    """Compare cross-miner results, detect cheating/divergence/faults, and build incident rows."""
    ts = (now or datetime.now(UTC)).isoformat(timespec="seconds")
    batch_id = f"audit-{probe.probe_id}"
    anomalies: list[dict[str, Any]] = []
    serialized_runs: list[dict[str, Any]] = []

    settled_latencies = [
        r.result.timings["total_ms"]
        for r in runs
        if r.result.status == "settled" and isinstance(r.result.timings.get("total_ms"), int)
    ]
    median_ms = statistics.median(settled_latencies) if settled_latencies else None
    passed_miners = [r.miner for r in runs if r.result.status == "settled" and r.verification.passed]

    for r in runs:
        res, ver = r.result, r.verification
        total_ms = res.timings.get("total_ms")
        run_view = {
            "miner": r.miner,
            "spend_id": res.spend_id,
            "status": res.status,
            "route": res.route,
            "verification": res.verification,
            "channel_id": res.channel_id,
            "settle_tx": res.settle_tx,
            "max_flop": format_flop(res.max_flop),
            "total_ms": total_ms,
            "needs_recovery": res.needs_recovery,
            "touchstone": res.touchstone,
            "error": res.error,
            "ground_truth_passed": ver.passed,
            "ground_truth_score": ver.score,
            "mismatches": ver.mismatches,
            "request_sha256": hashlib.sha256(res.request_bytes).hexdigest()
            if res.request_bytes else None,
            "response_sha256": hashlib.sha256(res.response_bytes).hexdigest()
            if res.response_bytes else None,
        }
        serialized_runs.append(run_view)

        base_inc = {
            "recorded_at": ts,
            "batch_id": batch_id,
            "network": cfg.network.name,
            "model": model,
            "probe_id": probe.probe_id,
            "probe_family": probe.family,
            "miner": r.miner,
            "spend_id": res.spend_id,
            "channel_id": res.channel_id,
            "settle_tx": res.settle_tx,
            "request_sha256": run_view["request_sha256"],
            "response_sha256": run_view["response_sha256"],
        }

        if res.status != "settled" or res.needs_recovery or (
            res.route == "direct" and res.verification != "sdk_verified"
        ):
            anomalies.append({
                **base_inc,
                "kind": "VERIFICATION_FAULT",
                "detail": res.error or f"status={res.status} verification={res.verification}",
                "needs_recovery": res.needs_recovery,
            })
        elif passed_miners and not ver.passed:
            anomalies.append({
                **base_inc,
                "kind": "ACCURACY_DIVERGENCE",
                "detail": (
                    f"failed deterministic ground truth while {len(passed_miners)}/{len(runs)} "
                    f"peer miners on {model} passed: {'; '.join(ver.mismatches[:3])}"
                ),
                "peer_pass_count": len(passed_miners),
                "score": ver.score,
            })

        if (
            median_ms is not None
            and len(settled_latencies) >= 3
            and isinstance(total_ms, int)
            and total_ms >= LATENCY_OUTLIER_MIN_MS
            and total_ms > median_ms * LATENCY_OUTLIER_RATIO
        ):
            anomalies.append({
                **base_inc,
                "kind": "LATENCY_OUTLIER",
                "detail": f"latency {total_ms}ms exceeds {LATENCY_OUTLIER_RATIO}x median "
                          f"({int(median_ms)}ms)",
                "total_ms": total_ms,
                "median_ms": int(median_ms),
            })

    if runs and not passed_miners and all(r.result.status == "settled" for r in runs):
        anomalies.append({
            "recorded_at": ts,
            "batch_id": batch_id,
            "network": cfg.network.name,
            "model": model,
            "probe_id": probe.probe_id,
            "probe_family": probe.family,
            "miner": None,
            "kind": "ALL_FAILED",
            "detail": f"0/{len(runs)} settled miners passed ground truth on {model}",
        })

    summary = {
        "miners_tested": len(runs),
        "settled": sum(1 for r in runs if r.result.status == "settled"),
        "passed_ground_truth": len(passed_miners),
        "anomaly_count": len(anomalies),
        "median_latency_ms": int(median_ms) if median_ms is not None else None,
    }
    return BatchAnalysis(
        batch_id=batch_id,
        checked_at=ts,
        network=cfg.network.name,
        model=model,
        probe=probe.describe(),
        runs=serialized_runs,
        anomalies=anomalies,
        summary=summary,
    )


# ---- persistence (incidents.jsonl + human-readable audit report) --------------------------------

def record_incidents(cfg: FlopConfig, analysis: BatchAnalysis) -> Path | None:
    """Append anomaly traces to `data/incidents.jsonl` (public metadata + hashes only)."""
    if not analysis.anomalies:
        return None
    path = cfg.data_dir / "incidents.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for inc in analysis.anomalies:
            f.write(json.dumps(inc, sort_keys=True) + "\n")
    return path


def write_audit_report(cfg: FlopConfig, analysis: BatchAnalysis) -> Path:
    """Write a human-readable Markdown audit summary under `data/audits/`."""
    out = cfg.subdir("audits") / f"{analysis.batch_id}.md"
    banner = BANNER.format(route="audit", verification="deterministic+sdk", spend_id="batch")
    lines = [
        f"# Miner Truth Serum Audit: {analysis.batch_id}",
        "",
        banner.strip(),
        f"> Checked: {analysis.checked_at} | Network: {analysis.network} | Model: {analysis.model}",
        (
            f"> Probe: `{analysis.probe['family']}` ({analysis.probe['prompt_bytes']} bytes, "
            f"seed `{analysis.probe['seed']}`)"
        ),
        "",
        "## Summary",
        "",
        f"- Miners tested: **{analysis.summary['miners_tested']}**",
        f"- Settled: **{analysis.summary['settled']}**",
        f"- Passed deterministic ground truth: **{analysis.summary['passed_ground_truth']}**",
        f"- Anomalies flagged: **{analysis.summary['anomaly_count']}**",
        "",
        "## Per-Miner Results",
        "",
        "| Miner | Status | Verification | Ground Truth | Latency (ms) | Escrow | Channel |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in analysis.runs:
        gt = "PASS" if r["ground_truth_passed"] else f"FAIL ({r['ground_truth_score']})"
        lines.append(
            f"| `{r['miner']}` | {r['status']} | {r['verification'] or '-'} | {gt} "
            f"| {r['total_ms'] or '-'} | {r['max_flop']} | `{r['channel_id'] or '-'}` |"
        )
    if analysis.anomalies:
        lines.extend(["", "## Anomalies Recorded (`data/incidents.jsonl`)", ""])
        for a in analysis.anomalies:
            lines.append(f"- **{a['kind']}** (`{a.get('miner') or 'batch'}`): {a['detail']}")

    tmp = out.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, out)
    return out


def analysis_to_dict(analysis: BatchAnalysis) -> dict[str, Any]:
    return asdict(analysis)


__all__ = [
    "DEFAULT_CONTEXT_KB",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_MINER_COUNT",
    "FAMILIES",
    "BatchAnalysis",
    "MinerRun",
    "Probe",
    "Verification",
    "analysis_to_dict",
    "analyze_batch",
    "generate_probe",
    "record_incidents",
    "select_audit_miners",
    "verify_reply",
    "write_audit_report",
]
