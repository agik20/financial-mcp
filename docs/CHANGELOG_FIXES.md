# CHANGELOG_FIXES.md — Financial-MCP

> Format: terbaru di atas. Hanya tulis setelah terverifikasi.

---

### Fix #19 — ingest.py: Postcondition failure di _atomic_alias_switch bisa hapus staging yang sedang dituju alias
> Terverifikasi — 135/135 passed (2026-11-18)

| | |
|---|---|
| **Tanggal** | 2026-11-18 |
| **File** | `financial_mcp/ingest.py` (`_atomic_alias_switch`, `run_ingest`, `SwitchOutcome`), `tests/test_ingest.py` |
| **Masalah** | Sebelum fix: `_atomic_alias_switch` raise `RuntimeError` jika postcondition gagal; `run_ingest` except block kemudian treat failure sebagai "before switch" dan hapus staging index. Padahal `update_aliases()` sudah sukses — alias mungkin sekarang target staging. Menghapus index yang sedang dituju alias = data hilang |
| **Akar** | `_atomic_alias_switch` hanya return `Optional[str]` (previous target) dan raise untuk semua failure. Tidak ada cara bagi `run_ingest` membedakan "update gagal sebelum diterapkan" dari "update sukses tapi verifikasi gagal" |
| **Fix** | (1) Tambah `SwitchOutcome` dataclass: `state` ∈ {"confirmed", "postcondition_failed", "not_attempted", "update_failed"} + `previous_active` + `detail`. (2) `_atomic_alias_switch` return `SwitchOutcome`, tidak lagi raise. (3) `run_ingest`: jika `state == "postcondition_failed"` → staging PRESERVED (alias state ambiguous, mungkin dituju alias); jika `state ∈ ("not_attempted", "update_failed")` → staging boleh dihapus (alias tidak mungkin target staging); jika `state == "confirmed"` → proceed ke Step 7. (4) Tambah 5 regression test di `TestSwitchOutcomePostconditionSafety` |
| **Verifikasi** | `pytest tests/test_ingest.py::TestSwitchOutcomePostconditionSafety -v` → 5 passed. Full suite 135/135 passed |
| **Pelajaran** | Ketika operasi multi-step (update → verify) bisa gagal di step terakhir setelah step sebelumnya sudah mutate state, caller harus tahu *di mana* operasi berhenti. Return structured state, bukan hanya raise/None. Postcondition failure ≠ update failure — cleanup strategy berbeda |
| **Log Keyword** | `SwitchOutcome postcondition_failed _atomic_alias_switch run_ingest staging preserve ambiguous alias state` |
| **Deploy** | Belum — local only, belum commit |

---

### Fix #20 — tests: 21 async test skipped karena pytest-asyncio belum terpasang
> Terverifikasi — 135/135 passed (2026-11-18)

| | |
|---|---|
| **Tanggal** | 2026-11-18 |
| **File** | `pyproject.toml`, `uv.lock` |
| **Masalah** | 21 `@pytest.mark.asyncio` test di `test_analytics.py` dan `test_transactions.py` selalu skipped karena `pytest-asyncio` tidak ada di dependency. Angka "107 passed" di CHANGELOG sebelumnya tidak jujur: 21 test tidak pernah dieksekusi |
| **Akar** | `pyproject.toml` tidak punya `[dependency-groups] dev` dan tidak ada pytest config untuk asyncio |
| **Fix** | Tambah `[dependency-groups] dev = ["pytest>=8.0.0", "pytest-asyncio>=0.23.0"]` dan `[tool.pytest.ini_options] asyncio_mode = "auto"` ke `pyproject.toml` |
| **Verifikasi** | `uv run python -m pytest tests/test_analytics.py tests/test_transactions.py -v` → 21 passed, 0 skipped. Full suite: 135 passed, 0 skipped |
| **Pelajaran** | Angka "N passed" di CHANGELOG harus selalu disertai "M skipped" jika ada skip. Async test yang skip = tidak terverifikasi |
| **Log Keyword** | `pytest-asyncio asyncio_mode auto dependency-groups dev pyproject.toml` |
| **Deploy** | Belum — local only, belum commit |

---

### Fix #21 — tests: Cross-process lock serialization belum teruji secara nyata
> Terverifikasi — 135/135 passed (2026-11-18)

