# Financial-MCP --- Initial Engineering Audit & Improvement Plan

**Repository:** `agik20/financial-mcp`\
**Document type:** Initial technical audit and implementation roadmap\
**Status:** Draft --- pending validation against a local dataset and
executable tests\
**Audit scope:** Repository source files and documentation reviewed
during the initial assessment\
**Primary objective:** Improve financial-data correctness, integrity,
security, reliability, and performance before expanding product
capabilities.

------------------------------------------------------------------------

## 1. Executive Summary

`financial-mcp` is an MCP server that exposes personal financial
transaction data stored in Elasticsearch. The current architecture is
intentionally straightforward:

1.  Convert bank or fintech exports into normalized JSON.
2.  Ingest the JSON into Elasticsearch.
3.  Expose transaction and analytics operations as MCP tools.
4.  Let an MCP-compatible AI client interpret natural-language requests
    and explain returned results.

The architecture is a reasonable starting point. However, the initial
source review identified correctness and data-integrity risks that
should be addressed before the project is treated as reliable for real
financial records.

The most important risks are:

-   Input-file path and documentation inconsistencies.
-   Currency parsing that does not safely handle Indonesian number
    formats.
-   Missing balance data in converted documents despite analytics
    expecting it.
-   Destructive index replacement during ingestion.
-   Potential transaction ID collisions.
-   Inconsistent Elasticsearch API-key handling.
-   Analytics filters and category calculations that do not fully match
    their intended semantics.
-   Insufficient validation, tests, and operational diagnostics.

**Recommendation:** Prioritize deterministic correctness and safe data
handling. Do not add LLM-heavy features until the underlying financial
calculations can be verified independently.

------------------------------------------------------------------------

## 2. Project Scope and Current Architecture

### 2.1 Current responsibility boundaries

  ---------------------------------------------------------------------------
  Component                               Current responsibility
  --------------------------------------- -----------------------------------
  `financial_mcp/config.py`               Elasticsearch settings, index name,
                                          and data-file paths

  `financial_mcp/converter.py`            CSV/Excel parsing and conversion
                                          into transaction documents

  `financial_mcp/ingest.py`               Elasticsearch index creation and
                                          bulk ingestion

  `financial_mcp/es_client.py`            Elasticsearch clients, document
                                          IDs, bulk actions, and query helper

  `financial_mcp/models.py`               Pydantic validation for transaction
                                          query parameters

  `financial_mcp/tools/transactions.py`   Raw transaction retrieval,
                                          filtering, and time-series
                                          aggregation

  `financial_mcp/tools/analytics.py`      Spending breakdown and financial
                                          summary

  `financial_mcp/server.py`               FastMCP server, tool registration,
                                          and prompts
  ---------------------------------------------------------------------------

### 2.2 Current data flow

``` text
Bank / Fintech CSV or Excel
          |
          v
   converter.py
          |
          v
 Normalized JSON
          |
          v
     ingest.py
          |
          v
 Elasticsearch index
          |
          v
  FastMCP server
          |
          v
 MCP-compatible AI client
          |
          v
 Natural-language response
```

The MCP server is a data-access and analytics layer. Natural-language
interpretation is performed by the connected AI client. Financial
arithmetic and aggregation should remain deterministic backend
operations.

### 2.3 Current MCP interface

The reviewed source registers four tools:

-   `get_all_transactions`
-   `query_transactions`
-   `get_spending_by_category`
-   `get_financial_summary`

It also registers three prompts:

-   `monthly_report`
-   `spending_analysis`
-   `cashflow_check`

------------------------------------------------------------------------

## 3. Findings and Risks

Severity definitions:

-   **P0 --- Critical:** Can cause incorrect financial data, data loss,
    or core workflow failure.
-   **P1 --- High:** Can produce misleading analytics or materially
    restrict correct usage.
-   **P2 --- Medium:** Affects robustness, usability, maintainability,
    or performance.
-   **P3 --- Later:** Valuable enhancement after core reliability is
    established.

These priorities are preliminary and should be revisited after running
tests against representative input data.

### 3.1 P0 --- Data correctness and integrity

#### F-01 --- Data-file path inconsistency

