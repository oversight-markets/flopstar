"""Escrow sizing: an exact upper bound from the public catalog, failing closed."""

from datetime import UTC, datetime

import pytest

from flopstar.flop import estimate
from flopstar.flop.estimate import MIN_ESCROW_RAW, TEMPLATE_TOKENS, EstimateUnavailable
from tests.flop_helpers import MINER, MODEL, SERVING, catalog_body

NOW = datetime(2026, 11, 4, 14, 0, tzinfo=UTC)
PRICE = 6 * 10**15  # 0.006 FLOP


def test_size_is_exact_and_bounds_tokens_by_bytes():
    e = estimate.size("héllo", 64, PRICE)  # é is 2 UTF-8 bytes: 6 bytes
    assert e.prompt_tokens_max == 6 + TEMPLATE_TOKENS
    assert e.cost_raw == (6 + TEMPLATE_TOKENS + 64) * PRICE
    assert e.escrow_raw == e.cost_raw
    assert e.describe()["escrow"] == "0.66"


def test_size_never_goes_below_the_force_open_floor():
    e = estimate.size("hi", 1, 1)
    assert e.escrow_raw == MIN_ESCROW_RAW


@pytest.mark.parametrize("bad", [0, -1, 1.5, True])
def test_size_rejects_bad_max_tokens(bad):
    with pytest.raises(ValueError):
        estimate.size("hi", bad, PRICE)


def test_price_takes_the_higher_side_and_matches_miner_by_pubkey():
    import json
    body = json.loads(catalog_body())
    body["data"][0]["pricing"]["completion"] = "0.008"
    raw = estimate.price_from_catalog(json.dumps(body).encode(), MODEL, SERVING, now=NOW)
    assert raw == 8 * 10**15
    assert estimate.price_from_catalog(catalog_body(), MODEL, None, now=NOW) == PRICE


@pytest.mark.parametrize("body,miner,match", [
    (b"[]", SERVING, "no data list"),
    (catalog_body(price="0.006e0"), SERVING, "price"),
    (catalog_body(price="0.0000000000000000001"), SERVING, "price"),
    (catalog_body(miners=[MINER]), SERVING, "no live price"),
    (catalog_body(expires_at="2026-11-04T13:59:59Z"), SERVING, "no live price"),
    (catalog_body(expires_at="garbage"), SERVING, "no live price"),
])
def test_price_fails_closed(body, miner, match):
    with pytest.raises(EstimateUnavailable, match=match):
        estimate.price_from_catalog(body, MODEL, miner, now=NOW)


def test_price_refuses_numbers_that_are_not_strings():
    import json
    body = json.loads(catalog_body())
    body["data"][0]["pricing"]["prompt"] = 0.006
    with pytest.raises(EstimateUnavailable, match="not a decimal string"):
        estimate.price_from_catalog(json.dumps(body).encode(), MODEL, None, now=NOW)


def test_duplicate_model_rows_are_ambiguous():
    import json
    body = json.loads(catalog_body())
    body["data"].append(body["data"][0])
    with pytest.raises(EstimateUnavailable, match="2 times"):
        estimate.price_from_catalog(json.dumps(body).encode(), MODEL, None, now=NOW)