| | |
|---|---|
| **Tanggal** | 2026-11-18 |
| **File** | `tests/test_ingest.py` (`TestCrossProcessLockSerialization`) |
| **Masalah** | `test_competing_transitions_cannot_both_use_stale_state` simulasi race dengan patch in-thread (mutate alias mock di antara update dan postcondition). Ini TIDAK membuktikan dua proses OS yang kooperatif benar-benar serialize pada file lock — thread tidak punya fd terpisah, jadi `msvcrt.locking`/`fcntl.flock` tidak menguji apa-apa di level OS |
| **Akar** | Tidak ada test yang menjalankan `_alias_transition_lock` di dua proses terpisah secara nyata |
| **Fix** | Tambah `TestCrossProcessLockSerialization` dengan 2 test: (1) `test_two_processes_serialize_on_alias_lock` — 2 `multiprocessing.Process` worker, barrier sync, masing-masing baca alias target dari file JSON shared lalu update; assert hanya 1 worker lihat "stg-initial", yang lain lihat target winner. (2) `test_lock_prevents_concurrent_stale_reads` — assert dua worker tidak pernah lihat 'previous' yang sama (indikasi lock tidak serialize). Dokumentasi: test ini validasi same-host only, bukan multi-host |
| **Verifikasi** | `pytest tests/test_ingest.py::TestCrossProcessLockSerialization -v` → 2 passed. Full suite 135/135 passed |
| **Pelajaran** | Lock cross-process harus diuji dengan proses terpisah (`multiprocessing.Process`), bukan thread. `msvcrt.locking` dan `fcntl.flock` keduanya per-fd (per-process), thread share fd table sehingga thread-based test tidak menguji apa-apa |
| **Log Keyword** | `multiprocessing Process barrier msvcrt.flock fcntl.flock cross-process serialization test two_workers alias_state` |
| **Deploy** | Belum — local only, belum commit |

---

### Fix #16 — ingest.py: CAT indices JSON response diparse sebagai dict, seharusnya list
> Terverifikasi — 107 passed, 21 skipped (skipped = async tests, pytest-asyncio belum terpasang) (2026-11-18); sekarang 135/135 passed setelah P3

| | |
|---|---|
| **Tanggal** | 2026-11-18 |
| **File** | `financial_mcp/ingest.py` (`_verify_staging`), `tests/test_ingest.py` |
| **Masalah** | `_verify_staging` mengasumsikan `client.cat.indices(index=staging, format="json")` return dict keyed by index name dengan field `state`. Padahal ES 8.x CAT indices API dengan `format="json"` return **list of objects** dengan field `index` (nama) dan `status` (`open`/`close`) — bukan dict, bukan `state` |
| **Akar** | Kesalahan asumsi schema: dokumentasi CAT API tidak eksplisit di codebase; mock awal berdasarkan tebakan bentuk response |
| **Fix** | Ganti parsing dari dict-keyed ke list-of-objects: (1) cek `isinstance(cat_body, list)` — raise jika bukan. (2) Iterasi list, reject entry non-dict (malformed). (3) Match dengan `entry.get("index") == staging`. (4) Reject jika 0 atau >1 match. (5) Baca `entry.get("status")` — reject jika bukan string non-kosong. (6) Reject jika `status != "open"`. Semua fail-closed |
| **Verifikasi** | `pytest tests/test_ingest.py::TestVerifyStagingStateFailClosed -v` → 9 passed (open, close, missing status, null status, empty list, duplicate, non-list, malformed entry, empty string). Full suite 107/107 passed |
| **Pelajaran** | CAT API `format="json"` return JSON array, bukan object. Field status bernama `status` (bukan `state`). Selalu verifikasi bentuk response terhadap dokumentasi resmi, jangan asumsikan dari mock |
| **Log Keyword** | `cat.indices format=json list status open close _verify_staging malformed entry duplicate` |
| **Deploy** | Belum — local only, belum commit |

---

### Fix #17 — ingest.py: Lock alias switch tidak mencakup seluruh check-then-update
> Terverifikasi — 107 passed, 21 skipped (skipped = async tests, pytest-asyncio belum terpasang) (2026-11-18); sekarang 135/135 passed setelah P3