**Observed in:** `financial_mcp/config.py`, `USER_MANUAL.md`,
`README.md`

The configuration references:

-   `Financ  - Transactions.json`
-   `Financ - Transactions.csv`

The documentation describes files named:

-   `Personal Finance  - Transactions.json`
-   `Personal Finance  - Transactions.csv`

These names are not equivalent.

**Impact:** Conversion or ingestion may fail because the configured
paths do not match the actual files.

**Recommended action:**

1.  Confirm the intended canonical filenames.
2.  Define paths in one configuration location.
3.  Add an early, explicit file-existence check.
4.  Add tests asserting that configured paths resolve as expected.
5.  Update README and user documentation to match implementation.

**Acceptance criteria:**

-   Converter and ingestion use the same canonical data-file
    configuration.
-   Missing input produces an actionable error.
-   Documentation and implementation agree on filenames.

#### F-02 --- Indonesian currency parsing is unsafe

**Observed in:** `financial_mcp/converter.py`, `clean_currency()`

The current implementation removes `Rp` and commas, then converts the
remaining string to an integer. This does not correctly normalize all
Indonesian and international number formats.

Examples requiring explicit handling:

  Input                              Expected interpretation
  ----------------- ----------------------------------------
  `Rp 1.234.567`                                     1234567
  `Rp 1,234,567`                                     1234567
  `Rp 1.234,56`          Requires an explicit decimal policy
  `Rp 1,234.56`          Requires an explicit decimal policy
  `-Rp 25.000`        -25000, if signed values are supported
  Empty / missing       Null or rejected according to schema

A financial parser must not guess silently when separators are
ambiguous.

**Impact:** Incorrect transaction values can corrupt every downstream
total, trend, and recommendation.

**Recommended action:**

1.  Define accepted locale formats.
2.  Define whether the canonical monetary representation supports
    fractional currency units.
3.  Parse using explicit locale-aware rules rather than chained string
    replacements.
4.  Reject or quarantine ambiguous values.
5.  Add parameterized tests for positive, negative, zero, missing,
    malformed, and decimal values.

**Acceptance criteria:**

-   Every supported input format has a documented expected output.
-   Ambiguous inputs are rejected or handled by a documented
    deterministic rule.
-   No malformed amount is silently converted into a plausible but
    incorrect value.

#### F-03 --- Balance is expected by analytics but omitted by conversion

**Observed in:** `financial_mcp/converter.py`,
`financial_mcp/tools/analytics.py`

`get_financial_summary()` retrieves `balance` from the latest document.
However, `transform_row()` does not currently add a `balance` field to
its returned transaction document.

The converter's docstring mentions `balance`, but the returned
dictionary does not include it.

**Impact:** `latest_balance` may be missing even when the source export
contains balance information.

**Recommended action:**

1.  Confirm the source column name and semantics.
2.  Parse balance using the same currency policy as transaction values.
3.  Include balance in the canonical document schema.
4.  Add mapping and test coverage.
5.  Define how to handle exports that do not provide balances.

**Acceptance criteria:**

-   Converted documents contain `balance` when the source provides it.
-   Missing balance is represented consistently.
-   Latest-balance analytics handles missing values without crashing.

#### F-04 --- Ingestion deletes the active index before replacement is ready

**Observed in:** `financial_mcp/ingest.py`

The ingestion flow deletes the existing index, creates a new one, and
then reads and bulk-loads the JSON file.

If file loading or bulk ingestion fails after deletion, the previously
available index is already gone.

**Impact:** A failed import can cause avoidable data unavailability or
data loss.

**Recommended action:**

Replace destructive in-place ingestion with a safer process:

1.  Validate and read the input file before changing the active index.
2.  Create a uniquely named staging index.
3.  Bulk-ingest into staging.
4.  Check failures and document counts.
5.  Refresh and validate the staged data.
6.  Atomically switch the read alias to the new index.
7.  Retain the previous index until the new index is confirmed healthy.
8.  Apply a documented cleanup policy to old indices.

**Acceptance criteria:**

-   A failed ingestion does not replace or delete the active dataset.
-   Alias switching happens only after validation succeeds.
-   The previous index remains recoverable until the new index is
    active.

