# financial-mcp

> MCP server for querying and analyzing personal financial transaction data using natural language, backed by Elasticsearch.

`financial-mcp` is a personal/internal MCP server that exposes financial transaction data stored in Elasticsearch as tools that can be called by an MCP-compatible AI client such as Claude Desktop.

The server does **not** contain an LLM. Natural-language interpretation is handled by the MCP client, while `financial-mcp` validates tool inputs, builds Elasticsearch queries, executes them, and returns structured financial data.

## Overview

Personal financial transaction exports are often easy to store but cumbersome to query and analyze manually. `financial-mcp` provides an MCP interface over transaction data so an AI assistant can answer questions such as:

- How much did I spend this month?
- Which categories account for most of my spending?
- Compare income and expenses between two months.
- Show my recent transactions.

The project currently uses a single sample financial transaction dataset and a local Elasticsearch instance.

## Features

- Convert bank/fintech CSV transaction exports to normalized JSON
- Normalize Indonesian Rupiah currency values
- Validate every record against a strict 12-field schema before indexing
- Safe staging-index ingestion: a new versioned physical index is created, fully loaded, and verified before the live alias is switched atomically — the currently-active index is never deleted during ingestion
- Query transactions by:
  - Date range
  - Transaction type
  - Category
  - Source account
- Perform daily, weekly, and monthly time-series aggregations
- Calculate spending by category
- Generate financial summaries
- Expose financial operations through MCP tools
- Provide MCP prompts for common financial analysis workflows
- Run entirely against a local Elasticsearch instance
- 98 automated tests with full coverage of ingestion, query, and validation logic

## Architecture

### Ingestion: Safe Staging Workflow

`ingest.py` uses a versioned physical index + alias architecture so that the
currently live data is never destroyed by an ingestion failure:

```text
  financial-transactions-active   ←  stable alias (what queries use)
  │
  ├── financial-transactions-20260101-120000-00   ← physical index v1
  ├── financial-transactions-20260102-093000-00   ← physical index v2  (live)
  └── (future runs create v3, v4, …)
```

Each ingestion run:

1. **Preflight** — validates the entire JSON dataset *before* touching Elasticsearch.
2. **Create staging index** — a new versioned physical index is created with the
   canonical 12-field mapping.
3. **Bulk load** — all documents are bulk-indexed into the staging index.
4. **Verify staging** — count, spot-fetch, and index health are checked on the
   new index before the switch.
5. **Atomic alias switch** — the `financial-transactions-active` alias is moved
   from the old physical index to the new one in a single `update_aliases` call.
6. **Final verify** — the alias target and document count are confirmed.

If any step before the alias switch fails, the staging index is cleaned up and
the alias is untouched. The previous physical index is always kept — the
operator can manually roll back at any time.

### Query path

All MCP tools query the `financial-transactions-active` alias, never a
physical index name.

```text
                    Bank / Fintech CSV
                           │
                           ▼
              financial_mcp/converter.py
                           │
                           ▼
           Normalized Transaction JSON
           (12-field canonical schema)
                           │
                           ▼
               financial_mcp/ingest.py
               (staging index + alias switch)
                           │
                           ▼
  Elasticsearch: financial-transactions-active
  (alias → versioned physical index)
                           │
                           ▼
              financial_mcp/server.py
                    FastMCP / stdio
                           │
                           ▼
                Claude Desktop / MCP Client
                           │
                           ▼
                  Natural Language
                    Interaction
```

The important distinction is that the natural-language reasoning happens on the **client side**.

For example:

```text
User:
"Berapa pengeluaran makanan saya bulan Maret?"

Claude Desktop:
    ↓
query_transactions(
    start_date="2026-03-01",
    end_date="2026-03-31",
    category="Food & Drink",
    type="Expense"
)

    ↓
financial-mcp
    ↓
Elasticsearch
    ↓
Structured result
    ↓
Claude Desktop
    ↓
Natural-language response
```

`financial-mcp` therefore acts as the data-access and analytics layer rather than an AI/LLM layer.

## Tech Stack