| | |
|---|---|
| **Tanggal** | 2026-11-18 |
| **File** | `financial_mcp/ingest.py` (`_atomic_alias_switch`, `run_ingest`), `tests/test_ingest.py` |
| **Masalah** | `_atomic_alias_switch` hanya mengunci `update_aliases()` — bukan seluruh sequence check-then-update. `run_ingest` membaca `previous = _resolve_alias_target(client)` **sebelum** lock, lalu `_atomic_alias_switch` memanggil lock hanya sekitar `update_aliases`. Race window: antara read alias (Step 2) dan update alias (Step 6), alias bisa berubah |
| **Akar** | Lock scope terlalu sempit — hanya membungkus satu API call, bukan seluruh critical section |
| **Fix** | Restructure `_atomic_alias_switch`: (1) Akuisisi lock **sebelum** read alias target. (2) Read `_resolve_alias_target` under lock. (3) Build actions. (4) Call `update_aliases` under lock. (5) Postcondition check `_resolve_alias_target` under lock — raise jika target != staging. (6) Release lock. `run_ingest` tidak lagi membaca alias target di Step 2; `result.previous_active` di-set dari return value `_atomic_alias_switch`. `rollback_alias` sudah benar (semua precondition + update + postcondition sudah dalam satu `with` block). Tambah test `test_competing_transitions_cannot_both_use_stale_state` yang mendemonstrasikan race: alias berubah antara update dan postcondition → RuntimeError |
| **Verifikasi** | `pytest tests/test_ingest.py::TestAliasTransitionLock -v` → 5 passed. Full suite 107/107 passed |
| **Pelajaran** | Lock harus mencakup seluruh check-then-update sequence, bukan hanya update call. Read alias target yang dilakukan sebelum lock adalah stale dan berbahaya. Document scope lock: hanya multi-process host-same, bukan multi-host |
| **Log Keyword** | `_atomic_alias_switch _alias_transition_lock _resolve_alias_target previous_active stale race postcondition check-then-update` |
| **Deploy** | Belum — local only, belum commit |

---

### Fix #18 — README: deskripsi signed_amount/net_cash_flow menyesatkan
> Terverifikasi — 107 passed, 21 skipped (skipped = async tests, pytest-asyncio belum terpasang) (2026-11-18); sekarang 135/135 passed setelah P3

| | |
|---|---|
| **Tanggal** | 2026-11-18 |
| **File** | `README.md` |
| **Masalah** | README menyatakan "`signed_amount` is used for all net cash-flow calculations" — menyesatkan karena `signed_amount` hanya di-sum untuk Income docs (dengan filter `type=Income`); Expense docs memakai `amount`; Transfer docs dikecualikan |
| **Akar** | Deskripsi awal tidak membedakan filter per-type |
| **Fix** | Perbarai paragraf: `signed_amount` untuk Income totals; `amount` untuk Expense/Transfer magnitudes; `net_cash_flow = total_income − total_expense` dengan Transfer excluded entirely |
| **Verifikasi** | Manual review. `grep -n "signed_amount" README.md` — tidak ada klaim "all net cash-flow" lagi. Full suite 107/107 passed |
| **Pelajaran** | Deskripsi schema di README harus konsisten dengan query ES yang sebenarnya dijalankan |
| **Log Keyword** | `README signed_amount net_cash_flow total_income total_expense Transfer excluded` |
| **Deploy** | Belum — local only, belum commit |

---

### Fix #13 — ingest.py: BadRequestError penanganan terlalu luas di _create_staging_index
> Terverifikasi — 104 passed, 21 skipped (skipped = async tests, pytest-asyncio belum terpasang) (2026-11-18); sekarang 135/135 passed setelah P3

