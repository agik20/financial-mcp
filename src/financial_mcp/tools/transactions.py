"""MCP tools for querying raw financial transactions."""

import json
from typing import Any, Dict

from mcp.server.fastmcp import FastMCP

from financial_mcp.es_client import query_elasticsearch
from financial_mcp.models import QueryTransactionParams


def register_transaction_tools(mcp: FastMCP) -> None:
    """Register transaction-related tools on *mcp*.

    Args:
        mcp: The FastMCP server instance to attach tools to.
    """

    @mcp.tool()
    async def get_all_transactions() -> str:
        """Get the most recent 50 financial transactions.

        Returns:
            A JSON string containing the total record count, the number
            of records returned, and the list of transaction objects.
        """
        query: Dict[str, Any] = {
            "size": 50,
            "sort": [{"creationDate": {"order": "desc"}}],
            "query": {"match_all": {}},
        }
        data = await query_elasticsearch(query)
        if not data:
            return json.dumps(
                {"error": "Unable to query Elasticsearch"}, indent=2
            )

        hits = data["hits"]["hits"]
        records = [h["_source"] for h in hits]
        return json.dumps(
            {
                "total_records": data["hits"]["total"]["value"],
                "returned": len(records),
                "data": records,
            },
            indent=2,
            ensure_ascii=False,
        )

    @mcp.tool()
    async def query_transactions(
        params: QueryTransactionParams,
    ) -> str:
        """Query financial transactions with filters and optional aggregation.

        Args:
            params: Filter / aggregation parameters (see
                :class:`QueryTransactionParams`).

        Returns:
            A JSON string.  When ``params.aggregation`` is set the result
            contains time-series buckets; otherwise it contains raw records.
        """
        must_clauses: list[Dict[str, Any]] = []

        # Date range filter
        if params.start_date or params.end_date:
            date_range: Dict[str, str] = {}
            if params.start_date:
                date_range["gte"] = params.start_date
            if params.end_date:
                date_range["lte"] = params.end_date
            must_clauses.append({"range": {"day": date_range}})

        # Type filter
        if params.type:
            must_clauses.append({"term": {"type": params.type}})

        # Category filter
        if params.category:
            must_clauses.append({"term": {"category": params.category}})

        # Source filter
        if params.source_name:
            must_clauses.append(
                {"term": {"sourceName": params.source_name}},
            )

        es_query: Dict[str, Any] = {
            "query": (
                {"bool": {"must": must_clauses}}
                if must_clauses
                else {"match_all": {}}
            ),
            "size": 0 if params.aggregation else (params.limit or 50),
            "sort": (
                []
                if params.aggregation
                else [{"creationDate": {"order": "desc"}}]
            ),
        }

        # Aggregation branch
        if params.aggregation:
            interval_map: Dict[str, str] = {
                "daily": "1d",
                "weekly": "1w",
                "monthly": "1M",
            }
            calendar_interval = interval_map[params.aggregation]
            es_query["aggs"] = {
                "time_series": {
                    "date_histogram": {
                        "field": "day",
                        "calendar_interval": calendar_interval,
                        "min_doc_count": 1,
                    },
                    "aggs": {
                        "total_expense": {
                            "filter": {"term": {"type": "Expense"}},
                            "aggs": {
                                "sum": {"sum": {"field": "value"}}
                            },
                        },
                        "total_income": {
                            "filter": {"term": {"type": "Income"}},
                            "aggs": {
                                "sum": {"sum": {"field": "value"}}
                            },
                        },
                        "net": {
                            "sum": {
                                "script": {
                                    "source": (
                                        "doc['type'].value == 'Income' "
                                        "? doc['value'].value "
                                        ": -doc['value'].value"
                                    )
                                }
                            }
                        },
                        "tx_count": {
                            "value_count": {"field": "value"}
                        },
                    },
                }
            }

            data = await query_elasticsearch(es_query)
            if not data:
                return json.dumps(
                    {"error": "Unable to query Elasticsearch"}, indent=2
                )

            buckets = (
                data.get("aggregations", {})
                .get("time_series", {})
                .get("buckets", [])
            )
            result: list[Dict[str, Any]] = []
            for b in buckets:
                result.append(
                    {
                        "period": b["key_as_string"],
                        "total_expense": b["total_expense"]["sum"]["value"],
                        "total_income": b["total_income"]["sum"]["value"],
                        "net": b["net"]["value"],
                        "tx_count": b["tx_count"]["value"],
                    }
                )

            return json.dumps(
                {
                    "aggregation": params.aggregation,
                    "total_periods": len(result),
                    "data": result,
                },
                indent=2,
                ensure_ascii=False,
            )

        # No aggregation -- return raw records
        data = await query_elasticsearch(es_query)
        if not data:
            return json.dumps(
                {"error": "Unable to query Elasticsearch"}, indent=2
            )

        hits = data["hits"]["hits"]
        records = [h["_source"] for h in hits]
        return json.dumps(
            {
                "total_matched": data["hits"]["total"]["value"],
                "returned": len(records),
                "data": records,
            },
            indent=2,
            ensure_ascii=False,
        )
