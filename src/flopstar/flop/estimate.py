"""
Size each request's escrow before anything is signed (docs/FLOP.md §3.3).

`flop session run --max-flop X` opens the channel with escrow exactly X, and a cooperative settle
pays the whole escrow to the miner (sdk compute-channel/session-estimate.d.ts, D-0403): the escrow
is the cost, not a ceiling. So `ask` and `run` escrow this estimate, and `max_flop_per_request` is
only the ceiling it must stay under.

The estimate is a deliberate upper bound from public data only (no key, no miner endpoint):

    tokens  = UTF-8 bytes of the prompt + TEMPLATE_TOKENS + max_tokens
    cost    = tokens x max(pricing.prompt, pricing.completion)   # gateway price, FLOP per token
    escrow  = max(cost, MIN_ESCROW_RAW)

- Byte-level BPE (Qwen2.5) never yields more regular tokens than UTF-8 bytes, so the prompt bound
  holds for any text; TEMPLATE_TOKENS covers the chat template (default system prompt and role
  markers) the miner wraps around one user message.
- The gateway prices per token: its figure is `flop_per_gn x gnPerPromptToken`, with
  gnPerPromptToken = ceil(2 P_active / 1e9) = 1 for Qwen2.5-0.5B (sdk model-config.d.ts). The
  per-miner `flop_per_gn` is not public, so the dry run against a real miner is the confirmation.
- The chain's own tariff (`basePerTurn` = 1 raw unit per turn + sum of G_n raw units) is negligible
  next to the price; MIN_ESCROW_RAW is the force-open floor read from the devnet runtime.

Every source fails closed: no catalog, no such model, a malformed price, or (direct route) a miner
the catalog does not currently price means no estimate, and the request is refused unless the
caller names an explicit --max-flop.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from .amounts import AmountError, format_flop, parse_flop
from .config import ConfigError, FlopConfig, ss58_pubkey

TEMPLATE_TOKENS = 40
# computeChannel.minForceOpenEscrowForFailedAck on devnet (spec_version 1014003), read 4 Nov 2026.
MIN_ESCROW_RAW = 50_000_000_000_000_000  # 0.05 FLOP
CATALOG_TIMEOUT_S = 20


class EstimateUnavailable(Exception):
    """No trustworthy estimate: the caller must refuse or take an explicit --max-flop."""


@dataclass(frozen=True)
class Estimate:
    prompt_tokens_max: int
    output_tokens_max: int
    raw_per_token: int
    cost_raw: int
    escrow_raw: int

    def describe(self) -> dict:
        return {"prompt_tokens_max": self.prompt_tokens_max,
                "output_tokens_max": self.output_tokens_max,
                "price_per_token": format_flop(self.raw_per_token),
                "cost": format_flop(self.cost_raw),
                "escrow": format_flop(self.escrow_raw),
                "floor": format_flop(MIN_ESCROW_RAW)}


def _price(value, what: str) -> int:
    if not isinstance(value, str):
        raise EstimateUnavailable(f"catalog {what} price is not a decimal string")
    try:
        return parse_flop(value)
    except AmountError as e:
        raise EstimateUnavailable(f"catalog {what} price: {e}") from None


def _parse_time(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        t = datetime.fromisoformat(value)
    except ValueError:
        return None
    return t if t.tzinfo else None


def price_from_catalog(body: bytes, model: str, miner: str | None,
                       now: datetime | None = None) -> int:
    """Raw FLOP per token for `model` from a `GET /models` body. Strict; raises EstimateUnavailable."""
    import json
    try:
        data = json.loads(body)
    except ValueError as e:
        raise EstimateUnavailable(f"catalog is not JSON: {e}") from None
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise EstimateUnavailable("catalog has no data list")
    matches = [r for r in rows if isinstance(r, dict) and r.get("id") == model]
    if len(matches) != 1:
        raise EstimateUnavailable(f"catalog lists {model!r} {len(matches)} times, expected once")
    row = matches[0]
    pricing = row.get("pricing")
    if not isinstance(pricing, dict):
        raise EstimateUnavailable(f"catalog has no pricing for {model!r}")
    per_token = max(_price(pricing.get("prompt"), "prompt"),
                    _price(pricing.get("completion"), "completion"))
    if per_token <= 0:
        raise EstimateUnavailable(f"catalog prices {model!r} at zero; refusing to guess")
    if miner is not None:
        now = now or datetime.now(UTC)
        sources = (row.get("x-flop") or {}).get("price_sources")
        try:
            want = ss58_pubkey(miner)
        except ConfigError as e:
            raise EstimateUnavailable(str(e)) from None
        live = False
        for s in sources if isinstance(sources, list) else []:
            if not isinstance(s, dict) or not isinstance(s.get("miner_address"), str):
                continue
            try:
                if ss58_pubkey(s["miner_address"]) != want:
                    continue
            except ConfigError:
                continue
            expires = _parse_time(s.get("expires_at"))
            if expires is not None and now < expires:
                live = True
        if not live:
            raise EstimateUnavailable(f"catalog has no live price for miner {miner}")
    return per_token


def fetch_price(cfg: FlopConfig, model: str, miner: str | None,
                transport: httpx.BaseTransport | None = None) -> int:
    """GET {gateway}/models (public, no key) and price `model`."""
    if not cfg.network.gateway_url:
        raise EstimateUnavailable(f"{cfg.network.name} has no gateway catalog to price from")
    try:
        with httpx.Client(transport=transport, timeout=CATALOG_TIMEOUT_S,
                          follow_redirects=False) as client:
            r = client.get(f"{cfg.network.gateway_url}/models")
    except httpx.HTTPError as e:
        raise EstimateUnavailable(f"catalog unreachable: {type(e).__name__}") from None
    if not r.is_success:
        raise EstimateUnavailable(f"catalog returned HTTP {r.status_code}")
    return price_from_catalog(r.content, model, miner)


def size(prompt: str, max_tokens: int, raw_per_token: int) -> Estimate:
    """The escrow upper bound for one turn at `raw_per_token`. Pure, exact integers."""
    if type(max_tokens) is not int or max_tokens <= 0:
        raise ValueError("max_tokens must be a positive int")
    prompt_tokens = len(prompt.encode("utf-8")) + TEMPLATE_TOKENS
    cost = (prompt_tokens + max_tokens) * raw_per_token
    return Estimate(prompt_tokens_max=prompt_tokens, output_tokens_max=max_tokens,
                    raw_per_token=raw_per_token, cost_raw=cost,
                    escrow_raw=max(cost, MIN_ESCROW_RAW))


def estimate(cfg: FlopConfig, *, model: str, prompt: str, max_tokens: int, miner: str | None,
             transport: httpx.BaseTransport | None = None) -> Estimate:
    return size(prompt, max_tokens, fetch_price(cfg, model, miner, transport))
