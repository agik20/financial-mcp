"""Tests for financial_mcp.tools.analytics (Transfer exclusion + canonical fields)."""

import json
import pytest

from financial_mcp.models import QueryTransactionParams
from financial_mcp.tools.analytics import register_analytics_tools
from mcp.server.fastmcp import FastMCP


@pytest.fixture
def mcp():
    mcp = FastMCP("test-analytics")
    register_analytics_tools(mcp)
    return mcp


class TestGetSpendingByCategory:
    @pytest.mark.asyncio
    async def test_expense_default_uses_amount(self, mcp, monkeypatch):
        import financial_mcp.tools.analytics as mod
        captured = {}

        async def fake_query(q):
            captured["query"] = q
            return {
                "aggregations": {
                    "grand_total": {"value": 200000},
                    "by_category": {
                        "buckets": [
                            {
                                "key": "Food & Drink",
                                "total": {"value": 120000},
                                "tx_count": {"value": 6},
                                "avg_per_tx": {"value": 20000},
                            },
                            {
                                "key": "Transport",
                                "total": {"value": 80000},
                                "tx_count": {"value": 4},
                                "avg_per_tx": {"value": 20000},
                            },
                        ]
                    },
                }
            }

        monkeypatch.setattr(mod, "query_elasticsearch", fake_query)
        params = QueryTransactionParams()
        tool = mcp._tool_manager._tools["get_spending_by_category"].fn
        result = json.loads(await tool(params))
        q = captured["query"]
        assert q["aggs"]["grand_total"]["sum"]["field"] == "amount"
        assert q["aggs"]["by_category"]["aggs"]["total"]["sum"]["field"] == "amount"
        assert q["query"]["bool"]["must"][0] == {"term": {"type": "Expense"}}
        food = next(c for c in result["categories"] if c["category"] == "Food & Drink")
        assert food["percentage"] == 60.0
        assert food["total"] == 120000
        assert food["tx_count"] == 6
        assert food["avg_per_tx"] == 20000.0

    @pytest.mark.asyncio
    async def test_income_uses_signed_amount(self, mcp, monkeypatch):
        import financial_mcp.tools.analytics as mod
        captured = {}

        async def fake_query(q):
            captured["query"] = q
            return {
                "aggregations": {
                    "grand_total": {"value": 500000},
                    "by_category": {
                        "buckets": [
                            {"key": "Salary", "total": {"value": 500000},
                             "tx_count": {"value": 1}, "avg_per_tx": {"value": 500000}},
                        ]
                    },
                }
            }

        monkeypatch.setattr(mod, "query_elasticsearch", fake_query)
        params = QueryTransactionParams(type="Income")
        tool = mcp._tool_manager._tools["get_spending_by_category"].fn
        result = json.loads(await tool(params))
        assert captured["query"]["query"]["bool"]["must"][0] == {"term": {"type": "Income"}}

    @pytest.mark.asyncio
    async def test_date_filter_uses_transaction_date(self, mcp, monkeypatch):
        import financial_mcp.tools.analytics as mod
        captured = {}

        async def fake_query(q):
            captured["query"] = q
            return {"aggregations": {"grand_total": {"value": 0},
                                     "by_category": {"buckets": []}}}

        monkeypatch.setattr(mod, "query_elasticsearch", fake_query)
        params = QueryTransactionParams(start_date="2026-01-01", end_date="2026-01-31")
        tool = mcp._tool_manager._tools["get_spending_by_category"].fn
        json.loads(await tool(params))
        must = captured["query"]["query"]["bool"]["must"]
        assert {"range": {"transaction_date": {"gte": "2026-01-01", "lte": "2026-01-31"}}} in must
        raw = json.dumps(captured["query"])
        assert '"day"' not in raw


# ---------------------------------------------------------------------------
# Regression: Transfer exclusion from consolidated net cash flow
# ---------------------------------------------------------------------------

