"""Tests for financial_mcp.tools.money (monetary coercion + integer average)."""

import math
from decimal import Decimal

import pytest

from financial_mcp.tools.money import (
    MonetaryCoercionError,
    coerce_count,
    coerce_monetary,
    half_even_div,
    monetary_error,
)


class TestCoerceMonetaryAccepts:
    @pytest.mark.parametrize("value", [0, 1, 42, 100, 84_218_175, -7, -1])
    def test_accepts_python_integers(self, value):
        assert coerce_monetary(value, "f") == value
        assert isinstance(coerce_monetary(value, "f"), int)

    def test_accepts_last_safe_integer(self):
        assert coerce_monetary(2 ** 53 - 1, "f") == 2 ** 53 - 1

    def test_accepts_last_safe_integer_as_float(self):
        v = float(2 ** 53 - 1)
        assert coerce_monetary(v, "f") == 2 ** 53 - 1
        assert isinstance(coerce_monetary(v, "f"), int)

    @pytest.mark.parametrize("value", [0.0, 1.0, 42.0, 84_218_175.0, -7.0])
    def test_accepts_integral_floats_in_range(self, value):
        result = coerce_monetary(value, "f")
        assert result == int(value)
        assert isinstance(result, int)


class TestCoerceMonetaryRejects:
    @pytest.mark.parametrize("value", [True, False])
    def test_rejects_bool(self, value):
        with pytest.raises(MonetaryCoercionError):
            coerce_monetary(value, "f")

    @pytest.mark.parametrize("value", [0.5, 1.5, 20000.333, -0.0001, 123.456])
    def test_rejects_fractional_floats(self, value):
        with pytest.raises(MonetaryCoercionError):
            coerce_monetary(value, "f")

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_rejects_non_finite_floats(self, value):
        with pytest.raises(MonetaryCoercionError):
            coerce_monetary(value, "f")

    def test_rejects_float_two_pow_53(self):
        with pytest.raises(MonetaryCoercionError):
            coerce_monetary(float(2 ** 53), "f")

    def test_rejects_float_two_pow_53_plus_1(self):
        # float(2**53 + 1) collapses to 2**53, still out of contract.
        with pytest.raises(MonetaryCoercionError):
            coerce_monetary(float(2 ** 53 + 1), "f")

    @pytest.mark.parametrize(
        "value",
        [None, "123", "1.5", b"123", Decimal("123"), Decimal("123.5"), [], {}, (1,)],
    )
    def test_rejects_unsupported_types(self, value):
        with pytest.raises(MonetaryCoercionError):
            coerce_monetary(value, "f")


class TestCoerceMonetaryExceptionContract:
    def test_exception_carries_field_name(self):
        with pytest.raises(MonetaryCoercionError) as exc_info:
            coerce_monetary(float("nan"), "total_income")
        assert exc_info.value.field == "total_income"

    def test_exception_message_has_no_value(self):
        with pytest.raises(MonetaryCoercionError) as exc_info:
            coerce_monetary(999_999.5, "grand_total")
        message = str(exc_info.value)
        assert "999999" not in message
        assert "999_999" not in message
        assert "999999.5" not in message
        assert message == "grand_total"

    def test_exception_does_not_retain_rejected_value(self):
        with pytest.raises(MonetaryCoercionError) as exc_info:
            coerce_monetary(float("inf"), "f")
        # No attribute on the exception should hold the raw value.
        for attr in vars(exc_info.value).values():
            assert attr != float("inf")


