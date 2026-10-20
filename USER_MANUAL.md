# User Manual: Financial MCP Server

## Overview

MCP server untuk querying data transaksi finansial pribadi yang disimpan di Elasticsearch.
Memungkinkan LLM (Claude, dll.) bertanya data keuangan dalam bahasa alami.

## Project Structure

```
apple-watch-health-mcp-main/
├── financial_mcp.py            # MCP server untuk data keuangan
├── ingest-financial-data.py    # Script ingest ke Elasticsearch
├── data-converter.py           # Konversi CSV → JSON
├── Personal Finance - Transactions.csv   # Data menta
├── Personal Finance - Transactions.json  # Data untuk ingest
├── pyproject.toml              # Konfigurasi Python
├── README.md
└── LICENSE
```

## Prerequisites

- Python 3.10+
- Elasticsearch 8.x di `http://localhost:9200`
- uv (package manager)

## Installation

```bash
uv venv
.venv\Scripts\activate   # Windows
uv add "elasticsearch>=8.0.0,<9.0.0" "mcp[cli]" pydantic
```

## Data Setup

1. **Konversi CSV → JSON** (opsional, jika data masih .csv):
   ```bash
   python data-converter.py
   ```

2. **Ingest ke Elasticsearch**:
   ```bash
   python ingest-financial-data.py
   ```
   Membuat index `financial-transactions` dan bulk-ingest semua dokumen.

## Menjalankan MCP Server

```bash
python financial_mcp.py
```

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

## Tools Tersedia

- `get_all_transactions` — 50 transaksi terbaru
- `query_transactions` — filter tanggal/type/category/source + optional aggregation
- `get_spending_by_category` — breakdown per kategori
- `get_financial_summary` — total income/expense, net, saldo terkini

## Parameter Query

- `start_date` / `end_date`: YYYY-MM-DD
- `type`: "Expense" atau "Income"
- `category`: misal "Food & Drink", "Loan"
- `source_name`: misal "BNI"
- `aggregation`: "daily", "weekly", "monthly"
- `limit`: jumlah maksimum record (default 50)

## Troubleshooting

1. **ES tidak terhubung** — pastikan Elasticsearch berjalan di `http://localhost:9200`
2. **Tidak ada data** — jalankan `python ingest-financial-data.py` terlebih dahulu
3. **Import error** — aktifkan venv, `uv add "elasticsearch>=8.0.0,<9.0.0"`

## Reset Data

```bash
curl -X DELETE http://localhost:9200/financial-transactions
python ingest-financial-data.py
```

## Security

- Setup lokal saja, jangan expose ke network publik
- Gunakan API key untuk autentikasi Elasticsearch
- Pertimbangkan regulasi privasi data

---

*Last updated: 2026-10-20*