# Financial MCP — Data Contract & Converter Specification

- **Document status:** Draft — ready for implementation review
- **Version:** 1.0
- **Decision:** CSV is the single source of truth; JSON is a generated artifact
- **Scope:** Source CSV validation, normalization, JSON serialization, and converter acceptance criteria
- **Repository:** `agik20/financial-mcp`

> This specification is based on the confirmed 11-column transaction contract, the five sample rows supplied in the project conversation, and a read-only review of the current `converter.py`, `config.py`, `ingest.py`, README, and user manual. It is not a runtime audit of the complete private dataset. Full-dataset validation remains pending.

---

## 1. Architectural decision

### 1.1 Source of truth

The authoritative data flow is:

```text
Authoritative CSV
      |
      v
Strict validation + normalization
      |
      v
Canonical JSON (generated; never manually maintained)
      |
      v
Elasticsearch ingestion
      |
      v
MCP tools and analytics
```

Rules:

1. The CSV is the only authoritative transaction source.
2. JSON is disposable and reproducible. If it is deleted, it must be possible to regenerate it from the CSV without losing business information.
3. Manual edits to generated JSON are unsupported. Any correction must be made in the CSV and followed by conversion.
4. Elasticsearch is a serving/index layer, not the source of truth.
5. Converter failure must not replace a previously valid JSON output.

### 1.2 Active filenames

The current `config.py` already points to these exact names:

- CSV: `data/Financ - Transactions.csv` (one space before the hyphen)
- JSON: `data/Financ  - Transactions.json` (two spaces before the hyphen)

Do not rename these paths as part of the converter implementation unless a separate migration is approved. The current converter module docstring and documentation still contain stale `Personal Finance` filenames and must be corrected.

The repository `.gitignore` excludes `data/`. Preserve that protection. Personal transaction files must not be committed.

---

## 2. Source CSV contract

### 2.1 Exact header and order

The CSV must contain exactly these 11 columns, in this order:

| # | Source column | Canonical JSON field | Required | Meaning |
|---:|---|---|---|---|
| 1 | `ID` | `transaction_id` | Yes | Stable source transaction identifier |
| 2 | `Date` | `transaction_date` | Yes | Transaction date |
| 3 | `Category` | `category` | Yes | Leaf category |
| 4 | `Type` | `type` | Yes | `Income`, `Expense`, or `Transfer` |
| 5 | `Account` | `source_account` | Yes | Account from which the transaction is recorded |
| 6 | `Destination Account` | `destination_account` | Conditional | Destination account for transfers; blank otherwise |
| 7 | `Amount` | `amount` | Yes | Non-negative transaction magnitude in IDR |
| 8 | `Detail Transaction` | `description` | No | Human-readable transaction description |
| 9 | `Pay Cycle` | `pay_cycle` | No | Pay-cycle date |
| 10 | `Signed Amount` | `signed_amount` | Yes | Signed amount from the perspective of `Account` |
| 11 | `Parent` | `parent_category` | Yes | Parent category |

Header matching is exact and case-sensitive. Missing, extra, duplicated, renamed, or reordered columns are validation errors. Do not silently infer a schema from partial headers.

### 2.2 Text normalization

- Decode CSV as UTF-8, accepting an optional UTF-8 BOM (`utf-8-sig`).
- Strip leading/trailing whitespace from identifiers, category names, type names, account names, and descriptions.
- Preserve the original case and internal whitespace of descriptive text and account names.
- Empty `Destination Account`, `Detail Transaction`, and `Pay Cycle` cells become JSON `null`.
- Required text fields that are empty after trimming are invalid.
- Do not silently map unknown category/type spellings to a known value.

### 2.3 Date contract

- Source dates are interpreted as month/day/year: `%m/%d/%Y`.
- Canonical output is ISO date: `YYYY-MM-DD`.
- `Date` is required and must be a real calendar date.
- `Pay Cycle` is optional; when present it follows the same source format and validation.
- Do not use pandas date inference or guess between day/month orderings.
- If Excel serial dates or other non-string date types are encountered in a future Excel import path, that path requires its own explicit adapter and tests. It must not weaken CSV parsing rules.
- The source contract is date-only. Do not fabricate transaction time, hour, or a timestamp at midnight as if the source supplied a time.

### 2.4 Currency and amount contract

Currency is fixed to `IDR`. Canonical amounts are integer rupiah units; no floating-point arithmetic is permitted.

Accepted examples:

| Input | Parsed integer |
|---|---:|
| `Rp588,000` | `588000` |
| `Rp 588,000` | `588000` |
| `Rp1.002.500` | `1002500` |
| `1000000` | `1000000` |
| `0` | `0` |
| `-Rp588,000` (Signed Amount only) | `-588000` |

Parsing rules:

1. Trim whitespace and remove an optional `Rp` prefix (case-insensitive).
2. Accept ungrouped digits or consistently grouped thousands using either comma or dot.
3. Accept a leading minus sign for `Signed Amount`; reject negative `Amount`.
4. Reject decimal fractions, mixed separators, malformed grouping, unexpected symbols, and ambiguous representations. Do not guess locale semantics.
5. If a CSV reader returns a numeric cell representation, accept it only if it is finite and mathematically integral.
6. Do not parse via `float` and then round.

Cross-field invariant:

```text
abs(signed_amount) == amount
```

A mismatch is a hard validation error. The converter must not repair it automatically.

### 2.5 Type and sign semantics

| `type` | Required sign of `signed_amount` | Consolidated cash-flow treatment |
|---|---|---|
| `Income` | Positive or zero | Included in income |
| `Expense` | Negative or zero | Included in expenses |
| `Transfer` | Negative from the source-account perspective | Excluded from consolidated income, expense, and net cash flow |

`amount` is always a non-negative magnitude. `signed_amount` carries direction.

A transfer row represents one movement from `source_account` to `destination_account`. Persist exactly one transaction document. Account-level analytics may derive a source-account debit and destination-account credit as projections from that one document; do not create a second persisted transaction row/document.

For `Transfer`:

- `destination_account` is required and non-empty.
- Source and destination accounts must differ.
- `amount` must be greater than zero.
- `signed_amount` must be negative, representing the movement from the source account.

For `Income` and `Expense`, `destination_account` must be blank/null. A populated destination on a non-transfer row is rejected rather than ignored.

---

## 3. Category/type/parent reference data

The converter must validate category, type, and parent as a coherent tuple. The initial approved mapping is:

| Category | Type | Parent |
|---|---|---|
| Food & Drink | Expense | Living |
| Housing | Expense | Living |
| Utilities | Expense | Living |
| Household | Expense | Living |
| Transport | Expense | Living |
| Personal Care | Expense | Living |
| Eating Out / Snacks | Expense | Lifestyle |
| Entertainment | Expense | Lifestyle |
| Shopping | Expense | Lifestyle |
| Subscription | Expense | Lifestyle |
| Loan | Expense | Financial |
| Bank Fee | Expense | Financial |
| Investment | Expense | Financial |
| Insurance | Expense | Financial |
| Tax | Expense | Financial |
| Other Outcome | Expense | Lifestyle |
| Salary | Income | Financial |
| Freelance | Income | Financial |
| Investment Return | Income | Financial |
| Other Income | Income | Financial |
| Bank Transfer | Transfer | Internal |

Unknown categories and mismatched type/parent combinations must fail validation. This mapping should live in one dedicated constant/module rather than being duplicated across converter, analytics, and tests.

If the full CSV contains a legitimate category not listed here, do not silently add it. Review the observed value, agree on its `Type` and `Parent`, then update the reference mapping and tests explicitly.

---

## 4. Canonical JSON document

### 4.1 Required shape

The JSON output remains a top-level array of transaction objects for compatibility with the current ingestion entry point. Each object uses canonical field names only:

```json
{
  "transaction_id": "TX-00002",
  "transaction_date": "2026-02-24",
  "category": "Loan",
  "type": "Expense",
  "parent_category": "Financial",
  "source_account": "BNI",
  "destination_account": null,
  "amount": 588000,
  "signed_amount": -588000,
  "currency": "IDR",
  "description": "ShopeepayLater Payment",
  "pay_cycle": "2026-02-24"
}
```

### 4.2 Field definitions

| JSON field | Type | Nullability | Elasticsearch mapping recommendation |
|---|---|---|---|
| `transaction_id` | string | Non-null | `keyword` |
| `transaction_date` | date string (`YYYY-MM-DD`) | Non-null | `date` |
| `category` | string | Non-null | `keyword` |
| `type` | string | Non-null | `keyword` |
| `parent_category` | string | Non-null | `keyword` |
| `source_account` | string | Non-null | `keyword` |
| `destination_account` | string | Nullable | `keyword` |
| `amount` | integer | Non-null | `long` |
| `signed_amount` | integer | Non-null | `long` |
| `currency` | string | Non-null (`IDR`) | `keyword` |
| `description` | string | Nullable | `text` (optionally add a `keyword` subfield if exact matching is needed) |
| `pay_cycle` | date string (`YYYY-MM-DD`) | Nullable | `date` |

