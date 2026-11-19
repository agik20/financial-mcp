"""MCP tools for financial analytics (category breakdown, summary).

All monetary calculations use the canonical transaction schema:

* **Expense** magnitudes come from :data:`amount` (non-negative integer)
  for expense documents.
* **Income** totals use :data:`signed_amount` (positive or zero for income
  documents).
* **Transfer transactions are excluded entirely** from consolidated income,
  expense, and net cash flow.  They represent money moving between the
  owner's own accounts and must not be counted as gains or losses.
* **Net cash flow** = total_income − total_expense (Transfers excluded).
* No account balances are returned.  The canonical data model does not
  carry balance snapshots, and inventing one would be a data-integrity
  failure -- the source CSV has no balance column.
"""

import json
from typing import Any, Dict

from mcp.server.fastmcp import FastMCP

from financial_mcp.es_client import query_elasticsearch
from financial_mcp.models import QueryTransactionParams


def register_analytics_tools(mcp: FastMCP) -> None:
    """Register analytics-related tools on *mcp*.

    Args:
        mcp: The FastMCP server instance to attach tools to.
    """

    @mcp.tool()
    async def get_spending_by_category(
        params: QueryTransactionParams,
    ) -> str:
        """Get total spending grouped by category.

        The transaction type defaults to ``"Expense"`` when not supplied.
        The result uses :data:`amount` for expense magnitudes and returns,
        for each category: total spending, percentage of grand total,
        transaction count, and average per-transaction amount.

        Args:
            params: Filter parameters.  ``type`` defaults to
                ``"Expense"`` when not specified; ``start_date`` /
                ``end_date`` filter on ``transaction_date``.

        Returns:
            A JSON string with the grand total, per-category breakdown
            (total, percentage, transaction count, average), and the
            type filter that was applied.
        """
        tx_type = params.type or "Expense"
        must_clauses: list[Dict[str, Any]] = [
            {"term": {"type": tx_type}}
        ]

        if params.start_date or params.end_date:
            date_range: Dict[str, str] = {}
            if params.start_date:
                date_range["gte"] = params.start_date
            if params.end_date:
                date_range["lte"] = params.end_date
            must_clauses.append(
                {"range": {"transaction_date": date_range}}
            )

        if params.category:
            must_clauses.append({"term": {"category": params.category}})

        if params.effective_source_account:
            must_clauses.append(
                {"term": {"source_account": params.effective_source_account}}
            )

        # Sum ``amount`` (positive magnitude) for the requested type.  For
        # expenses this is the spending total; for income it would be the
        # total received.  The tool name says "spending" but the filter is
        # the caller's responsibility -- the aggregation itself is always
        # magnitude-based.
        es_query: Dict[str, Any] = {
            "size": 0,
            "query": {"bool": {"must": must_clauses}},
            "aggs": {
                "by_category": {
                    "terms": {"field": "category", "size": 20},
                    "aggs": {
                        "total": {"sum": {"field": "amount"}},
                        "tx_count": {"value_count": {"field": "amount"}},
                        "avg_per_tx": {"avg": {"field": "amount"}},
                    },
                },
                "grand_total": {"sum": {"field": "amount"}},
            },
        }

        data = await query_elasticsearch(es_query)
        if not data:
            return json.dumps(
                {"error": "Unable to query Elasticsearch"}, indent=2
            )

        grand_total = data["aggregations"]["grand_total"]["value"]
        buckets = data["aggregations"]["by_category"]["buckets"]

        result: list[Dict[str, Any]] = []
        for b in buckets:
            total = b["total"]["value"]
            result.append(
                {
                    "category": b["key"],
                    "total": total,
                    "percentage": (
                        round((total / grand_total * 100), 1)
                        if grand_total
                        else 0
                    ),
                    "tx_count": int(b["tx_count"]["value"]),
                    "avg_per_tx": round(b["avg_per_tx"]["value"], 0),
                }
            )

        result.sort(key=lambda x: x["total"], reverse=True)

        return json.dumps(
            {
                "type": tx_type,
                "grand_total": grand_total,
                "categories": result,
            },
            indent=2,
            ensure_ascii=False,
        )

    @mcp.tool()
    async def get_financial_summary() -> str:
        """Get an overall financial summary across all transactions.

        Consolidated cash-flow figures **exclude Transfer transactions
        entirely**.  Transfers represent money moving between the owner's
        own accounts and must not be counted as gains or losses.

        The response contains:

        * ``total_income``  -- sum of ``signed_amount`` for income
          documents.
        * ``total_expense`` -- sum of ``amount`` for expense documents.
        * ``net_cash_flow`` -- ``total_income - total_expense``
          (Transfers excluded).
        * ``top_categories``-- the 10 highest-magnitude categories by
          ``amount`` (Income + Expense only; Transfers excluded).
        * ``total_documents``-- the total number of transaction documents
          (including Transfers, which are still stored and queryable).

        This tool deliberately does **not** report a ``latest_balance``:
        the canonical schema has no balance column, and inventing one
        would be a data-integrity failure.

        Returns:
            A JSON string with the figures above.
        """
        es_query: Dict[str, Any] = {
            "size": 0,
            "query": {"match_all": {}},
            "aggs": {
                "total_income": {
                    "filter": {"term": {"type": "Income"}},
                    "aggs": {"sum": {"sum": {"field": "signed_amount"}}},
                },
                "total_expense": {
                    "filter": {"term": {"type": "Expense"}},
                    "aggs": {"sum": {"sum": {"field": "amount"}}},
                },
                # net_cash_flow is computed client-side as
                # total_income - total_expense so Transfers are excluded
                # by construction.
                "by_category": {
                    "filter": {
                        "bool": {
                            "must_not": [{"term": {"type": "Transfer"}}]
                        }
                    },
                    "aggs": {
                        "terms": {
                            "terms": {
                                "field": "category",
                                "size": 10,
                                "order": [
                                    {"total": "desc"},
                                    {"_key": "asc"},
                                ],
                            },
                            "aggs": {
                                "total": {"sum": {"field": "amount"}}
                            },
                        }
                    },
                },
            },
        }

        data = await query_elasticsearch(es_query)
        if not data:
            return json.dumps(
                {"error": "Unable to query Elasticsearch"}, indent=2
            )

        aggs = data["aggregations"]
        total_income = aggs["total_income"]["sum"]["value"]
        total_expense = aggs["total_expense"]["sum"]["value"]
        net_cash_flow = total_income - total_expense

        by_cat_terms = aggs["by_category"]["terms"]
        top_categories = [
            {"category": b["key"], "total": b["total"]["value"]}
            for b in by_cat_terms["buckets"]
        ]
        top_categories.sort(key=lambda c: (-c["total"], c["category"]))

        return json.dumps(
            {
                "total_income": total_income,
                "total_expense": total_expense,
                "net_cash_flow": net_cash_flow,
                "top_categories": top_categories,
                "total_documents": data["hits"]["total"]["value"],
            },
            indent=2,
            ensure_ascii=False,
        )
