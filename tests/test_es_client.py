import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from financial_mcp.es_client import (
    CATEGORY_MAPPING,
    PARENT_CATEGORY_MAP,
    generate_actions,
    generate_doc_id,
    validate_date,
    validate_transaction,
)


def _valid_doc(overrides=None):
    doc = {
        "transaction_id": "TX-001",
        "transaction_date": "2026-01-15",
        "category": "Food & Drink",
        "type": "Expense",
        "parent_category": "Living",
        "source_account": "BNI",
        "destination_account": None,
        "amount": 150000,
        "signed_amount": -150000,
        "currency": "IDR",
        "description": "Makan siang",
        "pay_cycle": None,
    }
    if overrides:
        doc.update(overrides)
    return doc


def _write_json(tmp_path, docs):
    p = tmp_path / "data.json"
    p.write_text(json.dumps(docs), encoding="utf-8")
    return p


class TestGenerateDocId:
    def test_valid(self):
        assert generate_doc_id(_valid_doc()) == "TX-001"

    def test_missing_transaction_id(self):
        doc = _valid_doc()
        del doc["transaction_id"]
        with pytest.raises(ValueError, match="Missing or invalid"):
            generate_doc_id(doc)

    def test_empty_string(self):
        with pytest.raises(ValueError, match="Missing or invalid"):
            generate_doc_id(_valid_doc({"transaction_id": ""}))

    def test_none_id(self):
        with pytest.raises(ValueError, match="Missing or invalid"):
            generate_doc_id(_valid_doc({"transaction_id": None}))

    def test_non_string_id(self):
        with pytest.raises(ValueError, match="Missing or invalid"):
            generate_doc_id(_valid_doc({"transaction_id": 123}))

    def test_leading_whitespace(self):
        with pytest.raises(ValueError, match="Missing or invalid"):
            generate_doc_id(_valid_doc({"transaction_id": " TX-001"}))

    def test_trailing_whitespace(self):
        with pytest.raises(ValueError, match="Missing or invalid"):
            generate_doc_id(_valid_doc({"transaction_id": "TX-001 "}))

    def test_id_too_long(self):
        with pytest.raises(ValueError, match="exceeds Elasticsearch _id limit"):
            generate_doc_id(_valid_doc({"transaction_id": "x" * 513}))

    def test_id_at_limit(self):
        assert generate_doc_id(_valid_doc({"transaction_id": "x" * 512})) == "x" * 512


class TestValidateDate:
    def test_valid(self):
        validate_date("2026-01-15", "transaction_date", "TX-001")

    def test_leap_day_valid(self):
        validate_date("2024-02-29", "transaction_date", "TX-001")

    def test_impossible_date(self):
        with pytest.raises(ValueError, match="YYYY-MM-DD"):
            validate_date("2026-02-31", "transaction_date", "TX-001")

    def test_wrong_format(self):
        with pytest.raises(ValueError, match="YYYY-MM-DD"):
            validate_date("01/15/2026", "transaction_date", "TX-001")

    def test_non_string(self):
        with pytest.raises(ValueError, match="must be a string"):
            validate_date(20260115, "transaction_date", "TX-001")