| Component | Technology |
|---|---|
| Language | Python 3.14 |
| MCP | `mcp[cli]` / FastMCP |
| Search & Analytics | Elasticsearch 8.x |
| Data Validation | Pydantic 2.x |
| Data Processing | pandas 2.x |
| Dependency Management | uv |
| Build Backend | setuptools |
| MCP Transport | stdio |
| LLM | None on server; handled by MCP client |
| Containerization | Not currently implemented |
| Testing | pytest + 98 tests (mocked ES client, no live cluster required) |
| CI/CD | Not currently implemented |

## Project Structure

```text
financial-mcp/
├── financial_mcp/
│   ├── __init__.py
│   ├── config.py          # alias names, index prefix, data paths
│   ├── es_client.py       # client, validation, INDEX_MAPPING (12-field canonical schema)
│   ├── models.py          # Pydantic models (QueryTransactionParams)
│   ├── server.py          # FastMCP entry point + prompts
│   ├── ingest.py          # staging-index workflow (never deletes live index)
│   ├── converter.py       # CSV → normalized JSON
│   └── tools/
│       ├── __init__.py
│       ├── transactions.py
│       └── analytics.py
│
├── tests/                 # 98 pytest tests (mocked, no live ES needed)
├── docs/
│   └── USER_MANUAL.md
├── data/
├── backup/
├── pyproject.toml
└── README.md
```

### Core modules

| Module | Responsibility |
|---|---|
| `config.py` | Alias name, physical index prefix, data file paths |
| `es_client.py` | ES client, transaction validation, `INDEX_MAPPING` (12-field canonical schema) |
| `models.py` | Pydantic models (`QueryTransactionParams`) |
| `converter.py` | CSV → normalized JSON conversion |
| `ingest.py` | Staging-index workflow: create → load → verify → atomic alias switch |
| `server.py` | FastMCP server and MCP registration |
| `tools/transactions.py` | Transaction retrieval and filtering tools |
| `tools/analytics.py` | Financial analytics tools |

## Prerequisites

- Python 3.14
- Elasticsearch 8.x
- `uv`
- Claude Desktop or another MCP-compatible client

Elasticsearch must be available locally at:

```text
http://localhost:9200
```

Elasticsearch can be installed natively or run locally using Docker.

## Installation

Clone the repository and install the project dependencies:

```bash
git clone https://github.com/agik20/financial-mcp.git
cd financial-mcp
uv sync
```

`uv sync` installs the dependencies defined in `pyproject.toml`.

Current dependencies include:

```toml
dependencies = [
    "elasticsearch>=8.0.0,<9.0.0",
    "mcp[cli]>=1.26.0",
    "pydantic>=2.12.5",
    "pandas>=2.0.0",
]
```

## Elasticsearch Configuration

By default, the server expects Elasticsearch at:

```text
http://localhost:9200
```

Configuration is provided through environment variables:

| Variable | Required | Description |
|---|---|---|
| `ES_HOST` | No | Elasticsearch host URL |
| `ES_API_KEY` | No | Elasticsearch API key |

For a local Elasticsearch instance without authentication:

```text
ES_HOST=http://localhost:9200
ES_API_KEY=
```

No `.env` file is required by the current implementation.

## Data Conversion

`converter.py` converts the source CSV into the normalized JSON format used by Elasticsearch.

The converter reads the source CSV and produces canonical 12-field JSON
documents. The converter performs:

- Currency parsing (Indonesian Rupiah format normalization)
- Date parsing (source `MM/DD/YYYY` → ISO `YYYY-MM-DD`)
- Field validation against the canonical schema
- Sign conventions enforcement (`signed_amount`)
- Transfer invariant checks (destination required, source ≠ destination,
  amount > 0, signed_amount < 0)

For example:

```text
Rp588,000
```

is normalized into:

```text
588000
```

The resulting JSON is stored under the `data/` directory.

## Data Ingestion

`ingest.py` implements a safe staging workflow that never deletes or overwrites
the currently active index:

1. **Preflight** — validates the entire JSON dataset (schema, duplicates, types)
   before touching Elasticsearch.
2. **Create staging index** — a new versioned physical index is created under
   the prefix `financial-transactions-YYYYMMDD-HHMMSS[-seq]`.
