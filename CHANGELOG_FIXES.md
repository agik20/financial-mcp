# Changelog Perbaikan (Verified Fixes)

Terbaru di atas, terlama di bawah.

### Fix #5 — Validasi `destination_account` non-Transfer: cabang `else` tak terjangkau

| | |
|---|---|
| Tanggal | 2026-11-20 |
| File | `financial_mcp/es_client.py` (`validate_transaction`), `tests/test_es_client.py` |
| Masalah | `test_non_transfer_with_destination_rejected` dan `test_income_with_destination_rejected` gagal: `validate_transaction` tidak menolak Expense/Income dengan `destination_account != null`. |
| Akar | Rule non-Transfer diletakkan di cabang `else:` dari `if/elif Income/Expense/Transfer`. Karena validasi sebelumnya sudah menjamin `transaction_type in CATEGORY_MAPPING[category]`, dan mapping itu hanya menghasilkan tiga tipe itu, cabang `else` tak pernah dieksekusi. Rule-nya ada di bytecode tapi tak pernah terpanggil. |
| Fix | Rule dipindah menjadi guard eksplisit sebelum cabang `if/elif`: `if transaction_type != "Transfer" and destination_account is not None: raise`. Cabang `else:` dihapus. |
| Verifikasi | `uv run pytest -q` → 517 passed, 0 failed (sebelumnya 515 passed, 2 failed). |
| Pelajaran | `else` di akhir `if/elif` chain hanya akan jalan jika tak ada satu pun kondisi terpenuhi. Jika precondition sudah menjamin salah satu kondisi selalu benar, `else` adalah dead code — guard eksplisit lebih aman. |
| Log Keyword | non-Transfer, destination_account, else, unreachable, dead code, CATEGORY_MAPPING, es_client |
| Deploy | belum |

### Fix #4 — `monthly_report` prompt: rentang tanggal salah + tanpa validasi

| | |
|---|---|
| Tanggal | 2026-11-20 |
| File | `financial_mcp/server.py` (prompt `monthly_report`), `tests/test_server.py` (baru) |
| Masalah | Prompt `monthly_report(month="2026-02")` menghasilkan `end_date='2026-02-31'` — tanggal tak valid; bulan 30-hari dapat `-31`, bulan 28-hari dapat `-31`. Input `"2026-13"` tak ditolak dan lolos ke prompt. |
| Akar | Batas akhir bulan di-hardcode `f"{month}-31"` alih-alih dihitung (`calendar.monthrange`); tidak ada validasi format `YYYY-MM`. |
| Fix | Regex `^\d{4}-(0[1-9]\|1[0-2])$` + `ValueError` untuk input tak valid; akhir bulan dihitung `calendar.monthrange(year, mon)[1]`. |
| Verifikasi | `pytest tests/test_server.py -q` → 7 passed (2026-02→28, 2024-02→29, 2026-04→30, 2026-12→31, `"2026-13"`→ValueError). Suite penuh: 359 passed, 112 skipped, 0 failed. |
| Pelajaran | String template dengan konstanta di tempat semantik kalender = bug; gunakan library stdlib (`calendar`) dan validasi input di batas. |
| Log Keyword | monthly_report, YYYY-MM, monthrange, hardcode 31, calendar, ValueError |
| Deploy | belum |

### Fix #3 — Koreksi Fix #2: `coerce_count` terpisah dari `coerce_monetary`