class TestValidateTransaction:
    def test_missing_field(self):
        doc = _valid_doc()
        del doc["amount"]
        with pytest.raises(ValueError, match="Missing required field"):
            validate_transaction(doc)

    def test_extra_field(self):
        with pytest.raises(ValueError, match="Extra field"):
            validate_transaction(_valid_doc({"legacy_value": 100}))

    def test_bad_date(self):
        with pytest.raises(ValueError, match="YYYY-MM-DD"):
            validate_transaction(_valid_doc({"transaction_date": "2026-13-01"}))

    def test_unknown_category(self):
        with pytest.raises(ValueError, match="unknown category"):
            validate_transaction(_valid_doc({"category": "Unregistered"}))

    def test_type_mismatch(self):
        with pytest.raises(ValueError, match="does not allow type"):
            validate_transaction(_valid_doc({"type": "Income"}))

    def test_parent_mismatch(self):
        with pytest.raises(ValueError, match="does not belong to parent"):
            validate_transaction(_valid_doc({"parent_category": "Financial"}))

    def test_empty_source_account(self):
        with pytest.raises(ValueError, match="source_account must be a non-empty string"):
            validate_transaction(_valid_doc({"source_account": ""}))

    def test_zero_income_amount_valid(self):
        """Income with zero amount is valid per the data contract."""
        validate_transaction(_valid_doc({
            "type": "Income",
            "category": "Salary",
            "parent_category": "Financial",
            "amount": 0,
            "signed_amount": 0,
        }))

    def test_zero_expense_amount_valid(self):
        """Expense with zero amount is valid per the data contract."""
        validate_transaction(_valid_doc({
            "amount": 0,
            "signed_amount": 0,
        }))

    def test_zero_transfer_amount_rejected(self):
        """Transfer with zero amount is invalid -- must be > 0."""
        with pytest.raises(ValueError, match="Transfer amount must be greater than zero"):
            validate_transaction(_valid_doc({
                "type": "Transfer",
                "category": "Bank Transfer",
                "parent_category": "Internal",
                "destination_account": "BCA",
                "amount": 0,
                "signed_amount": 0,
            }))

    def test_non_transfer_with_destination_rejected(self):
        """Non-Transfer transactions must have destination_account = null."""
        with pytest.raises(ValueError, match="non-Transfer transaction must not have"):
            validate_transaction(_valid_doc({
                "destination_account": "BCA",
            }))

    def test_income_with_destination_rejected(self):
        with pytest.raises(ValueError, match="non-Transfer transaction must not have"):
            validate_transaction(_valid_doc({
                "type": "Income",
                "category": "Salary",
                "parent_category": "Financial",
                "destination_account": "BCA",
                "signed_amount": 100000,
            }))

    def test_nullable_description_valid(self):
        """description = None is valid."""
        validate_transaction(_valid_doc({"description": None}))

    def test_nullable_pay_cycle_valid(self):
        """pay_cycle = None is valid."""
        validate_transaction(_valid_doc({"pay_cycle": None}))

    def test_income_signed_zero_valid(self):
        """signed_amount = 0 for Income is valid (zero-value income)."""
        validate_transaction(_valid_doc({
            "type": "Income",
            "category": "Salary",
            "parent_category": "Financial",
            "amount": 0,
            "signed_amount": 0,
        }))

    def test_expense_signed_zero_valid(self):
        """signed_amount = 0 for Expense is valid (zero-value expense)."""
        validate_transaction(_valid_doc({
            "amount": 0,
            "signed_amount": 0,
        }))

    def test_transfer_parent_is_internal(self):
        """Bank Transfer must map to parent_category 'Internal'."""
        validate_transaction(_valid_doc({
            "type": "Transfer",
            "category": "Bank Transfer",
            "parent_category": "Internal",
            "destination_account": "BCA",
        }))

    def test_transfer_wrong_parent_rejected(self):
        with pytest.raises(ValueError, match="does not belong to parent"):
            validate_transaction(_valid_doc({
                "type": "Transfer",
                "category": "Bank Transfer",
                "parent_category": "Financial",
                "destination_account": "BCA",
            }))

    def test_negative_amount(self):
        with pytest.raises(ValueError, match="amount must be non-negative"):
            validate_transaction(_valid_doc({"amount": -100}))

    def test_float_amount(self):
        with pytest.raises(ValueError, match="amount must be an integer"):
            validate_transaction(_valid_doc({"amount": 150000.0}))

    def test_bool_amount(self):
        with pytest.raises(ValueError, match="amount must be an integer"):
            validate_transaction(_valid_doc({"amount": True}))

    def test_magnitude_mismatch(self):
        with pytest.raises(ValueError, match="signed_amount magnitude must equal amount"):
            validate_transaction(_valid_doc({"signed_amount": -200000}))

    def test_income_negative_signed_fails(self):
        with pytest.raises(ValueError, match="Income requires a non-negative"):
            validate_transaction(_valid_doc({
                "type": "Income",
                "category": "Salary",
                "parent_category": "Financial",
                "amount": 150000,
                "signed_amount": -150000,
            }))

    def test_income_valid(self):
        validate_transaction(_valid_doc({
            "type": "Income",
            "category": "Salary",
            "parent_category": "Financial",
            "signed_amount": 150000,
        }))

    def test_expense_valid(self):
        validate_transaction(_valid_doc())

    def test_expense_positive_signed_fails(self):
        with pytest.raises(ValueError, match="Expense requires a non-positive"):
            validate_transaction(_valid_doc({"signed_amount": 150000}))


    def test_transfer_valid(self):
        validate_transaction(_valid_doc({
            "type": "Transfer",
            "category": "Bank Transfer",
            "parent_category": "Internal",
            "destination_account": "BCA",
            "signed_amount": -150000,
        }))

    def test_transfer_positive_signed_fails(self):
        with pytest.raises(ValueError, match="Transfer requires a negative"):
            validate_transaction(_valid_doc({
                "type": "Transfer",
                "category": "Bank Transfer",
                "parent_category": "Internal",
                "destination_account": "BCA",
                "signed_amount": 150000,
            }))

    def test_transfer_no_destination(self):
        with pytest.raises(ValueError, match="Transfer requires a non-empty"):
            validate_transaction(_valid_doc({
                "type": "Transfer",
                "category": "Bank Transfer",
                "parent_category": "Internal",
                "destination_account": None,
            }))

    def test_transfer_same_source_and_destination(self):
        with pytest.raises(ValueError, match="must differ from source_account"):
            validate_transaction(_valid_doc({
                "type": "Transfer",
                "category": "Bank Transfer",
                "parent_category": "Internal",
                "destination_account": "BNI",
            }))

    def test_wrong_currency(self):
        with pytest.raises(ValueError, match="currency must be IDR"):
            validate_transaction(_valid_doc({"currency": "USD"}))

    def test_pay_cycle_none_valid(self):
        validate_transaction(_valid_doc({"pay_cycle": None}))

    def test_pay_cycle_bad_date(self):
        with pytest.raises(ValueError, match="YYYY-MM-DD"):
            validate_transaction(_valid_doc({"pay_cycle": "2026-00-01"}))

    def test_valid_full_doc(self):
        validate_transaction(_valid_doc())


