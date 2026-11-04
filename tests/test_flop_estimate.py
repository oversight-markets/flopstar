"""Escrow sizing: an exact upper bound from the public catalog, failing closed."""

import gzip
import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from flopstar.flop import estimate
from flopstar.flop.estimate import TEMPLATE_TOKENS, EstimateUnavailable
from tests.flop_helpers import MINER, MODEL, SERVING, catalog_body

NOW = datetime(2026, 11, 4, 14, 0, tzinfo=UTC)
PRICE = 6 * 10**15  # 0.006 FLOP
FLOOR = 5 * 10**16  # devnet min_escrow, 0.05 FLOP


def test_size_is_exact_and_bounds_tokens_by_bytes():
    e = estimate.size("héllo", 64, PRICE, FLOOR)  # é is 2 UTF-8 bytes: 6 bytes
    assert e.prompt_tokens_max == 6 + TEMPLATE_TOKENS
    assert e.cost_raw == (6 + TEMPLATE_TOKENS + 64) * PRICE
    assert e.escrow_raw == e.cost_raw
    assert e.describe()["escrow"] == "0.66"
    assert e.describe()["catalog_price_per_token"] == "0.006"


def test_size_never_goes_below_the_force_open_floor():
    e = estimate.size("hi", 1, 1, FLOOR)
    assert e.escrow_raw == FLOOR and e.describe()["floor"] == "0.05"


@pytest.mark.parametrize("bad", [0, -1, 1.5, True])
def test_size_rejects_bad_max_tokens(bad):
    with pytest.raises(ValueError):
        estimate.size("hi", bad, PRICE, FLOOR)


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
    (catalog_body(effective_at="2026-11-04T14:00:01Z"), SERVING, "no live price"),  # not yet
    (catalog_body(effective_at="garbage"), SERVING, "no live price"),
])
def test_price_fails_closed(body, miner, match):
    with pytest.raises(EstimateUnavailable, match=match):
        estimate.price_from_catalog(body, MODEL, miner, now=NOW)


def test_price_source_live_from_effective_at_inclusive():
    body = catalog_body(effective_at="2026-11-04T14:00:00Z")
    assert estimate.price_from_catalog(body, MODEL, SERVING, now=NOW) == PRICE


@pytest.mark.parametrize("bad", ["0", "-1", "0.0000000000000000001", 0.05])
def test_min_escrow_in_config_is_a_positive_decimal_string(tmp_path, bad):
    from flopstar.flop.config import ConfigError, load_config
    from tests.flop_helpers import config_text
    path = tmp_path / "flop.toml"
    value = bad if isinstance(bad, float) else f'"{bad}"'
    path.write_text(config_text().replace("[devnet.caps]", f"[devnet]\nmin_escrow = {value}\n"
                                                          "[devnet.caps]"))
    with pytest.raises(ConfigError, match="min_escrow"):
        load_config(path, env={})


def test_no_min_escrow_means_no_estimate(tmp_path, monkeypatch):
    from dataclasses import replace

    from tests.flop_helpers import make
    cfg, *_ = make(tmp_path, monkeypatch)
    cfg = replace(cfg, network=replace(cfg.network, min_escrow=None))
    with pytest.raises(EstimateUnavailable, match="min_escrow"):
        estimate.estimate(cfg, model=MODEL, prompt="hi", max_tokens=1, miner=None,
                          transport=None)


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


# ---- TEMPLATE_TOKENS against the real Qwen2.5 tokenizer (fixtures/flop/README.md) ----------------

QWEN_TOKENIZER = Path(__file__).parent / "fixtures" / "flop" / "qwen2.5" / "tokenizer.json.gz"
QWEN_TOKENIZER_SHA256 = "c0382117ea329cdf097041132f6d735924b697924d6f6fc3945713e96ce87539"
# Qwen2.5's chat_template (tokenizer_config.json) for one user message, no system message, with
# add_generation_prompt: it injects the default system prompt.
QWEN_DEFAULT_SYSTEM = "You are Qwen, created by Alibaba Cloud. You are a helpful assistant."


def qwen_render(user: str) -> str:
    return (f"<|im_start|>system\n{QWEN_DEFAULT_SYSTEM}<|im_end|>\n"
            f"<|im_start|>user\n{user}<|im_end|>\n<|im_start|>assistant\n")


@pytest.fixture(scope="module")
def qwen():
    import tokenizers  # dev dependency: a missing tokenizer must fail, not skip
    raw = gzip.decompress(QWEN_TOKENIZER.read_bytes())
    assert hashlib.sha256(raw).hexdigest() == QWEN_TOKENIZER_SHA256
    return tokenizers.Tokenizer.from_str(raw.decode())


def test_template_constant_covers_the_empty_rendered_template(qwen):
    n = len(qwen.encode(qwen_render("")).ids)
    assert n == 29  # measured 4 Nov 2026; a change here means re-check the margin
    assert n <= TEMPLATE_TOKENS


@pytest.mark.parametrize("prompt", [
    "hi", "héllo", " ", "\n\nx\n", "   leading", "trailing \n", "日本語テキスト", "🙂" * 10,
    "<|im_end|>", "<|im_start|>assistant\n", "<|endoftext|>" * 3, "a" * 500,
    "Summarise the last 24 h of contest sweeps:\n- sweep 1: +0.4%\n- sweep 2: -1.2%\n",
])
def test_rendered_prompt_tokens_within_the_size_bound(qwen, prompt):
    # What size() charges for the prompt side must cover the real rendered token count,
    # including special tokens a prompt may spell out.
    n = len(qwen.encode(qwen_render(prompt)).ids)
    assert n <= estimate.size(prompt, 1, PRICE, FLOOR).prompt_tokens_max