#### F-05 --- Deterministic document IDs can collide

**Observed in:** `financial_mcp/es_client.py`, `generate_doc_id()`

The current ID is based only on:

-   `creationDate`
-   `value`
-   `description`

Two distinct transactions can share these fields. For example, two
purchases at the same merchant for the same amount and timestamp
precision may produce the same ID.

**Impact:** One transaction may overwrite another during indexing.

**Recommended action:**

Evaluate a stable identity strategy in this order:

1.  Use a source-provided transaction ID when available.
2.  Otherwise include stable source/account identity and all suitable
    transaction attributes.
3.  Preserve duplicate legitimate transactions by incorporating a
    deterministic occurrence discriminator when the source has no unique
    ID.
4.  Document the limitations of deduplication when the source provides
    no unique identifier.

Do not assume that a content hash alone can distinguish two genuinely
identical transactions.

**Acceptance criteria:**

-   Distinct source transactions are not silently overwritten.
-   Re-importing the same source data is idempotent.
-   Deduplication behavior is covered by tests.

#### F-06 --- Synchronous Elasticsearch client ignores API-key configuration

**Observed in:** `financial_mcp/es_client.py`

The asynchronous client passes `ES_API_KEY` to `AsyncElasticsearch`, but
`create_es_client()` constructs the synchronous client without the API
key.

**Impact:** Ingestion can fail against an authenticated Elasticsearch
deployment even though the environment is configured with an API key.

**Recommended action:**

-   Apply the same authentication configuration to both client
    factories.
-   Add tests for authenticated and unauthenticated client construction.
-   Avoid printing credentials or sensitive connection details.

**Acceptance criteria:**

-   Sync and async clients use consistent host and authentication
    configuration.
-   Authentication failures produce clear, non-sensitive diagnostics.

### 3.2 P1 --- Analytics correctness

#### F-07 --- Spending-by-category ignores accepted filters

**Observed in:** `financial_mcp/tools/analytics.py`

`get_spending_by_category()` accepts `QueryTransactionParams`, but its
Elasticsearch query currently applies only:

-   Transaction type (defaulting to `Expense`)
-   Date range

It does not apply `category` or `source_name`, even though those fields
exist in the model.

**Impact:** The tool can return a broader result than the user
requested.

**Recommended action:**

-   Apply category and source filters consistently with
    `query_transactions()`.
-   Extract shared filter-building logic to reduce query drift.
-   Add tests for each filter individually and in combination.

**Acceptance criteria:**

-   Every accepted filter affects the query.
-   Returned totals and percentages are calculated only from the
    filtered population.

#### F-08 --- Top categories include income transactions

**Observed in:** `financial_mcp/tools/analytics.py`,
`get_financial_summary()`

The `by_category` aggregation is currently calculated over all
documents. It is not restricted to `Expense`.

**Impact:** Income categories can appear in a field intended to
represent the largest spending categories.

**Recommended action:**

-   Scope top spending categories to expense transactions.
-   If useful, expose income categories separately rather than mixing
    the two.
-   Clarify field names and descriptions to make their semantics
    unambiguous.

**Acceptance criteria:**

-   `top_expense_categories` contains only expenses.
-   Income and expense category analytics are not mixed.

#### F-09 --- Date range validation is incomplete

**Observed in:** `financial_mcp/models.py`

The model validates date string format but does not ensure that
`start_date` is earlier than or equal to `end_date`.

**Impact:** Invalid ranges can reach Elasticsearch and produce confusing
results.

**Recommended action:**

-   Add model-level cross-field validation.
-   Reject `start_date > end_date`.
-   Add boundary tests for equal dates, reversed dates, and missing
    endpoints.

**Acceptance criteria:**

-   Invalid date ranges fail before query execution.
-   Inclusive date-boundary behavior is documented and tested.

#### F-10 --- Transaction limit is not bounded

**Observed in:** `financial_mcp/models.py`,
`financial_mcp/tools/transactions.py`

The `limit` field is optional but has no explicit minimum or maximum
constraint.

**Impact:** Invalid or excessively large requests can cause unnecessary
query cost and oversized MCP responses.