class TestGenerateActions:
    def test_valid_batch(self, tmp_path):
        p = _write_json(tmp_path, [_valid_doc(), _valid_doc({"transaction_id": "TX-002"})])
        actions = list(generate_actions(str(p), "test-index"))
        assert len(actions) == 2
        assert actions[0]["_id"] == "TX-001"
        assert actions[1]["_id"] == "TX-002"
        assert actions[0]["_index"] == "test-index"
        assert actions[0]["_source"] == _valid_doc()

    def test_empty_file_fails(self, tmp_path):
        p = _write_json(tmp_path, [])
        with pytest.raises(ValueError, match="empty"):
            list(generate_actions(str(p), "test-index"))

    def test_root_not_list_fails(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text(json.dumps({"not": "a list"}), encoding="utf-8")
        with pytest.raises(ValueError, match="must be a list"):
            list(generate_actions(str(p), "test-index"))

    def test_duplicate_id_fails(self, tmp_path):
        p = _write_json(tmp_path, [_valid_doc(), _valid_doc()])
        with pytest.raises(ValueError, match="Duplicate transaction_id"):
            list(generate_actions(str(p), "test-index"))

    def test_bad_record_aborts_batch(self, tmp_path):
        docs = [
            _valid_doc(),
            _valid_doc({"transaction_id": "TX-BAD", "amount": 0}),
            _valid_doc({"transaction_id": "TX-003"}),
        ]
        p = _write_json(tmp_path, docs)
        with pytest.raises(ValueError):
            list(generate_actions(str(p), "test-index"))

    def test_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            list(generate_actions("/nonexistent/path.json", "test-index"))


class TestMappings:
    def test_all_categories_have_single_type(self):
        for cat, types in CATEGORY_MAPPING.items():
            assert len(types) == 1

    def test_all_categories_in_parent_map(self):
        all_cats = set().union(*PARENT_CATEGORY_MAP.values())
        for cat in CATEGORY_MAPPING:
            assert cat in all_cats, repr(cat) + " missing from PARENT_CATEGORY_MAP"

    def test_each_category_in_exactly_one_parent(self):
        for cat in CATEGORY_MAPPING:
            parents = [p for p, cats in PARENT_CATEGORY_MAP.items() if cat in cats]
            assert len(parents) == 1, repr(cat) + " in multiple parents: " + str(parents)
