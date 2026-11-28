"""
Read-only FLOP Network drift and opportunity radar (`flopstar flop scout`).

Checks four public sources without touching keys, signing anything, or tripping the kill switch:
1. Chain RPC (`state_getRuntimeVersion`, `chain_getBlockHash(0)`) — catches runtime spec-version
   drift even when `flop-sdk` rejects the new runtime, and checks the pinned genesis.
2. FLOP release manifest (`https://flop.network/releases.json`) — catches CLI image updates.
3. Public model catalog (`GET {gateway}/models`) — reports live models, per-miner FLOP prices,
   exact `tokens_per_min_escrow` compute density, and flags if any price source starts advertising
   an endpoint URL (S3 unblock signal).
4. Faucet limits (`GET {faucet}/limits` and `/chain`) — reports status, rollover rules, and daily
   cap headroom.
"""

from datetime import UTC, datetime
from typing import Any

import flop_sdk
import httpx
from flop_sdk.client import FLOPError, assert_supported_runtime

from .amounts import format_flop
from .config import FlopConfig
from .estimate import EstimateUnavailable, _live_sources, price_from_catalog
from .policy import FlopPolicy
from .wallet import spend_address

RELEASES_URL = "https://flop.network/releases.json"
SCOUT_TIMEOUT_S = 15
_ENDPOINT_KEYS = ("endpoint", "session_endpoint", "url", "http_endpoint", "https_endpoint")
_TEN_FLOP = 10 * 10**18


def _json_get(client: httpx.Client, url: str) -> tuple[Any | None, bytes, str | None]:
    try:
        r = client.get(url)
    except httpx.HTTPError as e:
        return None, b"", f"{type(e).__name__}: {e}"
    if not r.is_success:
        return None, r.content, f"HTTP {r.status_code}"
    try:
        return r.json(), r.content, None
    except ValueError as e:
        return None, r.content, f"non-JSON: {e}"


def _rpc_call(client: httpx.Client, url: str, method: str, params: list) -> tuple[Any | None, str | None]:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    try:
        r = client.post(url, json=payload)
    except httpx.HTTPError as e:
        return None, f"{type(e).__name__}: {e}"
    if not r.is_success:
        return None, f"HTTP {r.status_code}"
    try:
        data = r.json()
    except ValueError as e:
        return None, f"non-JSON: {e}"
    if "error" in data:
        return None, f"RPC error: {data['error']}"
    return data.get("result"), None


def _probe_runtime(client: httpx.Client, cfg: FlopConfig) -> tuple[dict, list[str]]:
    alerts: list[str] = []
    url = cfg.network.rpc_http_url
    if not url:
        return {"status": "unconfigured"}, ["RPC_UNCONFIGURED: rpc_http_url is not set"]
    rt, err = _rpc_call(client, url, "state_getRuntimeVersion", [])
    if err or not isinstance(rt, dict):
        return {"status": "error", "error": err or "unexpected response"}, [
            f"RPC_UNAVAILABLE: runtime version check failed ({err})"
        ]
    spec = rt.get("specVersion")
    spec_name = rt.get("specName")
    sdk_supported, sdk_error = True, None
    try:
        assert_supported_runtime(rt)
    except FLOPError as e:
        sdk_supported, sdk_error = False, str(e)
        alerts.append(
            f"RUNTIME_DRIFT: live spec {spec} ({spec_name}) unsupported by "
            f"flop-sdk=={flop_sdk.__version__}; re-pin flop-sdk"
        )
    genesis, g_err = _rpc_call(client, url, "chain_getBlockHash", [0])
    pinned = cfg.network.genesis_hash
    genesis_matches = bool(
        isinstance(genesis, str) and pinned and genesis.lower() == pinned.lower()
    )
    if g_err:
        alerts.append(f"RPC_UNAVAILABLE: genesis check failed ({g_err})")
    elif not genesis_matches:
        alerts.append(f"GENESIS_MISMATCH: chain={genesis} pinned={pinned}")
    return {
        "status": "ok",
        "spec_name": spec_name,
        "spec_version": spec,
        "sdk_version": flop_sdk.__version__,
        "sdk_supported": sdk_supported,
        "sdk_error": sdk_error,
        "genesis": genesis,
        "genesis_matches_pin": genesis_matches,
    }, alerts