| | |
|---|---|
| **Tanggal** | 2026-11-18 |
| **File** | `financial_mcp/ingest.py` (`_is_resource_already_exists`, `_create_staging_index`), `tests/test_ingest.py` |
| **Masalah** | `_create_staging_index` menangkap semua `es_exceptions.BadRequestError` dan return `False` (diperlakukan sebagai "index sudah ada / concurrent run"). Padahal HTTP 400 juga mencakup invalid mapping, malformed request, dan kegagalan lain yang bukan ownership conflict |
| **Akar** | Tidak ada inspeksi terhadap `exc.body["error"]["type"]` — semua 400 dianggap sama |
| **Fix** | Tambah helper `_is_resource_already_exists(exc)` yang memeriksa `exc.body["error"]["type"] == "resource_already_exists_exception"`. Return `True` hanya untuk error type persis itu. Jika `False`, exception re-raised sehingga kegagalan genuine propagates ke caller. Fails closed: body tanpa field `error` → return `False` → exception propagates |
| **Verifikasi** | `pytest tests/test_ingest.py::TestStagingIndexBadRequestPrecision -v` → 4 passed. Full suite 104/104 passed |
| **Pelajaran** | ES 8.x mem-wrap semua HTTP 400 sebagai `BadRequestError`. Untuk membedakan ownership conflict dari genuine failure, inspeksi `error.type` dari structured response body — jangan string-match `reason` |
| **Log Keyword** | `BadRequestError resource_already_exists_exception _is_resource_already_exists mapper_parsing_exception illegal_argument_exception` |
| **Deploy** | Belum — local only, belum commit |

---

### Fix #14 — ingest.py: _verify_staging state check tidak fail-closed
> Terverifikasi — 104 passed, 21 skipped (skipped = async tests, pytest-asyncio belum terpasang) (2026-11-18); sekarang 135/135 passed setelah P3

| | |
|---|---|
| **Tanggal** | 2026-11-18 |
| **File** | `financial_mcp/ingest.py` (`_verify_staging`), `tests/test_ingest.py` |
| **Masalah** | `_verify_staging` punya fallback: ketika `cat.indices` return unexpected type (bukan dict), kode set `state = "open"` sebagai default. Unknown/parsable response di-treat sebagai verified open index — kegagalan yang justru ditolak oleh review |
| **Akar** | Tidak ada explicit type check terhadap response body `cat.indices`; unknown shape di-handle dengan default "open" |
| **Fix** | `cat.indices` dipanggil dengan `format="json"` eksplisit. Response extraction: `cat_body = getattr(cat_resp, "body", cat_resp)` lalu: (1) jika `cat_body` bukan dict → raise `RuntimeError("unexpected cat.indices response type")`. (2) jika key staging tidak ada → raise `RuntimeError("no entry for staging index")`. (3) jika entry bukan dict → raise. (4) jika `state` bukan string non-kosong → raise. (5) jika `state != "open"` → raise dengan nilai aktual. Fallback "default open" dihapus total |
| **Verifikasi** | `pytest tests/test_ingest.py::TestVerifyStagingStateFailClosed -v` → 7 passed (open, close, missing state, null state, missing index, non-dict, empty string). Full suite 104/104 passed |
| **Pelajaran** | Fail-closed: semua bentuk response yang tidak ter-parse harus raise, bukan default ke nilai aman |
| **Log Keyword** | `cat.indices format=json state open close _verify_staging fail-closed unexpected response type` |
| **Deploy** | Belum — local only, belum commit |

---

### Fix #15 — ingest.py: Race window pada alias switch dan rollback
> Terverifikasi — 104 passed, 21 skipped (skipped = async tests, pytest-asyncio belum terpasang) (2026-11-18); sekarang 135/135 passed setelah P3

| | |
|---|---|
| **Tanggal** | 2026-11-18 |
| **File** | `financial_mcp/ingest.py` (`_alias_transition_lock`, `_atomic_alias_switch`, `rollback_alias`), `tests/test_ingest.py` |
| **Masalah** | Race window antara precondition check (`_resolve_alias_target`) dan `update_aliases` di `rollback_alias`, serta antara `_atomic_alias_switch` dan post-check di `run_ingest`. Dua proses concurrent bisa keduanya sukses dalam window check-then-update, menghasilkan alias yang tidak sesuai ekspektasi |
| **Akar** | Tidak ada mekanisme serialization cross-process; `update_aliases` atomik hanya dalam satu API call, bukan dalam check-then-update sequence |
| **Fix** | Tambah `_alias_transition_lock()` — context manager berbasis file lock (`msvcrt.locking` di Windows, `fcntl.flock` di POSIX) di `tempfile.gettempdir()/financial-mcp/alias.lock`. Lock diterapkan di: (1) `_atomic_alias_switch` — membungkus seluruh `update_aliases` call. (2) `rollback_alias` — membungkus precondition check 3–5 + `update_aliases` + postcondition check dalam satu `with` block. Lock file advisory: proses yang sengaja bypass lock tidak dilindungi (operasional failure mode, bukan code failure mode) |
| **Verifikasi** | `pytest tests/test_ingest.py::TestAliasTransitionLock -v` → 4 passed (lock acquire/release, rollback calls lock, atomic switch calls lock, lock file created). Full suite 104/104 passed |
| **Pelajaran** | `update_aliases` atomik hanya dalam satu API call; check-then-update butuh explicit lock di level proses. Postcondition tetap ada sebagai defense-in-depth — lock eliminates race window, postcondition detects if lock di-bypass |
| **Log Keyword** | `_alias_transition_lock alias.lock msvcrt.flock fcntl.flock rollback_alias _atomic_alias_switch cross-process race` |
| **Deploy** | Belum — local only, belum commit |

