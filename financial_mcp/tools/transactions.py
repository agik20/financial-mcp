"""MCP tools for querying raw financial transactions."""

import json
from typing import Any, Dict, Optional, Tuple

from mcp.server.fastmcp import FastMCP

from financial_mcp.es_client import query_elasticsearch
from financial_mcp.models import QueryTransactionParams


def _parse_es_total(hits_total: Any) -> Tuple[int, str]:
    """Normalise an Elasticsearch ``hits.total`` value.

    Handles the ES 8.x dict shape ``{"value": N, "relation": "eq"}``
    and the legacy scalar shape where ``hits.total`` is a plain integer
    (exact count, implied ``"eq"``).

    Returns:
        A ``(total_value, relation)`` tuple.
    """
    if isinstance(hits_total, dict):
        value = int(hits_total.get("value", 0))
        relation = hits_total.get("relation", "eq")
        # Fail closed: an unrecognised relation is treated as a lower
        # bound, never as an exact count.
        if relation not in ("eq", "gte"):
            relation = "gte"
        return value, relation
    # Legacy / ES 7.x scalar: always treated as exact
    return int(hits_total or 0), "eq"


def _compute_has_more(
    total_value: int,
    returned: int,
    relation: str,
) -> Optional[bool]:
    """Return the ``has_more`` value for a raw-record response.

    Truth table (F-001 spec):
    - eq + total > returned  -> True
    - eq + total <= returned -> False
    - gte + total > returned -> True
    - gte + total <= returned-> None  (completeness unknown)
    """
    if relation == "eq":
        return total_value > returned
    # relation == "gte": total_value is a lower bound
    if total_value > returned:
        return True
    return None


def register_transaction_tools(mcp: FastMCP) -> None:
    """Register transaction-related tools on *mcp*.

    Args:
        mcp: The FastMCP server instance to attach tools to.
    """

    @mcp.tool()
    async def get_all_transactions() -> str:
        """Get the most recent 50 financial transactions.

        The list is sorted descending by ``transaction_date`` and returns
        canonical 12-field transaction documents.

        Returns:
            A JSON string containing the total record count, the number
            of records returned, and the list of transaction objects.
        """
        query: Dict[str, Any] = {
            "size": 50,
            "sort": [{"transaction_date": {"order": "desc"}}],
            "query": {"match_all": {}},
            "track_total_hits": True,
        }
        data = await query_elasticsearch(query)
        if not data:
            return json.dumps(
                {"error": "Unable to query Elasticsearch"}, indent=2
            )

        hits = data["hits"]["hits"]
        records = [h["_source"] for h in hits]
        total_records, _ = _parse_es_total(data["hits"]["total"])
        return json.dumps(
            {
                "total_records": total_records,
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

        Filters use the canonical transaction schema:

        * Date range on ``transaction_date``.
        * Type filter (``Income`` / ``Expense`` / ``Transfer``).
        * Category filter.
        * Source account filter (``source_account``; the legacy
          ``source_name`` parameter still works and is folded into
          ``source_account`` by the model).

        When ``params.aggregation`` is set, the response contains
        time-series buckets keyed on ``transaction_date`` with the correct
        signed-amount semantics:

        * ``total_expense``  -- sum of *amount* for expense documents
          (positive magnitude of outflows).
        * ``total_income``   -- sum of *signed_amount* for income documents
          (positive or zero).
        * ``net``            -- ``total_income - total_expense``.
          Transfer transactions are **excluded** from the net figure;
          they represent money moving between the owner's own accounts
          and must not count as gains or losses.

        Args:
            params: Filter / aggregation parameters (see
                :class:`QueryTransactionParams`).

        Returns:
            A JSON string.  When ``params.aggregation`` is set the result
            contains time-series buckets; otherwise it contains raw records.
        """
        must_clauses: list[Dict[str, Any]] = []

        # Date range filter on the canonical date field.
        if params.start_date or params.end_date:
            date_range: Dict[str, str] = {}
            if params.start_date:
                date_range["gte"] = params.start_date
            if params.end_date:
                date_range["lte"] = params.end_date
            must_clauses.append(
                {"range": {"transaction_date": date_range}}
            )

        # Type filter (canonical: Income / Expense / Transfer).
        if params.type:
            must_clauses.append({"term": {"type": params.type}})

        # Category filter.
        if params.category:
            must_clauses.append({"term": {"category": params.category}})

        # Source account filter (canonical ``source_account``; the legacy
        # ``source_name`` parameter is folded in by the Pydantic model).
        if params.effective_source_account:
            must_clauses.append(
                {"term": {"source_account": params.effective_source_account}},
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
                else [{"transaction_date": {"order": "desc"}}]
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
            # ``total_expense`` sums ``amount`` for expense documents;
            # ``total_income`` sums ``signed_amount`` for income documents.
            # ``net`` is computed client-side as total_income - total_expense
            # so Transfer transactions are excluded by construction.
            es_query["aggs"] = {
                "time_series": {
                    "date_histogram": {
                        "field": "transaction_date",
                        "calendar_interval": calendar_interval,
                        "min_doc_count": 1,
                    },
                    "aggs": {
                        "total_expense": {
                            "filter": {"term": {"type": "Expense"}},
                            "aggs": {
                                "sum": {"sum": {"field": "amount"}}
                            },
                        },
                        "total_income": {
                            "filter": {"term": {"type": "Income"}},
                            "aggs": {
                                "sum": {"sum": {"field": "signed_amount"}}
                            },
                        },
                        "tx_count": {
                            "value_count": {"field": "signed_amount"}
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
                te = b["total_expense"]["sum"]["value"]
                ti = b["total_income"]["sum"]["value"]
                result.append(
                    {
                        "period": b["key_as_string"],
                        "total_expense": te,
                        "total_income": ti,
                        "net": ti - te,
                        "tx_count": int(b["tx_count"]["value"]),
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
        es_query["track_total_hits"] = True
        data = await query_elasticsearch(es_query)
        if not data:
            return json.dumps(
                {"error": "Unable to query Elasticsearch"}, indent=2
            )

        hits = data["hits"]["hits"]
        records = [h["_source"] for h in hits]
        total_matched, total_relation = _parse_es_total(data["hits"]["total"])
        has_more = _compute_has_more(total_matched, len(records), total_relation)
        return json.dumps(
            {
                "total_matched": total_matched,
                "returned": len(records),
                "has_more": has_more,
                "total_relation": total_relation,
                "data": records,
            },
            indent=2,
            ensure_ascii=False,
        )