def _extract_cli_image(manifest: Any) -> tuple[str | None, str | None]:
    """Pull CLI image reference and version from releases.json across common manifest layouts."""
    if not isinstance(manifest, dict):
        return None, None
    cli_obj = manifest.get("cli")
    if isinstance(cli_obj, dict):
        img = cli_obj.get("image") or cli_obj.get("docker_image") or cli_obj.get("dockerImage")
        ver = cli_obj.get("version") or cli_obj.get("latest")
        return (str(img) if img else None), (str(ver) if ver else None)
    images = manifest.get("images")
    if isinstance(images, dict) and isinstance(images.get("cli"), str):
        return images["cli"], str(manifest.get("version") or "") or None
    if isinstance(manifest.get("cli_image"), str):
        return manifest["cli_image"], str(manifest.get("cli_version") or "") or None
    return None, None


def _probe_releases(client: httpx.Client, cfg: FlopConfig,
                    releases_url: str) -> tuple[dict, list[str]]:
    alerts: list[str] = []
    data, _, err = _json_get(client, releases_url)
    if err:
        return {"status": "error", "error": err, "pinned": cfg.network.cli_image}, []
    published_img, published_ver = _extract_cli_image(data)
    pinned = cfg.network.cli_image
    drift = bool(published_img and pinned and published_img.strip() != pinned.strip())
    if drift:
        alerts.append(
            f"CLI_DRIFT: releases.json names {published_img} (pinned {pinned}); inspect & re-pin"
        )
    return {
        "status": "ok",
        "pinned": pinned,
        "published_image": published_img,
        "published_version": published_ver,
        "cli_drift": drift,
    }, alerts


