"""Integer micro-dollar arithmetic.

Every monetary value inside the system is an ``int`` count of micro-dollars
(1 USD = 1_000_000 µ$). Floats are never used for money, for two reasons:

1. Accumulating float error over tens of thousands of calls produces counters
   that disagree with the ledger, and "your budget system's numbers don't add
   up" defeats the purpose of the budget system.
2. Redis Lua (5.1) numbers are IEEE doubles. Integers stay exact below 2^53;
   micro-dollars put a $1M budget at 1e12, five orders of magnitude clear of
   that ceiling, so arithmetic inside the enforcement scripts is exact.

Conversion to a human-readable float or string happens only at the display
boundary — the API response and the dashboard.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

MICROS_PER_USD = 1_000_000
_CENT = Decimal("0.01")
_MICRO = Decimal("0.000001")


def usd_to_micros(value: Decimal | str | int | float) -> int:
    """Convert a USD amount to micro-dollars, rounding half-up.

    Accepts floats for ergonomics at the API boundary but routes them through
    ``str`` so that ``0.1`` means "one tenth", not its binary approximation.
    """
    dec = Decimal(str(value)) if not isinstance(value, Decimal) else value
    return int((dec / _MICRO).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def micros_to_usd(micros: int) -> Decimal:
    """Exact USD value as a Decimal (full micro-dollar precision)."""
    return (Decimal(micros) * _MICRO).normalize()


def format_usd(micros: int) -> str:
    """Cent-rounded display string, e.g. ``"50.00"``."""
    return str((Decimal(micros) * _MICRO).quantize(_CENT, rounding=ROUND_HALF_UP))


def format_usd_precise(micros: int) -> str:
    """Full micro-dollar precision, trailing zeros trimmed.

    Individual calls routinely cost a fraction of a cent, so cent-rounding a
    per-call figure reports "0.00" for every request and tells the reader
    nothing. Totals use :func:`format_usd`; single amounts use this.
    """
    value = Decimal(micros) * _MICRO
    text = f"{value:.6f}".rstrip("0")
    return text + "0" if text.endswith(".") else text


def micros_to_float(micros: int) -> float:
    """For JSON payloads where a number is more useful than a string.

    Lossy by nature; never feed the result back into a counter.
    """
    return float(Decimal(micros) * _MICRO)


def pct(consumed_micros: int, limit_micros: int) -> float:
    """Fraction consumed, 0.0 when the limit is zero/unset."""
    if limit_micros <= 0:
        return 0.0
    return consumed_micros / limit_micros


def cost_micros(tokens: int, micros_per_1k: int) -> int:
    """Cost of ``tokens`` at a per-1k rate, rounded up.

    Rounds up deliberately: under-charging a fraction of a micro-dollar on every
    call is a systematic leak in the direction of overspending, and this system
    exists to bias the other way.
    """
    if tokens <= 0 or micros_per_1k <= 0:
        return 0
    return -(-tokens * micros_per_1k // 1000)  # ceil division