**Recommended action:**

-   Require a positive integer.
-   Set a documented maximum page size.
-   Add pagination for larger result sets instead of allowing unbounded
    limits.

**Acceptance criteria:**

-   Zero and negative limits are rejected.
-   Requests above the maximum are rejected or safely capped according
    to documented behavior.

#### F-11 --- Monthly report constructs an invalid end date for some months

**Observed in:** `financial_mcp/server.py`, `monthly_report()`

The prompt builds the end date by appending `-31` to the requested
month.

Months with fewer than 31 days therefore receive invalid date values.

**Impact:** The generated prompt can request an invalid date range.

**Recommended action:**

-   Parse the requested year and month.
-   Calculate the actual final calendar day.
-   Validate the prompt argument before generating tool instructions.
-   Consider using a half-open interval internally if the data model and
    query layer are adapted consistently.

**Acceptance criteria:**

-   February, leap years, 30-day months, and 31-day months all generate
    valid ranges.

### 3.3 P2 --- Reliability, maintainability, and performance

#### F-12 --- Elasticsearch errors are collapsed into `None`

**Observed in:** `financial_mcp/es_client.py`, `query_elasticsearch()`

The helper catches broad exceptions and returns `None`. This hides
useful failure distinctions from callers.

**Impact:** MCP tools cannot reliably distinguish connection errors,
authentication failures, query errors, and empty results.

**Recommended action:**

-   Introduce structured internal exceptions or typed result handling.
-   Keep user-facing errors concise and safe.
-   Log diagnostic details without exposing secrets or financial
    records.
-   Distinguish an empty successful result from a failed query.

**Acceptance criteria:**

-   Empty results are not confused with infrastructure failures.
-   Error responses are actionable and do not leak sensitive data.

#### F-13 --- A new Elasticsearch client is created for each query

**Observed in:** `financial_mcp/es_client.py`

`query_elasticsearch()` opens a new async client context for each tool
query and closes it afterward. It also performs a ping before every
search.

**Impact:** Repeated connection setup and ping requests may add latency
and overhead.

**Recommended action:**

-   Evaluate FastMCP lifespan-managed client reuse.
-   Reuse a properly managed async client or connection pool.
-   Remove redundant health checks from the hot path if connection and
    request errors are handled correctly.
-   Benchmark before and after the change.

**Acceptance criteria:**

-   Client lifecycle is explicit and safely closed at shutdown.
-   Repeated tool calls reuse connections.
-   Latency and request overhead are measured.

#### F-14 --- Recent-transactions tool has no pagination

**Observed in:** `financial_mcp/tools/transactions.py`

`get_all_transactions()` always returns the latest 50 transactions.

**Impact:** Users cannot reliably inspect older records through this
tool.

**Recommended action:**

-   Prefer a single paginated query interface.
-   Use a stable sort with a tie-breaker.
-   Consider cursor/search-after pagination for Elasticsearch.
-   Keep page size bounded.

**Acceptance criteria:**

-   Users can retrieve subsequent pages without duplicates or skipped
    records under the documented consistency model.

#### F-15 --- Ingestion skips zero-value transactions

**Observed in:** `financial_mcp/es_client.py`, `generate_actions()`

The current condition skips documents when `not doc.get("value")`. A
value of zero is therefore treated like a missing value.

**Impact:** Legitimate zero-value records are excluded.

**Recommended action:**

-   Check explicitly for `None` or missing fields.
-   Validate the amount type and required fields.
-   Decide whether zero-value transactions are valid in the canonical
    schema.

**Acceptance criteria:**

-   Zero-value transactions follow an explicit policy and are not
    accidentally discarded.

#### F-16 --- Documentation and implementation are inconsistent

**Observed in:** `README.md`, `USER_MANUAL.md`, and source modules

Examples include:

-   Different data-file names.
-   README claims Python 3.14 while `pyproject.toml` declares Python
    3.10+.
-   The README lists an `Amount` and `Balance` input schema, while
    converter logic reads `Signed Amount`.
-   Documentation describes normalized fields that are not all emitted
    by the converter.

**Impact:** Setup and troubleshooting become unreliable, and
contributors may implement against the wrong schema.