3. **Bulk load** — all documents are bulk-indexed; partial success is never
   treated as complete.
4. **Verify staging** — count, spot-fetch, and index health are checked.
5. **Atomic alias switch** — the `financial-transactions-active` alias is moved
   from the previous target to the new index in a single `update_aliases` call.
6. **Final verify** — the alias target and document count are confirmed.

If any step before the alias switch fails, the staging index is deleted and the
alias is left untouched. The previous physical index is always preserved so the
operator can manually roll back.

Run with:

```bash
python -m financial_mcp.ingest
```

Output on success:

```text
OK  alias=financial-transactions-active staging=financial-transactions-20260101-120000-00 previous=None docs=106
```

## MCP Server

The MCP server is implemented using **FastMCP** and uses the `stdio` transport.

Run the server with:

```bash
python -m financial_mcp.server
```

The server is designed to be launched by an MCP client such as Claude Desktop rather than accessed directly through a browser or REST endpoint.

## Claude Desktop Configuration

Add the following MCP server configuration to Claude Desktop:

```json
{
  "mcpServers": {
    "financial": {
      "command": "python",
      "args": ["-m", "financial_mcp.server"],
      "cwd": "/path/to/financial-mcp",
      "env": {
        "ES_HOST": "http://localhost:9200",
        "ES_API_KEY": ""
      }
    }
  }
}
```

Replace:

```text
/path/to/financial-mcp
```

with the absolute path to the local repository.

The `cwd` is required because the application resolves data paths relative to the project root.

`ES_API_KEY` is optional. Leave it empty when Elasticsearch is running locally without authentication.

## MCP Tools

The server currently exposes four MCP tools.

### `get_all_transactions`

Returns the 50 most recent transactions.

### `query_transactions`

Queries transactions using optional filters including:

- Date range
- Transaction type
- Category
- Source

It also supports time-series aggregation:

```text
daily
weekly
monthly
```

This allows the same tool to support both transaction-level retrieval and time-based analysis.

### `get_spending_by_category`

Calculates spending grouped by category, including:

- Total spending
- Percentage of total spending
- Average spending

### `get_financial_summary`

Returns a high-level financial summary including:

- `total_income` — sum of `signed_amount` for Income documents
- `total_expense` — sum of `amount` for Expense documents
- `net_cash_flow` — `total_income - total_expense` (Transfers are excluded
  entirely; they represent money moving between the owner's own accounts
  and must not be counted as gains or losses)
- `top_categories` — the 10 highest-magnitude categories by `amount`
  (Income + Expense only; Transfers excluded)
- `total_documents` — total transaction count (includes Transfers, which
  remain stored and queryable)

The tool deliberately does **not** report a balance figure: the canonical data
model has no balance column.

## MCP Prompts

The server also provides three MCP prompts for common analysis workflows:

- `monthly_report`
- `spending_analysis`
- `cashflow_check`

These prompts provide structured entry points for common financial-analysis tasks while the underlying data retrieval remains handled by the MCP tools.

## Example Queries

Once connected to Claude Desktop, examples of natural-language requests include:

```text
"Berapa total pengeluaran saya?"
```

```text
"Kategori apa yang paling banyak menghabiskan uang?"
```

```text
"Bandingkan pengeluaran vs pemasukan bulan September dan Oktober."
```

```text
"Tampilkan 20 transaksi terakhir saya."
```

```text
```

Claude translates these requests into appropriate MCP tool calls.

For example:

```text
User
  │
  │ "Berapa pengeluaran makan bulan Maret?"
  ▼
Claude Desktop
  │
  │ query_transactions(...)
  ▼
financial-mcp
  │
  │ Elasticsearch query
  ▼
Elasticsearch
  │
  │ structured result
  ▼
financial-mcp
  │
  ▼
Claude Desktop
  │
  ▼
Natural-language answer
```

## Example Financial Summary

A financial summary returned by the server can contain data such as:

```json
{
  "total_income": 1250000,
  "total_expense": 8400000,
  "net_cash_flow": -7150000,
  "top_categories": [
    {
      "category": "Loan",
      "total": 5880000
    },
    {
      "category": "Food & Drink",
      "total": 1200000
    }
  ],
  "total_documents": 106
}
```

