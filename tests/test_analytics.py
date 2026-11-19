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


# ---------------------------------------------------------------------------
# Bug 1 regression: missing category and source_account filters
# ---------------------------------------------------------------------------

class TestSpendingByCategoryFilters:
    """Regression tests verifying that category and source_account filters
    are included in the ES query when supplied via params."""

    @staticmethod
    def _fake_spending(captured, buckets=None):
        async def fake_query(q):
            captured["query"] = q
            return {
                "aggregations": {
                    "grand_total": {"value": 0},
                    "by_category": {
                        "buckets": buckets or []
                    },
                }
            }
        return fake_query

    def _must(self, q):
        return q["query"]["bool"]["must"]

    @pytest.mark.asyncio
    async def test_category_only_filter(self, mcp, monkeypatch):
        """Category without source_account: term on category present,
        no source_account term."""
        import financial_mcp.tools.analytics as mod
        captured = {}
        monkeypatch.setattr(
            mod, "query_elasticsearch",
            self._fake_spending(captured)
        )
        tool = mcp._tool_manager._tools["get_spending_by_category"].fn
        json.loads(await tool(QueryTransactionParams(category="Food & Drink")))
        must = self._must(captured["query"])
        assert {"term": {"category": "Food & Drink"}} in must
        # No source_account clause
        assert not any("source_account" in c for c in must)

    @pytest.mark.asyncio
    async def test_source_account_only_filter(self, mcp, monkeypatch):
        """source_account without category: term on source_account present,
        no category term."""
        import financial_mcp.tools.analytics as mod
        captured = {}
        monkeypatch.setattr(
            mod, "query_elasticsearch",
            self._fake_spending(captured)
        )
        tool = mcp._tool_manager._tools["get_spending_by_category"].fn
        json.loads(await tool(QueryTransactionParams(source_account="BNI")))
        must = self._must(captured["query"])
        assert {"term": {"source_account": "BNI"}} in must
        # No category clause
        assert not any("category" in c for c in must)

    @pytest.mark.asyncio
    async def test_combined_category_and_source_account(self, mcp, monkeypatch):
        """Both filters supplied: both terms present."""
        import financial_mcp.tools.analytics as mod
        captured = {}
        monkeypatch.setattr(
            mod, "query_elasticsearch",
            self._fake_spending(captured)
        )
        tool = mcp._tool_manager._tools["get_spending_by_category"].fn
        json.loads(
            await tool(
                QueryTransactionParams(
                    category="Food & Drink", source_account="BNI"
                )
            )
        )
        must = self._must(captured["query"])
        assert {"term": {"category": "Food & Drink"}} in must
        assert {"term": {"source_account": "BNI"}} in must

    @pytest.mark.asyncio
    async def test_legacy_source_name_folds_to_source_account(self, mcp, monkeypatch):
        """source_name (legacy) is folded into source_account by the
        model validator and applied as a source_account term filter."""
        import financial_mcp.tools.analytics as mod
        captured = {}
        monkeypatch.setattr(
            mod, "query_elasticsearch",
            self._fake_spending(captured)
        )
        tool = mcp._tool_manager._tools["get_spending_by_category"].fn
        # Pass source_name directly (simulates an old MCP client)
        params = QueryTransactionParams(source_name="BCA")
        json.loads(await tool(params))
        must = self._must(captured["query"])
        assert {"term": {"source_account": "BCA"}} in must

    @pytest.mark.asyncio
    async def test_no_optional_filters_means_no_extra_terms(self, mcp, monkeypatch):
        """When neither category nor source_account is supplied, the
        must_clauses contain only type and (optionally) date range —
        no category or source_account term."""
        import financial_mcp.tools.analytics as mod
        captured = {}
        monkeypatch.setattr(
            mod, "query_elasticsearch",
            self._fake_spending(captured)
        )
        tool = mcp._tool_manager._tools["get_spending_by_category"].fn
        json.loads(await tool(QueryTransactionParams()))
        must = self._must(captured["query"])
        # Exactly one clause: type=Expense
        assert len(must) == 1
        assert must[0] == {"term": {"type": "Expense"}}
        assert not any("category" in c for c in must)
        assert not any("source_account" in c for c in must)


# ---------------------------------------------------------------------------
# Bug 2 regression: top_categories terms aggregation ordering
# ---------------------------------------------------------------------------

