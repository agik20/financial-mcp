"""Tests for financial_mcp.tools.analytics (Transfer exclusion + canonical fields)."""

import json
import pytest

from pydantic import ValidationError

from financial_mcp.models import QueryTransactionParams
from financial_mcp.tools.analytics import register_analytics_tools
from financial_mcp.tools.money import half_even_div
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
                            },
                            {
                                "key": "Transport",
                                "total": {"value": 80000},
                                "tx_count": {"value": 4},
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
        # F-003: the ES avg sub-agg is no longer requested; avg_per_tx is
        # computed client-side from total and tx_count.
        assert "avg_per_tx" not in q["aggs"]["by_category"]["aggs"]
        assert q["query"]["bool"]["must"][0] == {"term": {"type": "Expense"}}
        food = next(c for c in result["categories"] if c["category"] == "Food & Drink")
        assert food["percentage"] == 60.0
        assert food["total"] == 120000
        assert food["tx_count"] == 6
        assert food["avg_per_tx"] == 20000
        transport = next(c for c in result["categories"] if c["category"] == "Transport")
        assert transport["avg_per_tx"] == 20000

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
                             "tx_count": {"value": 1}},
                        ]
                    },
                }
            }

        monkeypatch.setattr(mod, "query_elasticsearch", fake_query)
        params = QueryTransactionParams(type="Income")
        tool = mcp._tool_manager._tools["get_spending_by_category"].fn
        result = json.loads(await tool(params))
        assert captured["query"]["query"]["bool"]["must"][0] == {"term": {"type": "Income"}}
        salary = next(c for c in result["categories"] if c["category"] == "Salary")
        assert salary["total"] == 500000
        assert salary["tx_count"] == 1
        assert salary["avg_per_tx"] == 500000

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


class TestSpendingByCategoryLimitValidation:
    """F-005: get_spending_by_category shares QueryTransactionParams, so
    invalid limits are rejected at the model boundary before any ES call."""

    def test_invalid_limit_rejected_by_model_before_any_es_call(self, mcp):
        """get_spending_by_category shares QueryTransactionParams; since the
        only construction path is the model, no ES call can ever see limit=-1."""
        with pytest.raises(ValidationError):
            QueryTransactionParams(limit=-1)

    @pytest.mark.asyncio
    async def test_valid_limit_accepted(self, mcp, monkeypatch):
        import financial_mcp.tools.analytics as mod
        async def fake(q):
            return {
                "aggregations": {
                    "by_category": {"buckets": []},
                    "grand_total": {"value": 0},
                }
            }
        monkeypatch.setattr(mod, "query_elasticsearch", fake)
        tool = mcp._tool_manager._tools["get_spending_by_category"].fn
        result = json.loads(await tool(QueryTransactionParams(limit=10000)))
        assert result["grand_total"] == 0


# ---------------------------------------------------------------------------
# F-003: exact integer avg_per_tx (banker's rounding, no float round-trip)
# ---------------------------------------------------------------------------

class TestAvgPerTxIsInteger:
    """avg_per_tx must be an exact int computed as half_even_div(total,
    tx_count), not a rounded float.  The examples below come from the
    F-003 design spec: 5/2 -> 2, 7/2 -> 4, 8/3 -> 3, 5/3 -> 2."""

    @staticmethod
    def _spending_response(total, tx_count):
        return {
            "aggregations": {
                "grand_total": {"value": total},
                "by_category": {
                    "buckets": [
                        {"key": "Test", "total": {"value": total},
                         "tx_count": {"value": tx_count}},
                    ]
                },
            }
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "total,tx_count,expected",
        [
            (5, 2, 2),
            (7, 2, 4),
            (8, 3, 3),
            (5, 3, 2),
        ],
    )
    async def test_half_even_examples(self, mcp, monkeypatch, total, tx_count, expected):
        import financial_mcp.tools.analytics as mod

        async def fake_query(q):
            return self._spending_response(total, tx_count)

        monkeypatch.setattr(mod, "query_elasticsearch", fake_query)
        tool = mcp._tool_manager._tools["get_spending_by_category"].fn
        result = json.loads(await tool(QueryTransactionParams()))
        category = result["categories"][0]
        assert category["avg_per_tx"] == expected
        assert isinstance(category["avg_per_tx"], int)

    @pytest.mark.asyncio
    async def test_avg_per_tx_zero_when_no_tx(self, mcp, monkeypatch):
        import financial_mcp.tools.analytics as mod

        async def fake_query(q):
            return self._spending_response(0, 0)

        monkeypatch.setattr(mod, "query_elasticsearch", fake_query)
        tool = mcp._tool_manager._tools["get_spending_by_category"].fn
        result = json.loads(await tool(QueryTransactionParams()))
        assert result["categories"][0]["avg_per_tx"] == 0

    def test_matches_module_function(self):
        assert half_even_div(5, 2) == 2
        assert half_even_div(7, 2) == 4
        assert half_even_div(8, 3) == 3
        assert half_even_div(5, 3) == 2


