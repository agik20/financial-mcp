# User Manual: Financial MCP Server

## Overview

MCP server untuk querying data transaksi finansial pribadi yang disimpan di
Elasticsearch. Memungkinkan LLM (Claude, dll.) bertanya data keuangan dalam
bahasa alami.

## Project Structure

```
financial-mcp/
├── financial_mcp/
│   ├── __init__.py
│   ├── config.py                 # Konstanta ES host, index, path file
│   ├── es_client.py              # Client sync/async + query helper + doc-ID
│   ├── converter.py              # CSV → JSON (pandas)
│   ├── ingest.py                 # Bulk ingest ke Elasticsearch
│   ├── models.py                 # Pydantic models (QueryTransactionParams)
│   ├── server.py                 # FastMCP entry point + prompts
│   └── tools/
│       ├── transactions.py       # get_all_transactions, query_transactions
│       └── analytics.py          # get_spending_by_category, get_financial_summary
├── data/
│   ├── Personal Finance  - Transactions.csv    # Data menta (JANGAN di-push)
│   └── Personal Finance  - Transactions.json   # Hasil konversi
├── pyproject.toml
├── README.md
└── USER_MANUAL.md
```

> **Catatan:** Nama file CSV secara sengaja mengandung dua spasi
> (`Finance  -`) — jangan "perbaiki" ini.

## Prerequisites

- Python 3.10+
- Elasticsearch 8.x di `http://localhost:9200`
- `uv` (package manager, disarankan) atau `pip`

## Installation

```bash
# Opsi 1: uv (disarankan)
uv venv
.venv\Scripts\activate        # Windows
uv add "elasticsearch>=8.0.0,<9.0.0" "mcp[cli]" "pydantic>=2.12.5" "pandas>=2.0.0"

# Opsi 2: pip
pip install "elasticsearch>=8.0.0,<9.0.0" "mcp[cli]" "pydantic>=2.12.5" "pandas>=2.0.0"
```

## Environment Variables

| Var        | Wajib? | Default                 | Keterangan                          |
|------------|--------|-------------------------|-------------------------------------|
| `ES_HOST`  | Tidak  | `http://localhost:9200` | Alamat Elasticsearch                |
| `ES_API_KEY` | Tidak | _(kosong)_             | API key auth (isi jika ES butuh)   |

## Data Setup

### 1. Konversi CSV → JSON

```bash
python -m financial_mcp.converter
```

Membaca `data/Personal Finance  - Transactions.csv` dan menghasilkan
`data/Personal Finance  - Transactions.json` dengan skema yang cocok
dengan index mapping.

### 2. Ingest ke Elasticsearch

```bash
python -m financial_mcp.ingest
```

Menghapus index lama (jika ada), membuat ulang index
`financial-transactions`, lalu bulk-insert semua dokumen.

## Menjalankan MCP Server

```bash
python -m financial_mcp.server
```

Server berjalan via stdio — tidak ada port HTTP. Dipanggil oleh MCP
client (Claude Desktop, Claude Code, dsb.).

## Konfigurasi Claude Desktop

File: `%APPDATA%\Claude\claude_desktop_config.json` (Windows)

```json
{
  "mcpServers": {
    "financial": {
      "command": "python",
      "args": ["-m", "financial_mcp.server"],
      "cwd": "C:/path/to/financial-mcp",
      "env": {
        "ES_HOST": "http://localhost:9200"
      }
    }
  }
}
```

> `cwd` penting agar Python bisa resolve import `financial_mcp` package.

## Tools Tersedia

| Tool | Deskripsi |
|------|-----------|
| `get_all_transactions` | 50 transaksi terbaru (sorted desc by `creationDate`) |
| `query_transactions` | Filter `type`/`category`/`source_name`/tanggal + optional `aggregation` (daily/weekly/monthly) |
| `get_spending_by_category` | Breakdown per kategori: total, %, jumlah tx, rata-rata |
| `get_financial_summary` | Total income, total expense, net, saldo terkini, top 10 kategori |

## Parameter `query_transactions`

| Param         | Tipe                        | Keterangan |
|---------------|-----------------------------|------------|
| `start_date`  | `YYYY-MM-DD`                | Filter tanggal awal (field `day`) |
| `end_date`    | `YYYY-MM-DD`                | Filter tanggal akhir (field `day`) |
| `type`        | `Expense` / `Income`        | Filter type transaksi |
| `category`    | string                      | Filter kategori, misal `"Food & Drink"` |
| `source_name` | string                      | Filter sumber, misal `"BNI"` |
| `aggregation` | `daily` / `weekly` / `monthly` | Jika diisi, return time-series; jika kosong return raw records |
| `limit`       | int (default 50)            | Maks. record saat tanpa aggregation |

## Prompts Terdaftar

- **`monthly_report`** (optional `month`: `YYYY-MM`) — instruksi untuk
  membuat laporan bulanan lengkap.
- **`spending_analysis`** (`start_date`, `end_date`) — analisis pola
  pengeluaran mingguan + breakdown kategori.
- **`cashflow_check`** (tanpa argumen) — cek kesehatan cashflow:
  overview + tren bulanan.

## Troubleshooting

1. **ES tidak terhubung** — pastikan Elasticsearch berjalan:
   ```bash
   curl http://localhost:9200/_cluster/health
   ```
2. **Tidak ada data** — jalankan `python -m financial_mcp.ingest`
3. **Import error saat menjalankan server** — pastikan venv aktif dan
   package terinstal: `pip install -e .`
4. **File CSV tidak ditemukan** — periksa path di `financial_mcp/config.py`;
   nama file mengandung dua spasi secara sengaja
5. **Nilai uang salah / tidak masuk akal** — bug `clean_currency`
   untuk format desimal Indonesia (`Rp 1.234,56`) belum di-fix
   (lihat `PROJECT_ANALYSIS.md` → F-02); gunakan data CSV dengan
   format bulat (`Rp 1.234.567`) sampai fix diterapkan

## Reset Data

```bash
curl -X DELETE http://localhost:9200/financial-transactions
python -m financial_mcp.ingest
```

## Security

- Setup lokal saja, jangan expose ke network publik
- Jika Elasticsearch butuh auth, isi `ES_API_KEY` di environment
- Pertimbangkan regulasi privasi data (UU PDP / GDPR jika relevan)

---

*Last updated: 2026-10-20*