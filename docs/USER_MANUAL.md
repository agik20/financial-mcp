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
│   ├── config.py                 # Alias name, index prefix, data paths
│   ├── es_client.py              # Client sync/async + query helper + 12-field canonical mapping
│   ├── converter.py              # CSV → JSON (pandas)
│   ├── ingest.py                 # Staging-index workflow: create → load → verify → alias switch
│   ├── models.py                 # Pydantic models (QueryTransactionParams)
│   ├── server.py                 # FastMCP entry point + prompts
│   └── tools/
│       ├── transactions.py       # get_all_transactions, query_transactions
│       └── analytics.py          # get_spending_by_category, get_financial_summary
├── tests/                        # 98 pytest tests (mocked ES, no live cluster)
├── docs/
│   └── USER_MANUAL.md
├── data/
│   ├── Personal Finance  - Transactions.csv    # Data menta (JANGAN di-push)
│   └── Personal Finance  - Transactions.json   # Hasil konversi
├── pyproject.toml
└── README.md
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

**Aman terhadap data live.** Ingest tidak pernah menghapus index yang sedang
aktif. Alur kerja:

1. **Preflight** — validasi seluruh JSON sebelum menyentuh Elasticsearch.
2. **Create staging index** — index fisik baru (`financial-transactions-YYYYMMDD-HHMMSS`) dibuat.
3. **Bulk load** — semua dokumen di-index ke staging; partial failure aborts.
4. **Verify** — count + spot-fetch + health check pada staging index.
5. **Atomic alias switch** — alias `financial-transactions-active` dipindah
   ke index baru dalam satu `update_aliases` call.
6. **Final verify** — alias target dan document count dikonfirmasikan.

Index fisik lama **tidak pernah dihapus otomatis** — operator bisa manual
rollback kapan saja.

Output sukses:

```text
OK  alias=financial-transactions-active staging=financial-transactions-20260101-120000-00 previous=None docs=106
```

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
| `get_all_transactions` | 50 transaksi terbaru (sorted desc by `transaction_date`) |
| `query_transactions` | Filter `type`/`category`/`source_account`/tanggal + optional `aggregation` (daily/weekly/monthly) |
| `get_spending_by_category` | Breakdown per kategori: total, %, jumlah tx, rata-rata |
| `get_financial_summary` | `total_income` (sum `signed_amount` Income), `total_expense` (sum `amount` Expense), `net_cash_flow` (= income − expense, Transfers dikecualikan), top 10 kategori, `total_documents` |

## Parameter `query_transactions`

| Param            | Tipe                        | Keterangan |
|------------------|-----------------------------|------------|
| `start_date`     | `YYYY-MM-DD`                | Filter tanggal awal (field `transaction_date`) |
| `end_date`       | `YYYY-MM-DD`                | Filter tanggal akhir (field `transaction_date`) |
| `type`           | `Expense` / `Income` / `Transfer` | Filter type transaksi |
| `category`       | string                      | Filter kategori, misal `"Food & Drink"` |
| `source_account` | string                      | Filter akun sumber, misal `"BNI"` |
| `source_name`    | string *(legacy)*           | Alias lama untuk `source_account`; akan digabung ke `source_account` secara otomatis |
| `aggregation`    | `daily` / `weekly` / `monthly` | Jika diisi, return time-series; jika kosong return raw records |
| `limit`          | int (default 50)            | Maks. record saat tanpa aggregation |

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

## Rollback After Failed Ingestion

Jika final verify gagal setelah alias switch berhasil, **kedua index
fisik dipertahankan** — tidak ada yang dihapus secara otomatis.

Gunakan `rollback_alias()` untuk memindahkan alias kembali ke index lama:

```python
from financial_mcp.ingest import rollback_alias
from financial_mcp.es_client import create_es_client

client = create_es_client()
rollback_alias(
    client=client,
    new_index="financial-transactions-20260101-120000-00",  # staging index baru
    previous_index="financial-transactions-20251201-080000-00",  # index lama
)
client.close()
```

> Rollback bersifat atomik (`update_aliases` dalam satu call). Kedua
> index fisik tetap ada; hapus staging index secara manual setelah
> mengonfirmasi data.

## Reset Data

Ingest workflow **tidak pernah** menghapus index lama secara otomatis.
Jika Anda memang ingin menghapus seluruh data dan memulai ulang:

```bash
# 1. Lihat alias saat ini
curl http://localhost:9200/_aliases?filter_path=*.aliases

# 2. (Opsional) Hapus index fisik lama secara manual
#    Ganti <index-name> dengan nama fisik dari langkah 1
curl -X DELETE "http://localhost:9200/<index-name>"

# 3. Jalankan ingest untuk membangun ulang
python -m financial_mcp.ingest
```

> **Catatan:** Index lama dipertahankan sengaja sehingga Anda bisa rollback
> kapan saja. Jangan hapus index yang masih ditargetkan alias
> `financial-transactions-active` tanpa backup.

## Security

- Setup lokal saja, jangan expose ke network publik
- Jika Elasticsearch butuh auth, isi `ES_API_KEY` di environment
- Pertimbangkan regulasi privasi data (UU PDP / GDPR jika relevan)

## Testing

Semua test berjalan dengan mocked Elasticsearch client — tidak perlu
cluster live.

```bash
# Jalankan semua test
pytest tests/ -v

# Jalankan test suite tertentu
pytest tests/test_ingest.py -v
pytest tests/test_transactions.py -v
pytest tests/test_es_client.py -v
```

## Running Type Checks

```bash
mypy financial_mcp/
```

---

*Last updated: 2026-11-18*

## Net Cash Flow — Konvensi

```
net_cash_flow = total_income − total_expense
```

Transfer (jenis `Transfer`) **dikecualikan** dari ketiga angka di atas.
Transfer adalah perpindahan antar akun milik sendiri — tidak mengubah
posisi cashflow keseluruhan.

| Tipe | `amount` | `signed_amount` | Termasuk dalam net? |
|---|---|---|---|
| `Income` | magnitude (≥ 0) | positif atau 0 | Ya (income) |
| `Expense` | magnitude (≥ 0) | negatif atau 0 | Ya (expense) |
| `Transfer` | magnitude (> 0) | selalu negatif | Tidak |