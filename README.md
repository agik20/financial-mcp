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
- When was my latest balance recorded?

The project currently uses a single sample financial transaction dataset and a local Elasticsearch instance.

## Features

- Convert bank/fintech CSV transaction exports to normalized JSON
- Normalize Indonesian Rupiah currency values
- Derive date-related fields such as day, day of week, and hour
- Store transactions in Elasticsearch
- Generate deterministic document IDs using MD5 hashes to prevent duplicate documents
- Query transactions by:
  - Date range
  - Transaction type
  - Category
  - Source
- Perform daily, weekly, and monthly time-series aggregations
- Calculate spending by category
- Generate financial summaries
- Expose financial operations through MCP tools
- Provide MCP prompts for common financial analysis workflows
- Run entirely against a local Elasticsearch instance

## Architecture

```text
                    Bank / Fintech CSV
                           │
                           ▼
              financial_mcp/converter.py
                           │
                           ▼
             Normalized Transaction JSON
                           │
                           ▼
                financial_mcp/ingest.py
                           │
                           ▼
             Elasticsearch: financial-transactions
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
| Testing | Not currently implemented |
| CI/CD | Not currently implemented |

## Project Structure

```text
financial-mcp/
├── financial_mcp/
│   ├── __init__.py
│   ├── config.py
│   ├── es_client.py
│   ├── models.py
│   ├── server.py
│   ├── ingest.py
│   ├── converter.py
│   └── tools/
│       ├── __init__.py
│       ├── transactions.py
│       └── analytics.py
│
├── data/
├── backup/
├── pyproject.toml
├── README.md
└── USER_MANUAL.md
```

### Core modules

| Module | Responsibility |
|---|---|
| `config.py` | Application and Elasticsearch configuration |
| `es_client.py` | Elasticsearch client utilities and query execution |
| `models.py` | Data models and validation |
| `converter.py` | CSV → normalized JSON conversion |
| `ingest.py` | Elasticsearch index creation/deletion and bulk ingestion |
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

Expected CSV columns:

```text
Date
Type
Category
Account
Amount
Balance
Detail Transaction
Pay Cycle
```

The converter performs operations including:

- Currency formatting cleanup
- Date parsing
- Transaction field normalization
- Derivation of:
  - `day`
  - `dayOfWeek`
  - `hour`

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

`ingest.py` manages the Elasticsearch index:

```text
financial-transactions
```

The ingestion process:

1. Creates or recreates the Elasticsearch index.
2. Reads the processed transaction JSON.
3. Generates deterministic document IDs using MD5 hashes.
4. Bulk-inserts the transaction documents into Elasticsearch.

The deterministic document ID prevents the same transaction from being inserted repeatedly when the same source data is ingested again.

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

- Total income
- Total expenses
- Net amount
- Latest balance
- Top 10 spending categories

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
"Saldo terakhir saya kapan dan berapa?"
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
  "net": 4100000,
  "latest_balance": 32977,
  "latest_balance_date": "2026-02-24",
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

The normalized transaction data contains fields including:

```text
type
category
sourceName
unit
value
balance
description
creationDate
day
dayOfWeek
hour
payCycle
```

Transactions currently use IDR as the currency unit.

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

Current limitations:

- No release tags
- No automated tests
- No test suite
- No linting configuration
- No static type-checking configuration
- No pre-commit hooks
- No GitHub Actions CI/CD
- No Docker configuration
- Single-source transaction dataset
- No documented public roadmap yet

These limitations are intentional indicators of the project's current development stage rather than supported production guarantees.

## Current Capabilities

The current implementation supports:

- CSV → JSON conversion
- Transaction normalization
- Elasticsearch index management
- Bulk transaction ingestion
- Four MCP tools
- Three MCP prompts
- Date-range filtering
- Transaction type filtering
- Category filtering
- Source filtering
- Daily time-series aggregation
- Weekly time-series aggregation
- Monthly time-series aggregation
- Category-level spending analysis
- Financial summary generation

## Roadmap

A formal roadmap has not yet been defined.

Potential future improvements may include testing, observability, additional financial analytics, broader data-source support, and deployment improvements.

## License

No license has currently been selected for this project.

Until a license is added, the repository should not be assumed to grant broad permissions to copy, modify, distribute, or commercially use the code.

## Repository

GitHub:

https://github.com/agik20/financial-mcp
