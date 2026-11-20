"""Monetary output coercion and exact integer average arithmetic.

All monetary values in this codebase are whole-IDR integers.  The storage
layer enforces this with ES ``long`` fields and Python ``int`` validation.
However, Elasticsearch aggregation results (``sum``, ``avg``,
``value_count``) arrive as IEEE-754 ``float64`` values, which can silently
carry precision error for magnitudes above 2**53 - 1.

This module provides a **fail-closed** coercion layer:

* :func:`coerce_monetary` validates a raw aggregation value and returns a
  Python ``int``, or raises :class:`MonetaryCoercionError` (no partial
  results, no value leakage into error messages).
* :func:`half_even_div` computes the exact integer half-to-even
  (banker's) quotient of ``num / den`` using pure integer arithmetic --
  no float round-trip.

Neither function recovers precision already lost inside Elasticsearch: if
an aggregation is inexact, this layer rejects it rather than emitting a
plausible-but-wrong integer.
"""

from __future__ import annotations

import math
from typing import Any


# ---------------------------------------------------------------------------
# Precision boundary
# ---------------------------------------------------------------------------

#: Last integer exactly representable as an IEEE-754 binary64 float.
#: Floats with absolute value above this are rejected because adjacent
#: integers can no longer be distinguished.
_MAX_SAFE_INT: int = 2 ** 53 - 1


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------

class MonetaryCoercionError(Exception):
    """Raised when a monetary aggregation value fails the coercion contract.

    Attributes:
        field:  Name of the output field that failed, e.g. ``"total_income"``.
            Only the field name is stored; the offending value is never
            captured here or in ``str(exc)``, so it cannot leak into logs
            or error envelopes.
    """

    def __init__(self, field: str) -> None:
        self.field = field
        super().__init__(field)


# ---------------------------------------------------------------------------
# Coercion
# ---------------------------------------------------------------------------

def coerce_monetary(value: Any, field: str) -> int:
    """Validate *value* as a whole-IDR monetary amount and return a Python ``int``.

    Accepts:
      * A Python ``int`` (JSON integers, stored ``_source`` fields).
      * A finite, integral ``float`` with ``abs(value) <= 2**53 - 1``.

    Rejects (raises :class:`MonetaryCoercionError`):
      * ``bool`` (``True`` / ``False`` -- a subclass of ``int``).
      * Fractional floats.
      * ``NaN``, ``+inf``, ``-inf``.
      * Floats with ``abs(value) > 2**53 - 1``.
      * ``None``, ``str``, ``bytes``, :class:`~decimal.Decimal`, and any
        other unsupported type.

    Args:
        value:  Raw value read from an ES aggregation result.
        field:  Name of the output field this value feeds into.

    Returns:
        The value as a Python ``int``.

    Raises:
        MonetaryCoercionError: when *value* does not satisfy the contract.
    """
    # bool is a subclass of int -- reject explicitly before int handling.
    if isinstance(value, bool):
        raise MonetaryCoercionError(field)

    if isinstance(value, int):
        return value

    if isinstance(value, float):
        if not math.isfinite(value):
            raise MonetaryCoercionError(field)
        if not value.is_integer():
            raise MonetaryCoercionError(field)
        if abs(value) > _MAX_SAFE_INT:
            raise MonetaryCoercionError(field)
        return int(value)

    raise MonetaryCoercionError(field)


# ---------------------------------------------------------------------------
# Exact integer half-to-even average
# ---------------------------------------------------------------------------

def half_even_div(num: int, den: int) -> int:
    """Return the exact half-to-even (banker's) integer quotient of ``num / den``.

    Pure integer arithmetic; no float division is performed.  This is the
    authoritative implementation for ``avg_per_tx = total / tx_count``
    where *total* and *tx_count* are validated integers.

    Ties round to the nearest **even** integer (the same rule Python uses
    for ``round()`` on whole numbers).

    Examples:
        5 / 2 -> 2   (tie -> even)
        7 / 2 -> 4   (tie -> even)
        8 / 3 -> 3   (8/3 = 2.667, rounds up)
        5 / 3 -> 2   (5/3 = 1.667, rounds down)
        1 / 2 -> 0   (tie -> 0 is even)
        3 / 2 -> 2   (tie -> even)
       -5 / 2 -> -2  (tie -> -2 is even)
       -7 / 2 -> -4  (tie -> -4 is even)

    Args:
        num: Numerator (validated integer sum).  May be negative.
        den: Denominator (validated integer count).  Must be a positive
             ``int`` (not zero, not ``bool``).

    Returns:
        The half-to-even integer quotient.

    Raises:
        ValueError: if *den* is zero, ``bool``, or not an ``int``.
    """
    if isinstance(den, bool) or not isinstance(den, int):
        raise ValueError("denominator must be a positive int")
    if den <= 0:
        raise ValueError("denominator must be positive")

    q, r = divmod(num, den)
    twice_r = 2 * r
    if twice_r == den:
        # Exact tie: `q & 1` is 1 only when q is odd, so this steps to
        # the even neighbour on the same side of the exact quotient.
        q += q & 1
    elif twice_r > den:
        q += 1
    return q


# ---------------------------------------------------------------------------
# Count coercion
# ---------------------------------------------------------------------------

def coerce_count(value: Any, field: str) -> int:
    """Validate *value* as a non-negative transaction count and return a Python ``int``.

    Count semantics differ from monetary precision limits: counts are exact
    Python integers of arbitrary size (ES ``value_count`` returns a
    ``long``) and are *not* subject to the 2**53 monetary float guard.

    Accepts:
      * A Python ``int`` with ``value >= 0``.
      * A finite, integral ``float`` with ``value >= 0`` (ES may return
        small counts as ``float``; integrality and finiteness are checked
        explicitly before conversion to ``int``).

    Rejects (raises :class:`MonetaryCoercionError`):
      * ``bool`` (``True`` / ``False`` — a subclass of ``int``).
      * Negative values.
      * Fractional values.
      * ``NaN``, ``+inf``, ``-inf``.
      * ``None``, ``str``, ``bytes``, :class:`~decimal.Decimal`, and any
        other unsupported type.

    No permissive ``int()`` truncation is performed.

    Args:
        value:  Raw value read from an ES aggregation result.
        field:  Name of the output field this value feeds into.

    Returns:
        The value as a Python ``int``.

    Raises:
        MonetaryCoercionError: when *value* does not satisfy the contract.
    """
    if isinstance(value, bool):
        raise MonetaryCoercionError(field)

    if isinstance(value, int):
        if value < 0:
            raise MonetaryCoercionError(field)
        return value

    if isinstance(value, float):
        if not math.isfinite(value):
            raise MonetaryCoercionError(field)
        if not value.is_integer():
            raise MonetaryCoercionError(field)
        if value < 0:
            raise MonetaryCoercionError(field)
        return int(value)

    raise MonetaryCoercionError(field)


# ---------------------------------------------------------------------------
# Shared error envelope
# ---------------------------------------------------------------------------

def monetary_error(field: str) -> dict:
    """Return the canonical JSON error envelope for a monetary validation failure.

    The envelope identifies the invalid field but never the rejected value,
    so no monetary data or raw exception text leaks to the caller.

    Args:
        field:  Name of the field that failed validation.

    Returns:
        A dict with a single ``"error"`` key, suitable for ``json.dumps``.
    """
    return {"error": f"monetary value failed validation: {field}"}
