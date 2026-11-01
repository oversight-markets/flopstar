"""FLOP amounts are exact 18-decimal base units; floats are refused."""

from decimal import Decimal

import pytest

from flopstar.flop.amounts import (
    MAX_RAW,
    UNIT,
    AmountError,
    format_flop,
    parse_flop,
    raw_from_str,
    to_decimal,
)


def test_sdk_docs_example_is_exact():
    # docs.flop.network send-to-specific-miner: 10_000_000_000_000_000 = 0.01 FLOP
    assert parse_flop("0.01") == 10_000_000_000_000_000
    assert format_flop(10_000_000_000_000_000) == "0.01"


@pytest.mark.parametrize(
    "text",
    ["0", "1", "0.5", "25", "0.000000000000000001", "123456789.123456789123456789",
     "340282366920938463463.374607431768211455"],
)
def test_round_trip_is_exact(text):
    raw = parse_flop(text)
    assert format_flop(raw) == text
    assert parse_flop(Decimal(text)) == raw
    assert to_decimal(raw) == Decimal(text)
    assert raw_from_str(str(raw)) == raw


def test_every_raw_value_round_trips_through_text():
    for raw in (1, 7, UNIT - 1, UNIT, UNIT + 1, 4727999999999999759, MAX_RAW):
        assert parse_flop(format_flop(raw)) == raw


def test_floats_and_bools_are_refused():
    for bad in (0.01, 1.0, True, float("nan")):
        with pytest.raises(AmountError):
            parse_flop(bad)


@pytest.mark.parametrize(
    "bad", ["", "-1", "1e-2", "0.0000000000000000001", ".5", "1.", "1,000", " NaN", "0x10", "\u0661.\u0665"]
)
def test_malformed_text_is_refused(bad):
    with pytest.raises(AmountError):
        parse_flop(bad)


def test_overflow_and_bad_storage_strings_are_refused():
    with pytest.raises(AmountError):
        parse_flop("340282366920938463463.374607431768211456")
    for bad in ("-5", "1.5", "", 5, "\u00b2"):
        with pytest.raises(AmountError):
            raw_from_str(bad)
    with pytest.raises(AmountError):
        parse_flop(Decimal("Infinity"))


def test_decimal_inputs_beyond_default_precision_stay_exact():
    # The default 28-digit decimal context would round these silently.
    text = "340282366920938463463.374607431768211455"
    assert parse_flop(Decimal(text)) == MAX_RAW
    assert to_decimal(MAX_RAW) == Decimal(text)
    assert parse_flop(Decimal("4.727999999999999759")) == 4727999999999999759
