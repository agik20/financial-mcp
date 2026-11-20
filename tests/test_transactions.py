"""Tests for financial_mcp.tools.transactions (canonical field usage)."""

import json
import pytest

from pydantic import ValidationError

from financial_mcp.models import QueryTransactionParams
from financial_mcp.tools.transactions import register_transaction_tools
from mcp.server.fastmcp import FastMCP


@pytest.fixture
def mcp():
    mcp = FastMCP("test-transactions")
    register_transaction_tools(mcp)
    return mcp


class TestGetAllTransactions:
    @pytest.mark.asyncio
    async def test_sorts_by_transaction_date(self, mcp, monkeypatch):
        import financial_mcp.tools.transactions as mod
        captured = {}

        async def fake_query(q):
            captured["query"] = q
            return {"hits": {"total": {"value": 1}, "hits": [{"_source": {"transaction_date": "2026-01-01"}}]}}

        monkeypatch.setattr(mod, "query_elasticsearch", fake_query)
        tool = mcp._tool_manager._tools["get_all_transactions"].fn
        result = json.loads(await tool())
        assert result["data"][0]["transaction_date"] == "2026-01-01"
        assert captured["query"]["sort"] == [{"transaction_date": {"order": "desc"}}]

    @pytest.mark.asyncio
    async def test_returns_error_when_query_fails(self, mcp, monkeypatch):
        import financial_mcp.tools.transactions as mod
        from financial_mcp.es_client import ElasticsearchQueryError, es_error_payload

        async def fail_query(q):
            raise ElasticsearchQueryError(
                es_error_payload("es_connection")["error"],
                reason="es_connection",
            )

        monkeypatch.setattr(mod, "query_elasticsearch", fail_query)
        tool = mcp._tool_manager._tools["get_all_transactions"].fn
        result = json.loads(await tool())
        assert result == es_error_payload("es_connection")
        assert "data" not in result
        assert "total_records" not in result