**Recommended action:**

-   Treat code and tests as the source of truth.
-   Define a canonical input contract.
-   Update all documentation after implementation decisions are
    finalized.
-   Add a documentation review checklist for schema changes.

**Acceptance criteria:**

-   Installation requirements, input columns, filenames, and output
    fields agree across documentation and implementation.

------------------------------------------------------------------------

## 4. Target Canonical Transaction Schema

The project should define one normalized transaction schema independent
of each bank or fintech export format.

The following is a proposed starting point, not yet a confirmed
contract.

  -------------------------------------------------------------------------
  Field                     Suggested type          Meaning
  ------------------------- ----------------------- -----------------------
  `transaction_id`          keyword                 Stable internal/source
                                                    transaction identity

  `source_transaction_id`   keyword, nullable       Original ID from
                                                    provider if available

  `type`                    keyword                 `Income` or `Expense`

  `category`                keyword                 Normalized transaction
                                                    category

  `parent_category`         keyword, nullable       Parent category

  `source_name`             keyword                 Bank, wallet, or
                                                    account name

  `currency`                keyword                 ISO currency code,
                                                    e.g. `IDR`

  `amount_minor`            long                    Amount in the
                                                    currency's minor unit,
                                                    if applicable

  `balance_minor`           long, nullable          Account balance in the
                                                    same minor unit

  `description`             text                    Original or normalized
                                                    transaction description

  `occurred_at`             date                    Transaction timestamp

  `transaction_date`        date                    Calendar date used for
                                                    date filtering

  `pay_cycle`               date, nullable          Associated pay-cycle
                                                    date

  `ingested_at`             date                    Time the record entered
                                                    the system

  `source_file`             keyword, nullable       Import provenance

  `schema_version`          integer                 Version of normalized
                                                    document schema
  -------------------------------------------------------------------------

### 4.1 Important monetary representation decision

Before implementation, decide whether to store:

-   Whole currency units as integers, or
-   Minor currency units as integers.

Do not assume every currency has two decimal places. The representation
must be currency-aware.

For IDR-only input, whole Rupiah values may be sufficient if the source
data and use cases never require fractional Rupiah. If multi-currency
support is planned, define a currency-aware amount policy first.

### 4.2 Preserve provenance

Where practical, retain enough source metadata to trace a normalized
record back to its original input row or source transaction. This is
important for debugging imports and resolving duplicate transactions.

------------------------------------------------------------------------

## 5. Target Architecture

``` text
             External Financial Exports
           CSV / XLSX / Future Provider APIs
                         |
                         v
              Source-specific Adapters
                         |
                         v
              Validation & Normalization
        Currency / Dates / Categories / Identity
                         |
                         v
                 Canonical Schema
                         |
                         v
              Deduplication & Staging
                         |
                         v
             Elasticsearch Data Layer
                         |
                         v
              Deterministic Analytics
      Transactions / Cashflow / Balance / Trends
                         |
                         v
                   MCP Interface
              Tools / Resources / Prompts
                         |
                         v
                 MCP-compatible Client
                         |
                         v
               Natural-language Explanation
```

### 5.1 Architectural principles

1.  **Deterministic calculations:** Financial arithmetic belongs in
    tested backend code.
2.  **Canonical data model:** Provider-specific CSV differences should
    be handled by adapters.
3.  **Safe ingestion:** New data must be validated before it replaces
    active data.
4.  **Traceability:** Imported records should preserve source provenance
    where possible.
5.  **Explicit semantics:** Income, expenses, transfers, refunds, and
    balances must not be conflated.
6.  **Bounded operations:** Query sizes, date ranges, and response sizes
    should have safe limits.
7.  **Privacy by default:** Avoid unnecessary external transmission of
    raw financial records.

------------------------------------------------------------------------

## 6. Implementation Roadmap

### Phase 0 --- Confirm the data contract

**Goal:** Remove ambiguity before code changes.

Tasks:

-   [ ] Confirm actual CSV/Excel filenames.
-   [ ] Confirm actual input column names.
-   [ ] Obtain a small, anonymized sample of representative rows.
-   [ ] Confirm currency and decimal conventions.
-   [ ] Confirm whether `Amount` is signed or whether `Type` determines
    the sign.
