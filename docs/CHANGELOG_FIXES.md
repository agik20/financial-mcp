# CHANGELOG_FIXES.md — Financial-MCP

> Format: terbaru di atas. Hanya tulis setelah terverifikasi.

---

### Fix #1 — converter.py: `clean_currency` salah parsing angka Indonesia
> PENDING verifikasi

| | |
|---|---|
| **Tanggal** | 2026-10-20 (analisis, belum di-fix) |
| **File** | `financial_mcp/converter.py:39-44` |
| **Masalah** | `Rp 1.234,56` → `int("1.23456")` → 1,234,560 (bukan 1,234.56) |
| **Akar** | `replace(",","")` menghilangkan separator desimal; tidak ada handling `.` sebagai thousand separator |
| **Fix** | `s.replace(".","").replace(",",".")` lalu `int(round(float(s)))` |
| **Verifikasi** | PENDING — belum di-apply |
| **Pelajaran** | Saat parse angka lokal Indonesia: `.` = thousand, `,` = decimal |

---

### Fix #2 — es_client.py: `response.body` AttributeError pada elasticsearch-py >= 8.12
> PENDING verifikasi

| | |
|---|---|
| **Tanggal** | 2026-10-20 (analisis, belum di-fix) |
| **File** | `financial_mcp/es_client.py:138-139` |
| **Masalah** | Semua MCP tool gagal: `AttributeError: 'Response' object has no attribute 'body'` |
| **Akar** | `.body` dihapus/deprecated; async client 8.x return parsed dict langsung |
| **Fix** | Ganti `body=query` ke `**query`; return `response` bukan `response.body` |
| **Verifikasi** | PENDING — belum di-apply |
| **Pelajaran** | Selalu pin versi library; `.body` di sync client masih ada di 8.x |

---

### Fix #3 — ingest.py: `indices.create(body=...)` deprecated
> PENDING verifikasi

| | |
|---|---|
| **Tanggal** | 2026-10-20 (analisis, belum di-fix) |
| **File** | `financial_mcp/ingest.py:63` |
| **Masalah** | `DeprecationWarning` pada ES client 8.x; akan error total pada 9.x |
| **Akar** | `body=` parameter dihapus; gunakan `mappings=` dan `settings=` terpisah |
| **Fix** | `client.indices.create(index=ES_INDEX, mappings=..., settings=...)` |
| **Verifikasi** | PENDING |
| **Pelajaran** | Selalu cek changelog library saat upgrade mayor |