class TestQueryTransactions:
    @pytest.mark.asyncio
    async def test_no_legacy_fields_in_query(self, mcp, monkeypatch):
        import financial_mcp.tools.transactions as mod
        captured = {}

        async def fake_query(q):
            captured["query"] = q
            return {"hits": {"total": {"value": 0}, "hits": []}}

        monkeypatch.setattr(mod, "query_elasticsearch", fake_query)
        params = QueryTransactionParams(start_date="2026-01-01", end_date="2026-01-31",
                                        type="Expense", category="Loan",
                                        source_account="BNI", limit=10)
        tool = mcp._tool_manager._tools["query_transactions"].fn
        json.loads(await tool(params))
        q = captured["query"]
        raw = json.dumps(q)
        for legacy in ("creationDate", "day", "sourceName", "value", "payCycle"):
            assert f'"{legacy}"' not in raw, f"legacy field {legacy!r} present in query"
        assert "transaction_date" in raw
        assert "source_account" in raw
        assert "category" in raw

    @pytest.mark.asyncio
    async def test_date_filter_uses_transaction_date(self, mcp, monkeypatch):
        import financial_mcp.tools.transactions as mod
        captured = {}

        async def fake_query(q):
            captured["query"] = q
            return {"hits": {"total": {"value": 0}, "hits": []}}

        monkeypatch.setattr(mod, "query_elasticsearch", fake_query)
        params = QueryTransactionParams(start_date="2026-01-01", end_date="2026-01-31")
        tool = mcp._tool_manager._tools["query_transactions"].fn
        json.loads(await tool(params))
        must = captured["query"]["query"]["bool"]["must"]
        assert {"range": {"transaction_date": {"gte": "2026-01-01", "lte": "2026-01-31"}}} in must

    @pytest.mark.asyncio
    async def test_type_filter(self, mcp, monkeypatch):
        import financial_mcp.tools.transactions as mod
        captured = {}

        async def fake_query(q):
            captured["query"] = q
            return {"hits": {"total": {"value": 0}, "hits": []}}

        monkeypatch.setattr(mod, "query_elasticsearch", fake_query)
        params = QueryTransactionParams(type="Income")
        tool = mcp._tool_manager._tools["query_transactions"].fn
        json.loads(await tool(params))
        must = captured["query"]["query"]["bool"]["must"]
        assert {"term": {"type": "Income"}} in must

    @pytest.mark.asyncio
    async def test_source_account_filter(self, mcp, monkeypatch):
        import financial_mcp.tools.transactions as mod
        captured = {}

        async def fake_query(q):
            captured["query"] = q
            return {"hits": {"total": {"value": 0}, "hits": []}}

        monkeypatch.setattr(mod, "query_elasticsearch", fake_query)
        params = QueryTransactionParams(source_account="BNI")
        tool = mcp._tool_manager._tools["query_transactions"].fn
        json.loads(await tool(params))
        must = captured["query"]["query"]["bool"]["must"]
        assert {"term": {"source_account": "BNI"}} in must

    @pytest.mark.asyncio
    async def test_legacy_source_name_folds_into_source_account(self, mcp, monkeypatch):
        import financial_mcp.tools.transactions as mod
        captured = {}

        async def fake_query(q):
            captured["query"] = q
            return {"hits": {"total": {"value": 0}, "hits": []}}

        monkeypatch.setattr(mod, "query_elasticsearch", fake_query)
        params = QueryTransactionParams(source_name="BNI")
        assert params.source_account == "BNI"
        assert params.source_name is None
        tool = mcp._tool_manager._tools["query_transactions"].fn
        json.loads(await tool(params))
        must = captured["query"]["query"]["bool"]["must"]
        assert {"term": {"source_account": "BNI"}} in must

    @pytest.mark.asyncio
    async def test_daily_aggregation_uses_transaction_date(self, mcp, monkeypatch):
        import financial_mcp.tools.transactions as mod
        captured = {}

        async def fake_query(q):
            captured["query"] = q
            return {
                "aggregations": {
                    "time_series": {
                        "buckets": [
                            {
                                "key_as_string": "2026-01-01",
                                "total_expense": {"sum": {"value": 100}},
                                "total_income": {"sum": {"value": 5000}},
                                "tx_count": {"value": 2},
                            }
                        ]
                    }
                }
            }

        monkeypatch.setattr(mod, "query_elasticsearch", fake_query)
        params = QueryTransactionParams(aggregation="daily")
        tool = mcp._tool_manager._tools["query_transactions"].fn
        result = json.loads(await tool(params))
        agg = captured["query"]["aggs"]["time_series"]
        assert agg["date_histogram"]["field"] == "transaction_date"
        assert agg["date_histogram"]["calendar_interval"] == "1d"
        # Expense sums the positive magnitude field (amount).
        assert agg["aggs"]["total_expense"]["aggs"]["sum"]["sum"]["field"] == "amount"
        # Income sums signed_amount (positive by construction).
        assert agg["aggs"]["total_income"]["aggs"]["sum"]["sum"]["field"] == "signed_amount"
        # No net sub-agg -- computed client-side as total_income - total_expense
        # so Transfers are excluded by construction.
        assert "net" not in agg["aggs"], "net sub-agg present -- Transfers would be included"
        # Count uses signed_amount as a stand-in for "all rows have a value".
        assert agg["aggs"]["tx_count"]["value_count"]["field"] == "signed_amount"
        assert result["data"][0]["total_expense"] == 100
        assert result["data"][0]["total_income"] == 5000
        # net = 5000 - 100 = 4900 (transfers excluded)
        assert result["data"][0]["net"] == 4900

    @pytest.mark.asyncio
    async def test_time_series_net_excludes_transfers(self, mcp, monkeypatch):
        """Regression: net = total_income - total_expense; Transfers are not in the sum.
        Data contract example: Income +10,000,000; Expense 500,000; Transfer -2,000,000
        Expected net = 9,500,000 (not 7,500,000 which would include the transfer)."""
        import financial_mcp.tools.transactions as mod
        captured = {}

        async def fake_query(q):
            captured["query"] = q
            return {
                "aggregations": {
                    "time_series": {
                        "buckets": [
                            {
                                "key_as_string": "2026-03-01",
                                "total_expense": {"sum": {"value": 500_000}},
                                "total_income": {"sum": {"value": 10_000_000}},
                                "tx_count": {"value": 3},
                            }
                        ]
                    }
                }
            }

        monkeypatch.setattr(mod, "query_elasticsearch", fake_query)
        params = QueryTransactionParams(aggregation="monthly")
        tool = mcp._tool_manager._tools["query_transactions"].fn
        result = json.loads(await tool(params))
        bucket = result["data"][0]
        assert bucket["total_income"] == 10_000_000
        assert bucket["total_expense"] == 500_000
        # Transfers excluded: 10,000,000 - 500,000 = 9,500,000
        assert bucket["net"] == 9_500_000
        # Confirm no net sub-agg in the ES query.
        agg = captured["query"]["aggs"]["time_series"]
        assert "net" not in agg["aggs"]

    @pytest.mark.asyncio
    async def test_weekly_monthly_intervals(self, mcp, monkeypatch):
        import financial_mcp.tools.transactions as mod
        captured = {}

        async def fake_query(q):
            captured["query"] = q
            return {"aggregations": {"time_series": {"buckets": []}}}

        monkeypatch.setattr(mod, "query_elasticsearch", fake_query)
        tool = mcp._tool_manager._tools["query_transactions"].fn
        for agg, expected_interval in (("weekly", "1w"), ("monthly", "1M")):
            json.loads(await tool(QueryTransactionParams(aggregation=agg)))
            assert captured["query"]["aggs"]["time_series"]["date_histogram"]["calendar_interval"] == expected_interval

# ---------------------------------------------------------------------------
# F-001: Completeness metadata tests
# ---------------------------------------------------------------------------


def _make_hits(value: int, relation: str, n_records: int) -> dict:
    """Build a synthetic ES response body with *n_records* hits."""
    hits = [
        {"_source": {"transaction_date": "2026-01-01", "amount": i, "signed_amount": i}}
        for i in range(n_records)
    ]
    return {"hits": {"total": {"value": value, "relation": relation}, "hits": hits}}


