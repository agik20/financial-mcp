"""Tests for financial_mcp.converter: NaN handling, currency parsing, schema."""

import json
import pandas as pd
import pytest

from financial_mcp.converter import (
    _str_or_none,
    clean_currency,
    parse_date,
    transform_row,
)
from financial_mcp.es_client import REQUIRED_FIELDS


# ---------------------------------------------------------------------------
# _str_or_none
# ---------------------------------------------------------------------------

class TestStrOrNone:
    def test_nan_returns_none(self):
        assert _str_or_none(float("nan")) is None

    def test_none_returns_none(self):
        assert _str_or_none(None) is None

    def test_empty_string_returns_none(self):
        assert _str_or_none("") is None

    def test_whitespace_string_returns_none(self):
        assert _str_or_none("   ") is None

    def test_valid_string_is_stripped(self):
        assert _str_or_none("  hello  ") == "hello"

    def test_numeric_value_is_converted(self):
        assert _str_or_none(42) == "42"


# ---------------------------------------------------------------------------
# transform_row — nullable field handling
# ---------------------------------------------------------------------------

def _make_row(**overrides):
    """Build a synthetic pd.Series matching the 11-column CSV contract."""
    base = {
        "ID": "TX-00001",
        "Date": "01/15/2026",
        "Category": "Food & Drink",
        "Type": "Expense",
        "Account": "BNI",
        "Destination Account": None,
        "Amount": "Rp1,000",
        "Detail Transaction": "synthetic desc",
        "Pay Cycle": None,
        "Signed Amount": "-Rp1,000",
        "Parent": "Living",
    }
    base.update(overrides)
    return pd.Series(base)


class TestTransformRowNullableFields:
    def test_blank_description_becomes_none(self):
        row = _make_row(**{"Detail Transaction": None})
        result = transform_row(row)
        assert result["description"] is None

    def test_blank_pay_cycle_becomes_none(self):
        row = _make_row(**{"Pay Cycle": None})
        result = transform_row(row)
        assert result["pay_cycle"] is None

    def test_blank_destination_becomes_none(self):
        row = _make_row(**{"Destination Account": None})
        result = transform_row(row)
        assert result["destination_account"] is None

    def test_nonblank_description_preserved(self):
        row = _make_row(**{"Detail Transaction": "  grocery run  "})
        result = transform_row(row)
        assert result["description"] == "grocery run"

    def test_nonblank_destination_preserved(self):
        row = _make_row(**{"Destination Account": "  BCA  "})
        result = transform_row(row)
        assert result["destination_account"] == "BCA"

    def test_nonblank_pay_cycle_preserved(self):
        row = _make_row(**{"Pay Cycle": "01/31/2026"})
        result = transform_row(row)
        assert result["pay_cycle"] == "2026-01-31"

    def test_output_has_exactly_12_fields(self):
        result = transform_row(_make_row())
        assert set(result.keys()) == REQUIRED_FIELDS

    def test_existing_fields_preserved(self):
        result = transform_row(_make_row())
        assert result["transaction_id"] == "TX-00001"
        assert result["type"] == "Expense"
        assert result["amount"] == 1000
        assert result["signed_amount"] == -1000
        assert result["currency"] == "IDR"
        assert result["parent_category"] == "Living"
        assert result["source_account"] == "BNI"
        assert result["category"] == "Food & Drink"


# ---------------------------------------------------------------------------
# clean_currency
# ---------------------------------------------------------------------------

class TestCleanCurrency:
    def test_rp_comma_thousands(self):
        assert clean_currency("Rp 1,000") == 1000

    def test_plain_int(self):
        assert clean_currency("1000") == 1000

    def test_negative_rp(self):
        assert clean_currency("-Rp26,000") == -26000

    def test_nan_returns_none(self):
        assert clean_currency(float("nan")) is None

    def test_none_returns_none(self):
        assert clean_currency(None) is None

    def test_empty_string_raises(self):
        with pytest.raises(ValueError):
            clean_currency("")

    def test_indonesian_thousand_sep_unsupported(self):
        with pytest.raises(ValueError):
            clean_currency("Rp 1.000")


# ---------------------------------------------------------------------------
# JSON serialization
# ---------------------------------------------------------------------------

class TestJsonSerialization:
    def test_json_dump_rejects_nan(self, tmp_path):
        out = tmp_path / "out.json"
        with pytest.raises(ValueError):
            with open(out, "w", encoding="utf-8") as f:
                json.dump([float("nan")], f, allow_nan=False)

    def test_valid_record_serializes_cleanly(self):
        result = transform_row(_make_row())
        raw = json.dumps([result], allow_nan=False)
        parsed = json.loads(raw)
        assert parsed[0]["description"] == "synthetic desc"

    def test_none_description_serializes_as_null(self):
        result = transform_row(_make_row(**{"Detail Transaction": None}))
        raw = json.dumps([result], allow_nan=False)
        parsed = json.loads(raw)
        assert parsed[0]["description"] is None
