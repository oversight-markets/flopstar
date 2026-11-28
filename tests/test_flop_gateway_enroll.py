"""Encrypted gateway wallet enrollment: 8-line challenge check, FlopPolicy gate, CLI & 0600 key."""

import json
import stat
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from flopstar.flop import cli, gateway_enroll
from flopstar.flop.policy import validate_gateway_enrollment_challenge
from flopstar.flop.wallet import create_spend_keystore, load_spend_keypair
from flopstar.signer import PolicyViolation
from tests.flop_helpers import GENESIS, make_full

TESTNET_GENESIS = "0x918abd58fb16306b30ef3886514cfaebffecb4de43e82b6f8ec4795c43cfd800"
FAKE_BEARER = "flop_test_secret_bearer_token_do_not_print_987654"


def _make_challenge(
    *,
    host: str = "api.flop.network",
    network: str = "devnet",
    address: str,
    genesis: str = GENESIS,
    nonce: str = "a" * 64,
    issued_at: datetime,
    ttl_seconds: int = 300,
    action: str | None = None,
) -> str:
    expires = issued_at + timedelta(seconds=ttl_seconds)
    act = action or f"flop-{network}-enroll/v1"
    return "\n".join([
        f"{host} wants you to enroll a FLOP {network} gateway key with your account:",
        address,
        "",
        f"Action: {act}",
        f"Genesis: {genesis}",
        f"Nonce: {nonce}",
        f"Issued At: {issued_at.astimezone(UTC).isoformat().replace('+00:00', 'Z')}",
        f"Expiration Time: {expires.astimezone(UTC).isoformat().replace('+00:00', 'Z')}",
    ])


def test_challenge_validation_accepts_exact_and_rejects_tampering():
    now = datetime(2026, 11, 28, 13, 0, tzinfo=UTC)
    addr = "5C9zWSykX2EHGF6pbRkPGTHrjYcnj2WjQRnm2xbPfye4uUFb"
    url = "https://gateway.testnet.flop.network/v1"

    good = _make_challenge(
        host="gateway.testnet.flop.network",
        network="testnet",
        address=addr,
        genesis=TESTNET_GENESIS,
        issued_at=now - timedelta(seconds=5),
    )
    assert validate_gateway_enrollment_challenge(
        good, address=addr, gateway_url=url, genesis=TESTNET_GENESIS, network="testnet", now=now
    ) == "a" * 64

    bad_cases = [
        _make_challenge(host="evil.example", network="testnet", address=addr,
                        genesis=TESTNET_GENESIS, issued_at=now),
        _make_challenge(host="gateway.testnet.flop.network", network="devnet", address=addr,
                        genesis=TESTNET_GENESIS, issued_at=now),
        _make_challenge(host="gateway.testnet.flop.network", network="testnet", address=addr,
                        genesis="0x" + "11" * 32, issued_at=now),
        _make_challenge(host="gateway.testnet.flop.network", network="testnet",
                        address="5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty",
                        genesis=TESTNET_GENESIS, issued_at=now),
        _make_challenge(host="gateway.testnet.flop.network", network="testnet", address=addr,
                        genesis=TESTNET_GENESIS, nonce="not-hex", issued_at=now),
        _make_challenge(host="gateway.testnet.flop.network", network="testnet", address=addr,
                        genesis=TESTNET_GENESIS, issued_at=now - timedelta(minutes=6),
                        ttl_seconds=300),
        _make_challenge(host="gateway.testnet.flop.network", network="testnet", address=addr,
                        genesis=TESTNET_GENESIS, issued_at=now, ttl_seconds=301),
        _make_challenge(host="gateway.testnet.flop.network", network="testnet", address=addr,
                        genesis=TESTNET_GENESIS, issued_at=now + timedelta(minutes=2)),
        good + "\nextra-line",
    ]
    for bad in bad_cases:
        with pytest.raises(PolicyViolation):
            validate_gateway_enrollment_challenge(
                bad, address=addr, gateway_url=url, genesis=TESTNET_GENESIS,
                network="testnet", now=now,
            )


def test_policy_sign_gateway_enrollment_verifies_and_logs(tmp_path, monkeypatch):
    cfg, _, policy, clock = make_full(tmp_path, monkeypatch)
    addr = create_spend_keystore(cfg, "correct horse battery staple")
    kp = load_spend_keypair(cfg, "correct horse battery staple")
    msg = _make_challenge(
        host="api.flop.network", network="devnet", address=addr,
        genesis=GENESIS, issued_at=clock() - timedelta(seconds=1),
    )

    sig_hex = policy.sign_gateway_enrollment(kp, msg)
    assert sig_hex.startswith("0x") and len(sig_hex) == 130
    assert kp.verify(msg.encode("utf-8"), bytes.fromhex(sig_hex[2:]))
    log = [json.loads(line) for line in cfg.signatures_log.read_text().splitlines()]
    assert log[-1]["action"] == "gateway_enroll" and log[-1]["signer"] == addr
    assert not cfg.kill_path.exists()


