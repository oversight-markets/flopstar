"""
Safe, encrypted gateway wallet enrollment (`flopstar flop gateway enroll [--post]`).

Unlike `@flop-labs/cli`'s `agent credential` (which writes an unencrypted enrollment wallet to
`~/.flop/`), this module reuses Flopstar's existing encrypted spend keystore (`spend.json`) and
signs the gateway's 8-line challenge strictly through `FlopPolicy.sign_gateway_enrollment()`.

Protocol verified against `@flop-labs/cli@0.10.4` (`dist/cli.js`):
1. Discovery: `GET {gateway_url}` must advertise
   `wallet_enrollment == {gateway_url}/enroll/wallet/challenge`.
2. Challenge: `GET {gateway_url}/enroll/wallet/challenge?address={ss58}` -> `{message}`.
3. Validation: strict 8-line check against host, network, pinned genesis, SS58, 64-hex nonce,
   and <= 5 min expiry (`policy.validate_gateway_enrollment_challenge`).
4. Sign inside `FlopPolicy.sign_gateway_enrollment(keypair, message)`.
5. Exchange: `POST {gateway_url}/enroll/wallet` with `{address, message, signature}` ->
   `{key, model, remaining_quota, expires_at}`.
6. Store `key` atomically with `0600` permissions; never print or log the bearer key.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from .config import ConfigError, FlopConfig
from .policy import FlopPolicy, validate_gateway_enrollment_challenge

ENROLL_TIMEOUT_S = 30


class EnrollmentError(Exception):
    """Gateway wallet enrollment is unavailable, refused, or returned a malformed response."""


def _expected_challenge_url(cfg: FlopConfig) -> str:
    if not cfg.network.gateway_url:
        raise EnrollmentError(f"{cfg.network.name} has no gateway_url configured")
    return f"{cfg.network.gateway_url.rstrip('/')}/enroll/wallet/challenge"


def check_discovery(cfg: FlopConfig, client: httpx.Client) -> dict[str, Any]:
    """Read-only GET {gateway_url} checking whether `wallet_enrollment` is enabled."""
    url = cfg.network.gateway_url
    if not url:
        return {"status": "unconfigured", "enabled": False, "url": None}
    expected = _expected_challenge_url(cfg)
    try:
        r = client.get(url)
    except httpx.HTTPError as e:
        return {"status": "error", "enabled": False, "error": f"{type(e).__name__}: {e}"}
    if not r.is_success:
        return {"status": "error", "enabled": False, "error": f"HTTP {r.status_code}"}
    try:
        data = r.json()
    except ValueError as e:
        return {"status": "error", "enabled": False, "error": f"non-JSON: {e}"}
    advertised = data.get("wallet_enrollment") if isinstance(data, dict) else None
    enabled = isinstance(advertised, str) and advertised == expected
    return {
        "status": "ok",
        "enabled": enabled,
        "expected": expected,
        "advertised": advertised if isinstance(advertised, str) else None,
    }


def resolve_output_path(cfg: FlopConfig, out_path: Path | None) -> Path | None:
    if out_path is not None:
        return out_path.expanduser()
    return cfg.gateway_key_file


def plan(
    cfg: FlopConfig,
    policy: FlopPolicy,
    address: str | None,
    *,
    out_path: Path | None = None,
    rotate: bool = False,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Read-only enrollment dry-run plan. Never requests a challenge or touches keys."""
    problems: list[str] = []
    if policy.kill_reason() is not None:
        problems.append("kill switch is tripped")
    try:
        pinned = cfg.require_pinned_genesis()
    except ConfigError as e:
        pinned = None
        problems.append(str(e))
    if not address:
        problems.append("no spend keystore address (run `flop wallet setup --post`)")
    target = resolve_output_path(cfg, out_path)
    if target is None:
        problems.append(
            "no key output path: set FLOPSTAR_FLOP_GATEWAY_KEY_FILE or pass --out PATH"
        )
    elif target.exists() and not rotate:
        problems.append(f"gateway key file {target} already exists (pass --rotate to replace)")

    with httpx.Client(transport=transport, timeout=ENROLL_TIMEOUT_S,
                      follow_redirects=False) as client:
        disc = check_discovery(cfg, client)
    if not disc.get("enabled"):
        err_hint = f" ({disc['error']})" if disc.get("error") else ""
        problems.append(
            f"wallet enrollment is not enabled on {cfg.network.name} gateway "
            f"{cfg.network.gateway_url}{err_hint}"
        )

    return {
        "network": cfg.network.name,
        "genesis_pinned": pinned,
        "gateway_url": cfg.network.gateway_url,
        "address": address,
        "key_file": str(target) if target else None,
        "rotate": rotate,
        "discovery": disc,
        "problems": problems,
    }


