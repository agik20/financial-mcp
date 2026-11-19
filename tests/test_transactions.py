"""Tests for financial_mcp.tools.transactions (canonical field usage)."""

import json
import pytest

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
        async def fail_query(q):
            return None
        monkeypatch.setattr(mod, "query_elasticsearch", fail_query)
        tool = mcp._tool_manager._tools["get_all_transactions"].fn
        result = json.loads(await tool())
        assert "error" in result

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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