class TestGetFinancialSummaryTransferExclusion:
    """Regression tests verifying Transfer transactions are excluded from
    consolidated income, expense, and net cash flow calculations.

    Data contract (authoritative):
        Net Cash Flow = Total Income - Total Expense
        Transfers are excluded entirely.
    """

    @staticmethod
    def _summary_response(total_income, total_expense, docs_total=3):
        """Build a realistic ES 8.x response for get_financial_summary."""
        return {
            "hits": {"total": {"value": docs_total}},
            "aggregations": {
                "total_income": {"sum": {"value": total_income}},
                "total_expense": {"sum": {"value": total_expense}},
                "by_category": {
                    "terms": {
                        "buckets": []
                    }
                },
            },
        }

    @pytest.mark.asyncio
    async def test_transfer_excluded_from_net_cash_flow(self, mcp, monkeypatch):
        """Income +10,000,000; Expense 500,000; Transfer -2,000,000.
        Expected: net_cash_flow = 9,500,000 (transfers excluded)."""
        import financial_mcp.tools.analytics as mod
        captured = {}

        async def fake_query(q):
            captured["query"] = q
            return TestGetFinancialSummaryTransferExclusion._summary_response(
                total_income=10_000_000,
                total_expense=500_000,
                docs_total=3,
            )

        monkeypatch.setattr(mod, "query_elasticsearch", fake_query)
        tool = mcp._tool_manager._tools["get_financial_summary"].fn
        result = json.loads(await tool())

        assert result["total_income"] == 10_000_000
        assert result["total_expense"] == 500_000
        # Transfers are excluded: net = 10,000,000 - 500,000 = 9,500,000
        assert result["net_cash_flow"] == 9_500_000
        assert result["total_documents"] == 3

        # The query must NOT contain a net_cash_flow sub-aggregation
        # (net is computed client-side as total_income - total_expense).
        q = captured["query"]
        assert "net_cash_flow" not in q["aggs"], (
            "net_cash_flow sub-agg present -- Transfers would be included"
        )

    @pytest.mark.asyncio
    async def test_zero_expense_net_equals_income(self, mcp, monkeypatch):
        import financial_mcp.tools.analytics as mod

        async def fake_query(q):
            return TestGetFinancialSummaryTransferExclusion._summary_response(0, 0, docs_total=1)

        monkeypatch.setattr(mod, "query_elasticsearch", fake_query)
        tool = mcp._tool_manager._tools["get_financial_summary"].fn
        result = json.loads(await tool())
        assert result["net_cash_flow"] == 0

    @pytest.mark.asyncio
    async def test_no_legacy_fields_in_summary(self, mcp, monkeypatch):
        import financial_mcp.tools.analytics as mod
        captured = {}

        async def fake_query(q):
            captured["query"] = q
            return TestGetFinancialSummaryTransferExclusion._summary_response(0, 0, docs_total=0)

        monkeypatch.setattr(mod, "query_elasticsearch", fake_query)
        tool = mcp._tool_manager._tools["get_financial_summary"].fn
        json.loads(await tool())
        raw = json.dumps(captured["query"])
        for legacy in ("creationDate", "day", "sourceName", "value", "payCycle", "balance"):
            assert f'"{legacy}"' not in raw, f"legacy field {legacy!r} in summary query"


# ---------------------------------------------------------------------------
# Regression: zero-value Income and Expense are valid
# ---------------------------------------------------------------------------

class TestZeroValueTransactions:
    @pytest.mark.asyncio
    async def test_zero_income_valid(self, mcp, monkeypatch):
        """A zero-value Income document is a valid contract case."""
        import financial_mcp.tools.analytics as mod

        async def fake_query(q):
            return TestGetFinancialSummaryTransferExclusion._summary_response(0, 0, docs_total=1)

        monkeypatch.setattr(mod, "query_elasticsearch", fake_query)
        tool = mcp._tool_manager._tools["get_financial_summary"].fn
        result = json.loads(await tool())
        assert result["total_income"] == 0
        assert result["total_expense"] == 0
        assert result["net_cash_flow"] == 0

    @pytest.mark.asyncio
    async def test_zero_expense_valid(self, mcp, monkeypatch):
        import financial_mcp.tools.analytics as mod

        async def fake_query(q):
            return TestGetFinancialSummaryTransferExclusion._summary_response(100, 0, docs_total=1)

        monkeypatch.setattr(mod, "query_elasticsearch", fake_query)
        tool = mcp._tool_manager._tools["get_financial_summary"].fn
        result = json.loads(await tool())
        assert result["total_expense"] == 0
        assert result["net_cash_flow"] == 100