def _write_secret_0600(path: Path, secret_text: str) -> None:
    """Atomically write `secret_text` with owner-only `0600` permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(tmp, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(secret_text.strip() + "\n")
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def enroll(
    cfg: FlopConfig,
    policy: FlopPolicy,
    client: httpx.Client,
    address: str,
    load_keypair: Callable[[], Any],
    *,
    out_path: Path | None = None,
    rotate: bool = False,
) -> dict[str, Any]:
    """
    Enroll `address` on the configured FLOP gateway using the encrypted spend key via `FlopPolicy`.
    Writes the returned bearer key to `0600` file and returns non-secret metadata only.
    """
    policy.ensure_alive()
    genesis = cfg.require_pinned_genesis().lower()
    target = resolve_output_path(cfg, out_path)
    if target is None:
        raise EnrollmentError(
            "no key output path: set FLOPSTAR_FLOP_GATEWAY_KEY_FILE or pass --out PATH"
        )
    if target.exists() and not rotate:
        raise EnrollmentError(
            f"gateway key file {target} already exists (pass --rotate to replace)"
        )

    disc = check_discovery(cfg, client)
    if not disc.get("enabled"):
        raise EnrollmentError(
            f"wallet enrollment is not enabled on {cfg.network.name} gateway "
            f"{cfg.network.gateway_url}"
        )

    challenge_url = _expected_challenge_url(cfg)
    r_ch = client.get(challenge_url, params={"address": address})
    if r_ch.status_code == 404:
        raise EnrollmentError(
            f"wallet enrollment challenge returned 404 on {cfg.network.name} gateway"
        )
    if not r_ch.is_success:
        raise EnrollmentError(
            f"enrollment challenge failed: HTTP {r_ch.status_code} {r_ch.text[:240]}"
        )
    try:
        message = r_ch.json()["message"]
    except (ValueError, KeyError, TypeError) as e:
        raise EnrollmentError(f"malformed challenge response: {e}") from None

    # Pre-validate before decrypting the spend keystore so malformed servers never prompt/decrypt.
    validate_gateway_enrollment_challenge(
        message,
        address=address,
        gateway_url=cfg.network.gateway_url or "",
        genesis=genesis,
        network=cfg.network.name,
        now=policy.now(),
    )

    keypair = load_keypair()
    signature = policy.sign_gateway_enrollment(keypair, message)

    enroll_url = f"{cfg.network.gateway_url.rstrip('/')}/enroll/wallet"
    r_en = client.post(
        enroll_url,
        json={"address": address, "message": message, "signature": signature},
    )
    if not r_en.is_success:
        raise EnrollmentError(
            f"enrollment submission refused: HTTP {r_en.status_code} {r_en.text[:300]}"
        )
    try:
        body = r_en.json()
    except ValueError as e:
        raise EnrollmentError(f"malformed enrollment response: {e}") from None

    bearer = body.get("key") if isinstance(body, dict) else None
    if not isinstance(bearer, str) or len(bearer.strip()) < 16:
        raise EnrollmentError("enrollment response did not include a valid bearer key")

    _write_secret_0600(target, bearer)
    key_sha_prefix = hashlib.sha256(bearer.strip().encode("utf-8")).hexdigest()[:12]
    return {
        "status": "enrolled",
        "network": cfg.network.name,
        "address": address,
        "key_file": str(target),
        "key_sha256_prefix": key_sha_prefix,
        "model": body.get("model"),
        "remaining_quota": body.get("remaining_quota"),
        "expires_at": body.get("expires_at"),
    }


__all__ = [
    "ENROLL_TIMEOUT_S",
    "EnrollmentError",
    "check_discovery",
    "enroll",
    "plan",
    "resolve_output_path",
]