class TestQueryConstruction:
    @pytest.mark.asyncio
    async def test_get_all_includes_track_total_hits(self, mcp, monkeypatch):
        import financial_mcp.tools.transactions as mod
        captured = {}

        async def fake(q):
            captured["q"] = q
            return _make_hits(1, "eq", 1)

        monkeypatch.setattr(mod, "query_elasticsearch", fake)
        tool = mcp._tool_manager._tools["get_all_transactions"].fn
        json.loads(await tool())
        assert captured["q"].get("track_total_hits") is True

    @pytest.mark.asyncio
    async def test_query_raw_includes_track_total_hits(self, mcp, monkeypatch):
        import financial_mcp.tools.transactions as mod
        captured = {}

        async def fake(q):
            captured["q"] = q
            return _make_hits(0, "eq", 0)

        monkeypatch.setattr(mod, "query_elasticsearch", fake)
        tool = mcp._tool_manager._tools["query_transactions"].fn
        json.loads(await tool(QueryTransactionParams()))
        assert captured["q"].get("track_total_hits") is True

    @pytest.mark.asyncio
    async def test_query_agg_does_not_force_track_total_hits(self, mcp, monkeypatch):
        """Aggregation mode does not set track_total_hits -- exact hit
        tracking is only needed for raw-record completeness metadata."""
        import financial_mcp.tools.transactions as mod
        captured = {}

        async def fake(q):
            captured["q"] = q
            return {"aggregations": {"time_series": {"buckets": []}}}

        monkeypatch.setattr(mod, "query_elasticsearch", fake)
        tool = mcp._tool_manager._tools["query_transactions"].fn
        json.loads(await tool(QueryTransactionParams(aggregation="daily")))
        assert captured["q"].get("track_total_hits") is not True


class TestCompletenessMetadata:
    """F-001 Cases A-F for query_transactions raw mode."""

    @pytest.mark.asyncio
    async def test_case_a_more_records_eq(self, mcp, monkeypatch):
        import financial_mcp.tools.transactions as mod
        resp = _make_hits(15000, "eq", 50)

        async def fake(q):
            return resp

        monkeypatch.setattr(mod, "query_elasticsearch", fake)
        tool = mcp._tool_manager._tools["query_transactions"].fn
        result = json.loads(await tool(QueryTransactionParams()))
        assert result["total_matched"] == 15000
        assert result["returned"] == 50
        assert result["has_more"] is True
        assert result["total_relation"] == "eq"

    @pytest.mark.asyncio
    async def test_case_b_all_returned_eq(self, mcp, monkeypatch):
        import financial_mcp.tools.transactions as mod
        resp = _make_hits(50, "eq", 50)

        async def fake(q):
            return resp

        monkeypatch.setattr(mod, "query_elasticsearch", fake)
        tool = mcp._tool_manager._tools["query_transactions"].fn
        result = json.loads(await tool(QueryTransactionParams()))
        assert result["has_more"] is False
        assert result["total_relation"] == "eq"
        assert result["total_matched"] == 50
        assert result["returned"] == 50
        assert isinstance(result["total_matched"], int)
        assert isinstance(result["returned"], int)
        assert isinstance(result["data"], list)

    @pytest.mark.asyncio
    async def test_case_c_empty_eq(self, mcp, monkeypatch):
        import financial_mcp.tools.transactions as mod
        resp = _make_hits(0, "eq", 0)

        async def fake(q):
            return resp

        monkeypatch.setattr(mod, "query_elasticsearch", fake)
        tool = mcp._tool_manager._tools["query_transactions"].fn
        result = json.loads(await tool(QueryTransactionParams()))
        assert result["total_matched"] == 0
        assert result["returned"] == 0
        assert result["has_more"] is False
        assert result["data"] == []

    @pytest.mark.asyncio
    async def test_case_d_lower_bound_gte_more(self, mcp, monkeypatch):
        import financial_mcp.tools.transactions as mod
        resp = _make_hits(10000, "gte", 50)

        async def fake(q):
            return resp

        monkeypatch.setattr(mod, "query_elasticsearch", fake)
        tool = mcp._tool_manager._tools["query_transactions"].fn
        result = json.loads(await tool(QueryTransactionParams()))
        assert result["has_more"] is True
        assert result["total_relation"] == "gte"
        assert result["total_matched"] == 10000

    @pytest.mark.asyncio
    async def test_case_e_lower_bound_gte_ambiguous(self, mcp, monkeypatch):
        import financial_mcp.tools.transactions as mod
        # gte with total == returned: completeness unknown -> has_more is null (None)
        resp = _make_hits(10000, "gte", 10000)

        async def fake(q):
            return resp

        monkeypatch.setattr(mod, "query_elasticsearch", fake)
        tool = mcp._tool_manager._tools["query_transactions"].fn
        result = json.loads(await tool(QueryTransactionParams()))
        assert result["has_more"] is None
        assert result["total_relation"] == "gte"
        assert result["has_more"] is not False

    @pytest.mark.asyncio
    async def test_case_f_legacy_scalar_total(self, mcp, monkeypatch):
        import financial_mcp.tools.transactions as mod
        # Legacy ES 7.x shape: hits.total is a plain int
        resp = {"hits": {"total": 50, "hits": [
            {"_source": {"transaction_date": "2026-01-01"}},
        ] * 50}}

        async def fake(q):
            return resp

        monkeypatch.setattr(mod, "query_elasticsearch", fake)
        tool = mcp._tool_manager._tools["query_transactions"].fn
        result = json.loads(await tool(QueryTransactionParams()))
        assert result["total_matched"] == 50
        assert result["total_relation"] == "eq"
        assert result["has_more"] is False


