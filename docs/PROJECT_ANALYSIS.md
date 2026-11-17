# Financial-MCP — Full Project Analysis

> Generated: 2026-10-20
> Status: **Prototype — production-unready**

---

## Architecture Overview

```
data/ (CSV, personal finance export)
  │
  ▼  converter.py (pandas)
data/Personal Finance  - Transactions.json
  │
  ▼  ingest.py (elasticsearch.helpers.bulk)
Elasticsearch: financial-transactions
  │
  ▼  es_client.query_elasticsearch()
server.py (MCP tools: get_transactions, get_summary, search)
  │
  ▼  MCP client (Claude Code, etc.)
```

**Modules:** `config.py` → `es_client.py` → `converter.py` + `ingest.py` → `server.py`

---

## Findings by Severity

### 🔴 Critical (financial-correctness / active bug)

#### F-01 — `es_client.py:139` — `response.body` deprecated (Revised 2026-10-22: non-issue on es-py 8.x)

> **Status revision (2026-10-22):** Live-tested against the installed
> `elasticsearch-py 8.19.3`. `response.body` **still exists and returns
> the full response dict** on every elasticsearch-py 8.x release. The
> earlier claim that ".body was removed in ≥ 8.12" is **incorrect** — it
> is only *deprecated* on 8.x and scheduled for removal in **ES 9.x**.
> No `AttributeError` occurs; all query tools work normally.
>
> Verified at runtime:
> ```
> elasticsearch-py: 8.19.3
> search response type: elastic_transport.ObjectApiResponse
> hasattr(resp, "body"): True
> resp.body: dict -> ['took', 'timed_out', '_shards', 'hits']
> ```
>
> This becomes a **breaking change only after upgrading to elasticsearch-py 9.x**,
> at which point `.body` is removed and this line will crash.

`body=query` in `client.search()` is deprecated on 8.x; migrate to `**query`
keyword unpacking. `response.body` remains functional throughout 8.x.

```python
# Current (works on ES 8.x, will break on ES 9.x)
response = await client.search(index=ES_INDEX, body=query)
return response.body

# Forward-compatible fix (works on ES 8.x AND 9.x)
response = await client.search(index=ES_INDEX, **query)
# response IS a dict on ES 9.x (and .body also returns dict on 8.x)
return response.body if hasattr(response, "body") else response
```

**Impact on es-py 8.19.3:** None — deprecated only, all tools work.
**Impact on es-py 9.x:** 100% of query tools fail with AttributeError.
**Effort to make forward-compatible:** ~5 min fix.

---

#### F-02 — `converter.py:41` — `clean_currency` breaks IDR decimal format (Inferred)

`clean_currency` strips `","` globally:

```python
return int(str(value).replace("Rp","").replace(",","").strip())
```

Indonesian financial format uses `.` as thousands separator and `,` as
decimal separator: `"Rp 1.234,56"` → after `.replace(",","")` →
`"1.23456"` → `int("1.23456")` → **1,234,560** not 1,234.56.

If the CSV contains any decimal amounts (very common), the value is
inflated 1,000×. Even `int()` truncates decimals silently —
`"Rp 1.234.567,89"` (nested thousands) becomes garbage.

**Fix:**
```python
import re
def clean_currency(value: Any) -> Optional[int]:
    if pd.isna(value):
        return None
    s = str(value).replace("Rp", "").strip()
    # Indonesian: "1.234.567,89" → 1234567.89
    s = s.replace(".", "").replace(",", ".")
    try:
        return int(round(float(s)))
    except ValueError:
        return None
```

**Impact:** Any decimal financial values silently wrong by 3 orders of
magnitude.
**Effort:** ~10 min fix.

---

#### F-03 — `es_client.py:79-84` — MD5 doc-ID collision risk (Inferred)

Document ID = MD5(`creationDate + value + description`). Two legitimate
duplicate transactions (e.g., salary credit + matching debit, or two
transfers with same date, amount, and description) produce the same
ID → one silently overwrites the other in Elasticsearch.

**Impact:** Silent data loss — records vanish without error.
**Fix:** Add a hash-of-full-document or a UUID to the ID, or use ES's
`_id` auto-generation with a source-transaction-ID field if available.

**Effort:** ~15 min fix.

---

#### F-04 — `converter.py:92` — `hour` derived from system timezone, not data (Inferred)

```python
"hour": date.hour if date is not None else 0
```

`pd.to_datetime` on a bare `"YYYY-MM-DD HH:MM"` string (no timezone
suffix) returns a **naive** timestamp. `.hour` then depends on the
pandas/OS timezone context. If the CSV timestamp is already in UTC but
the system is WIB (UTC+7), the `hour` field is off by 7. Date-histogram
aggregations on `hour` will produce wrong bucket counts.

**Fix:** Parse with `tz=timedelta(hours=7)` explicitly, or extract
`hour` from the raw string before any timezone conversion.

**Effort:** ~5 min fix.

---

### 🟠 High