---

### Fix #10 — ingest.py: Staging index ownership tidak ter-track di failure path
> Terverifikasi — 107/107 test lulus (2026-11-18)

| | |
|---|---|
| **Tanggal** | 2026-11-18 |
| **File** | `financial_mcp/ingest.py` (`_create_staging_index`, `run_ingest`), `tests/test_ingest.py` |
| **Masalah** | `run_ingest` memanggil `_safe_delete(client, staging, is_own_staging=True)` secara unconditional. Jika racing condition membuat staging index sudah ada saat probe, `is_own_staging=True` tetap dipasangkan dengan index milik proses lain — proses ini bisa menghapus staging index milik concurrent run |
| **Akar** | `_create_staging_index` hanya raise jika index sudah ada saat probe. Tidak ada flag yang mencatat apakah proses ini berhasil membuat index (setelah `indices.create` sukses) vs index sudah ada. `run_ingest` tidak bisa membedakan dua kasus |
| **Fix** | (1) `_create_staging_index` return `bool` — `True` hanya jika `indices.create` sukses. Race `ResourceAlreadyExistsError` (ES 8.x: `BadRequestError`) ditangani di except block, return `False`. (2) `run_ingest` set `owns_staging = _create_staging_index(...)`. Jika `False`, abort dengan error "concurrent run detected". (3) Failure path di `run_ingest` hanya panggil `_safe_delete` jika `owns_staging is True`. (4) Tambah 2 test: `test_concurrent_run_staging_not_touched` (unit `_create_staging_index`), `test_run_ingest_race_not_owned_does_not_delete` (full `run_ingest` race path) |
| **Verifikasi** | `pytest tests/test_ingest.py::TestStagingCleanupOwnership -v` → 3 passed. Full suite 107/107 passed |
| **Pelajaran** | Ownership harus di-track sejak creation, bukan diasumsikan di cleanup. `is_own_staging=True` tidak boleh dipasangkan secara unconditional |
| **Log Keyword** | `ingest owns_staging _create_staging_index concurrent run _safe_delete ownership` |
| **Deploy** | Belum — local only, belum commit |

---

### Fix #11 — ingest.py: _verify_staging tidak memeriksa index open state
> Terverifikasi — 107/107 test lulus (2026-11-18)

| | |
|---|---|
| **Tanggal** | 2026-11-18 |
| **File** | `financial_mcp/ingest.py` (`_verify_staging`), `tests/test_ingest.py` |
| **Masalah** | `_verify_staging` hanya pakai `indices.get` untuk cek existence. Di ES 8.x, `indices.get` return metadata untuk index open DAN closed. Closed index lolos check existence lalu gagal di `count` atau `search` dengan error yang membingungkan |
| **Akar** | Tidak ada check terhadap field `state` index. API `cat.indices` di ES 8.x mem-expose `state` field (`open` / `close`) per index, tetapi tidak ada di `indices.get` response |
| **Fix** | Tambah check `client.cat.indices(index=staging)` sebelum count. Response dict dengan key index name dan field `state`. Jika `state != 'open'`, raise `RuntimeError` dengan pesan "refusing to activate a closed index". Update `_make_full_fake` dan `MockElasticsearch` dengan `cat.indices` mock. Tambah 4 test: `test_closed_index_rejected`, `test_missing_index_rejected`, `test_yellow_status_accepted`, `test_open_state_passes` |
| **Verifikasi** | `pytest tests/test_ingest.py::TestStagingHealthVerificationFailure -v` → 6 passed |
| **Pelajaran** | Di ES 8.x, closed index tidak otomatis ditolak oleh `indices.get`. State harus dicek via `cat.indices` atau `indices.stats` |
| **Log Keyword** | `ingest cat.indices state open close _verify_staging closed index` |
| **Deploy** | Belum — local only, belum commit |