class TestHalfEvenDiv:
    @pytest.mark.parametrize(
        "num,den,expected",
        [
            (5, 2, 2),
            (7, 2, 4),
            (5, 3, 2),
            (8, 3, 3),
            (1, 2, 0),
            (3, 2, 2),
            # 5/2 = 2.5 -> tie -> 2 (even); 3/2 = 1.5 -> tie -> 2 (even).
            # 4/2 = 2 is exact, not a tie.
            (4, 2, 2),
            (6, 2, 3),  # 6/2 = 3 exact
            (10, 2, 5),  # 10/2 = 5 exact
        ],
    )
    def test_required_examples(self, num, den, expected):
        assert half_even_div(num, den) == expected

    @pytest.mark.parametrize(
        "num,den,expected",
        [
            (-5, 2, -2),
            (-7, 2, -4),
            (-8, 3, -3),
            (-1, 2, 0),
            (-3, 2, -2),
        ],
    )
    def test_negative_numerators(self, num, den, expected):
        assert half_even_div(num, den) == expected

    @pytest.mark.parametrize("num", [-6, -4, -2, 0, 2, 4, 6])
    def test_zero_numerator_and_even_division(self, num):
        assert half_even_div(num, 2) == num // 2

    def test_zero_numerator(self):
        assert half_even_div(0, 5) == 0

    def test_large_exact_operands(self):
        assert half_even_div(2 ** 53 - 1, 1) == 2 ** 53 - 1
        # (2**53 - 1) / 2 is an exact tie; half-to-even rounds up to the
        # even neighbour, which is not the floor quotient.
        assert half_even_div(2 ** 53 - 1, 2) == 2 ** 52

    @pytest.mark.parametrize("den", [0, -1, -5])
    def test_rejects_non_positive_denominator(self, den):
        with pytest.raises(ValueError):
            half_even_div(10, den)

    @pytest.mark.parametrize("den", [True, False])
    def test_rejects_bool_denominator(self, den):
        with pytest.raises(ValueError):
            half_even_div(10, den)

    @pytest.mark.parametrize("den", [2.0, "2", None, Decimal("2")])
    def test_rejects_non_int_denominator(self, den):
        with pytest.raises(ValueError):
            half_even_div(10, den)

    def test_no_float_arithmetic_matches_python_round(self):
        """For non-negative inputs the result matches round(num/den)."""
        for num in range(0, 40):
            for den in range(1, 8):
                assert half_even_div(num, den) == round(num / den)

    def test_math_isnan_not_involved(self):
        # Guard against accidental float division of huge operands.
        result = half_even_div(2 ** 53 - 1, 3)
        assert math.isfinite(result)


class TestCoerceCountAccepts:
    @pytest.mark.parametrize("value", [0, 1, 42, 1_000_000, 10**20])
    def test_accepts_python_integers(self, value):
        assert coerce_count(value, "f") == value
        assert isinstance(coerce_count(value, "f"), int)

    @pytest.mark.parametrize("value", [0.0, 1.0, 42.0, 1_000_000.0])
    def test_accepts_integral_floats(self, value):
        result = coerce_count(value, "f")
        assert result == int(value)
        assert isinstance(result, int)

    def test_accepts_large_count_unaffected_by_monetary_guard(self):
        # Counts have no 2**53 guard — arbitrary-size ints pass through.
        assert coerce_count(2 ** 53, "f") == 2 ** 53
        assert coerce_count(2 ** 200, "f") == 2 ** 200


class TestCoerceCountRejects:
    @pytest.mark.parametrize("value", [True, False])
    def test_rejects_bool(self, value):
        with pytest.raises(MonetaryCoercionError):
            coerce_count(value, "f")

    @pytest.mark.parametrize("value", [-1, -7, -100])
    def test_rejects_negative_integers(self, value):
        with pytest.raises(MonetaryCoercionError):
            coerce_count(value, "f")

    @pytest.mark.parametrize("value", [-0.5, -1.5, 0.5, 1.5])
    def test_rejects_fractional_floats(self, value):
        with pytest.raises(MonetaryCoercionError):
            coerce_count(value, "f")

    def test_rejects_negative_integral_float(self):
        with pytest.raises(MonetaryCoercionError):
            coerce_count(-1.0, "f")

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_rejects_non_finite_floats(self, value):
        with pytest.raises(MonetaryCoercionError):
            coerce_count(value, "f")

    @pytest.mark.parametrize(
        "value",
        [None, "42", b"42", Decimal("42"), Decimal("42.5"), [], {}, (1,)],
    )
    def test_rejects_unsupported_types(self, value):
        with pytest.raises(MonetaryCoercionError):
            coerce_count(value, "f")


class TestCoerceCountExceptionContract:
    def test_exception_carries_field_name(self):
        with pytest.raises(MonetaryCoercionError) as exc_info:
            coerce_count(-1, "categories.tx_count")
        assert exc_info.value.field == "categories.tx_count"

    def test_exception_message_has_no_value(self):
        with pytest.raises(MonetaryCoercionError) as exc_info:
            coerce_count(-42, "f")
        message = str(exc_info.value)
        assert "-42" not in message
        assert message == "f"


class TestMonetaryErrorEnvelope:
    def test_envelope_shape(self):
        assert monetary_error("total_expense") == {
            "error": "monetary value failed validation: total_expense"
        }

    def test_envelope_contains_no_value(self):
        envelope = monetary_error("grand_total")
        assert "grand_total" in envelope["error"]
        assert str(200000) not in envelope["error"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
