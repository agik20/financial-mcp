"""Elasticsearch client, transaction validation, and bulk-action helpers.

This module is the *integrity gate* for transaction data.  Nothing is sent
to Elasticsearch until the entire JSON file has passed validation:

* :func:`generate_actions` validates **every** record in a file before
  yielding a single bulk action (ingestion safety).
* :func:`validate_transaction` enforces the exact field-level contract
  (schema integrity).
* :func:`create_es_client` / :func:`get_async_es_client` handle
  connection lifecycle consistently and never leak query bodies or
  financial data into logs (operational reliability).
"""

import json
import logging
import re
from contextlib import asynccontextmanager
from datetime import date
from typing import Any, AsyncGenerator, Dict, Iterator, Optional

from elasticsearch import AsyncElasticsearch, Elasticsearch

from financial_mcp.config import ES_API_KEY, ES_HOST, ES_INDEX

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Category & Parent Mapping
# ---------------------------------------------------------------------------

#: category -> allowed transaction types
CATEGORY_MAPPING: Dict[str, tuple] = {
    # Expense
    "Food & Drink": ("Expense",),
    "Housing": ("Expense",),
    "Utilities": ("Expense",),
    "Household": ("Expense",),
    "Transport": ("Expense",),
    "Personal Care": ("Expense",),
    "Eating Out / Snacks": ("Expense",),
    "Entertainment": ("Expense",),
    "Shopping": ("Expense",),
    "Subscription": ("Expense",),
    "Loan": ("Expense",),
    "Bank Fee": ("Expense",),
    "Investment": ("Expense",),
    "Insurance": ("Expense",),
    "Tax": ("Expense",),
    "Other Outcome": ("Expense",),
    # Income
    "Salary": ("Income",),
    "Freelance": ("Income",),
    "Investment Return": ("Income",),
    "Other Income": ("Income",),
    # Transfer
    "Bank Transfer": ("Transfer",),
}

#: parent_category -> set of allowed child categories
PARENT_CATEGORY_MAP: Dict[str, set] = {
    "Living": {
        "Food & Drink",
        "Housing",
        "Utilities",
        "Household",
        "Transport",
        "Personal Care",
    },
    "Lifestyle": {
        "Eating Out / Snacks",
        "Entertainment",
        "Shopping",
        "Subscription",
        "Other Outcome",
    },
    "Financial": {
        "Loan",
        "Bank Fee",
        "Investment",
        "Insurance",
        "Tax",
        "Salary",
        "Freelance",
        "Investment Return",
        "Other Income",
    },
    "Internal": {
        "Bank Transfer",
    },
}


# ---------------------------------------------------------------------------
# Validation Constants
# ---------------------------------------------------------------------------

REQUIRED_FIELDS: frozenset = frozenset(
    {
        "transaction_id",
        "transaction_date",
        "category",
        "type",
        "parent_category",
        "source_account",
        "destination_account",
        "amount",
        "signed_amount",
        "currency",
        "description",
        "pay_cycle",
    }
)

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_ES_ID_MAX_BYTES = 512

#: Canonical 12-field mapping.  ``dynamic: strict`` means any field outside
#: this schema is rejected at index time, so validation cannot be bypassed by
#: accidentally ingesting a document with extra keys.
INDEX_MAPPING: Dict[str, Any] = {
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 0,
    },
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "transaction_id": {"type": "keyword"},
            "transaction_date": {"type": "date"},
            "category": {"type": "keyword"},
            "type": {"type": "keyword"},
            "parent_category": {"type": "keyword"},
            "source_account": {"type": "keyword"},
            "destination_account": {"type": "keyword"},
            "amount": {"type": "long"},
            "signed_amount": {"type": "long"},
            "currency": {"type": "keyword"},
            "description": {"type": "text"},
            "pay_cycle": {"type": "date"},
        },
    },
}