class TestTopCategoriesRanking:
    """Regression tests verifying the by_category terms aggregation in
    get_financial_summary orders by total (desc) with _key (asc) tie-breaker,
    and that the returned list is sorted the same way."""

    @staticmethod
    def _summary_with_buckets(buckets):
        """Build a realistic ES 8.x summary response with custom buckets.

        buckets: list of dicts with keys "key" and "total" (the
        sub-aggregation value), matching the ES 8.x nesting:
            aggregations.by_category.terms.buckets[].total.value
        """
        es_buckets = [
            {"key": b["key"], "total": {"value": b["total"]}}
            for b in buckets
        ]
        return {
            "hits": {"total": {"value": 1321}},
            "aggregations": {
                "total_income": {"sum": {"value": 84_218_175}},
                "total_expense": {"sum": {"value": 83_746_035}},
                "by_category": {
                    "terms": {
                        "buckets": es_buckets
                    }
                },
            },
        }

    @staticmethod
    def _terms_config(captured):
        return captured["query"]["aggs"]["by_category"]["aggs"]["terms"]

    @pytest.mark.asyncio
    async def test_terms_agg_size_is_10(self, mcp, monkeypatch):
        import financial_mcp.tools.analytics as mod
        captured = {}
        async def fake(q):
            captured["query"] = q
            return self._summary_with_buckets([])
        monkeypatch.setattr(mod, "query_elasticsearch", fake)
        tool = mcp._tool_manager._tools["get_financial_summary"].fn
        json.loads(await tool())
        assert self._terms_config(captured)["terms"]["size"] == 10

    @pytest.mark.asyncio
    async def test_terms_agg_orders_by_total_desc(self, mcp, monkeypatch):
        import financial_mcp.tools.analytics as mod
        captured = {}
        async def fake(q):
            captured["query"] = q
            return self._summary_with_buckets([])
        monkeypatch.setattr(mod, "query_elasticsearch", fake)
        tool = mcp._tool_manager._tools["get_financial_summary"].fn
        json.loads(await tool())
        order = self._terms_config(captured)["terms"]["order"]
        assert {"total": "desc"} in order

    @pytest.mark.asyncio
    async def test_terms_agg_uses_key_asc_tie_breaker(self, mcp, monkeypatch):
        import financial_mcp.tools.analytics as mod
        captured = {}
        async def fake(q):
            captured["query"] = q
            return self._summary_with_buckets([])
        monkeypatch.setattr(mod, "query_elasticsearch", fake)
        tool = mcp._tool_manager._tools["get_financial_summary"].fn
        json.loads(await tool())
        order = self._terms_config(captured)["terms"]["order"]
        assert {"_key": "asc"} in order

    @pytest.mark.asyncio
    async def test_transfer_exclusion_still_present(self, mcp, monkeypatch):
        import financial_mcp.tools.analytics as mod
        captured = {}
        async def fake(q):
            captured["query"] = q
            return self._summary_with_buckets([])
        monkeypatch.setattr(mod, "query_elasticsearch", fake)
        json.loads(
            await mcp._tool_manager._tools["get_financial_summary"].fn()
        )
        bc_agg = captured["query"]["aggs"]["by_category"]
        assert "filter" in bc_agg
        must_not = bc_agg["filter"]["bool"]["must_not"]
        assert {"term": {"type": "Transfer"}} in must_not

    @pytest.mark.asyncio
    async def test_returned_categories_sorted_by_total_desc(self, mcp, monkeypatch):
        import financial_mcp.tools.analytics as mod
        # Simulate ES returning buckets already in correct order (total desc)
        buckets = [
            {"key": "A", "total": 900},
            {"key": "B", "total": 500},
            {"key": "C", "total": 100},
        ]
        async def fake(q):
            return self._summary_with_buckets(buckets)
        monkeypatch.setattr(mod, "query_elasticsearch", fake)
        tool = mcp._tool_manager._tools["get_financial_summary"].fn
        result = json.loads(await tool())
        cats = result["top_categories"]
        totals = [c["total"] for c in cats]
        assert totals == sorted(totals, reverse=True)

    @pytest.mark.asyncio
    async def test_equal_totals_sorted_by_category_asc(self, mcp, monkeypatch):
        import financial_mcp.tools.analytics as mod
        # Simulate ES returning buckets in _key asc order for equal totals
        # (which is what the _key asc tie-breaker produces)
        buckets = [
            {"key": "Apple", "total": 500},
            {"key": "Banana", "total": 500},
            {"key": "Cherry", "total": 900},
        ]
        async def fake(q):
            return self._summary_with_buckets(buckets)
        monkeypatch.setattr(mod, "query_elasticsearch", fake)
        tool = mcp._tool_manager._tools["get_financial_summary"].fn
        result = json.loads(await tool())
        cats = result["top_categories"]
        assert [c["category"] for c in cats] == ["Cherry", "Apple", "Banana"]

    @pytest.mark.asyncio
    async def test_at_most_10_categories_returned(self, mcp, monkeypatch):
        """ES with size=10 returns at most 10 buckets; Python must not
        expand the list beyond what ES returned."""
        import financial_mcp.tools.analytics as mod
        # Simulate ES returning exactly 10 buckets (the max from size=10)
        buckets = [
            {"key": f"Cat{i}", "total": (10 - i) * 100} for i in range(10)
        ]
        async def fake(q):
            return self._summary_with_buckets(buckets)
        monkeypatch.setattr(mod, "query_elasticsearch", fake)
        tool = mcp._tool_manager._tools["get_financial_summary"].fn
        result = json.loads(await tool())
        assert len(result["top_categories"]) == 10

    @pytest.mark.asyncio
    async def test_income_expense_net_semantics_unchanged(self, mcp, monkeypatch):
        """total_income, total_expense, net_cash_flow, total_documents
        must still be computed correctly regardless of bucket content."""
        import financial_mcp.tools.analytics as mod
        buckets = [{"key": "Food & Drink", "total": 50_000}]
        async def fake(q):
            return self._summary_with_buckets(buckets)
        monkeypatch.setattr(mod, "query_elasticsearch", fake)
        tool = mcp._tool_manager._tools["get_financial_summary"].fn
        result = json.loads(await tool())
        assert result["total_income"] == 84_218_175
        assert result["total_expense"] == 83_746_035
        assert result["net_cash_flow"] == 84_218_175 - 83_746_035
        assert result["total_documents"] == 1321


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