-   [ ] Confirm the meaning and availability of `Balance`.
-   [ ] Confirm whether the source provides a stable transaction ID.
-   [ ] Define behavior for transfers, refunds, reversals, and
    zero-value rows.

**Exit criteria:** An approved input schema and normalization policy.

### Phase 1 --- Correctness foundation

**Goal:** Ensure the same source data always produces correct,
reproducible normalized records.

Tasks:

-   [ ] Fix configured file paths.
-   [ ] Implement deterministic currency parsing.
-   [ ] Parse and retain balance.
-   [ ] Define canonical transaction schema.
-   [ ] Improve transaction identity and deduplication.
-   [ ] Correct zero-value handling.
-   [ ] Add converter and identity unit tests.

**Exit criteria:** Representative source rows convert correctly and
tests cover supported edge cases.

### Phase 2 --- Analytics correctness

**Goal:** Ensure every returned financial metric has clear, correct
semantics.

Tasks:

-   [ ] Centralize Elasticsearch filter construction.
-   [ ] Apply category and source filters consistently.
-   [ ] Restrict spending categories to expenses.
-   [ ] Separate income and expense category reporting where useful.
-   [ ] Validate date ranges and limits.
-   [ ] Fix monthly prompt date calculation.
-   [ ] Add query and aggregation tests.

**Exit criteria:** Known test fixtures produce expected totals,
percentages, counts, and date buckets.

### Phase 3 --- Safe ingestion and operational reliability

**Goal:** Prevent failed imports from destroying usable data.

Tasks:

-   [ ] Validate source JSON before index changes.
-   [ ] Add staging index creation.
-   [ ] Bulk-ingest and inspect failures.
-   [ ] Verify counts and required fields.
-   [ ] Switch aliases only after validation.
-   [ ] Retain prior index for rollback.
-   [ ] Align sync and async authentication configuration.
-   [ ] Improve structured error handling.

**Exit criteria:** Failed ingestion leaves the active index untouched;
successful ingestion can be rolled back according to policy.

### Phase 4 --- Performance and usability

**Goal:** Improve latency and support larger datasets without unsafe
requests.

Tasks:

-   [ ] Evaluate reusable Elasticsearch client lifecycle.
-   [ ] Benchmark query latency.
-   [ ] Add bounded pagination.
-   [ ] Use stable sorting and cursor-based pagination where
    appropriate.
-   [ ] Review Elasticsearch mappings and aggregation costs.
-   [ ] Avoid unnecessary ping requests on every query.

**Exit criteria:** Performance changes are supported by before/after
measurements and pagination tests.

### Phase 5 --- Engineering maturity

**Goal:** Make the project maintainable and easier to contribute to.

Tasks:

-   [ ] Add pytest test suite.
-   [ ] Add Ruff or equivalent linting.
-   [ ] Add static type checking.
-   [ ] Add GitHub Actions CI.
-   [ ] Add dependency and security checks.
-   [ ] Add Docker-based local development if useful.
-   [ ] Update README and user manual.
-   [ ] Define release and versioning policy.

**Exit criteria:** Pull requests run automated checks and the documented
setup can be reproduced.

------------------------------------------------------------------------

## 7. Testing Strategy

Testing should be introduced before major refactoring.

### 7.1 Unit tests

  -----------------------------------------------------------------------
  Module                              Test focus
  ----------------------------------- -----------------------------------
  `converter.py`                      Currency parsing, date parsing,
                                      missing values, row transformation

  `models.py`                         Valid and invalid filters, reversed
                                      dates, limits

  `es_client.py`                      ID stability, duplicate handling,
                                      zero values, client configuration

  `transactions.py`                   Filter composition, pagination,
                                      aggregation result mapping

  `analytics.py`                      Category totals, percentages,
                                      expense-only categories, empty
                                      datasets

  `server.py`                         Prompt date boundaries and argument
                                      validation
  -----------------------------------------------------------------------

### 7.2 Integration tests

Use a disposable Elasticsearch instance or test container.

Test:

-   Index creation and mappings.
-   Bulk ingestion.
-   Alias switching.
-   Rollback after failed ingestion.
-   Date filtering.
-   Aggregations.
-   Authentication behavior where practical.

### 7.3 Financial invariants

Add explicit tests for core arithmetic:

-   `net = total_income - total_expense`, assuming expenses are
    represented as positive magnitudes.
-   Category percentages sum to approximately 100% for a non-zero total,
    subject to rounding.
-   Expense-only reports contain no income transactions.
-   Transaction counts match the filtered source population.
-   Re-importing the same source does not duplicate records.
-   Distinct legitimate transactions are not overwritten.
-   Missing balance does not become an invented zero balance.

The sign convention must be confirmed before enforcing the first
invariant.

------------------------------------------------------------------------

## 8. Security and Privacy Considerations

Financial records are sensitive. Security requirements should be treated
as part of the core design, not as a final polish step.

Recommended controls:

-   Keep Elasticsearch bound to localhost unless remote access is
    explicitly required.
-   Require authentication and TLS for remote Elasticsearch.
-   Never commit source exports, normalized financial JSON, credentials,
    or tokens.
-   Avoid logging raw transaction descriptions or full query results.
-   Validate imported file structure and content.
-   Restrict query sizes and expensive operations.
-   Document what data is sent to the connected MCP client and its model
    provider.
-   Keep secrets out of exception messages and diagnostics.
-   Define backup, retention, and deletion procedures.

------------------------------------------------------------------------

## 9. Decisions Required Before Implementation

The following questions block safe implementation of the data layer.

1.  **Input filenames:** What are the exact filenames currently used
    locally?
2.  **Input columns:** Does the real export contain `Amount`,
    `Signed Amount`, or both?
3.  **Sign convention:** Are expenses negative in the source, or is
    transaction direction represented only by `Type`?
4.  **Balance:** Is the balance column present on every row, and is it a
    post-transaction balance?
5.  **Currency:** Is the current dataset IDR-only? Are decimal Rupiah
    values possible?
6.  **Transaction identity:** Does the source export contain a unique
    transaction/reference ID?
7.  **Transfers:** Are transfers between the user's own accounts
    represented as income/expense, or should they be modeled separately?

Do not infer these answers from documentation alone. Validate them
against a sanitized source sample.

------------------------------------------------------------------------

## 10. Immediate Next Steps

The recommended first implementation cycle is:

1.  Confirm the real input format using an anonymized sample of 3--5
    rows.
2.  Agree on the canonical transaction schema and monetary
    representation.
3.  Implement and test the converter.
4.  Fix the analytics defects against deterministic test fixtures.
5.  Replace destructive ingestion with a staged, validated process.

### First deliverable

**A tested normalization pipeline** that converts the actual source
export into canonical transaction documents without silently changing
monetary values or losing required fields.

This deliverable becomes the foundation for every subsequent analytics
and MCP capability.

------------------------------------------------------------------------

## 11. Audit Limitations

This document records findings from an initial source-code and
documentation review. It is not yet a runtime performance audit or a
production security assessment.

The following have not yet been established:

-   Actual behavior against the user's current local CSV/Excel file.
-   Elasticsearch cluster configuration and runtime version.
-   Real dataset size and query latency.
-   Whether source exports contain stable transaction identifiers.
-   Whether all documented workflows currently execute successfully.
-   Whether the repository's current default branch has changed since
    the reviewed snapshot.

Findings should be confirmed with local fixtures and automated tests
before being treated as resolved defects.

------------------------------------------------------------------------

## 12. Definition of Done

The initial hardening effort is complete when:

-   [ ] Input schema and currency policy are documented.
-   [ ] Converter output is deterministic and covered by tests.
-   [ ] Required transaction and balance fields are normalized
    correctly.
-   [ ] Distinct transactions are not silently overwritten.
-   [ ] Analytics filters and financial calculations pass fixture-based
    tests.
-   [ ] Failed ingestion cannot destroy the active dataset.
-   [ ] Elasticsearch authentication works consistently.
-   [ ] Query limits and pagination are safe.
-   [ ] CI runs tests and quality checks.
-   [ ] Documentation matches the implemented behavior.

