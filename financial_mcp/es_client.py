"""Synchronous and asynchronous Elasticsearch client factories and helpers.

Provides:
    * :func:`create_es_client`        -- sync client (used by ingest / CLI)
    * :func:`get_async_es_client`     -- async context-manager (used by MCP tools)
    * :func:`generate_doc_id`         -- deterministic MD5 document ID
    * :func:`generate_actions`        -- bulk-action generator for ``helpers.bulk``
    * :func:`query_elasticsearch`     -- async search wrapper with error handling
"""

import hashlib
import json
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Dict, Iterator, Optional

from elasticsearch import AsyncElasticsearch, Elasticsearch

from financial_mcp.config import ES_API_KEY, ES_HOST, ES_INDEX


# ---------------------------------------------------------------------------
# Synchronous client (ingest / CLI scripts)
# ---------------------------------------------------------------------------
def create_es_client() -> Optional[Elasticsearch]:
    """Create and verify a synchronous :class:`Elasticsearch` client.

    Returns:
        A connected ``Elasticsearch`` instance, or ``None`` when the
        connection could not be established.
    """
    print(f"Connecting to Elasticsearch at {ES_HOST}...")
    try:
        client = Elasticsearch(hosts=[ES_HOST])

        if not client.ping():
            raise ConnectionError("Could not connect to Elasticsearch.")

        print("Connection successful!")
        return client

    except Exception as e:
        print(f"Connection error: {e}")
        return None


# ---------------------------------------------------------------------------
# Asynchronous client (MCP tools)
# ---------------------------------------------------------------------------
@asynccontextmanager
async def get_async_es_client() -> AsyncGenerator[AsyncElasticsearch, None]:
    """Context manager that yields an :class:`AsyncElasticsearch` client.

    The client is closed when the context exits.  When ``ES_API_KEY`` is
    set it is passed to the constructor for API-key authentication.
    """
    if ES_API_KEY:
        client = AsyncElasticsearch([ES_HOST], api_key=ES_API_KEY)
    else:
        client = AsyncElasticsearch([ES_HOST])
    try:
        yield client
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# Document-ID helper (anti-duplicate)
# ---------------------------------------------------------------------------
def generate_doc_id(doc: Dict[str, Any]) -> str:
    """Return a deterministic MD5 hash for a transaction document.

    Args:
        doc: A transaction dictionary containing ``creationDate``,
             ``value``, and ``description`` keys.

    Returns:
        32-character lowercase hex MD5 string.
    """
    raw = (
        f"{doc.get('creationDate')}_"
        f"{doc.get('value')}_"
        f"{doc.get('description')}"
    )
    return hashlib.md5(raw.encode()).hexdigest()  # noqa: S324


# ---------------------------------------------------------------------------
# Bulk-action generator
# ---------------------------------------------------------------------------
def generate_actions(filepath: str, index_name: str) -> Iterator[Dict[str, Any]]:
    """Yield bulk index actions for every valid document in *filepath*.

    Documents missing ``value`` or ``creationDate`` are silently skipped.

    Args:
        filepath: Path to a JSON file containing a list of transaction dicts.
        index_name: Target Elasticsearch index.

    Yields:
        Action dictionaries suitable for ``elasticsearch.helpers.bulk``.
    """
    import json as _json

    with open(filepath, "r", encoding="utf-8") as f:
        data = _json.load(f)

    for doc in data:
        # Basic validation -- skip incomplete records
        if not doc.get("value") or not doc.get("creationDate"):
            continue

        yield {
            "_index": index_name,
            "_id": generate_doc_id(doc),
            "_source": doc,
        }


# ---------------------------------------------------------------------------
# Async query wrapper (used by MCP tool functions)
# ---------------------------------------------------------------------------
async def query_elasticsearch(
    query: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Run an Elasticsearch search and return the response body.

    Args:
        query: A fully-formed Elasticsearch query body.

    Returns:
        The response ``body`` dict, or ``None`` on any error.
    """
    print(f"Sending query to Elasticsearch: {json.dumps(query)}")
    try:
        async with get_async_es_client() as client:
            ping = await client.ping()
            print(f"ES ping result: {ping}")
            response = await client.search(index=ES_INDEX, body=query)
            return response.body
    except Exception as e:
        import traceback

        print(
            f"Error querying Elasticsearch: "
            f"{type(e).__name__}: {str(e)}"
        )
        traceback.print_exc()
        return None