Values shown above represent an example response structure, not live financial data.

## Data Model

The canonical transaction schema (12 fields, `dynamic: strict` — any extra
key is rejected at index time) is:

| Field | Type | Notes |
|---|---|---|
| `transaction_id` | `keyword` | Document `_id` — must match exactly, no hashing |
| `transaction_date` | `date` | `YYYY-MM-DD` (required) |
| `category` | `keyword` | Must be a known category (validated against `CATEGORY_MAPPING`) |
| `type` | `keyword` | `Expense`, `Income`, or `Transfer` |
| `parent_category` | `keyword` | `Living`, `Lifestyle`, or `Financial` |
| `source_account` | `keyword` | e.g. `"BNI"` |
| `destination_account` | `keyword` | `null` for non-transfers; must differ from `source_account` when `type=Transfer` |
| `amount` | `long` | Positive integer magnitude (IDR) |
| `signed_amount` | `long` | Positive for `Income`, negative for `Expense`/`Transfer` |
| `currency` | `keyword` | `"IDR"` |
| `description` | `text` | Free-form transaction detail |
| `pay_cycle` | `date` / `null` | `YYYY-MM-DD` for salary/income cycles, `null` otherwise |

`signed_amount` is used for Income totals; `amount` is used for Expense
magnitudes and Transfer magnitudes. `net_cash_flow` is computed as
`total_income − total_expense` with Transfer transactions excluded entirely
(Transfers represent money moving between the owner's own accounts and do
not count as gains or losses). There is no balance field in the schema —
the source CSV does not carry balance snapshots.

## Elasticsearch Analytics

The server uses Elasticsearch for structured querying and aggregation rather than semantic search.

Current query capabilities include:

- Boolean filtering
- Date filtering
- `sum` aggregations
- `terms` aggregations
- `date_histogram` aggregations
- Filtered aggregations
- Script-based calculations where required

There are currently:

- No embeddings
- No vector database
- No semantic search
- No server-side LLM
- No external AI API dependency

## Data Privacy & Security

This project is intended as a **personal/internal tool**.

The intended deployment keeps financial data on the local machine:

```text
Financial Data
     │
     ▼
Local Elasticsearch
     │
     ▼
Local MCP Server
     │
     ▼
MCP Client
```

The current architecture does not send financial transaction data to Elastic Cloud or another remote Elasticsearch service.

However, when using an external MCP client or LLM provider, users should understand how that client handles tool inputs and outputs.

### Important

**Do not commit sensitive financial information to the repository.**

Never commit:

- Real financial transaction exports
- `.env` files
- Elasticsearch API keys
- Passwords
- Authentication tokens
- Other personal or financial information

The `data/` directory is intended to remain excluded from version control.

The repository's Git history has been cleaned after sensitive development data was previously committed during early development.

## Development Status

This project is currently in **development**.

Current state:

- 98 automated tests (pytest, mocked ES — no live cluster required)
- mypy-clean type checking
- Staging-index ingestion with atomic alias switch
- Canonical 12-field schema with strict validation
- No release tags
- No linting configuration
- No pre-commit hooks
- No GitHub Actions CI/CD
- No Docker configuration
- Single-source transaction dataset
- No documented public roadmap yet

## Current Capabilities

The current implementation supports:

- CSV → JSON conversion
- Canonical 12-field schema validation
- Safe staging-index ingestion (alias switch, no live-index destruction)
- Four MCP tools
- Three MCP prompts
- Date-range filtering (`transaction_date`)
- Transaction type filtering
- Category filtering
- Source-account filtering
- Daily / weekly / monthly time-series aggregation
- Category-level spending analysis
- Financial summary generation
- 98 automated tests (pytest)

## Roadmap

A formal roadmap has not yet been defined.

Potential future improvements may include testing, observability, additional financial analytics, broader data-source support, and deployment improvements.

## License

No license has currently been selected for this project.

Until a license is added, the repository should not be assumed to grant broad permissions to copy, modify, distribute, or commercially use the code.

## Repository

GitHub:

https://github.com/agik20/financial-mcp