#### F-05 — `ingest.py:63` — `indices.create` deprecated `body` parameter (Verified)

```python
client.indices.create(index=ES_INDEX, body=INDEX_MAPPING)
```

In elasticsearch-py 8.x `body=` is deprecated in favour of
`mappings=`, `settings=` kwargs. Will trigger `DeprecationWarning`
and break on 9.x when `body` is removed entirely.

```python
# Fix
client.indices.create(
    index=ES_INDEX,
    mappings=INDEX_MAPPING["mappings"],
    settings=INDEX_MAPPING["settings"],
)
```

**Impact:** Warning now; hard break on ES 9.
**Effort:** 2 min.

---

#### F-06 — README.md & USER_MANUAL.md reference deleted files (Verified)

Both docs reference `financial_mcp.py`, `ingest-financial-data.py`, and
`data-converter.py` — none of which exist. The current structure is
`financial_mcp/` package with `es_client.py`, `ingest.py`,
`converter.py`, `server.py`.

**Impact:** Anyone following the docs will immediately hit
`ModuleNotFoundError` / "file not found."
**Effort:** ~20 min rewrite.

---

#### F-07 — `es_client.py:133` and `converter.py:131` — Raw financial data in stdout (Inferred)

`print(f"Sending query to Elasticsearch: {json.dumps(query)}")` logs
the full query (including transaction text). `print(f"Error processing
row: {row}")` logs raw row data. If stdout is captured by a process
manager or CI log, financial PII leaks into logs.

**Fix:** Use `logging` module with appropriate level; suppress or
redact sensitive fields in log output.

**Effort:** ~15 min.

---

### 🟡 Medium

#### F-08 — No `track_total_hits` in any query

Elasticsearch 7+ defaults to `track_total_hits: false` which caps
results at 10,000 for the `total` field. The `get_summary` tool's
`total_records` count will be wrong for any index > 10K docs.

**Fix:** Add `"track_total_hits": True` to the base query template.

---

#### F-09 — `by_category` terms aggregation has no `shard_size`

```python
"terms": {"field": "category", "size": 20}
```

Without `shard_size` the shard-level bucket count is limited to
`size`. For large datasets across multiple shards, some categories may
silently drop out. With `shards=1` (set in INDEX_MAPPING) this is
currently safe, but the code is fragile if shard count ever changes.

---

#### F-10 — `creationDate` date-mapping has no format hint

The ES mapping declares `creationDate: {type: "date"}` without
`format`. If the ISO string has an offset (`+07:00`) or lacks one, ES
may interpret differently on write vs read. Pin the format:
`{"type": "date", "format": "strict_date_optional_time||epoch_millis"}`.

---

### 🟢 Low

#### F-11 — `backup/` folder contains stale references

Gitignored but physically present on disk. Contains references to old
file names. Harmless in a git repo, confusing to new collaborators.
**Recommend:** Delete the folder from local disk; add a note in README
that it was a legacy artifact.

---

#### F-12 — No tests, no CI, no linter config, no LICENSE

Standard Python project hygiene: `pyproject.toml` has no
`[project.optional-dependencies]` for dev tools, no
`[tool.black]`/`[tool.ruff]` config. No test suite exists.
No LICENSE file. Not portfolio-ready without at least a smoke test
for `clean_currency` and `generate_doc_id`.

---

## Portfolio Assessment

**Strengths:**
- Correct MCP + Elasticsearch architecture (tool registration, async
  client, bulk ingest)
- Clean module separation: config → client → converter → ingest → server
- Good docstrings (NumPy-style, Args/Returns)
- Sensible use of `@asynccontextmanager` for async client lifecycle

**Blockers to fix before it's presentable:**
1. ~~**F-01**~~ — **revised 2026-10-22: not an active bug on es-py 8.x**;
   `response.body` still works. Only migrate to `**query` + `hasattr`
   guard when upgrading to ES 9.x.
2. **F-02** (10 min) — verify against your actual CSV values first;
   silently wrong by 1000× if decimal IDR amounts are present.
3. **F-03** — silent data loss via MD5 doc-ID collision for duplicate
   transactions; check dataset before deciding.
4. **F-06** (20 min) — docs reference deleted file names.
5. **F-12** — at minimum add 3-4 unit tests for `clean_currency`,
   `generate_doc_id`, and a basic `query_elasticsearch` mock test.

**Nice-to-have (if portfolio goes further):**
- F-05 (deprecated `body` param on `indices.create`), F-07
  (logging hygiene)
- A `Makefile` or `pyproject` scripts entry: `make convert`, `make ingest`
- CHANGELOG_FIXES.md for any verified fixes (per your global instructions)

**Overall:** Architecturally sound, **functioning** prototype. F-01 was
previously flagged as critical but live testing on es-py 8.19.3
confirmed `response.body` still works throughout 8.x. The remaining
critical items are **F-02** (data correctness, verify against your CSV)
and **F-03** (silent data loss on duplicate transactions).
