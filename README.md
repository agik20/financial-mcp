# MCP Server for Personal Financial Transactions

MCP server untuk querying data transaksi finansial pribadi yang disimpan di Elasticsearch.

## Prerequisites

- Python 3.10+
- Elasticsearch 8.x di `http://localhost:9200`
- `uv` (package manager)

## Setup

```shell
uv venv
.venv\Scripts\activate   # Windows
uv add "elasticsearch>=8.0.0,<9.0.0" "mcp[cli]" pydantic
```

## Pipeline

### 1. Konversi CSV → JSON (opsional, jika data masih .csv)

```shell
python data-converter.py
```

### 2. Ingest ke Elasticsearch

```shell
python ingest-financial-data.py
```

Membuat index `financial-transactions` dan bulk-ingest semua dokumen dari `Personal Finance - Transactions.json`.

### 3. Jalankan MCP Server

```shell
python financial_mcp.py
```

## Tools Tersedia

| Tool | Deskripsi |
|---|---|
| `get_all_transactions` | 50 transaksi terbaru |
| `query_transactions` | Filter by tanggal, type, category, source + optional aggregation |
| `get_spending_by_category` | Breakdown total per kategori |
| `get_financial_summary` | Total income/expense, net, saldo terkini |

## Konfigurasi Claude Desktop

```json
{
  "mcpServers": {
    "financial": {
      "command": "python",
      "args": ["/path/to/financial_mcp.py"],
      "env": {
        "ES_HOST": "http://localhost:9200"
      }
    }
  }
}
```

## Reset Data

```bash
curl -X DELETE http://localhost:9200/financial-transactions
python ingest-financial-data.py
```
