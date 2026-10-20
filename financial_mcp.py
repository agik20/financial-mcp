"""
financial_mcp.py
A Model Context Protocol server for querying personal financial transaction data
stored in Elasticsearch.
"""

import json
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Optional

from elasticsearch import AsyncElasticsearch
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, field_validator

# Initialize FastMCP server
mcp = FastMCP("financial-transactions")

# Constants
ES_HOST = os.getenv("ES_HOST", "http://localhost:9200")
ES_INDEX = "financial-transactions"
ES_API_KEY = os.getenv("ES_API_KEY")


# ========= PYDANTIC MODELS =========

class QueryTransactionParams(BaseModel):
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    type: Optional[str] = None          # "Expense" | "Income"
    category: Optional[str] = None      # "Food & Drink", "Loan", etc.
    source_name: Optional[str] = None   # "BNI", etc.
    aggregation: Optional[str] = None   # "daily" | "weekly" | "monthly"
    limit: Optional[int] = 50

    @field_validator("start_date", "end_date")
    def validate_date(cls, v):
        if v is None:
            return v
        try:
            datetime.strptime(v, "%Y-%m-%d")
            return v
        except ValueError:
            raise ValueError("Invalid date format. Use YYYY-MM-DD")

    @field_validator("type")
    def validate_type(cls, v):
        if v is not None and v not in ("Expense", "Income"):
            raise ValueError("type must be 'Expense' or 'Income'")
        return v

    @field_validator("aggregation")
    def validate_aggregation(cls, v):
        valid = ("daily", "weekly", "monthly", None)
        if v not in valid:
            raise ValueError(f"aggregation must be one of: {valid[:-1]}")
        return v


# ========= ES CLIENT =========

@asynccontextmanager
async def get_es_client():
    """Context manager for Elasticsearch client."""
    if ES_API_KEY:
        client = AsyncElasticsearch([ES_HOST], api_key=ES_API_KEY)
    else:
        client = AsyncElasticsearch([ES_HOST])
    try:
        yield client
    finally:
        await client.close()


# ========= QUERY HELPER =========

async def query_elasticsearch(query: dict) -> dict[str, Any] | None:
    """Makes a request to Elasticsearch with proper error handling."""
    print(f"Sending query to Elasticsearch: {json.dumps(query)}")
    try:
        async with get_es_client() as client:
            ping = await client.ping()
            print(f"ES ping result: {ping}")
            response = await client.search(index=ES_INDEX, body=query)
            return response.body
    except Exception as e:
        print(f"Error querying Elasticsearch: {type(e).__name__}: {str(e)}")
        import traceback
        traceback.print_exc()
        return None


# ========= TOOLS =========

@mcp.tool()
async def get_all_transactions() -> str:
    """Get the most recent 50 financial transactions."""
    query = {
        "size": 50,
        "sort": [{"creationDate": {"order": "desc"}}],
        "query": {"match_all": {}}
    }
    data = await query_elasticsearch(query)
    if not data:
        return json.dumps({"error": "Unable to query Elasticsearch"}, indent=2)

    hits = data["hits"]["hits"]
    records = [h["_source"] for h in hits]
    return json.dumps({
        "total_records": data["hits"]["total"]["value"],
        "returned": len(records),
        "data": records
    }, indent=2, ensure_ascii=False)