# ---------------------------------------------------------------------------
# F-003: fail-closed monetary coercion error envelopes
# ---------------------------------------------------------------------------

class TestMonetaryCoercionErrorEnvelopes:
    """Out-of-contract monetary values are rejected without partial results;
    the error envelope names the field but never the raw value."""

    @staticmethod
    def _summary_with_bad_value(value):
        return {
            "hits": {"total": {"value": 3}},
            "aggregations": {
                "total_income": {"sum": {"value": value}},
                "total_expense": {"sum": {"value": 0}},
                "by_category": {"terms": {"buckets": []}},
            },
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad_value",
        [float("nan"), float("inf"), 1.5, float(2 ** 53 + 1), True],
    )
    async def test_get_financial_summary_rejects_bad_total_income(
        self, mcp, monkeypatch, bad_value
    ):
        import financial_mcp.tools.analytics as mod

        async def fake_query(q):
            return self._summary_with_bad_value(bad_value)

        monkeypatch.setattr(mod, "query_elasticsearch", fake_query)
        tool = mcp._tool_manager._tools["get_financial_summary"].fn
        result = json.loads(await tool())
        assert result["error"] == "monetary value failed validation: total_income"
        assert "total_documents" not in result
        assert "top_categories" not in result

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_value", [-1, -42, 1.5, float("nan"), True, False])
    async def test_get_spending_by_category_rejects_bad_tx_count(
        self, mcp, monkeypatch, bad_value
    ):
        import financial_mcp.tools.analytics as mod

        async def fake_query(q):
            return {
                "aggregations": {
                    "grand_total": {"value": 0},
                    "by_category": {
                        "buckets": [
                            {
                                "key": "Food",
                                "total": {"value": 0},
                                "tx_count": {"value": bad_value},
                            }
                        ]
                    },
                }
            }

        monkeypatch.setattr(mod, "query_elasticsearch", fake_query)
        tool = mcp._tool_manager._tools["get_spending_by_category"].fn
        result = json.loads(await tool(QueryTransactionParams()))
        assert result["error"] == "monetary value failed validation: categories.tx_count"
        assert "categories" not in result
        assert "grand_total" not in result
        assert "type" not in result

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_bool", [True, False])
    async def test_bool_tx_count_rejected_get_spending_by_category(
        self, mcp, monkeypatch, bad_bool
    ):
        """Explicit bool regression: bool is a subclass of int, so a naive
        ``int()`` / ``isinstance(v, int)`` check would silently accept
        ``True`` as 1 and ``False`` as 0.  Both must be rejected."""
        import financial_mcp.tools.analytics as mod

        async def fake_query(q):
            return {
                "aggregations": {
                    "grand_total": {"value": 0},
                    "by_category": {
                        "buckets": [
                            {
                                "key": "Food",
                                "total": {"value": 0},
                                "tx_count": {"value": bad_bool},
                            }
                        ]
                    },
                }
            }

        monkeypatch.setattr(mod, "query_elasticsearch", fake_query)
        tool = mcp._tool_manager._tools["get_spending_by_category"].fn
        result = json.loads(await tool(QueryTransactionParams()))
        assert result == {"error": "monetary value failed validation: categories.tx_count"}
        assert set(result.keys()) == {"error"}
        assert "categories" not in result
        assert "grand_total" not in result

    @pytest.mark.asyncio
    async def test_no_partial_output_across_buckets(self, mcp, monkeypatch):
        """First bucket is valid; a later bucket has an invalid tx_count.
        The whole call fails closed — no partial ``categories`` list, no
        ``grand_total``, only the validation error envelope."""
        import financial_mcp.tools.analytics as mod

        async def fake_query(q):
            return {
                "aggregations": {
                    "grand_total": {"value": 300000},
                    "by_category": {
                        "buckets": [
                            {
                                "key": "Food & Drink",
                                "total": {"value": 200000},
                                "tx_count": {"value": 4},
                            },
                            {
                                "key": "Transport",
                                "total": {"value": 100000},
                                "tx_count": {"value": -3},
                            },
                        ]
                    },
                }
            }

        monkeypatch.setattr(mod, "query_elasticsearch", fake_query)
        tool = mcp._tool_manager._tools["get_spending_by_category"].fn
        result = json.loads(await tool(QueryTransactionParams()))
        assert result == {"error": "monetary value failed validation: categories.tx_count"}
        assert "categories" not in result
        assert "grand_total" not in result
        assert "type" not in result

    @pytest.mark.asyncio
    async def test_get_spending_by_category_rejects_bad_grand_total(
        self, mcp, monkeypatch
    ):
        import financial_mcp.tools.analytics as mod

        async def fake_query(q):
            return {
                "aggregations": {
                    "grand_total": {"value": float("nan")},
                    "by_category": {"buckets": []},
                }
            }

        monkeypatch.setattr(mod, "query_elasticsearch", fake_query)
        tool = mcp._tool_manager._tools["get_spending_by_category"].fn
        result = json.loads(await tool(QueryTransactionParams()))
        assert result["error"] == "monetary value failed validation: grand_total"
        assert "categories" not in result

    @pytest.mark.asyncio
    async def test_error_envelope_does_not_leak_value(self, mcp, monkeypatch):
        import financial_mcp.tools.analytics as mod

        async def fake_query(q):
            return self._summary_with_bad_value(999_999.5)

        monkeypatch.setattr(mod, "query_elasticsearch", fake_query)
        tool = mcp._tool_manager._tools["get_financial_summary"].fn
        result = json.loads(await tool())
        assert "999999" not in result["error"]
        assert "999_999" not in result["error"]