| Field | Nilai |
|---|---|
| Tanggal | 2026-11-20 |
| File | `financial_mcp/tools/money.py`, `financial_mcp/tools/analytics.py`, `financial_mcp/tools/transactions.py`, `tests/test_money.py`, `tests/test_analytics.py`, `tests/test_transactions.py`, `docs/USER_MANUAL.md` |
| Masalah | Fix #2 memakai satu validator (`coerce_monetary`) untuk SEMUA angka: nilai uang **dan** hitungan (`tx_count`, `categories.tx_count`). Batas `abs ≤ 2^53−1` itu benar untuk uang (float64 kehilangan presisi), tapi salah untuk hitungan — `value_count` ES selalu `long`, dan count bisa sah jauh di atas 2^53 tanpa masalah presisi. Akibatnya transaksi legal ber-volume besar ditolak dengan pesan "monetary value failed validation", padahal nilainya tidak bermasalah. |
| Akar | Satu kontrak dipakai untuk dua semantik berbeda. Fix #2 memperbaiki masalah `int()` mentah tapi mengganti guard-nya dengan guard uang, bukan guard hitungan. |
| Fix | `money.py`: tambah `coerce_count(value, field)` — terima `int ≥ 0` (tanpa batas 2^53) dan float integral non-negatif terbatas; tolak `bool`, negatif, pecahan, non-finit, `None`/`str`/`bytes`/`Decimal`. `analytics.py` + `transactions.py`: `tx_count` / `categories.tx_count` dialihkan ke `coerce_count`; `total_income`/`total_expense`/`grand_total`/`categories.total`/`top_categories.total` tetap di `coerce_monetary`. `get_financial_summary.total_documents` tidak dikoersi sama sekali (bukan output moneter, dibaca langsung dari `hits.total`). Docs: bedakan tabel kontrak moneter vs count. |
| Verifikasi | `uv run pytest -q` → 430 passed (naik dari 391). Test baru: 4 kelas `TestCoerceCount*` di `tests/test_money.py` (terima int besar 10^20/2^200, tolak negatif/pecahan/bool/non-finit/tipe tak didukung; kontrak exception tanpa bocor nilai); `test_negative_tx_count_rejected` + `test_tx_count_above_2_pow_53_passes` (count 2^53+1 diterima sementara `coerce_monetary` menolak float yang sama — inilah pemisahan semantik yang jadi alasan fix ini); 5 parametrisasi `test_get_spending_by_category_rejects_bad_tx_count`. Satu test lama (`test_rejects_bad_tx_count[2**53+1]`) sengaja diubah: nilai itu kini SAH untuk count, jadi tesnya pindah ke `test_tx_count_above_2_pow_53_passes`. `mypy financial_mcp/` → 1 error pre-existing (stub pandas di `converter.py`, tidak terkait). |
| Pelajaran | Jangan pakai satu validator untuk dua semantik hanya karena keduanya "angka". Batas presisi float64 relevan untuk uang; untuk hitungan, batas yang benar adalah "bilangan bulat non-negatif". Test lama yang memakai nilai `2**53+1` sebagai "contoh buruk" menjadi FAIL saat semantik diperbaiki — itu tanda test-nya yang salah, bukan kodenya. |
| Log Keyword | coerce_count, coerce_monetary, tx_count, value_count, 2**53, precision guard, semantic split, Fix #2 correction |
| Deploy | — (lokal saja; belum commit/push) |

### Fix #2 — F-003: Koersi moneter fail-closed + rata-rata presisi