Do not include legacy aliases such as `sourceName`, `value`, `creationDate`, `parent-category`, or `payCycle` in the new canonical output. All consumers should migrate to canonical names in a coordinated change.

Do not include `balance`: the confirmed 11-column source schema has no balance field. A latest balance cannot be inferred from transaction amounts alone.

Do not persist redundant derived fields (`day`, `dayOfWeek`, `hour`) in the first canonical version. They can be derived from `transaction_date` at query/aggregation time if needed. The source currently provides no reliable transaction time.

### 4.3 Identity

`transaction_id` is the stable identity of a transaction and must be used as the Elasticsearch document `_id`.

- Missing or duplicate IDs are fatal validation errors.
- Do not derive identity from amount, date, account, or description.
- Re-running ingestion with the same source should update/replace the same logical document identity, not create duplicates.

---

## 5. Converter specification

### 5.1 Responsibilities

`financial_mcp/converter.py` must:

1. Resolve input/output paths from `financial_mcp.config`.
2. Verify the input exists and is a regular readable file.
3. Read the exact CSV schema.
4. Validate every row and the dataset-level constraints.
5. Normalize valid values into canonical JSON documents.
6. Write JSON atomically only after the entire dataset passes validation.
7. Return a non-zero process exit status on validation or I/O failure.
8. Emit actionable diagnostics without printing full transaction rows or sensitive descriptions.

The converter must not connect to Elasticsearch or mutate an Elasticsearch index.

### 5.2 Recommended implementation structure

Keep parsing, validation, transformation, and writing independently testable. Suggested functions:

```python
read_source_csv(path) -> list[dict[str, str]]
parse_idr_integer(raw_value, *, allow_negative: bool) -> int
parse_source_date(raw_value, *, field_name: str, row_number: int) -> date | None
validate_header(fieldnames) -> None
validate_and_transform_row(row, *, row_number: int) -> dict
validate_dataset(documents) -> None
write_json_atomically(documents, output_path) -> None
convert_csv_to_json() -> None
```

Names may change to fit repository conventions, but each responsibility should remain separable and unit-testable.

### 5.3 Dataset validation

Before writing output, validate:

- Exact header and column order.
- Every required field is present and non-empty.
- Every `transaction_id` is unique across the entire file.
- Every date is valid and matches the declared source format.
- Every amount parses as an integer IDR amount.
- `abs(signed_amount) == amount` for every row.
- Every category/type/parent tuple exists in the approved mapping.
- Transfer account/destination/sign invariants hold.
- Non-transfer rows do not specify a destination account.

A dataset containing one invalid transaction is not a successful conversion. Do not skip bad rows and write a partial JSON file.

### 5.4 Error reporting

- Include source filename, CSV row number (header is row 1), field name, and concise reason.
- Aggregate multiple row errors where practical, with a configurable cap (for example, first 100 errors) and a final count of additional errors.
- Never log the full row, transaction description, or other unnecessary personal financial details.
- Distinguish validation errors from file I/O and unexpected internal errors.
- A failed run must clearly state that the output was not replaced.

Example diagnostic:

```text
ERROR data/Financ - Transactions.csv: row 18, field 'Signed Amount':
absolute value does not match 'Amount'. Existing JSON output was not modified.
```

### 5.5 Atomic output

Write to a temporary file in the same directory as the target JSON, flush and close it, and then replace the target using an atomic filesystem replace operation (`os.replace`). Clean up the temporary file on failure.

The old JSON must remain byte-for-byte intact if parsing, validation, serialization, or temporary-file writing fails before the final replace.

### 5.6 Determinism

For the same CSV bytes and same converter version/configuration, conversion should produce semantically identical JSON:

- Stable source row order is preserved.
- No generated timestamps or random IDs are added.
- Amounts remain integers.
- Dates use one ISO format.
- JSON is UTF-8, indented consistently, and does not emit NaN/Infinity.

### 5.7 CLI behavior

The existing invocation remains:

```bash
python -m financial_mcp.converter
```

Expected outcomes:

- Exit code `0`: every row validated and output replaced successfully.
- Non-zero exit code: validation or I/O failure; previous output preserved.
- Success output reports source filename, number of rows converted, and output path. It must not print personal transaction contents.

---

## 6. Migration and downstream compatibility