def _probe_catalog(client: httpx.Client, cfg: FlopConfig,
                   now: datetime) -> tuple[dict, list[str]]:
    alerts: list[str] = []
    url = cfg.network.gateway_url
    if not url:
        return {"status": "unconfigured"}, []
    data, raw_body, err = _json_get(client, f"{url}/models")
    if err or not isinstance(data, dict):
        return {"status": "error", "error": err or "non-object response"}, [
            f"CATALOG_UNAVAILABLE: {url}/models ({err})"
        ]
    rows = data.get("data")
    if not isinstance(rows, list):
        return {"status": "error", "error": "catalog missing data list"}, [
            "CATALOG_UNAVAILABLE: catalog missing data list"
        ]
    if not rows:
        alerts.append("CATALOG_EMPTY: /models returned 0 models (transient on testnet; retry)")
    min_escrow = cfg.network.min_escrow
    models_out: list[dict] = []
    endpoint_signals: list[str] = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            continue
        model_id = row["id"]
        live = _live_sources(row, None, now)
        has_endpoint = any(
            any(k in s and s[k] for k in _ENDPOINT_KEYS)
            for s in live
        )
        if has_endpoint:
            endpoint_signals.append(model_id)
        try:
            raw_price = price_from_catalog(raw_body, model_id, None, now=now)
            price_str = format_flop(raw_price)
            tokens_per_floor = (min_escrow // raw_price) if (min_escrow and raw_price > 0) else None
        except EstimateUnavailable:
            price_str, tokens_per_floor = None, None
        models_out.append({
            "id": model_id,
            "allowed": model_id in cfg.caps.allowed_models,
            "live_miners": len(live),
            "price_per_token": price_str,
            "tokens_per_min_escrow": tokens_per_floor,
            "endpoint_signal": has_endpoint,
        })
    if endpoint_signals:
        alerts.append(
            f"S3_SIGNAL: catalog price sources include endpoint fields for {endpoint_signals}!"
        )
    return {
        "status": "ok",
        "model_count": len(models_out),
        "min_escrow": format_flop(min_escrow) if min_escrow is not None else None,
        "models": models_out,
    }, alerts


def _probe_faucet(client: httpx.Client, cfg: FlopConfig) -> tuple[dict, list[str]]:
    alerts: list[str] = []
    url = cfg.network.faucet_url
    if not url:
        return {"status": "unconfigured"}, []
    limits, _, err = _json_get(client, f"{url}/limits")
    if err or not isinstance(limits, dict):
        return {"status": "error", "error": err or "non-object"}, [
            f"FAUCET_UNAVAILABLE: {url}/limits ({err})"
        ]
    faucet_info = limits.get("faucet") if isinstance(limits.get("faucet"), dict) else {}
    agent_info = limits.get("agent") if isinstance(limits.get("agent"), dict) else {}
    status = faucet_info.get("status") or limits.get("status")
    if status and status != "open":
        alerts.append(f"FAUCET_STATUS: faucet reports status={status!r}")
    rollover = agent_info.get("rolloverRequiresSpend")
    if rollover is True:
        # Informational: only alert if local spend is 0 today while balance might be waiting.
        pass
    return {
        "status": "ok",
        "faucet_status": status,
        "rollover_requires_spend": rollover,
        "agent_grant": agent_info.get("grantFlop") or agent_info.get("grant"),
        "agent_cooldown_seconds": agent_info.get("cooldownSeconds"),
        "daily_budget_used": faucet_info.get("dailyUsedFlop") or limits.get("dailyUsed"),
        "daily_budget_cap": faucet_info.get("dailyCapFlop") or limits.get("dailyCap"),
        "pending_grants": faucet_info.get("pendingGrants") or limits.get("pendingGrants"),
    }, alerts


def _probe_gateway_enrollment(client: httpx.Client, cfg: FlopConfig) -> tuple[dict, list[str]]:
    """Read-only GET {gateway_url} checking whether `wallet_enrollment` is enabled (S4)."""
    alerts: list[str] = []
    url = cfg.network.gateway_url
    if not url:
        return {"status": "unconfigured", "enabled": False}, []
    data, _, err = _json_get(client, url)
    if err or not isinstance(data, dict):
        return {"status": "error", "enabled": False, "error": err or "non-object"}, []
    expected = f"{url.rstrip('/')}/enroll/wallet/challenge"
    advertised = data.get("wallet_enrollment")
    enabled = isinstance(advertised, str) and advertised == expected
    has_key = bool(cfg.gateway_key_file and cfg.gateway_key_file.exists())
    if enabled and not has_key:
        alerts.append(
            f"S4_SIGNAL: {cfg.network.name} gateway wallet_enrollment is ENABLED ({advertised}); "
            "run `flopstar flop gateway enroll` (dry run first)"
        )
    return {
        "status": "ok",
        "enabled": enabled,
        "wallet_enrollment_url": advertised if isinstance(advertised, str) else None,
        "local_gateway_key_present": has_key,
    }, alerts


def scout(cfg: FlopConfig, policy: FlopPolicy, *,
          transport: httpx.BaseTransport | None = None,
          now: datetime | None = None,
          releases_url: str = RELEASES_URL) -> dict:
    """Run all read-only FLOP Network probes and return a structured report with alerts."""
    now = now or datetime.now(UTC)
    alerts: list[str] = []
    kill = policy.kill_reason()
    if kill is not None:
        alerts.append(f"KILL_SWITCH: {cfg.kill_path} is tripped ({kill})")
    if not cfg.miners:
        alerts.append(
            f"S3_BLOCKED: [{cfg.network.name}.miners] is empty; no direct miner endpoint configured"
        )
    if cfg.network.name == "testnet" and cfg.caps.max_flop_per_day < _TEN_FLOP:
        alerts.append(
            f"CAP_NOTE: max_flop_per_day is {format_flop(cfg.caps.max_flop_per_day)} FLOP "
            "(below testnet's 10 FLOP daily agent grant; raise once scored phase & S3 open)"
        )

    with httpx.Client(transport=transport, timeout=SCOUT_TIMEOUT_S,
                      follow_redirects=False) as client:
        rt_view, rt_alerts = _probe_runtime(client, cfg)
        rel_view, rel_alerts = _probe_releases(client, cfg, releases_url)
        cat_view, cat_alerts = _probe_catalog(client, cfg, now)
        gw_view, gw_alerts = _probe_gateway_enrollment(client, cfg)
        fau_view, fau_alerts = _probe_faucet(client, cfg)

    alerts.extend(rt_alerts)
    alerts.extend(rel_alerts)
    alerts.extend(cat_alerts)
    alerts.extend(gw_alerts)
    alerts.extend(fau_alerts)

    return {
        "checked_at": now.isoformat(timespec="seconds"),
        "network": cfg.network.name,
        "local": {
            "spend_account": spend_address(cfg),
            "kill_switch": kill or "clear",
            "configured_miners": len(cfg.miners),
            "allowed_models": list(cfg.caps.allowed_models),
            "budget": policy.budget().describe(),
        },
        "runtime": rt_view,
        "cli_release": rel_view,
        "catalog": cat_view,
        "gateway_enrollment": gw_view,
        "faucet": fau_view,
        "alerts": alerts,
    }