@mcp.tool()
async def query_transactions(params: QueryTransactionParams) -> str:
    """
    Query financial transactions with filters and optional aggregation.

    Args:
        start_date: Start date YYYY-MM-DD
        end_date: End date YYYY-MM-DD
        type: Transaction type — 'Expense' or 'Income'
        category: Category filter e.g. 'Food & Drink', 'Loan'
        source_name: Source filter e.g. 'BNI'
        aggregation: Group results by 'daily', 'weekly', or 'monthly'
        limit: Max records to return (default 50)
    """
    must_clauses = []

    # Date range filter
    if params.start_date or params.end_date:
        date_range: dict = {}
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
        must_clauses.append({"term": {"sourceName": params.source_name}})

    es_query: dict = {
        "query": {"bool": {"must": must_clauses}} if must_clauses else {"match_all": {}},
        "size": 0 if params.aggregation else (params.limit or 50),
        "sort": [{"creationDate": {"order": "desc"}}] if not params.aggregation else [],
    }

    # Aggregation
    if params.aggregation:
        interval_map = {
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
                    "min_doc_count": 1
                },
                "aggs": {
                    "total_expense": {
                        "filter": {"term": {"type": "Expense"}},
                        "aggs": {"sum": {"sum": {"field": "value"}}}
                    },
                    "total_income": {
                        "filter": {"term": {"type": "Income"}},
                        "aggs": {"sum": {"sum": {"field": "value"}}}
                    },
                    "net": {"sum": {
                        "script": {
                            "source": "doc['type'].value == 'Income' ? doc['value'].value : -doc['value'].value"
                        }
                    }},
                    "tx_count": {"value_count": {"field": "value"}}
                }
            }
        }

        data = await query_elasticsearch(es_query)
        if not data:
            return json.dumps({"error": "Unable to query Elasticsearch"}, indent=2)

        buckets = data.get("aggregations", {}).get("time_series", {}).get("buckets", [])
        result = []
        for b in buckets:
            result.append({
                "period": b["key_as_string"],
                "total_expense": b["total_expense"]["sum"]["value"],
                "total_income": b["total_income"]["sum"]["value"],
                "net": b["net"]["value"],
                "tx_count": b["tx_count"]["value"]
            })

        return json.dumps({
            "aggregation": params.aggregation,
            "total_periods": len(result),
            "data": result
        }, indent=2, ensure_ascii=False)

    # No aggregation — return raw records
    data = await query_elasticsearch(es_query)
    if not data:
        return json.dumps({"error": "Unable to query Elasticsearch"}, indent=2)

    hits = data["hits"]["hits"]
    records = [h["_source"] for h in hits]
    return json.dumps({
        "total_matched": data["hits"]["total"]["value"],
        "returned": len(records),
        "data": records
    }, indent=2, ensure_ascii=False)


@mcp.tool()
async def get_spending_by_category(params: QueryTransactionParams) -> str:
    """
    Get total spending grouped by category.

    Args:
        start_date: Start date YYYY-MM-DD
        end_date: End date YYYY-MM-DD
        type: 'Expense' or 'Income' (default: Expense)
    """
    must_clauses: list = [{"term": {"type": params.type or "Expense"}}]

    if params.start_date or params.end_date:
        date_range: dict = {}
        if params.start_date:
            date_range["gte"] = params.start_date
        if params.end_date:
            date_range["lte"] = params.end_date
        must_clauses.append({"range": {"day": date_range}})

    es_query = {
        "size": 0,
        "query": {"bool": {"must": must_clauses}},
        "aggs": {
            "by_category": {
                "terms": {"field": "category", "size": 20},
                "aggs": {
                    "total": {"sum": {"field": "value"}},
                    "tx_count": {"value_count": {"field": "value"}},
                    "avg_per_tx": {"avg": {"field": "value"}}
                }
            },
            "grand_total": {"sum": {"field": "value"}}
        }
    }

    data = await query_elasticsearch(es_query)
    if not data:
        return json.dumps({"error": "Unable to query Elasticsearch"}, indent=2)

    grand_total = data["aggregations"]["grand_total"]["value"]
    buckets = data["aggregations"]["by_category"]["buckets"]

    result = []
    for b in buckets:
        total = b["total"]["value"]
        result.append({
            "category": b["key"],
            "total": total,
            "percentage": round((total / grand_total * 100), 1) if grand_total else 0,
            "tx_count": int(b["tx_count"]["value"]),
            "avg_per_tx": round(b["avg_per_tx"]["value"], 0)
        })

    result.sort(key=lambda x: x["total"], reverse=True)

    return json.dumps({
        "type": params.type or "Expense",
        "grand_total": grand_total,
        "categories": result
    }, indent=2, ensure_ascii=False)