class TestBackwardCompatibility:
    @pytest.mark.asyncio
    async def test_raw_response_has_all_original_fields(self, mcp, monkeypatch):
        import financial_mcp.tools.transactions as mod
        resp = _make_hits(5, "eq", 5)

        async def fake(q):
            return resp

        monkeypatch.setattr(mod, "query_elasticsearch", fake)
        tool = mcp._tool_manager._tools["query_transactions"].fn
        result = json.loads(await tool(QueryTransactionParams()))
        for key in ("total_matched", "returned", "data"):
            assert key in result, f"missing original key: {key}"
        assert isinstance(result["total_matched"], int)
        assert isinstance(result["returned"], int)
        assert isinstance(result["data"], list)

    @pytest.mark.asyncio
    async def test_get_all_response_preserves_total_records(self, mcp, monkeypatch):
        import financial_mcp.tools.transactions as mod
        resp = _make_hits(200, "eq", 50)

        async def fake(q):
            return resp

        monkeypatch.setattr(mod, "query_elasticsearch", fake)
        tool = mcp._tool_manager._tools["get_all_transactions"].fn
        result = json.loads(await tool())
        assert result["total_records"] == 200
        assert result["returned"] == 50
        assert isinstance(result["data"], list)
        assert "has_more" not in result
        assert "total_relation" not in result

    @pytest.mark.asyncio
    async def test_get_all_legacy_scalar_total(self, mcp, monkeypatch):
        """F-004: get_all_transactions handles legacy scalar hits.total."""
        import financial_mcp.tools.transactions as mod
        resp = {
            "hits": {
                "total": 50,
                "hits": [{"_source": {"transaction_date": "2026-01-01"}}] * 50,
            }
        }

        async def fake(q):
            return resp

        monkeypatch.setattr(mod, "query_elasticsearch", fake)
        tool = mcp._tool_manager._tools["get_all_transactions"].fn
        result = json.loads(await tool())
        assert result["total_records"] == 50
        assert result["returned"] == 50
        assert isinstance(result["data"], list)
        assert "has_more" not in result
        assert "total_relation" not in result

    @pytest.mark.asyncio
    async def test_agg_response_unchanged(self, mcp, monkeypatch):
        import financial_mcp.tools.transactions as mod

        async def fake(q):
            return {
                "aggregations": {
                    "time_series": {
                        "buckets": [
                            {
                                "key_as_string": "2026-01-01",
                                "total_expense": {"sum": {"value": 100}},
                                "total_income": {"sum": {"value": 50}},
                                "tx_count": {"value": 2},
                            }
                        ]
                    }
                }
            }

        monkeypatch.setattr(mod, "query_elasticsearch", fake)
        tool = mcp._tool_manager._tools["query_transactions"].fn
        result = json.loads(await tool(QueryTransactionParams(aggregation="daily")))
        assert set(result.keys()) == {"aggregation", "total_periods", "data"}
        assert result["aggregation"] == "daily"
        assert result["total_periods"] == 1
        assert "has_more" not in result
        assert "total_relation" not in result


class TestDateRangeValidation:
    """F-010: cross-field date-range validation in QueryTransactionParams."""

    def test_inverted_range_rejected(self):
        with pytest.raises(ValidationError, match="must not be after"):
            QueryTransactionParams(start_date="2026-06-01", end_date="2026-01-01")

    def test_equal_dates_accepted(self):
        p = QueryTransactionParams(start_date="2026-01-01", end_date="2026-01-01")
        assert p.start_date == "2026-01-01"
        assert p.end_date == "2026-01-01"

    def test_start_only_accepted(self):
        p = QueryTransactionParams(start_date="2026-01-01")
        assert p.end_date is None

    def test_end_only_accepted(self):
        p = QueryTransactionParams(end_date="2026-01-31")
        assert p.start_date is None

    def test_both_omitted_accepted(self):
        p = QueryTransactionParams()
        assert p.start_date is None
        assert p.end_date is None

    def test_invalid_date_format_still_rejected(self):
        with pytest.raises(ValidationError, match="Invalid date format"):
            QueryTransactionParams(start_date="not-a-date")

    @pytest.mark.asyncio
    async def test_inverted_range_rejected_before_es_call(self, mcp, monkeypatch):
        import financial_mcp.tools.transactions as mod
        called = {}

        async def fake_query(q):
            called["query"] = q
            return {"hits": {"total": {"value": 0}, "hits": []}}

        monkeypatch.setattr(mod, "query_elasticsearch", fake_query)
        tool = mcp._tool_manager._tools["query_transactions"].fn
        with pytest.raises(ValidationError, match="must not be after"):
            await tool(QueryTransactionParams(
                start_date="2026-06-01",
                end_date="2026-01-01",
            ))
        assert "query" not in called


class TestLimitValidation:
    """F-005: explicit bounds on QueryTransactionParams.limit."""

    @pytest.mark.parametrize("value", [1, 50, 10000])
    def test_valid_limits_accepted(self, value):
        assert QueryTransactionParams(limit=value).limit == value

    def test_default_limit_when_omitted(self):
        assert QueryTransactionParams().limit == 50

    @pytest.mark.parametrize("value", [0, -1, -100, 10001, 1000000, None])
    def test_out_of_range_or_null_limits_rejected(self, value):
        with pytest.raises(ValidationError):
            QueryTransactionParams(limit=value)

    def test_non_integer_limit_rejected(self):
        with pytest.raises(ValidationError):
            QueryTransactionParams(limit="abc")

    # Pydantic v2 lax mode coerces numeric strings: "50" → 50 (int).
    # This is accepted behaviour; documented here so it is intentional.
    def test_numeric_string_coerced_to_int(self):
        assert QueryTransactionParams(limit="50").limit == 50

    def test_float_limit_rejected(self):
        with pytest.raises(ValidationError):
            QueryTransactionParams(limit=50.5)