# ---------------------------------------------------------------------------
# Transaction Validation
# ---------------------------------------------------------------------------

def generate_doc_id(doc: Dict[str, Any]) -> str:
    """Return the canonical ``transaction_id`` to use as the ES document ``_id``.

    The ID is **not** derived (no hashing, no trimming): it must be exactly
    what is stored in ``_source.transaction_id`` so that the document's
    identity and its payload can never disagree.

    Raises:
        ValueError: when ``transaction_id`` is missing, empty, carries
            leading/trailing whitespace, or exceeds the 512-byte ES limit.
    """
    transaction_id = doc.get("transaction_id")
    if (
        not isinstance(transaction_id, str)
        or not transaction_id
        or transaction_id != transaction_id.strip()
    ):
        raise ValueError("Missing or invalid transaction_id")
    if len(transaction_id.encode("utf-8")) > _ES_ID_MAX_BYTES:
        raise ValueError("transaction_id exceeds Elasticsearch _id limit")
    return transaction_id


def validate_date(value: Any, field: str, transaction_id: str) -> None:
    """Reject *value* unless it is a string in exact ``YYYY-MM-DD`` form."""
    if not isinstance(value, str):
        raise ValueError(f"{transaction_id}: {field} must be a string")
    if not _DATE_RE.fullmatch(value):
        raise ValueError(
            f"{transaction_id}: {field} must use exact YYYY-MM-DD format"
        )
    # Round-trip through date.fromisoformat to reject impossible dates
    # (e.g. 2026-02-31) that a regex alone would accept.
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError(
            f"{transaction_id}: {field} must use exact YYYY-MM-DD format"
        ) from None
    if parsed.isoformat() != value:
        raise ValueError(
            f"{transaction_id}: {field} must use exact YYYY-MM-DD format"
        )


def _validate_signed_amount_rules(doc: Dict[str, Any], transaction_id: str) -> None:
    """Enforce type-specific amount and signed_amount conventions.

    Rules (from the authoritative data contract):

    * ``amount`` is a non-negative integer for Income and Expense;
      zero is permitted.
    * ``amount`` must be strictly greater than zero for Transfer.
    * ``signed_amount`` may be zero for Income and Expense.
    * ``signed_amount`` must be strictly negative for Transfer
      (source-account perspective).
    * ``abs(signed_amount) == amount`` always.
    """
    amount = doc["amount"]
    signed = doc["signed_amount"]
    if abs(signed) != amount:
        raise ValueError(
            f"{transaction_id}: signed_amount magnitude must equal amount"
        )
    tx_type = doc["type"]
    if tx_type == "Income" and signed < 0:
        raise ValueError(
            f"{transaction_id}: Income requires a non-negative signed_amount "
            f"(got {signed})"
        )
    if tx_type == "Expense" and signed > 0:
        raise ValueError(
            f"{transaction_id}: Expense requires a non-positive signed_amount "
            f"(got {signed})"
        )
    if tx_type == "Transfer":
        if amount <= 0:
            raise ValueError(
                f"{transaction_id}: Transfer amount must be greater than zero"
            )
        if signed >= 0:
            raise ValueError(
                f"{transaction_id}: Transfer requires a negative signed_amount "
                f"(source-account perspective)"
            )


