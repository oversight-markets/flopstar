"""Exact FLOP amounts: 18 decimals, held as int base units. Floats are refused everywhere.

flop-sdk converts with `int(amount * 10**18)` on floats (flop_sdk/utils.py), which is lossy; we
never hand it a float and never use its conversions (docs/FLOP.md §2.5).
"""

import re
from decimal import Context, Decimal, Inexact

DECIMALS = 18
UNIT = 10**DECIMALS
# Chain balances are u128 (cli.js parseSessionMaxFlop bounds escrow below 2^128).
MAX_RAW = (1 << 128) - 1

# Default decimal contexts round to 28 digits; u128 base units need 39. Trap any rounding.
_EXACT = Context(prec=80, traps=[Inexact])

_DECIMAL_TEXT = re.compile(r"^([0-9]+)(?:\.([0-9]{1,18}))?$")
_DIGITS = re.compile(r"^[0-9]+$")


class AmountError(ValueError):
    """An amount that is not an exact, non-negative FLOP value."""


def parse_flop(value: str | int | Decimal) -> int:
    """
    FLOP (a decimal string like "0.01", an int, or a finite Decimal) -> base units, exactly.
    Rejects floats, bools, negatives, exponents, more than 18 fractional digits and overflow.
    """
    if isinstance(value, (bool, float)):
        raise AmountError(f"refusing {type(value).__name__} amount {value!r}; use a decimal string")
    if isinstance(value, int):
        raw = value * UNIT
    elif isinstance(value, Decimal):
        if not value.is_finite():
            raise AmountError(f"non-finite amount {value}")
        try:
            scaled = _EXACT.multiply(value, UNIT)
        except Inexact:
            raise AmountError(f"{value} is too large to convert exactly") from None
        if scaled != scaled.to_integral_value():
            raise AmountError(f"{value} has more than {DECIMALS} fractional digits")
        raw = int(scaled)
    elif isinstance(value, str):
        m = _DECIMAL_TEXT.match(value.strip())
        if not m:
            raise AmountError(f"not a plain decimal FLOP amount: {value!r}")
        whole, frac = m.group(1), m.group(2) or ""
        raw = int(whole) * UNIT + int(frac.ljust(DECIMALS, "0"))
    else:
        raise AmountError(f"unsupported amount type {type(value).__name__}")
    return check_raw(raw)


def check_raw(raw: int) -> int:
    """Validate base units: a non-negative int that fits u128."""
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise AmountError(f"base units must be int, got {type(raw).__name__}")
    if raw < 0:
        raise AmountError(f"negative amount {raw}")
    if raw > MAX_RAW:
        raise AmountError(f"amount {raw} does not fit u128")
    return raw


def raw_from_str(text: str) -> int:
    """Base units stored as a digit string (ledger, chain JSON) -> int."""
    if not isinstance(text, str) or not _DIGITS.match(text):
        raise AmountError(f"base units must be a digit string, got {text!r}")
    return check_raw(int(text))


def to_decimal(raw: int) -> Decimal:
    """Base units -> exact Decimal FLOP."""
    return _EXACT.scaleb(Decimal(check_raw(raw)), -DECIMALS)


def format_flop(raw: int) -> str:
    """Base units -> shortest exact decimal string ("0.01", "25", "0.000000000000000001")."""
    whole, frac = divmod(check_raw(raw), UNIT)
    if not frac:
        return str(whole)
    return f"{whole}.{str(frac).rjust(DECIMALS, '0').rstrip('0')}"