---

### Fix #12 — ingest.py: rollback_alias tanpa precondition check
> Terverifikasi — 107/107 test lulus (2026-11-18)

| | |
|---|---|
| **Tanggal** | 2026-11-18 |
| **File** | `financial_mcp/ingest.py` (`rollback_alias`), `tests/test_ingest.py` |
| **Masalah** | `rollback_alias` langsung jalankan `update_aliases` tanpa memverifikasi: (1) new_index ≠ previous_index, (2) kedua index masih ada di cluster, (3) alias saat ini benar-benar target new_index. Jika state alias tidak seperti yang diharapkan, rollback bisa melakukan hal yang tidak sesuai ekspektasi |
| **Akar** | Tidak ada precondition check; fungsi hanya melakukan alias move dan log |
| **Fix** | Tambah 4 precondition check sebelum `update_aliases`: (1) `previous_index is not None` (ada sesuatu untuk di-restore), (2) `new_index != previous_index` (bukan no-op), (3) kedua index ada di cluster (`_existing_index_names`), (4) alias saat ini target `new_index` (`_resolve_alias_target`). Postcondition: setelah `update_aliases`, verifikasi alias sekarang target `previous_index`. Tambah 4 test: `test_rollback_same_index_rejected`, `test_rollback_new_index_missing_rejected`, `test_rollback_previous_index_missing_rejected`, `test_rollback_wrong_alias_target_rejected` |
| **Verifikasi** | `pytest tests/test_ingest.py::TestRollbackAlias -v` → 7 passed |
| **Pelajaran** | Operator-facing function yang mengubah state cluster harus selalu verifikasi preconditions dan report unexpected state, bukan melanjutkan dalam kondisi tidak diketahui |
| **Log Keyword** | `ingest rollback_alias precondition new_index previous_index alias target update_aliases` |
| **Deploy** | Belum — local only, belum commit |

---

### Fix #5 — tools/analytics.py: Net cash flow menyertakan Transfer (regresi)
> Terverifikasi — 98/98 test lulus (2026-11-18)

| | |
|---|---|
| **Tanggal** | 2026-11-18 |
| **File** | `financial_mcp/tools/analytics.py`, `financial_mcp/tools/transactions.py`, `tests/test_analytics.py`, `tests/test_transactions.py` |
| **Masalah** | `net_cash_flow` di `get_financial_summary` dihitung sebagai sum `signed_amount` semua dokumen — Transfer (selalu negatif) mengurangi nilai net. Data contract: `Net = Income − Expense`, Transfer dikecualikan |
| **Akar** | `net_cash_flow` dihitung sebagai sub-aggregation `sum(signed_amount)` di ES query; untuk Transfer `signed_amount` selalu negatif sehingga net selalu terdistorsi |
| **Fix** | `net_cash_flow` dihitung client-side: `total_income - total_expense` (sub-agg dihapus dari query ES). `top_categories` di-filter `must_not: Transfer`. Di `query_transactions` time-series, `net` juga dihitung client-side dari `total_income - total_expense` per bucket |
| **Verifikasi** | `pytest tests/ -v` → 98 passed. Regresi: Income 10,000,000 + Expense 500,000 + Transfer −2,000,000 → net = 9,500,000 (bukan 7,500,000) |
| **Pelajaran** | Transfer adalah gerakan antar-akun pribadi; tidak boleh memengaruhi angka income/expense/net. Selalu hitung net client-side, bukan sebagai ES sub-agg |
| **Log Keyword** | `net_cash_flow transfer excluded analytics total_income total_expense` |
| **Deploy** | Belum — local only, belum commit |

---

### Fix #6 — ingest.py: Staging health verification tidak memeriksa cluster health
> Terverifikasi — 98/98 test lulus (2026-11-18)

