import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

import pytest
from elasticsearch import AsyncElasticsearch

from financial_mcp import es_client
from financial_mcp.es_client import (
    CATEGORY_MAPPING,
    ES_REASONS,
    PARENT_CATEGORY_MAP,
    ElasticsearchQueryError,
    classify_es_failure,
    es_error_payload,
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
                "amount": 0,
                "signed_amount": 0,
            }))

    def test_income_with_destination_rejected(self):
        with pytest.raises(ValueError, match="non-Transfer transaction must not have"):
            validate_transaction(_valid_doc({
                "type": "Income",
                "category": "Salary",
                "parent_category": "Financial",
                "destination_account": "BCA",
                "amount": 100000,
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


class TestAsyncEsClient:
    """Regression tests for the elasticsearch[async] dependency.

    These prove that ``AsyncElasticsearch`` can be constructed -- which
    requires ``aiohttp`` -- and that the async client lifecycle works,
    without a live Elasticsearch cluster. ``AsyncElasticsearch`` is patched
    with a MagicMock so the constructor never opens a network connection.
    """

    async def test_async_elasticsearch_constructs(self):
        """Proves aiohttp is available: construction raises ValueError if not."""
        client = AsyncElasticsearch(hosts=["http://localhost:9200"])
        assert isinstance(client, AsyncElasticsearch)
        await client.close()

    async def test_get_async_es_client_closes_on_exit(self):
        mock_cls = mock.MagicMock()
        mock_cls.return_value.close = mock.AsyncMock()
        with mock.patch.object(es_client, "AsyncElasticsearch", mock_cls):
            async with es_client.get_async_es_client() as client:
                assert client is mock_cls.return_value
        mock_cls.return_value.close.assert_awaited_once()

    async def test_get_async_es_client_passes_hosts(self):
        mock_cls = mock.MagicMock()
        mock_cls.return_value.close = mock.AsyncMock()
        with mock.patch.object(es_client, "AsyncElasticsearch", mock_cls), \
             mock.patch.object(es_client, "ES_API_KEY", None):
            async with es_client.get_async_es_client():
                pass
        options = mock_cls.call_args.kwargs
        assert options == {"hosts": [es_client.ES_HOST]}

    async def test_get_async_es_client_passes_api_key_when_set(self):
        mock_cls = mock.MagicMock()
        mock_cls.return_value.close = mock.AsyncMock()
        with mock.patch.object(es_client, "AsyncElasticsearch", mock_cls), \
             mock.patch.object(es_client, "ES_API_KEY", "test-key-123"):
            async with es_client.get_async_es_client():
                pass
        options = mock_cls.call_args.kwargs
        assert options["hosts"] == [es_client.ES_HOST]
        assert options["api_key"] == "test-key-123"

    async def test_get_async_es_client_no_api_key_when_unset(self):
        mock_cls = mock.MagicMock()
        mock_cls.return_value.close = mock.AsyncMock()
        with mock.patch.object(es_client, "AsyncElasticsearch", mock_cls), \
             mock.patch.object(es_client, "ES_API_KEY", None):
            async with es_client.get_async_es_client():
                pass
        options = mock_cls.call_args.kwargs
        assert "api_key" not in options


# ---------------------------------------------------------------------------
# F-007: sanitized Elasticsearch error classification
# ---------------------------------------------------------------------------

def _api_error(status: int, cls=None):
    from elasticsearch import ApiError

    meta = mock.MagicMock()
    meta.status = status
    return cls(status, meta, {}) if cls is not None else ApiError(status, meta, {})


def _transport_error(cause=None):
    from elasticsearch import TransportError

    if cause is None:
        return TransportError("transport failure")
    exc = TransportError(f"transport failure: {cause}")
    # Mirror ``raise ... from cause`` so __cause__ is set explicitly --
    # the classifier walks __cause__ before __context__.
    exc.__cause__ = cause
    return exc


class TestClassifyEsFailure:
    """The F-007 classifier maps the installed client's exception
    hierarchy onto the closed-set :data:`ES_REASONS`."""

    def test_auth_exceptions_map_to_es_auth(self):
        from elasticsearch import AuthenticationException, AuthorizationException

        assert classify_es_failure(_api_error(401, AuthenticationException)) == "es_auth"
        assert classify_es_failure(_api_error(403, AuthorizationException)) == "es_auth"

    def test_timeout_maps_to_es_timeout(self):
        from elastic_transport import ConnectionTimeout

        assert classify_es_failure(ConnectionTimeout("timed out")) == "es_timeout"

    def test_404_maps_to_es_not_found(self):
        from elasticsearch import NotFoundError

        assert classify_es_failure(_api_error(404, NotFoundError)) == "es_not_found"
        assert classify_es_failure(_api_error(404)) == "es_not_found"

    def test_400_409_map_to_es_validation(self):
        assert classify_es_failure(_api_error(400)) == "es_validation"
        assert classify_es_failure(_api_error(409)) == "es_validation"

    def test_other_http_statuses_map_to_es_unknown(self):
        assert classify_es_failure(_api_error(429)) == "es_unknown"
        assert classify_es_failure(_api_error(500)) == "es_unknown"
        assert classify_es_failure(_api_error(503)) == "es_unknown"

    def test_connection_error_maps_to_es_connection(self):
        from elasticsearch import ConnectionError

        assert classify_es_failure(ConnectionError("conn refused")) == "es_connection"

    def test_import_error_maps_to_es_dependency(self):
        assert classify_es_failure(ImportError("aiohttp missing")) == "es_dependency"
        assert classify_es_failure(ModuleNotFoundError("elastic_transport")) == "es_dependency"

    def test_transport_error_maps_to_es_connection(self):
        assert classify_es_failure(_transport_error()) == "es_connection"

    def test_transport_error_caused_by_timeout_maps_to_es_timeout(self):
        from elastic_transport import ConnectionTimeout

        exc = _transport_error(ConnectionTimeout("read timeout"))
        assert classify_es_failure(exc) == "es_timeout"

    def test_unrelated_exception_maps_to_es_unknown(self):
        assert classify_es_failure(ValueError("boom")) == "es_unknown"

    def test_all_reasons_are_closed_set_members(self):
        for reason in ES_REASONS:
            assert reason in ES_REASONS


class TestEsErrorPayload:
    """The error envelope carries only a static message + closed reason --
    never the underlying exception text, query, endpoint, or credentials."""

    def test_payload_shape_for_every_reason(self):
        for reason in ES_REASONS:
            payload = es_error_payload(reason)
            assert set(payload.keys()) == {"error", "reason"}
            assert payload["reason"] == reason
            assert isinstance(payload["error"], str) and payload["error"]

    def test_reasons_are_stable_and_disjoint(self):
        messages = {es_error_payload(r)["error"] for r in ES_REASONS}
        assert len(messages) == len(ES_REASONS)

    def test_no_leak_of_exception_detail(self):
        from elasticsearch import ConnectionError

        exc = ConnectionError(
            "Connection refused: https://es.internal:9243/fin_v3 "
            "api_key=sk-test"
        )
        payload = es_error_payload(classify_es_failure(exc))
        text = str(payload)
        assert "sk-test" not in text
        assert "es.internal" not in text
        assert "Connection refused" not in text
        assert payload["reason"] == "es_connection"


class TestQueryElasticsearchContract:
    """query_elasticsearch raises ElasticsearchQueryError with a sanitized
    reason; successful zero-hit results still return the response body."""

    @staticmethod
    def _raise_search_error(side_effect):
        client = mock.MagicMock()
        client.search = mock.AsyncMock(side_effect=side_effect)
        return client

    @pytest.mark.asyncio
    async def test_successful_empty_result_returns_body(self, monkeypatch):
        from elasticsearch import NotFoundError  # noqa: F401  (hierarchy check)
        body = {"hits": {"total": {"value": 0}, "hits": []}}
        client = mock.MagicMock()
        resp = mock.MagicMock()
        resp.body = body
        client.search = mock.AsyncMock(return_value=resp)
        with mock.patch.object(
            es_client, "get_async_es_client",
            mock.MagicMock(side_effect=lambda: _fake_cm(client)),
        ):
            result = await es_client.query_elasticsearch({"size": 50})
        assert result is body

    @pytest.mark.asyncio
    async def test_auth_failure_raises_sanitized_error(self, monkeypatch):
        from elasticsearch import AuthenticationException

        client = self._raise_search_error(_api_error(401, AuthenticationException))
        monkeypatch.setattr(
            es_client, "get_async_es_client", _fake_cm_factory(client)
        )
        with pytest.raises(ElasticsearchQueryError) as excinfo:
            await es_client.query_elasticsearch({"size": 50})
        assert excinfo.value.reason == "es_auth"
        assert excinfo.value.message == es_error_payload("es_auth")["error"]
        # No exception text in the sanitized error.
        assert "denied" not in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_timeout_raises_sanitized_error(self, monkeypatch):
        from elastic_transport import ConnectionTimeout

        client = self._raise_search_error(ConnectionTimeout("read timeout"))
        monkeypatch.setattr(
            es_client, "get_async_es_client", _fake_cm_factory(client)
        )
        with pytest.raises(ElasticsearchQueryError) as excinfo:
            await es_client.query_elasticsearch({"size": 50})
        assert excinfo.value.reason == "es_timeout"

    @pytest.mark.asyncio
    async def test_404_raises_sanitized_error(self, monkeypatch):
        from elasticsearch import NotFoundError

        client = self._raise_search_error(_api_error(404, NotFoundError))
        monkeypatch.setattr(
            es_client, "get_async_es_client", _fake_cm_factory(client)
        )
        with pytest.raises(ElasticsearchQueryError) as excinfo:
            await es_client.query_elasticsearch({"size": 50})
        assert excinfo.value.reason == "es_not_found"

    @pytest.mark.asyncio
    async def test_400_raises_sanitized_error(self, monkeypatch):
        client = self._raise_search_error(_api_error(400))
        monkeypatch.setattr(
            es_client, "get_async_es_client", _fake_cm_factory(client)
        )
        with pytest.raises(ElasticsearchQueryError) as excinfo:
            await es_client.query_elasticsearch({"size": 50})
        assert excinfo.value.reason == "es_validation"

    @pytest.mark.asyncio
    async def test_500_raises_sanitized_unknown(self, monkeypatch):
        client = self._raise_search_error(_api_error(500))
        monkeypatch.setattr(
            es_client, "get_async_es_client", _fake_cm_factory(client)
        )
        with pytest.raises(ElasticsearchQueryError) as excinfo:
            await es_client.query_elasticsearch({"size": 50})
        assert excinfo.value.reason == "es_unknown"

    @pytest.mark.asyncio
    async def test_connection_error_raises_sanitized_error(self, monkeypatch):
        from elasticsearch import ConnectionError

        client = self._raise_search_error(ConnectionError("reset"))
        monkeypatch.setattr(
            es_client, "get_async_es_client", _fake_cm_factory(client)
        )
        with pytest.raises(ElasticsearchQueryError) as excinfo:
            await es_client.query_elasticsearch({"size": 50})
        assert excinfo.value.reason == "es_connection"

    @pytest.mark.asyncio
    async def test_import_error_raises_sanitized_dependency(self, monkeypatch):
        client = self._raise_search_error(ImportError("aiohttp"))
        monkeypatch.setattr(
            es_client, "get_async_es_client", _fake_cm_factory(client)
        )
        with pytest.raises(ElasticsearchQueryError) as excinfo:
            await es_client.query_elasticsearch({"size": 50})
        assert excinfo.value.reason == "es_dependency"

    @pytest.mark.asyncio
    async def test_error_carries_cause_for_in_process_debugging(self, monkeypatch):
        from elasticsearch import ConnectionError

        original = ConnectionError("reset")
        client = self._raise_search_error(original)
        monkeypatch.setattr(
            es_client, "get_async_es_client", _fake_cm_factory(client)
        )
        with pytest.raises(ElasticsearchQueryError) as excinfo:
            await es_client.query_elasticsearch({"size": 50})
        assert excinfo.value.__cause__ is original


def _fake_cm_factory(client):
    """Patch target: a function returning the async CM context factory."""
    def factory(*args, **kwargs):
        return _fake_cm(client)
    return factory


def _fake_cm(client):
    @asynccontextmanager
    async def cm():
        yield client
    return cm()


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