def test_cli_gateway_enroll_dry_run_and_post_0600_and_rotation(tmp_path, monkeypatch, capsys):
    cfg, _, _, _ = make_full(tmp_path, monkeypatch)
    addr = create_spend_keystore(cfg, "correct horse battery staple")
    out_key = tmp_path / "creds" / "gw.key"
    monkeypatch.setenv("FLOPSTAR_FLOP_CONFIG", str(cfg.config_path))
    monkeypatch.setenv("FLOPSTAR_FLOP_GATEWAY_KEY_FILE", str(out_key))
    monkeypatch.setitem(cli.HOOKS, "passphrase", "correct horse battery staple")

    # 1. Disabled gateway (like live testnet 0.1.526) -> dry run clearly flags problem.
    disabled_t = httpx.MockTransport(lambda req: httpx.Response(200, json={
        "name": "FLOP Inference API", "models": "https://api.flop.network/v1/models",
    }))
    monkeypatch.setitem(cli.HOOKS, "transport", disabled_t)
    cli.run_flop(["gateway", "enroll", "--json"])
    disabled_view = json.loads(capsys.readouterr().out)
    assert disabled_view["dry_run"] is True
    assert disabled_view["discovery"]["enabled"] is False
    assert any("not enabled" in p for p in disabled_view["problems"])
    assert not out_key.exists()

    # 2. Enabled gateway -> dry run shows clean plan; --post signs via FlopPolicy & writes 0600.
    seen_posts = []
    now = datetime.now(UTC)

    def enabled_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "GET" and url == "https://api.flop.network/v1":
            return httpx.Response(200, json={
                "name": "FLOP Inference API",
                "wallet_enrollment": "https://api.flop.network/v1/enroll/wallet/challenge",
            })
        if request.method == "GET" and "/enroll/wallet/challenge" in url:
            msg = _make_challenge(
                host="api.flop.network", network="devnet", address=addr,
                genesis=GENESIS, issued_at=now - timedelta(seconds=2),
            )
            return httpx.Response(200, json={"message": msg})
        if request.method == "POST" and url == "https://api.flop.network/v1/enroll/wallet":
            body = json.loads(request.content)
            seen_posts.append(body)
            return httpx.Response(200, json={
                "key": FAKE_BEARER,
                "model": "qwen/qwen2.5-0.5b-instruct",
                "remaining_quota": "1.0",
                "expires_at": 1796000000000,
            })
        return httpx.Response(404, json={"error": "not_found"})

    enabled_t = httpx.MockTransport(enabled_handler)
    monkeypatch.setitem(cli.HOOKS, "transport", enabled_t)
    monkeypatch.setitem(
        cli.HOOKS, "http", lambda: httpx.Client(transport=enabled_t, follow_redirects=False)
    )

    cli.run_flop(["gateway", "enroll", "--json"])
    plan_out = json.loads(capsys.readouterr().out)
    assert plan_out["dry_run"] is True and plan_out["problems"] == []
    assert seen_posts == [] and not out_key.exists()

    cli.run_flop(["gateway", "enroll", "--json", "--post"])
    raw_stdout = capsys.readouterr().out
    assert FAKE_BEARER not in raw_stdout  # NEVER print the bearer key
    res = json.loads(raw_stdout)
    assert res["status"] == "enrolled"
    assert res["address"] == addr
    assert out_key.exists()
    assert out_key.read_text().strip() == FAKE_BEARER
    assert stat.S_IMODE(out_key.stat().st_mode) == 0o600
    assert FAKE_BEARER not in cfg.signatures_log.read_text()
    assert len(seen_posts) == 1 and seen_posts[0]["signature"].startswith("0x")

    # 3. Refuses overwrite without --rotate, succeeds with --rotate.
    with pytest.raises(SystemExit, match="already exists"):
        cli.run_flop(["gateway", "enroll", "--post"])
    cli.run_flop(["gateway", "enroll", "--rotate", "--json", "--post"])
    res2 = json.loads(capsys.readouterr().out)
    assert res2["status"] == "enrolled" and len(seen_posts) == 2
    assert gateway_enroll.resolve_output_path(cfg, out_key) == out_key