| | |
|---|---|
| **Tanggal** | 2026-11-18 |
| **File** | `financial_mcp/ingest.py`, `tests/test_ingest.py` |
| **Masalah** | `_verify_staging` hanya memeriksa count + spot-fetch; tidak memeriksa cluster health (unassigned_shards, status). Index dengan shard tidak sehat bisa diaktifkan |
| **Akar** | `indices.stats()` tidak mengembalikan `state` atau `unassigned_shards` dalam respons — perlu API `cluster.health` |
| **Fix** | Tambah langkah 4 di `_verify_staging`: panggil `client.cluster.health(index=staging)`, tolak jika `unassigned_shards != 0` atau `status` bukan `green`/`yellow` |
| **Verifikasi** | `pytest tests/test_ingest.py::TestStagingHealthVerificationFailure -v` → 2 passed |
| **Pelajaran** | Health check harus pakai `cluster.health`, bukan `indices.stats` — response body-nya berbeda |
| **Log Keyword** | `ingest cluster.health unassigned_shards staging verify_staging` |
| **Deploy** | Belum — local only, belum commit |

---

### Fix #7 — ingest.py: Tidak ada rollback alias setelah switch berhasil tapi verify gagal
> Terverifikasi — 98/98 test lulus (2026-11-18)

| | |
|---|---|
| **Tanggal** | 2026-11-18 |
| **File** | `financial_mcp/ingest.py`, `tests/test_ingest.py` |
| **Masalah** | Jika alias switch berhasil tapi `_final_verify` gagal, tidak ada mekanisme untuk operator mengembalikan alias ke index lama; error message tidak menyebut target alias saat ini |
| **Akar** | Tidak ada fungsi `rollback_alias`; error path setelah switch tidak melaporkan state alias |
| **Fix** | Tambah `rollback_alias(client, new_index, previous_index)` — atomik via `update_aliases` (remove dari new, add ke previous). Error path setelah switch: report target alias aktual, jelaskan bahwa kedua index dipertahankan, dan tunjuk `rollback_alias` sebagai cara recover. `previous_index_kept` di-set `True` pada path switch-succeeded |
| **Verifikasi** | `pytest tests/test_ingest.py::TestRollbackAlias -v` → 3 passed |
| **Pelajaran** | Setelah atomic switch, tidak boleh ada auto-delete — operator yang memutuskan kapan hapus. Rollback juga harus atomik (satu `update_aliases` call) |
| **Log Keyword** | `ingest rollback_alias update_aliases post-switch error path` |
| **Deploy** | Belum — local only, belum commit |

---

### Fix #4 — es_client.py: Implementasi lengkap integrity gate + test suite 47 test
> Terverifikasi — 47/47 test lulus (2026-11-18)

| | |
|---|---|
| **Tanggal** | 2026-11-18 |
| **File** | `financial_mcp/es_client.py` (rewrite penuh), `tests/test_es_client.py` (baru) |
| **Masalah** | es_client.py belum di-approve: struktur section tidak sesuai spec, `pay_cycle=None` crash di `validate_date`, tidak ada test suite |
| **Akar** | `validate_transaction` memanggil `validate_date(doc["pay_cycle"], ...)` tanpa guard terhadap `None` |
| **Fix** | (1) Reorganisasi section sesuai spec user: `CATEGORY_MAPPING` → `PARENT_CATEGORY_MAP` → `REQUIRED_FIELDS` → `generate_doc_id` → `validate_date` → `validate_transaction` → clients → `generate_actions` → `query_elasticsearch`. (2) Tambah guard: `if doc["pay_cycle"] is not None: validate_date(...)`. (3) Buat `tests/test_es_client.py` — 47 test covering: `generate_doc_id` (9), `validate_date` (5), `validate_transaction` (27), `generate_actions` (6), `TestMappings` (3) |
| **Verifikasi** | `pytest tests/test_es_client.py -v` → 47 passed in 2.52s |
| **Pelajaran** | Field nullable wajib di-guard sebelum dipanggil validator; `pay_cycle` nullable per spec data contract §4.2 |
| **Log Keyword** | `es_client.py integrity gate pay_cycle None validate_date` |
| **Deploy** | Belum — local only, belum commit |

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
