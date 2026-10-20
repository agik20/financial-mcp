"""MCP tools for financial analytics (category breakdown, summary)."""

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

        Args:
            params: Filter parameters.  ``type`` defaults to ``"Expense"``
                when not specified.

        Returns:
            A JSON string with the grand total, per-category breakdown
            (total, percentage, transaction count, average), and the
            category filter that was applied.
        """
        must_clauses: list[Dict[str, Any]] = [
            {"term": {"type": params.type or "Expense"}}
        ]

        if params.start_date or params.end_date:
            date_range: Dict[str, str] = {}
            if params.start_date:
                date_range["gte"] = params.start_date
            if params.end_date:
                date_range["lte"] = params.end_date
            must_clauses.append({"range": {"day": date_range}})

        es_query: Dict[str, Any] = {
            "size": 0,
            "query": {"bool": {"must": must_clauses}},
            "aggs": {
                "by_category": {
                    "terms": {"field": "category", "size": 20},
                    "aggs": {
                        "total": {"sum": {"field": "value"}},
                        "tx_count": {"value_count": {"field": "value"}},
                        "avg_per_tx": {"avg": {"field": "value"}},
                    },
                },
                "grand_total": {"sum": {"field": "value"}},
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
                "type": params.type or "Expense",
                "grand_total": grand_total,
                "categories": result,
            },
            indent=2,
            ensure_ascii=False,
        )

    @mcp.tool()
    async def get_financial_summary() -> str:
        """Get overall financial summary: total income, expense, net balance,
        and top categories.

        Returns:
            A JSON string with totals, net, latest balance info, top
            10 categories, and total document count.
        """
        es_query: Dict[str, Any] = {
            "size": 0,
            "query": {"match_all": {}},
            "aggs": {
                "total_income": {
                    "filter": {"term": {"type": "Income"}},
                    "aggs": {"sum": {"sum": {"field": "value"}}},
                },
                "total_expense": {
                    "filter": {"term": {"type": "Expense"}},
                    "aggs": {"sum": {"sum": {"field": "value"}}},
                },
                "by_category": {
                    "terms": {"field": "category", "size": 10},
                    "aggs": {"total": {"sum": {"field": "value"}}},
                },
                "latest_balance": {
                    "top_hits": {
                        "size": 1,
                        "sort": [{"creationDate": {"order": "desc"}}],
                        "_source": ["balance", "creationDate"],
                    }
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
        latest = aggs["latest_balance"]["hits"]["hits"]
        latest_balance = (
            latest[0]["_source"]["balance"] if latest else None
        )
        latest_date = (
            latest[0]["_source"]["creationDate"] if latest else None
        )

        top_categories = [
            {"category": b["key"], "total": b["total"]["value"]}
            for b in aggs["by_category"]["buckets"]
        ]

        return json.dumps(
            {
                "total_income": total_income,
                "total_expense": total_expense,
                "net": total_income - total_expense,
                "latest_balance": latest_balance,
                "latest_balance_date": latest_date,
                "top_categories": top_categories,
                "total_documents": data["hits"]["total"]["value"],
            },
            indent=2,
            ensure_ascii=False,
        )