The current repository still uses legacy JSON fields in ingestion and query/analytics modules. Moving to canonical JSON therefore requires a coordinated migration; changing only the converter would break downstream consumers.

Required follow-up changes:

1. Update Elasticsearch mapping in `ingest.py` to canonical field names and types.
2. Set Elasticsearch `_id` from `transaction_id` in the bulk action generator.
3. Update transaction queries and sort fields to `transaction_date`, `source_account`, `amount`, and `signed_amount` as appropriate.
4. Update analytics to use canonical fields and exclude `Transfer` from consolidated income/expense/net calculations.
5. Remove latest-balance output unless a future authoritative balance source is added.
6. Update README and USER_MANUAL examples, filenames, and schema descriptions.
7. Ensure old-index data is not mixed with canonical documents. Use a staging index and safe alias swap as a separate ingestion change.

Do not attempt an in-place field rename inside Elasticsearch as a substitute for rebuilding from the authoritative CSV.

---

## 7. Converter test plan

### 7.1 Schema and file tests

- Correct exact 11-column header succeeds.
- Missing column fails.
- Extra column fails.
- Duplicate header fails.
- Reordered header fails.
- Missing source file returns a clear non-zero failure.
- UTF-8 BOM is accepted.
- Completely blank rows are handled according to one documented policy; partially populated malformed rows fail.

### 7.2 Date tests

- `2/24/2026` becomes `2026-02-24`.
- Leap-day valid date succeeds.
- Invalid calendar date fails.
- Ambiguous or alternate date format fails rather than being guessed.
- Blank optional `Pay Cycle` becomes `null`.
- Invalid non-empty `Pay Cycle` fails.

### 7.3 Currency tests

- `Rp588,000` becomes `588000`.
- `Rp 1,002,500` becomes `1002500`.
- `Rp1.002.500` becomes `1002500`.
- `1000000` becomes `1000000`.
- `0` becomes `0`.
- `-Rp588,000` is accepted only for `Signed Amount`.
- Negative `Amount` fails.
- Decimal, mixed-separator, malformed grouping, non-finite, and unsupported symbols fail.

### 7.4 Business-rule tests

- Income with positive signed amount succeeds.
- Expense with negative signed amount succeeds.
- `abs(signed_amount) != amount` fails.
- Unknown category fails.
- Known category with wrong type fails.
- Known category with wrong parent fails.
- Duplicate transaction ID fails.
- Blank transaction ID fails.
- Transfer without destination fails.
- Transfer with same source and destination fails.
- Transfer with positive signed amount fails.
- Transfer with zero amount fails.
- Non-transfer with destination account fails.
- One transfer source row produces exactly one JSON document.

### 7.5 Atomicity and regression tests

- If any row is invalid, existing output file remains unchanged.
- If JSON serialization or temporary write fails, existing output remains unchanged.
- Successful conversion replaces output and preserves source row order.
- Every output document contains the canonical fields and no legacy aliases.
- All monetary fields are integers, never floats.
- Sample five-row fixture converts to the expected five canonical documents.

---

## 8. Acceptance criteria for Phase 1A

Converter Phase 1A is complete only when all of the following are true:

- [ ] CSV is the only documented source of truth.
- [ ] Exact 11-column schema is enforced.
- [ ] Currency/date parsing is explicit and deterministic.
- [ ] All category/type/parent combinations are validated.
- [ ] IDs are preserved, unique, and mandatory.
- [ ] Amount/sign and transfer invariants are enforced.
- [ ] No invalid rows are silently skipped.
- [ ] Output uses the canonical JSON schema.
- [ ] Output replacement is atomic.
- [ ] Failure leaves the previous JSON intact.
- [ ] Unit tests cover the cases in Section 7.
- [ ] No personal transaction data is committed to Git.

---

## 9. Known limitations and decisions still requiring full-data validation

The five supplied rows demonstrate two Expense rows and three Income rows. They do not demonstrate a Transfer row, empty descriptions, missing pay cycles, unusual account names, duplicate IDs, malformed values, or the full set of categories in the actual export.

Before declaring the contract fully validated against production data:

1. Run the converter test suite against synthetic fixtures.
2. Run a read-only profiling/validation pass over the complete authoritative CSV.
3. Report counts of rows, unique IDs, types, categories, blank optional fields, and validation failures without exposing transaction descriptions or sensitive values.
4. Review any newly observed category/type/parent tuple before changing the reference mapping.
5. Verify the generated JSON count equals the number of accepted source rows.

This document defines the intended contract and implementation target. It does not claim that the complete private dataset has passed validation.