def validate_transaction(doc: Dict[str, Any]) -> None:
    """Validate a single transaction document against the full contract.

    Raises:
        ValueError: if any check fails.  Every message is prefixed with
            the ``transaction_id`` when available so operators can locate
            the offending record without the record's financial values.
    """
    # --- root shape -------------------------------------------------------
    if not isinstance(doc, dict):
        raise ValueError("Transaction document must be a JSON object")

    # --- field set: no missing, no extra ----------------------------------
    keys = set(doc)
    missing = REQUIRED_FIELDS - keys
    if missing:
        raise ValueError(f"Missing required field(s): {sorted(missing)}")
    extra = keys - REQUIRED_FIELDS
    if extra:
        raise ValueError(f"Extra field(s) not in contract: {sorted(extra)}")

    transaction_id: str = doc.get("transaction_id") or "<unknown>"

    # --- transaction_id ---------------------------------------------------
    tid = doc["transaction_id"]
    if not isinstance(tid, str) or not tid:
        raise ValueError("transaction_id must be a non-empty string")
    # Reject IDs that carry surrounding whitespace so that _id and
    # _source.transaction_id always agree exactly.
    if tid != tid.strip():
        raise ValueError(
            f"{transaction_id}: transaction_id must not contain "
            f"leading/trailing whitespace"
        )

    # --- string fields ----------------------------------------------------
    for field in (
        "category",
        "type",
        "parent_category",
        "source_account",
        "currency",
    ):
        val = doc[field]
        if not isinstance(val, str) or not val.strip():
            raise ValueError(
                f"{transaction_id}: {field} must be a non-empty string"
            )

    # Currency contract: dataset is IDR-only
    if doc["currency"].strip().upper() != "IDR":
        raise ValueError(f"{transaction_id}: currency must be IDR")

    # --- optional string: description (null allowed, non-string not) ------
    desc = doc["description"]
    if desc is not None and not isinstance(desc, str):
        raise ValueError(
            f"{transaction_id}: description must be a string or null"
        )

    # --- destination_account: nullable, type-dependent ---------------------
    dest = doc["destination_account"]
    if dest is not None and not isinstance(dest, str):
        raise ValueError(
            f"{transaction_id}: destination_account must be a string or null"
        )
    if doc["type"] == "Transfer":
        if not dest:
            raise ValueError(
                f"{transaction_id}: Transfer requires a "
                f"non-empty destination_account"
            )
        if dest == doc.get("source_account"):
            raise ValueError(
                f"{transaction_id}: destination_account must differ "
                f"from source_account"
            )
    else:
        # Non-Transfer (Income / Expense): destination_account must be null.
        # A populated value is rejected, not silently ignored.
        if dest is not None:
            raise ValueError(
                f"{transaction_id}: non-Transfer transaction must not have "
                f"a destination_account (got {dest!r})"
            )

    # --- dates: transaction_date (required) and pay_cycle (nullable) ------
    validate_date(doc["transaction_date"], "transaction_date", transaction_id)
    if doc["pay_cycle"] is not None:
        validate_date(doc["pay_cycle"], "pay_cycle", transaction_id)

    # --- money: amount / signed_amount ------------------------------------
    amount = doc["amount"]
    signed = doc["signed_amount"]
    if not isinstance(amount, int) or isinstance(amount, bool):
        raise ValueError(f"{transaction_id}: amount must be an integer")
    if not isinstance(signed, int) or isinstance(signed, bool):
        raise ValueError(f"{transaction_id}: signed_amount must be an integer")
    # Income/Expense: amount >= 0 (zero permitted).
    # Transfer: amount > 0 (checked in _validate_signed_amount_rules).
    if amount < 0:
        raise ValueError(
            f"{transaction_id}: amount must be non-negative (got {amount})"
        )

    # --- category semantics -----------------------------------------------
    if doc["category"] not in CATEGORY_MAPPING:
        raise ValueError(
            f"{transaction_id}: unknown category {doc['category']!r}"
        )
    allowed_types = CATEGORY_MAPPING[doc["category"]]
    if doc["type"] not in allowed_types:
        raise ValueError(
            f"{transaction_id}: category {doc['category']!r} "
            f"does not allow type {doc['type']!r}"
        )

    parent = doc["parent_category"]
    if parent not in PARENT_CATEGORY_MAP:
        raise ValueError(f"{transaction_id}: unknown parent_category {parent!r}")
    if doc["category"] not in PARENT_CATEGORY_MAP[parent]:
        raise ValueError(
            f"{transaction_id}: category {doc['category']!r} "
            f"does not belong to parent {parent!r}"
        )

    _validate_signed_amount_rules(doc, transaction_id)