# ---------------------------------------------------------------------------
# Regression: specific numeric scenarios for Transfer exclusion
# ---------------------------------------------------------------------------

class TestTransferExclusionNumeric:
    """Concrete numeric regression tests for the data contract:
    Net Cash Flow = Total Income - Total Expense (Transfers excluded).
    """

    @staticmethod
    def _summary_response(total_income, total_expense, docs_total=3):
        return TestGetFinancialSummaryTransferExclusion._summary_response(
            total_income, total_expense, docs_total=docs_total
        )

    @pytest.mark.asyncio
    async def test_income_100_expense_40_transfer_30_net_is_60(self, mcp, monkeypatch):
        """Income +100, Expense -40, Transfer -30.
        Expected: total_income=100, total_expense=40, net=100-40=60 (not 30)."""
        import financial_mcp.tools.analytics as mod

        async def fake_query(q):
            return self._summary_response(
                total_income=100, total_expense=40, docs_total=3
            )

        monkeypatch.setattr(mod, "query_elasticsearch", fake_query)
        tool = mcp._tool_manager._tools["get_financial_summary"].fn
        result = json.loads(await tool())

        assert result["total_income"] == 100
        assert result["total_expense"] == 40
        # net = 100 - 40 = 60, NOT 30 (which would be 100 - 40 - 30)
        assert result["net_cash_flow"] == 60, (
            f"net_cash_flow should be 60 (Income - Expense, Transfers excluded), "
            f"got {result['net_cash_flow']}"
        )
        # All 3 docs are reported (Transfer doc is stored and queryable)
        assert result["total_documents"] == 3

    @pytest.mark.asyncio
    async def test_transfer_only_dataset_all_zeros(self, mcp, monkeypatch):
        """A dataset containing only Transfer transactions must yield
        total_income=0, total_expense=0, net_cash_flow=0."""
        import financial_mcp.tools.analytics as mod

        async def fake_query(q):
            # Simulate: only Transfer docs exist; Income and Expense filters
            # match zero documents, so sums are 0.
            return self._summary_response(
                total_income=0, total_expense=0, docs_total=5
            )

        monkeypatch.setattr(mod, "query_elasticsearch", fake_query)
        tool = mcp._tool_manager._tools["get_financial_summary"].fn
        result = json.loads(await tool())

        assert result["total_income"] == 0
        assert result["total_expense"] == 0
        assert result["net_cash_flow"] == 0
        # The Transfer docs are still counted in total_documents
        assert result["total_documents"] == 5

    @pytest.mark.asyncio
    async def test_transfer_signed_amount_not_in_net(self, mcp, monkeypatch):
        """Verify the ES query does NOT sum signed_amount across all docs.
        The net_cash_flow sub-aggregation must be absent from the query;
        net is computed client-side as total_income - total_expense."""
        import financial_mcp.tools.analytics as mod
        captured = {}

        async def fake_query(q):
            captured["query"] = q
            return self._summary_response(0, 0, docs_total=0)

        monkeypatch.setattr(mod, "query_elasticsearch", fake_query)
        tool = mcp._tool_manager._tools["get_financial_summary"].fn
        json.loads(await tool())

        q = captured["query"]
        # There must be no sub-agg that sums signed_amount across all docs
        assert "net_cash_flow" not in q["aggs"], (
            "net_cash_flow sub-agg present in ES query -- Transfers would be included"
        )
        # total_income must filter on Income type
        ti_agg = q["aggs"]["total_income"]
        assert ti_agg["filter"]["term"]["type"] == "Income"
        # total_expense must filter on Expense type
        te_agg = q["aggs"]["total_expense"]
        assert te_agg["filter"]["term"]["type"] == "Expense"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