| Field | Nilai |
|---|---|
| Tanggal | 2026-11-20 |
| File | `financial_mcp/tools/money.py` (baru), `financial_mcp/tools/analytics.py`, `financial_mcp/tools/transactions.py`, `tests/test_money.py` (baru), `tests/test_analytics.py`, `tests/test_transactions.py`, `docs/USER_MANUAL.md` |
| Masalah | Nilai moneter ES (sum/value_count) sampai sebagai float64 → diam-diam kehilangan presisi di atas 2^53−1; sub-agregasi `avg` menghasilkan rata-rata float yang tidak presisi dan tidak terdokumentasi; `tx_count` dipaksakan `int()` yang bisa memotong desimal secara diam-diam |
| Akar | Tidak ada lapisan koersi di jalur output; `avg_per_tx` dihitung di ES (`{"avg": ...}`) bukan di sisi klien; `tx_count` memakai `int()` mentah |
| Fix | Modul `money.py`: `coerce_monetary` (reject bool, desimal, non-finit, abs>2^53−1, tipe tak didukung; raise `MonetaryCoercionError` tanpa menyimpan/meloloskan nilai mentah), `half_even_div` (banker's rounding presisi penuh tanpa float), `monetary_error` (envelope JSON: nama field saja, tanpa nilai). `get_spending_by_category`: hilangkan sub-agg `avg`; `avg_per_tx` = `half_even_div(total, tx_count)` (0 bila `tx_count==0`); koersi `grand_total`/`categories.total`/`categories.tx_count`. `get_financial_summary`: koersi `total_income`/`total_expense`/`top_categories.total`; `total_documents` **tidak** dikoersi moneter (dibaca langsung dari `hits.total.value` — hitungan, bukan nilai uang; dikoreksi di Fix #3). `query_transactions` (mode agregasi): koersi `total_expense`/`total_income`/`tx_count` (sekarang int, bukan float64). Kesalahan koersi → `{"error": "monetary value failed validation: <field>"}` tanpa hasil parsial, tanpa log nilai mentah. `docs/USER_MANUAL.md`: kontrak moneter, rumus `avg_per_tx`, `tx_count` int, pesan error. |
| Verifikasi | `uv run pytest -q` → 391 passed (termasuk 29 test F-003 baru di `tests/test_money.py`: accept/reject `coerce_monetary`, kontrak `MonetaryCoercionError` (field saja, tanpa nilai), 6 contoh `half_even_div` (5/2→2, 7/2→4, 5/3→2, 8/3→3, 1/2→0, 3/2→2) + negative + boundary + reject den≤0/bool/non-int; 8 test `get_spending_by_category` avg-per-tx int; 5 test error envelope `get_financial_summary`; 12 test error envelope agregasi `query_transactions` + valid aggregation int). `git diff --check` bersih. Catatan: `2**53+1` sebagai **int Python** diterima (presisi penuh, arbitrary precision) — guard hanya menolak *float* di atas 2^53−1. |
| Pelajaran | Koersi tipe dan batasan presisi di satu modul kecil (`money.py`) memudahkan audit dan pengujian ulang; hindari `int()` mentah di output moneter — selalu validasi dulu; `avg` sebagai sub-agregasi ES tidak terdokumentasi sebagai presisi penuh, jadi hitung rata-rata di sisi klien dengan integer exact arithmetic |
| Log Keyword | F-003, coerce_monetary, MonetaryCoercionError, half_even_div, 2**53, avg_per_tx, banker's, tx_count, monetary_error, value leak, no partial result |
| Deploy | — (lokal saja; belum commit/push) |

### Fix #1 — F-005: Validasi batas `limit` pagination

| Field | Nilai |
|---|---|
| Tanggal | 2026-11-20 |
| File | `financial_mcp/models.py`, `financial_mcp/tools/transactions.py`, `docs/USER_MANUAL.md` |
| Masalah | `limit` tanpa batas: `limit=0` diam-diam jadi 50 (falsy di `params.limit or 50`); `limit=-1` lolos sebagai `size=-1` ke ES → error generik; `limit=1000000` lolos → ditolak ES di batas 10k dengan pesan tidak informatif |
| Akar | `limit: Optional[int] = 50` tanpa constraint `ge`/`le`, dan fallback truthiness `params.limit or 50` di `transactions.py:168` |
| Fix | Hardening: `limit: int = Field(default=50, ge=1, le=10000)` (field non-optional agar `None` ditolak, bukan lolos); fallback truthiness `params.limit or 50` diganti `params.limit` langsung; docstring model + tool + USER_MANUAL disesuaikan |
| Verifikasi | `uv run pytest -q` → 287 passed (termasuk 13 test F-005 baru: batas valid 1/50/10000, default 50, reject 0/-1/-100/10001/1000000/**None**/string/float; koersi string numerik `"50"`→50 didokumentasikan; limit eksplisit sampai ke `size` ES; `size` raw-mode selalu int, tidak pernah `None`; aggregation tetap `size=0`). `git diff --check` bersih. |
| Pelajaran | Fallback truthiness (`x or default`) di parameter pagination menyembunyikan nilai eksplisit yang tidak valid — gunakan constraint model di satu tempat, lalu konsumsi nilai tanpa fallback |
| Log Keyword | F-005, limit, ge=1, le=10000, size |
| Deploy | — (lokal saja; belum commit/push) |