# ---------------------------------------------------------------------------
# Elasticsearch Clients
# ---------------------------------------------------------------------------

def create_es_client() -> Optional[Elasticsearch]:
    """Create and verify a synchronous :class:`Elasticsearch` client.

    The client is always closed when the connection cannot be established,
    so no resource leaks regardless of where ``ping()`` fails.

    Returns:
        A connected ``Elasticsearch`` instance, or ``None`` when the
        connection could not be established.
    """
    client: Optional[Elasticsearch] = None
    try:
        options: Dict[str, Any] = {"hosts": [ES_HOST]}
        if ES_API_KEY:
            options["api_key"] = ES_API_KEY
        client = Elasticsearch(**options)
        if not client.ping():
            raise ConnectionError("Could not connect to Elasticsearch")
        return client
    except Exception as exc:
        if client is not None:
            client.close()
        logger.error(
            "Elasticsearch connection failed: %s",
            type(exc).__name__,
        )
        return None


@asynccontextmanager
async def get_async_es_client() -> AsyncGenerator[AsyncElasticsearch, None]:
    """Context manager that yields an :class:`AsyncElasticsearch` client.

    The client is closed when the context exits.  When ``ES_API_KEY`` is
    set it is passed to the constructor for API-key authentication.
    """
    options: Dict[str, Any] = {"hosts": [ES_HOST]}
    if ES_API_KEY:
        options["api_key"] = ES_API_KEY
    client = AsyncElasticsearch(**options)
    try:
        yield client
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# Bulk Action Generator
# ---------------------------------------------------------------------------

def generate_actions(filepath: str, index_name: str) -> Iterator[Dict[str, Any]]:
    """Yield bulk index actions after the **entire** file has been validated.

    Ingestion safety: validation of every record runs to completion before
    the first action is yielded.  One bad record aborts the whole batch --
    a partially ingested file is never produced.

    Args:
        filepath: Path to a JSON file containing a list of transaction dicts.
        index_name: Target Elasticsearch index.

    Yields:
        Action dictionaries suitable for ``elasticsearch.helpers.bulk``.

    Raises:
        ValueError: root JSON is not a list, file is empty, or any record
            fails :func:`validate_transaction` / :func:`generate_doc_id`.
    """
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise ValueError(
            f"Transaction JSON root must be a list, got {type(data).__name__}"
        )
    if not data:
        raise ValueError("Transaction JSON is empty; ingestion aborted")

    # Phase 1: validate every document before yielding a single action.
    for _i, doc in enumerate(data):
        validate_transaction(doc)

    # Phase 2: duplicate transaction_id check (catches _id collisions).
    seen: set = set()
    for doc in data:
        doc_id = generate_doc_id(doc)
        if doc_id in seen:
            raise ValueError(f"Duplicate transaction_id: {doc_id!r}")
        seen.add(doc_id)

    # Phase 3: all checks passed -- now safe to yield.
    for doc in data:
        yield {
            "_index": index_name,
            "_id": generate_doc_id(doc),
            "_source": doc,
        }


# ---------------------------------------------------------------------------
# Async Query Wrapper
# ---------------------------------------------------------------------------

async def query_elasticsearch(
    query: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Run an Elasticsearch search and return the response body.

    Financial data and query contents are **never** printed to logs.
    Errors are reported by type only -- no query body, no transaction
    values, no traceback.

    Args:
        query: A fully-formed Elasticsearch query body.

    Returns:
        The response ``body`` dict, or ``None`` on any error.
    """
    try:
        async with get_async_es_client() as client:
            response = await client.search(index=ES_INDEX, body=query)
            return response.body
    except Exception as exc:
        logger.error(
            "Elasticsearch query failed: %s",
            type(exc).__name__,
        )
        return None