# ---------------------------------------------------------------------------
# F-007: caller-level error response tests (analytics tools)
# ---------------------------------------------------------------------------

from financial_mcp.es_client import ElasticsearchQueryError, es_error_payload

_F007_ANALYTICS_REASONS = ("es_connection", "es_auth", "es_validation")


class TestF007AnalyticsCallerLevel:
    """Verify that analytics MCP tools translate ElasticsearchQueryError
    into a sanitized JSON error payload."""

    def _make_fail(self, reason):
        async def _fail(q):
            raise ElasticsearchQueryError(
                es_error_payload(reason)["error"], reason=reason
            )
        return _fail

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reason", _F007_ANALYTICS_REASONS)
    async def test_get_spending_by_category_returns_sanitized_error(self, mcp, monkeypatch, reason):
        import financial_mcp.tools.analytics as mod
        monkeypatch.setattr(mod, "query_elasticsearch", self._make_fail(reason))
        tool = mcp._tool_manager._tools["get_spending_by_category"].fn
        params = QueryTransactionParams(start_date="2026-01-01", end_date="2026-01-31")
        result = json.loads(await tool(params))
        assert "error" in result
        assert result["reason"] == reason

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reason", _F007_ANALYTICS_REASONS)
    async def test_get_financial_summary_returns_sanitized_error(self, mcp, monkeypatch, reason):
        import financial_mcp.tools.analytics as mod
        monkeypatch.setattr(mod, "query_elasticsearch", self._make_fail(reason))
        tool = mcp._tool_manager._tools["get_financial_summary"].fn
        result = json.loads(await tool())
        assert "error" in result
        assert result["reason"] == reason


# ---------------------------------------------------------------------------
# F-003: total_documents is NOT subject to monetary validation
# ---------------------------------------------------------------------------

