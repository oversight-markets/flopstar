"""Read-only FLOP Network scout radar: runtime drift, CLI pin, catalog density, S3 signal, faucet."""

import json
from datetime import UTC, datetime

import httpx

from flopstar.flop import cli, scout
from flopstar.flop.config import CLI_IMAGE
from tests.flop_helpers import FIXTURES, GENESIS, MODEL, config_text, make, make_full

TESTNET_GENESIS = "0x918abd58fb16306b30ef3886514cfaebffecb4de43e82b6f8ec4795c43cfd800"


def _mock_transport(
    *,
    spec_version: int = 1014039,
    spec_name: str = "flop-testnet",
    genesis: str = TESTNET_GENESIS,
    cli_image: str = CLI_IMAGE,
    catalog: bytes | dict | None = None,
    faucet_status: str = "open",
    rollover_requires_spend: bool = True,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "POST":
            body = json.loads(request.content)
            if body["method"] == "state_getRuntimeVersion":
                return httpx.Response(200, json={
                    "jsonrpc": "2.0", "id": 1,
                    "result": {"specName": spec_name, "specVersion": spec_version},
                })
            if body["method"] == "chain_getBlockHash":
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": genesis})
        if url == scout.RELEASES_URL:
            return httpx.Response(200, json={"cli": {"image": cli_image, "version": "0.1.505"}})
        if url.endswith("/models"):
            if isinstance(catalog, bytes):
                return httpx.Response(200, content=catalog)
            return httpx.Response(200, json=catalog or {"data": []})
        if url.endswith("/limits"):
            return httpx.Response(200, json={
                "faucet": {
                    "status": faucet_status,
                    "dailyUsedFlop": "5000000",
                    "dailyCapFlop": "11000000",
                    "pendingGrants": 0,
                },
                "agent": {
                    "grantFlop": "10",
                    "cooldownSeconds": 86400,
                    "rolloverRequiresSpend": rollover_requires_spend,
                },
            })
        return httpx.Response(404, json={"error": "not found"})

    return httpx.MockTransport(handler)


def test_scout_computes_density_from_real_testnet_catalog(tmp_path, monkeypatch):
    raw = (FIXTURES / "testnet-models-2026-11-26.json").read_bytes()
    parsed = json.loads(raw)
    # Pick a timestamp inside the fixture's live price window.
    sample_src = parsed["data"][0]["x-flop"]["price_sources"][0]
    now = datetime.fromisoformat(sample_src["effective_at"])

    cfg, _, policy, _ = make(tmp_path, monkeypatch, text=config_text("testnet"))
    report = scout.scout(cfg, policy, transport=_mock_transport(catalog=raw), now=now)

    assert report["network"] == "testnet"
    assert report["runtime"]["sdk_supported"] is True
    assert report["runtime"]["genesis_matches_pin"] is True
    assert report["cli_release"]["cli_drift"] is False
    assert report["catalog"]["status"] == "ok"
    models = {m["id"]: m for m in report["catalog"]["models"]}
    assert "qwen/qwen3-8b" in models and "qwen/qwen3-14b" in models
    # 0.05 FLOP / 0.00000034 FLOP/token = 147,058 tokens; 0.05 / 0.00000060 = 83,333 tokens
    assert models["qwen/qwen3-8b"]["tokens_per_min_escrow"] == 147_058
    assert models["qwen/qwen3-14b"]["tokens_per_min_escrow"] == 83_333
    assert any(a.startswith("S3_BLOCKED:") for a in report["alerts"])
    assert any(a.startswith("CAP_NOTE:") for a in report["alerts"])
    assert not any(a.startswith("RUNTIME_DRIFT:") for a in report["alerts"])
    assert not any(a.startswith("CLI_DRIFT:") for a in report["alerts"])
    assert not cfg.kill_path.exists()


def test_scout_flags_runtime_cli_genesis_faucet_and_s3_signal(tmp_path, monkeypatch):
    now = datetime(2026, 11, 28, 12, 0, tzinfo=UTC)
    catalog = {
        "data": [{
            "id": MODEL,
            "pricing": {"prompt": "0.000001", "completion": "0.000001"},
            "x-flop": {
                "price_sources": [{
                    "miner_address": "5ED68Tgj9G6xtPnWKxzBkr7cij6nPR3kVXUYQ2RT27igsrH2",
                    "effective_at": "2026-11-28T11:00:00+00:00",
                    "expires_at": "2026-11-28T13:00:00+00:00",
                    "endpoint": "https://miner.example/session",
                }],
            },
        }]
    }
    cfg, _, policy, _ = make(tmp_path, monkeypatch)
    transport = _mock_transport(
        spec_version=9_999_999,
        spec_name="flop-devnet",
        genesis="0x" + "aa" * 32,
        cli_image="ghcr.io/flop-labs/flop-core/cli:v0.1.999@sha256:" + "11" * 32,
        catalog=catalog,
        faucet_status="daily_cap_reached",
    )
    report = scout.scout(cfg, policy, transport=transport, now=now)

    alerts = report["alerts"]
    assert report["runtime"]["sdk_supported"] is False
    assert report["runtime"]["genesis_matches_pin"] is False
    assert report["cli_release"]["cli_drift"] is True
    assert any(a.startswith("RUNTIME_DRIFT:") for a in alerts)
    assert any(a.startswith("GENESIS_MISMATCH:") for a in alerts)
    assert any(a.startswith("CLI_DRIFT:") for a in alerts)
    assert any(a.startswith("FAUCET_STATUS:") for a in alerts)
    assert any(a.startswith("S3_SIGNAL:") for a in alerts)
    assert not cfg.kill_path.exists()  # read-only scout never trips KILL-flop


def test_cli_scout_json_is_read_only(tmp_path, monkeypatch, capsys):
    cfg, _, _, _ = make_full(tmp_path, monkeypatch)
    monkeypatch.setenv("FLOPSTAR_FLOP_CONFIG", str(cfg.config_path))
    monkeypatch.setitem(
        cli.HOOKS,
        "transport",
        _mock_transport(spec_version=1014038, spec_name="flop-devnet", genesis=GENESIS),
    )
    cli.run_flop(["scout", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["network"] == "devnet"
    assert payload["runtime"]["genesis_matches_pin"] is True
    assert payload["local"]["configured_miners"] == 3
    assert not cfg.kill_path.exists()