# ---------------------------------------------------------------------------
# F-010: cross-field date-range validation
# ---------------------------------------------------------------------------

class TestDateRangeValidation:
    """F-010: start_date > end_date must be rejected at model construction,
    before any Elasticsearch request can occur."""

    def test_inverted_range_rejected(self):
        with pytest.raises(ValidationError, match="start_date must not be after end_date"):
            QueryTransactionParams(start_date="2026-06-01", end_date="2026-01-01")

    def test_equal_dates_accepted(self):
        params = QueryTransactionParams(start_date="2026-01-01", end_date="2026-01-01")
        assert params.start_date == "2026-01-01"
        assert params.end_date == "2026-01-01"

    def test_start_date_only_accepted(self):
        params = QueryTransactionParams(start_date="2026-01-01")
        assert params.start_date == "2026-01-01"
        assert params.end_date is None

    def test_end_date_only_accepted(self):
        params = QueryTransactionParams(end_date="2026-12-31")
        assert params.start_date is None
        assert params.end_date == "2026-12-31"

    def test_both_omitted_accepted(self):
        params = QueryTransactionParams()
        assert params.start_date is None
        assert params.end_date is None

    def test_invalid_date_format_still_rejected(self):
        with pytest.raises(ValidationError, match="Invalid date format"):
            QueryTransactionParams(start_date="2026-13-01", end_date="2026-01-01")

    @pytest.mark.asyncio
    async def test_inverted_range_rejected_before_es_call(self, mcp):
        import financial_mcp.tools.transactions as mod
        called = False

        async def fail_if_called(q):
            nonlocal called
            called = True
            return {"hits": {"total": {"value": 0}, "hits": []}}

        mod.query_elasticsearch = fail_if_called
        try:
            with pytest.raises(ValidationError, match="start_date must not be after end_date"):
                QueryTransactionParams(start_date="2026-06-01", end_date="2026-01-01")
            assert not called, "Elasticsearch must not be called for an invalid date range"
        finally:
            # Restore the real query_elasticsearch so other tests are unaffected
            import importlib
            import financial_mcp.es_client as es_client_mod
            mod.query_elasticsearch = es_client_mod.query_elasticsearch


# ---------------------------------------------------------------------------
# F-007: caller-level error response tests
# ---------------------------------------------------------------------------

from financial_mcp.es_client import ElasticsearchQueryError, es_error_payload

_F007_REASONS = ("es_connection", "es_auth", "es_validation")


class TestF007CallerLevel:
    """Verify that each MCP tool caller translates ElasticsearchQueryError
    into a sanitized JSON error payload without leaking raw exception text,
    endpoint, query body, credentials, or transaction data."""

    def _make_fail(self, reason):
        async def _fail(q):
            raise ElasticsearchQueryError(
                es_error_payload(reason)["error"], reason=reason
            )
        return _fail

    # -- get_all_transactions --------------------------------------------------

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reason", _F007_REASONS)
    async def test_get_all_transactions_returns_sanitized_error(self, mcp, monkeypatch, reason):
        import financial_mcp.tools.transactions as mod
        monkeypatch.setattr(mod, "query_elasticsearch", self._make_fail(reason))
        tool = mcp._tool_manager._tools["get_all_transactions"].fn
        result = json.loads(await tool())
        assert "error" in result
        assert result["reason"] == reason
        # No raw exception text / endpoint / query body leaked
        assert "query" not in result
        assert "http" not in result.get("error", "").lower()

    # -- query_transactions raw branch ---------------------------------------

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reason", _F007_REASONS)
    async def test_query_transactions_raw_returns_sanitized_error(self, mcp, monkeypatch, reason):
        import financial_mcp.tools.transactions as mod
        monkeypatch.setattr(mod, "query_elasticsearch", self._make_fail(reason))
        tool = mcp._tool_manager._tools["query_transactions"].fn
        params = QueryTransactionParams(start_date="2026-01-01", end_date="2026-01-31")
        result = json.loads(await tool(params))
        assert "error" in result
        assert result["reason"] == reason

    # -- query_transactions aggregation branch -------------------------------

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reason", _F007_REASONS)
    async def test_query_transactions_agg_returns_sanitized_error(self, mcp, monkeypatch, reason):
        import financial_mcp.tools.transactions as mod
        monkeypatch.setattr(mod, "query_elasticsearch", self._make_fail(reason))
        tool = mcp._tool_manager._tools["query_transactions"].fn
        params = QueryTransactionParams(
            start_date="2026-01-01",
            end_date="2026-01-31",
            aggregation="daily",
        )
        result = json.loads(await tool(params))
        assert "error" in result
        assert result["reason"] == reason


# ---------------------------------------------------------------------------
# F-003: fail-closed monetary coercion error envelopes
# ---------------------------------------------------------------------------