class TestTotalDocumentsNotCoerced:
    """total_documents is a raw count read directly from ``hits.total.value``.
    It must never be passed through ``coerce_monetary`` (which has a 2**53
    float guard that is inappropriate for document counts).  These tests
    prove:
      1. ``coerce_monetary`` is NOT called for ``total_documents``
      2. A large integer (e.g. 2**53 + 1) is preserved verbatim
      3. Monetary fields in the same response still go through validation
    """

    @staticmethod
    def _summary_with_docs_total(docs_total, total_income=0, total_expense=0):
        return {
            "hits": {"total": {"value": docs_total}},
            "aggregations": {
                "total_income": {"sum": {"value": total_income}},
                "total_expense": {"sum": {"value": total_expense}},
                "by_category": {"terms": {"buckets": []}},
            },
        }

    @pytest.mark.asyncio
    async def test_coerce_monetary_not_called_for_total_documents(
        self, mcp, monkeypatch
    ):
        """Monkeypatch ``coerce_monetary`` to raise if invoked with a value
        matching ``total_documents``.  Also confirm via call log that it was
        never called with the docs_total value."""
        import financial_mcp.tools.analytics as mod
        from financial_mcp.tools.money import coerce_monetary

        big = 2 ** 53 + 1
        calls: list = []

        def tracking_coerce(value, field):
            calls.append((field, value))
            if value == big:
                raise AssertionError(
                    f"coerce_monetary was called with total_documents value {big!r} "
                    f"(field={field!r}); total_documents must not be monetarily coerced"
                )
            return coerce_monetary(value, field)

        monkeypatch.setattr(mod, "coerce_monetary", tracking_coerce)

        async def fake_query(q):
            return self._summary_with_docs_total(
                docs_total=big, total_income=100, total_expense=50
            )

        monkeypatch.setattr(mod, "query_elasticsearch", fake_query)
        tool = mcp._tool_manager._tools["get_financial_summary"].fn
        result = json.loads(await tool())

        # total_documents must appear unchanged in the output
        assert result["total_documents"] == big
        # No call to coerce_monetary may have received the total_documents value
        for field, value in calls:
            assert value != big, f"coerce_monetary called on total_documents: {calls}"

    @pytest.mark.asyncio
    async def test_large_total_documents_preserved_verbatim(
        self, mcp, monkeypatch
    ):
        """A total_documents value above 2**53 is an int (ES long), not a
        float.  It must pass through unchanged without any monetary guard."""
        import financial_mcp.tools.analytics as mod

        big = 2 ** 53 + 1
        assert isinstance(big, int)
        # Confirm it would be rejected by the monetary guard if coerced as float
        from financial_mcp.tools.money import coerce_monetary
        import pytest as _pytest
        with _pytest.raises(Exception):
            coerce_monetary(float(big), "check")

        async def fake_query(q):
            return self._summary_with_docs_total(
                docs_total=big, total_income=0, total_expense=0
            )

        monkeypatch.setattr(mod, "query_elasticsearch", fake_query)
        tool = mcp._tool_manager._tools["get_financial_summary"].fn
        result = json.loads(await tool())
        assert result["total_documents"] == big
        assert isinstance(result["total_documents"], int)
        # Monetary fields still valid in the same response
        assert result["total_income"] == 0
        assert result["total_expense"] == 0

    @pytest.mark.asyncio
    async def test_monetary_fields_still_validated_alongside_total_documents(
        self, mcp, monkeypatch
    ):
        """Even when total_documents is a large int, a bad monetary value in
        the same response must still trigger the fail-closed error envelope."""
        import financial_mcp.tools.analytics as mod

        big_docs = 2 ** 53 + 1

        async def fake_query(q):
            return {
                "hits": {"total": {"value": big_docs}},
                "aggregations": {
                    "total_income": {"sum": {"value": float("nan")}},
                    "total_expense": {"sum": {"value": 0}},
                    "by_category": {"terms": {"buckets": []}},
                },
            }

        monkeypatch.setattr(mod, "query_elasticsearch", fake_query)
        tool = mcp._tool_manager._tools["get_financial_summary"].fn
        result = json.loads(await tool())
        assert result["error"] == "monetary value failed validation: total_income"
        assert "total_documents" not in result


# ---------------------------------------------------------------------------
# F-007: caller-level error response tests (analytics)
# ---------------------------------------------------------------------------

from financial_mcp.es_client import ElasticsearchQueryError, es_error_payload


def _make_es_error(reason: str) -> ElasticsearchQueryError:
    """Build a real ElasticsearchQueryError with the static message."""
    return ElasticsearchQueryError(
        es_error_payload(reason)["error"], reason=reason
    )


