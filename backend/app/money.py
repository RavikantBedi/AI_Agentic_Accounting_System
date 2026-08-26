"""Money primitives.

Money is *always* ``decimal.Decimal``. Binary floating point is never used for
monetary values anywhere in this codebase — see ``tests/test_money.py``.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

# Ledger amounts are stored and compared at 2dp; intermediate tax/quantity math
# uses higher precision before a single final quantisation.
MONEY_DP = Decimal("0.01")
RATE_DP = Decimal("0.000001")
QTY_DP = Decimal("0.0001")

ZERO = Decimal("0.00")


class MoneyError(ValueError):
    """Raised when a value cannot be interpreted as money."""


def D(value: Any) -> Decimal:
    """Coerce anything sane into a Decimal without ever going through float.

    ``float`` inputs are accepted (external JSON cannot express Decimal) but are
    routed through ``repr`` so 0.1 becomes Decimal('0.1'), not the binary
    expansion.
    """
    if isinstance(value, Decimal):
        return value
    if value is None or value == "":
        return ZERO
    if isinstance(value, bool):
        raise MoneyError("bool is not a monetary value")
    try:
        if isinstance(value, float):
            return Decimal(repr(value))
        if isinstance(value, int):
            return Decimal(value)
        text = str(value).strip().replace(",", "").replace("\u20b9", "")
        if text.startswith("(") and text.endswith(")"):  # (1,200.00) => -1200.00
            text = "-" + text[1:-1]
        if text in {"", "-", "."}:
            return ZERO
        return Decimal(text)
    except (InvalidOperation, ArithmeticError, TypeError) as exc:  # pragma: no cover
        raise MoneyError(f"Cannot interpret {value!r} as money") from exc


def money(value: Any) -> Decimal:
    """Coerce and quantise to 2 decimal places, half-up (accounting rounding)."""
    return D(value).quantize(MONEY_DP, rounding=ROUND_HALF_UP)


def quantity(value: Any) -> Decimal:
    return D(value).quantize(QTY_DP, rounding=ROUND_HALF_UP)


def rate(value: Any) -> Decimal:
    return D(value).quantize(RATE_DP, rounding=ROUND_HALF_UP)


def is_zero(value: Any) -> bool:
    return money(value) == ZERO


def split_evenly(total: Any, parts: int) -> list[Decimal]:
    """Split an amount into ``parts`` pieces that sum *exactly* back to total.

    Used by amortisation/depreciation schedules so rounding never leaks money.
    """
    if parts <= 0:
        raise MoneyError("parts must be positive")
    total_d = money(total)
    base = money(total_d / Decimal(parts))
    shares = [base] * parts
    drift = total_d - sum(shares)
    # Push the entire rounding drift onto the final instalment.
    shares[-1] = money(shares[-1] + drift)
    return shares


def allocate_proportionally(total: Any, weights: list[Decimal]) -> list[Decimal]:
    """Allocate ``total`` across ``weights``, preserving the exact sum."""
    total_d = money(total)
    weight_sum = sum(D(w) for w in weights)
    if weight_sum == 0:
        return split_evenly(total_d, len(weights)) if weights else []
    out: list[Decimal] = []
    for w in weights:
        out.append(money(total_d * D(w) / weight_sum))
    drift = total_d - sum(out)
    if out:
        out[-1] = money(out[-1] + drift)
    return out


def fmt_inr(value: Any) -> str:
    """Format using the Indian digit grouping convention (12,34,567.89)."""
    amount = money(value)
    negative = amount < 0
    digits = f"{abs(amount):.2f}"
    whole, frac = digits.split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    out = f"\u20b9{whole}.{frac}"
    return f"-{out}" if negative else out