class TestMonetaryCoercionErrorEnvelopes:
    """Time-series aggregation values are validated as whole-IDR integers.
    Out-of-contract values are rejected without partial results; the error
    envelope names the field but never the raw value."""

    @staticmethod
    def _aggregation_with_bad_value(key, bad_value, total_expense=0, total_income=0):
        return {
            "aggregations": {
                "time_series": {
                    "buckets": [
                        {
                            "key_as_string": key,
                            "total_expense": {"sum": {"value": total_expense}},
                            "total_income": {"sum": {"value": total_income}},
                            "tx_count": {"value": bad_value},
                        }
                    ]
                }
            }
        }

    # tx_count uses coerce_count (dedicated count validator, no monetary
    # 2**53 precision guard).  The values below are out of the count
    # contract: non-finite, fractional, or bool.  A valid large count
    # (e.g. 2**53+1) is accepted by coerce_count — see
    # test_tx_count_above_2_pow_53_passes for that case.
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad_value",
        [float("nan"), float("inf"), 1.5, True, False],
    )
    async def test_rejects_bad_tx_count(self, mcp, monkeypatch, bad_value):
        import financial_mcp.tools.transactions as mod

        async def fake_query(q):
            return self._aggregation_with_bad_value("2026-01-01", bad_value)

        monkeypatch.setattr(mod, "query_elasticsearch", fake_query)
        tool = mcp._tool_manager._tools["query_transactions"].fn
        result = json.loads(await tool(QueryTransactionParams(aggregation="daily")))
        assert result["error"] == "monetary value failed validation: tx_count"
        assert "data" not in result
        assert "total_periods" not in result

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_bool", [True, False])
    async def test_bool_tx_count_rejected(self, mcp, monkeypatch, bad_bool):
        """Bool is a subclass of int; both True and False must be rejected
        explicitly — a naive int check would accept True as 1, False as 0."""
        import financial_mcp.tools.transactions as mod

        async def fake_query(q):
            return self._aggregation_with_bad_value("2026-01-01", bad_bool)

        monkeypatch.setattr(mod, "query_elasticsearch", fake_query)
        tool = mcp._tool_manager._tools["query_transactions"].fn
        result = json.loads(await tool(QueryTransactionParams(aggregation="daily")))
        assert result == {"error": "monetary value failed validation: tx_count"}
        assert set(result.keys()) == {"error"}
        assert "data" not in result
        assert "total_periods" not in result
        assert "aggregation" not in result

    @pytest.mark.asyncio
    async def test_no_partial_output_across_buckets(self, mcp, monkeypatch):
        """First bucket is valid; a later bucket has an invalid tx_count.
        Fail-closed: no partial ``data`` or ``total_periods``, only the
        validation error envelope."""
        import financial_mcp.tools.transactions as mod

        async def fake_query(q):
            return {
                "aggregations": {
                    "time_series": {
                        "buckets": [
                            {
                                "key_as_string": "2026-01-01",
                                "total_expense": {"sum": {"value": 100}},
                                "total_income": {"sum": {"value": 5000}},
                                "tx_count": {"value": 2},
                            },
                            {
                                "key_as_string": "2026-01-02",
                                "total_expense": {"sum": {"value": 200}},
                                "total_income": {"sum": {"value": 6000}},
                                "tx_count": {"value": 0.5},
                            },
                        ]
                    }
                }
            }

        monkeypatch.setattr(mod, "query_elasticsearch", fake_query)
        tool = mcp._tool_manager._tools["query_transactions"].fn
        result = json.loads(await tool(QueryTransactionParams(aggregation="daily")))
        assert result == {"error": "monetary value failed validation: tx_count"}
        assert "data" not in result
        assert "total_periods" not in result
        assert "aggregation" not in result

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad_value",
        [float("nan"), float("inf"), 1.5, float(2 ** 53 + 1), True],
    )
    async def test_rejects_bad_total_expense(self, mcp, monkeypatch, bad_value):
        import financial_mcp.tools.transactions as mod

        async def fake_query(q):
            return self._aggregation_with_bad_value(
                "2026-01-01", 0, total_expense=bad_value
            )

        monkeypatch.setattr(mod, "query_elasticsearch", fake_query)
        tool = mcp._tool_manager._tools["query_transactions"].fn
        result = json.loads(await tool(QueryTransactionParams(aggregation="daily")))
        assert result["error"] == "monetary value failed validation: total_expense"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad_value",
        [float("nan"), float("inf"), 1.5, float(2 ** 53 + 1), True],
    )
    async def test_rejects_bad_total_income(self, mcp, monkeypatch, bad_value):
        import financial_mcp.tools.transactions as mod

        async def fake_query(q):
            return self._aggregation_with_bad_value(
                "2026-01-01", 0, total_income=bad_value
            )

        monkeypatch.setattr(mod, "query_elasticsearch", fake_query)
        tool = mcp._tool_manager._tools["query_transactions"].fn
        result = json.loads(await tool(QueryTransactionParams(aggregation="daily")))
        assert result["error"] == "monetary value failed validation: total_income"

    @pytest.mark.asyncio
    async def test_error_envelope_does_not_leak_value(self, mcp, monkeypatch):
        import financial_mcp.tools.transactions as mod

        async def fake_query(q):
            return self._aggregation_with_bad_value("2026-01-01", 999_999.5)

        monkeypatch.setattr(mod, "query_elasticsearch", fake_query)
        tool = mcp._tool_manager._tools["query_transactions"].fn
        result = json.loads(await tool(QueryTransactionParams(aggregation="daily")))
        assert "999999" not in result["error"]
        assert "999_999" not in result["error"]

    @pytest.mark.asyncio
    async def test_valid_aggregation_still_works(self, mcp, monkeypatch):
        import financial_mcp.tools.transactions as mod

        async def fake_query(q):
            return {
                "aggregations": {
                    "time_series": {
                        "buckets": [
                            {
                                "key_as_string": "2026-01-01",
                                "total_expense": {"sum": {"value": 100}},
                                "total_income": {"sum": {"value": 5000}},
                                "tx_count": {"value": 2},
                            }
                        ]
                    }
                }
            }

        monkeypatch.setattr(mod, "query_elasticsearch", fake_query)
        tool = mcp._tool_manager._tools["query_transactions"].fn
        result = json.loads(await tool(QueryTransactionParams(aggregation="daily")))
        assert result["data"][0]["tx_count"] == 2
        assert isinstance(result["data"][0]["tx_count"], int)
        assert result["data"][0]["net"] == 4900

    @pytest.mark.asyncio
    async def test_negative_tx_count_rejected(self, mcp, monkeypatch):
        import financial_mcp.tools.transactions as mod

        async def fake_query(q):
            return self._aggregation_with_bad_value("2026-01-01", -1)

        monkeypatch.setattr(mod, "query_elasticsearch", fake_query)
        tool = mcp._tool_manager._tools["query_transactions"].fn
        result = json.loads(await tool(QueryTransactionParams(aggregation="daily")))
        assert result["error"] == "monetary value failed validation: tx_count"

    @pytest.mark.asyncio
    async def test_tx_count_above_2_pow_53_passes(self, mcp, monkeypatch):
        """tx_count uses coerce_count (no monetary precision guard), so a
        count of 2**53+1 is accepted even though the same value would be
        rejected by coerce_monetary.  This is the semantic split that
        motivated separating the two validators."""
        import financial_mcp.tools.money as money_mod
        from financial_mcp.tools.money import coerce_count, coerce_monetary

        big = 2 ** 53 + 1
        # Confirm the precondition: the monetary guard rejects this float.
        with pytest.raises(money_mod.MonetaryCoercionError):
            coerce_monetary(float(big), "f")
        # But the count validator accepts it — counts are arbitrary-size ints.
        assert coerce_count(big, "f") == big

        import financial_mcp.tools.transactions as mod

        async def fake_query(q):
            return {
                "aggregations": {
                    "time_series": {
                        "buckets": [
                            {
                                "key_as_string": "2026-01-01",
                                "total_expense": {"sum": {"value": 0}},
                                "total_income": {"sum": {"value": 0}},
                                "tx_count": {"value": big},
                            }
                        ]
                    }
                }
            }

        monkeypatch.setattr(mod, "query_elasticsearch", fake_query)
        tool = mcp._tool_manager._tools["query_transactions"].fn
        result = json.loads(await tool(QueryTransactionParams(aggregation="daily")))
        assert "error" not in result
        assert result["data"][0]["tx_count"] == big