@mcp.tool()
async def get_financial_summary() -> str:
    """Get overall financial summary: total income, expense, net balance, and top categories."""
    es_query = {
        "size": 0,
        "query": {"match_all": {}},
        "aggs": {
            "total_income": {
                "filter": {"term": {"type": "Income"}},
                "aggs": {"sum": {"sum": {"field": "value"}}}
            },
            "total_expense": {
                "filter": {"term": {"type": "Expense"}},
                "aggs": {"sum": {"sum": {"field": "value"}}}
            },
            "by_category": {
                "terms": {"field": "category", "size": 10},
                "aggs": {"total": {"sum": {"field": "value"}}}
            },
            "latest_balance": {
                "top_hits": {
                    "size": 1,
                    "sort": [{"creationDate": {"order": "desc"}}],
                    "_source": ["balance", "creationDate"]
                }
            }
        }
    }

    data = await query_elasticsearch(es_query)
    if not data:
        return json.dumps({"error": "Unable to query Elasticsearch"}, indent=2)

    aggs = data["aggregations"]
    total_income = aggs["total_income"]["sum"]["value"]
    total_expense = aggs["total_expense"]["sum"]["value"]
    latest = aggs["latest_balance"]["hits"]["hits"]
    latest_balance = latest[0]["_source"]["balance"] if latest else None
    latest_date = latest[0]["_source"]["creationDate"] if latest else None

    top_categories = [
        {"category": b["key"], "total": b["total"]["value"]}
        for b in aggs["by_category"]["buckets"]
    ]

    return json.dumps({
        "total_income": total_income,
        "total_expense": total_expense,
        "net": total_income - total_expense,
        "latest_balance": latest_balance,
        "latest_balance_date": latest_date,
        "top_categories": top_categories,
        "total_documents": data["hits"]["total"]["value"]
    }, indent=2, ensure_ascii=False)


# ========= PROMPTS =========

@mcp.prompt()
def monthly_report(month: str = "") -> str:
    """Generate a monthly financial report. month format: YYYY-MM"""
    if month:
        start = f"{month}-01"
        # rough end date
        end = f"{month}-31"
        return (
            f"Buatkan laporan keuangan bulanan untuk periode {month}. "
            f"Gunakan tool query_transactions dengan start_date='{start}' end_date='{end}' aggregation='daily', "
            f"lalu get_spending_by_category untuk breakdown per kategori. "
            f"Tampilkan total pemasukan, pengeluaran, net, dan kategori terbesar."
        )
    return (
        "Buatkan laporan keuangan bulan ini. "
        "Gunakan get_financial_summary untuk overview, lalu get_spending_by_category untuk detail per kategori."
    )


@mcp.prompt()
def spending_analysis(start_date: str, end_date: str) -> str:
    """Analyze spending patterns between two dates."""
    return (
        f"Analisis pola pengeluaran dari {start_date} sampai {end_date}. "
        f"Gunakan query_transactions dengan aggregation='weekly' untuk tren mingguan, "
        f"dan get_spending_by_category untuk breakdown kategori. "
        f"Identifikasi kategori terbesar dan berikan rekomendasi."
    )


@mcp.prompt()
def cashflow_check() -> str:
    """Quick cashflow health check."""
    return (
        "Lakukan pengecekan cashflow: gunakan get_financial_summary untuk overview, "
        "lalu query_transactions dengan aggregation='monthly' untuk tren bulanan. "
        "Hitung apakah saldo cenderung naik atau turun, dan identifikasi bulan dengan pengeluaran tertinggi."
    )


# ========= MAIN =========

if __name__ == "__main__":
    mcp.run(transport="stdio")