class TestGetSpendingByCategoryEsErrors:
    """F-007: get_spending_by_category returns a sanitized error envelope."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reason", ["es_connection", "es_auth", "es_validation"])
    async def test_returns_sanitized_error(self, mcp, monkeypatch, reason):
        import financial_mcp.tools.analytics as mod

        async def fail(q):
            raise _make_es_error(reason)

        monkeypatch.setattr(mod, "query_elasticsearch", fail)
        tool = mcp._tool_manager._tools["get_spending_by_category"].fn
        result = json.loads(await tool(QueryTransactionParams()))
        assert result == es_error_payload(reason)
        assert "grand_total" not in result
        assert "categories" not in result

    @pytest.mark.asyncio
    async def test_error_is_not_successful_empty(self, mcp, monkeypatch):
        """es_connection must not be silently treated as an empty result set."""
        import financial_mcp.tools.analytics as mod

        async def fail(q):
            raise _make_es_error("es_connection")

        monkeypatch.setattr(mod, "query_elasticsearch", fail)
        tool = mcp._tool_manager._tools["get_spending_by_category"].fn
        result = json.loads(await tool(QueryTransactionParams()))
        assert "grand_total" not in result
        assert "categories" not in result
        assert "error" in result


class TestGetFinancialSummaryEsErrors:
    """F-007: get_financial_summary returns a sanitized error envelope."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reason", ["es_timeout", "es_not_found", "es_dependency"])
    async def test_returns_sanitized_error(self, mcp, monkeypatch, reason):
        import financial_mcp.tools.analytics as mod

        async def fail(q):
            raise _make_es_error(reason)

        monkeypatch.setattr(mod, "query_elasticsearch", fail)
        tool = mcp._tool_manager._tools["get_financial_summary"].fn
        result = json.loads(await tool())
        assert result == es_error_payload(reason)
        assert "total_income" not in result
        assert "total_documents" not in result

    @pytest.mark.asyncio
    async def test_error_is_not_successful_empty(self, mcp, monkeypatch):
        """es_connection must not be silently treated as an empty result set."""
        import financial_mcp.tools.analytics as mod

        async def fail(q):
            raise _make_es_error("es_connection")

        monkeypatch.setattr(mod, "query_elasticsearch", fail)
        tool = mcp._tool_manager._tools["get_financial_summary"].fn
        result = json.loads(await tool())
        assert "total_income" not in result
        assert "total_documents" not in result
        assert "error" in result


class TestEsQueryErrorCallerContract:
    """F-007: analytics tools must translate ElasticsearchQueryError
    into the sanitized JSON error envelope."""

    @pytest.mark.asyncio
    async def test_get_spending_by_category_returns_sanitized_error(
        self, mcp, monkeypatch
    ):
        import financial_mcp.tools.analytics as mod
        from financial_mcp.es_client import (
            ElasticsearchQueryError,
            es_error_payload,
        )

        async def fail(q):
            raise ElasticsearchQueryError(
                es_error_payload("es_connection")["error"],
                reason="es_connection",
            )
        monkeypatch.setattr(mod, "query_elasticsearch", fail)
        tool = mcp._tool_manager._tools["get_spending_by_category"].fn
        result = json.loads(await tool(QueryTransactionParams()))
        assert result["reason"] == "es_connection"
        assert result["error"] == "Elasticsearch connection unavailable"
        assert "grand_total" not in result
        assert "categories" not in result

    @pytest.mark.asyncio
    async def test_get_financial_summary_returns_sanitized_error(
        self, mcp, monkeypatch
    ):
        import financial_mcp.tools.analytics as mod
        from financial_mcp.es_client import (
            ElasticsearchQueryError,
            es_error_payload,
        )

        async def fail(q):
            raise ElasticsearchQueryError(
                es_error_payload("es_auth")["error"],
                reason="es_auth",
            )
        monkeypatch.setattr(mod, "query_elasticsearch", fail)
        tool = mcp._tool_manager._tools["get_financial_summary"].fn
        result = json.loads(await tool())
        assert result["reason"] == "es_auth"
        assert result["error"] == "Elasticsearch authentication or authorization failed"
        assert "total_income" not in result
        assert "net_cash_flow" not in result


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