class TestLimitReachesElasticsearch:
    """F-005: the validated limit is what lands in ES `size`."""

    @pytest.mark.asyncio
    async def test_explicit_limit_passed_unchanged(self, mcp, monkeypatch):
        import financial_mcp.tools.transactions as mod
        captured = {}

        async def fake(q):
            captured["q"] = q
            return {"hits": {"total": {"value": 0}, "hits": []}}

        monkeypatch.setattr(mod, "query_elasticsearch", fake)
        tool = mcp._tool_manager._tools["query_transactions"].fn
        await tool(QueryTransactionParams(limit=10000))
        assert captured["q"]["size"] == 10000

    @pytest.mark.asyncio
    async def test_default_limit_used_when_omitted(self, mcp, monkeypatch):
        import financial_mcp.tools.transactions as mod
        captured = {}

        async def fake(q):
            captured["q"] = q
            return {"hits": {"total": {"value": 0}, "hits": []}}

        monkeypatch.setattr(mod, "query_elasticsearch", fake)
        tool = mcp._tool_manager._tools["query_transactions"].fn
        await tool(QueryTransactionParams())
        assert captured["q"]["size"] == 50

    def test_invalid_limit_rejected_by_model_before_any_es_call(self, mcp):
        """limit=0 is rejected by the Pydantic model; since the only
        construction path is the model, no ES call can ever receive it."""
        with pytest.raises(ValidationError):
            QueryTransactionParams(limit=0)

    @pytest.mark.asyncio
    async def test_size_is_never_none_in_raw_mode(self, mcp, monkeypatch):
        """Raw-record query must always send an int `size`, never None."""
        import financial_mcp.tools.transactions as mod
        captured = {}

        async def fake(q):
            captured["q"] = q
            return {"hits": {"total": {"value": 0}, "hits": []}}

        monkeypatch.setattr(mod, "query_elasticsearch", fake)
        tool = mcp._tool_manager._tools["query_transactions"].fn
        await tool(QueryTransactionParams())
        assert captured["q"]["size"] is not None
        assert isinstance(captured["q"]["size"], int)

    @pytest.mark.asyncio
    async def test_aggregation_still_uses_size_zero(self, mcp, monkeypatch):
        """Aggregation mode ignores limit for `size` -- unchanged by F-005."""
        import financial_mcp.tools.transactions as mod
        captured = {}

        async def fake(q):
            captured["q"] = q
            return {"aggregations": {"time_series": {"buckets": []}}}

        monkeypatch.setattr(mod, "query_elasticsearch", fake)
        tool = mcp._tool_manager._tools["query_transactions"].fn
        await tool(QueryTransactionParams(aggregation="daily", limit=10000))
        assert captured["q"]["size"] == 0


