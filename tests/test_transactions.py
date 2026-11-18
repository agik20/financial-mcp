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

if __name__ == "__main__":
    pytest.main([__file__, "-v"])

