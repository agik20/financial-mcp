import json
from elasticsearch import Elasticsearch, helpers
import hashlib


# ========= CONFIG =========
ES_HOST = "http://localhost:9200"
ES_INDEX = "financial-transactions"
DATA_FILE_PATH = r"C:\Users\ardut\projects\apple-watch-health-mcp-main\Personal Finance  - Transactions.json"


# ========= INDEX MAPPING =========
INDEX_MAPPING = {
    "mappings": {
        "properties": {
            "type": {"type": "keyword"},
            "category": {"type": "keyword"},
            "sourceName": {"type": "keyword"},

            "unit": {"type": "keyword"},

            "value": {"type": "long"},
            "balance": {"type": "long"},

            "description": {"type": "text"},

            "creationDate": {"type": "date"},
            "day": {"type": "date"},
            "dayOfWeek": {"type": "keyword"},
            "hour": {"type": "integer"},

            "payCycle": {"type": "date"}
        }
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 0
    }
}


# ========= ELASTIC CLIENT =========
def create_es_client():
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


# ========= DOC ID (ANTI DUPLICATE) =========
def generate_doc_id(doc):
    raw = f"{doc.get('creationDate')}_{doc.get('value')}_{doc.get('description')}"
    return hashlib.md5(raw.encode()).hexdigest()


# ========= GENERATE BULK ACTIONS =========
def generate_actions(filepath, index_name):
    with open(filepath, 'r', encoding='utf-8') as f:
        data = json.load(f)

    for doc in data:
        # Basic validation (skip bad data)
        if not doc.get("value") or not doc.get("creationDate"):
            continue

        yield {
            "_index": index_name,
            "_id": generate_doc_id(doc),
            "_source": doc
        }


# ========= INGESTION PROCESS =========
def ingest_data(client: Elasticsearch):

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
    count = client.count(index=ES_INDEX)['count']

    print(f"📊 Final document count: {count}")


# ========= MAIN =========
if __name__ == "__main__":
    es_client = create_es_client()

    if es_client:
        ingest_data(es_client)