class TestEsQueryErrorCallerContract:
    """F-007: every tool caller must translate ElasticsearchQueryError
    into the sanitized JSON error envelope, not a raw exception or a
    successful empty result."""

    @pytest.mark.asyncio
    async def test_get_all_transactions_returns_sanitized_error(self, mcp, monkeypatch):
        import financial_mcp.tools.transactions as mod
        from financial_mcp.es_client import (
            ElasticsearchQueryError,
            es_error_payload,
        )

        raw_msg = "ConnectionError: https://es.internal:9200 [Errno 111] refused"
        async def fail(q):
            raise ElasticsearchQueryError(
                es_error_payload("es_connection")["error"],
                reason="es_connection",
            )
        monkeypatch.setattr(mod, "query_elasticsearch", fail)
        tool = mcp._tool_manager._tools["get_all_transactions"].fn
        result = json.loads(await tool())
        assert result["reason"] == "es_connection"
        assert result["error"] == "Elasticsearch connection unavailable"
        assert raw_msg not in json.dumps(result)
        assert "9200" not in json.dumps(result)

    @pytest.mark.asyncio
    async def test_query_transactions_raw_returns_sanitized_error(self, mcp, monkeypatch):
        import financial_mcp.tools.transactions as mod
        from financial_mcp.es_client import ElasticsearchQueryError, es_error_payload

        async def fail(q):
            raise ElasticsearchQueryError(
                es_error_payload("es_auth")["error"],
                reason="es_auth",
            )
        monkeypatch.setattr(mod, "query_elasticsearch", fail)
        tool = mcp._tool_manager._tools["query_transactions"].fn
        result = json.loads(await tool(QueryTransactionParams()))
        assert result["reason"] == "es_auth"
        assert result["error"] == "Elasticsearch authentication or authorization failed"
        assert "data" not in result
        assert "hits" not in result

    @pytest.mark.asyncio
    async def test_query_transactions_agg_returns_sanitized_error(self, mcp, monkeypatch):
        import financial_mcp.tools.transactions as mod
        from financial_mcp.es_client import ElasticsearchQueryError, es_error_payload

        async def fail(q):
            raise ElasticsearchQueryError(
                es_error_payload("es_validation")["error"],
                reason="es_validation",
            )
        monkeypatch.setattr(mod, "query_elasticsearch", fail)
        tool = mcp._tool_manager._tools["query_transactions"].fn
        result = json.loads(await tool(QueryTransactionParams(aggregation="daily")))
        assert result["reason"] == "es_validation"
        assert result["error"] == "Elasticsearch rejected the query"
        assert "aggregation" not in result
        assert "data" not in result


# ---------------------------------------------------------------------------
# F-007: caller-level error response tests
# ---------------------------------------------------------------------------

from financial_mcp.es_client import ElasticsearchQueryError, es_error_payload


def _make_es_error(reason: str) -> ElasticsearchQueryError:
    """Build a real ElasticsearchQueryError with the static message."""
    return ElasticsearchQueryError(
        es_error_payload(reason)["error"], reason=reason
    )


class TestGetAllTransactionsEsErrors:
    """F-007: get_all_transactions returns a sanitized error envelope."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reason", ["es_connection", "es_auth", "es_validation"])
    async def test_returns_sanitized_error(self, mcp, monkeypatch, reason):
        import financial_mcp.tools.transactions as mod

        async def fail(q):
            raise _make_es_error(reason)

        monkeypatch.setattr(mod, "query_elasticsearch", fail)
        tool = mcp._tool_manager._tools["get_all_transactions"].fn
        result = json.loads(await tool())
        assert result == es_error_payload(reason)
        # No raw exception text, endpoint, or transaction data leaks.
        raw = json.dumps(result)
        assert "Traceback" not in raw
        assert "data" not in result

    @pytest.mark.asyncio
    async def test_error_is_not_successful_empty(self, mcp, monkeypatch):
        """es_connection must not be silently treated as an empty result set."""
        import financial_mcp.tools.transactions as mod

        async def fail(q):
            raise _make_es_error("es_connection")

        monkeypatch.setattr(mod, "query_elasticsearch", fail)
        tool = mcp._tool_manager._tools["get_all_transactions"].fn
        result = json.loads(await tool())
        assert "data" not in result
        assert "total_records" not in result
        assert "error" in result


class TestQueryTransactionsEsErrors:
    """F-007: query_transactions (raw + aggregation branches) return sanitized errors."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reason", ["es_connection", "es_timeout", "es_unknown"])
    async def test_raw_branch_returns_sanitized_error(self, mcp, monkeypatch, reason):
        import financial_mcp.tools.transactions as mod

        async def fail(q):
            raise _make_es_error(reason)

        monkeypatch.setattr(mod, "query_elasticsearch", fail)
        tool = mcp._tool_manager._tools["query_transactions"].fn
        result = json.loads(await tool(QueryTransactionParams()))
        assert result == es_error_payload(reason)
        assert "data" not in result
        assert "total_matched" not in result

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reason", ["es_auth", "es_not_found", "es_dependency"])
    async def test_agg_branch_returns_sanitized_error(self, mcp, monkeypatch, reason):
        import financial_mcp.tools.transactions as mod

        async def fail(q):
            raise _make_es_error(reason)

        monkeypatch.setattr(mod, "query_elasticsearch", fail)
        tool = mcp._tool_manager._tools["query_transactions"].fn
        result = json.loads(await tool(QueryTransactionParams(aggregation="daily")))
        assert result == es_error_payload(reason)
        assert "data" not in result
        assert "total_periods" not in result


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

