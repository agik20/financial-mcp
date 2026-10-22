"""Ingest JSON transaction data into Elasticsearch.

Deletes and recreates the index, then bulk-inserts every valid document
from the JSON data file.

Usage::

    python -m financial_mcp.ingest
"""

from elasticsearch import Elasticsearch, helpers

from financial_mcp.config import DATA_FILE_PATH, ES_INDEX
from financial_mcp.es_client import create_es_client, generate_actions

# ---------------------------------------------------------------------------
# Index mapping
# ---------------------------------------------------------------------------
INDEX_MAPPING: dict = {
    "mappings": {
        "properties": {
            "type": {"type": "keyword"},
            "category": {"type": "keyword"},
            "parent-category": {"type": "keyword"},
            "sourceName": {"type": "keyword"},
            "unit": {"type": "keyword"},
            "value": {"type": "long"},
            "description": {"type": "text"},
            "creationDate": {"type": "date"},
            "day": {"type": "date"},
            "dayOfWeek": {"type": "keyword"},
            "hour": {"type": "integer"},
            "payCycle": {"type": "date"},
        }
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 0,
    },
}


def ingest_data(client: Elasticsearch) -> None:
    """Delete, recreate, and bulk-fill the transactions index.

    Steps:
        1. Delete the existing index (if present).
        2. Create a fresh index with the standard mapping.
        3. Bulk-insert documents from *DATA_FILE_PATH*.
        4. Refresh and print the final document count.

    Args:
        client: A connected synchronous :class:`Elasticsearch` instance.
    """
    # 1. Delete existing index
    if client.indices.exists(index=ES_INDEX):
        print(f"Index '{ES_INDEX}' found. Deleting...")
        client.indices.delete(index=ES_INDEX)
        print("Index deleted.")

    # 2. Create new index
    print(f"Creating index '{ES_INDEX}'...")
    client.indices.create(index=ES_INDEX, body=INDEX_MAPPING)
    print("Index created successfully.")

    # 3. Bulk ingest
    print(f"Ingesting data from '{DATA_FILE_PATH}'...")

    try:
        actions = generate_actions(DATA_FILE_PATH, ES_INDEX)

        success, failed = helpers.bulk(client, actions)

        print(f"✅ Documents ingested: {success}")
        if failed:
            print(f"⚠️ Failed documents: {len(failed)}")

    except FileNotFoundError:
        print(f"ERROR: File '{DATA_FILE_PATH}' not found.")
        return

    except Exception as e:
        print(f"ERROR during ingestion: {e}")
        return

    # 4. Refresh + count
    client.indices.refresh(index=ES_INDEX)
    count = client.count(index=ES_INDEX)["count"]

    print(f"📊 Final document count: {count}")


def run_ingest() -> None:
    """Top-level entry-point: connect, then ingest.

    Creates a synchronous Elasticsearch client and, on success, runs
    :func:`ingest_data`.
    """
    es_client = create_es_client()

    if es_client:
        ingest_data(es_client)


if __name__ == "__main__":
    run_ingest()