**Guiding principle:** Build a trustworthy financial data engine first.
Add intelligence on top only after the numbers can be trusted.


------------------------------------------------------------------------

## 13. Confirmed CSV Data Contract (User-Provided)

**Status:** Confirmed against the complete set of CSV columns and representative
transaction examples supplied by the project owner. This section supersedes
earlier assumptions in this draft wherever they conflict.

### 13.1 Source columns and canonical fields

| Source column | Canonical field | Interpretation |
|---|---|---|
| `ID` | `transaction_id` | Stable source transaction identifier; use as the primary document identity |
| `Date` | `transaction_date` | Transaction date, parsed explicitly as `M/D/YYYY` (`%m/%d/%Y`) |
| `Category` | `category` | Transaction category |
| `Type` | `type` | One of `Income`, `Expense`, or `Transfer` |
| `Account` | `source_account` | Account from which the transaction is recorded |
| `Destination Account` | `destination_account` | Destination account; populated for bank transfers |
| `Amount` | `amount` | Positive absolute transaction magnitude |
| `Detail Transaction` | `description` | Transaction description |
| `Pay Cycle` | `pay_cycle` | Pay-cycle date, using the same confirmed `M/D/YYYY` date format |
| `Signed Amount` | `signed_amount` | Signed amount; sign represents direction from the source-account perspective |
| `Parent` | `parent_category` | Parent grouping mapped from `Category` |

The supplied CSV has 11 columns. No `Balance` column is present. Do not
derive or expose an account's current balance as if it were authoritative
from this dataset alone. Balance functionality requires an opening balance
plus a complete transaction history, a balance snapshot, or another
authoritative source.

### 13.2 Confirmed category mapping

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

Treat this mapping as an explicit domain rule. Validate incoming category/type/
parent combinations against it and report unknown or inconsistent combinations;
do not silently infer or rewrite them.

### 13.3 Financial semantics and validation

- `Income` transactions must have a positive `signed_amount`.
- `Expense` transactions must have a negative `signed_amount`.
- `Transfer` is a separate transaction type, not income or expense. The
  supplied example records a transfer from BNI to Mandiri as `-1,002,500`
  from the source-account perspective. A transfer must not contribute to
  income or expense totals.
- `amount` is expected to be non-negative and
  `abs(signed_amount) == amount`. A mismatch is a validation error or
  quarantined record, not an invitation to silently repair source data.
- A transfer should identify both source and destination accounts. Missing
  account endpoints should be surfaced as invalid/incomplete transfer data.
- For transfers, the sign describes the movement from the `Account` field's
  perspective. The reciprocal destination-side entry, if present in the
  source export, must not be assumed or synthesized without evidence.
- Parse both `Date` and `Pay Cycle` with the explicit `%m/%d/%Y` format.
  Reject invalid dates with row-level diagnostics.

### 13.4 Implementation consequences

1. Replace any generated/hash-based document identity with the source `ID`,
   after validating that it is present and unique. Define an explicit
   duplicate-ID policy before indexing.
2. Preserve `type`, `category`, `parent_category`, `source_account`,
   `destination_account`, `amount`, `signed_amount`, `description`,
   `transaction_date`, and `pay_cycle` in the normalized document.
3. Make analytics type-aware:
   - income aggregation includes only `Income`;
   - expense/spending aggregation includes only `Expense`;
   - transfer aggregation is separate and excluded from income/expense;
   - net cash flow should document whether transfers are excluded (recommended
     for consolidated personal cash flow) or reported separately.
4. Use `Parent` as a category grouping dimension, not as a substitute for
   transaction type.
5. Do not implement a `balance` field from this CSV because the source does
   not provide one.

### 13.5 Remaining implementation checks

The source contract is now sufficiently defined to begin converter and
analytics work. Before declaring the pipeline production-ready, validate
the rules against the full CSV, including duplicate IDs, blank destination
accounts, zero-value transactions, malformed currency strings, unknown
categories, and any transfer pairs or reversals. The supplied example
establishes the source-side sign convention for one transfer; it does not
establish whether every transfer is represented by one row or by paired
rows.
