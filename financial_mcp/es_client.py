
import json
import logging
from contextlib import asynccontextmanager
from datetime import date
from typing import Any, AsyncGenerator, Dict, Iterator, Optional

from elasticsearch import AsyncElasticsearch, Elasticsearch

from financial_mcp.config import ES_API_KEY, ES_HOST, ES_INDEX

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# F-007: sanitized Elasticsearch error classification
# ---------------------------------------------------------------------------

ES_REASONS = (
    "es_connection",
    "es_auth",
    "es_validation",
    "es_timeout",
    "es_not_found",
    "es_dependency",
    "es_unknown",
)

_ES_REASON_MESSAGES = {
    "es_connection": "Elasticsearch connection unavailable",
    "es_auth": "Elasticsearch authentication or authorization failed",
    "es_validation": "Elasticsearch rejected the query",
    "es_timeout": "Elasticsearch request timed out",
    "es_not_found": "Elasticsearch resource not found",
    "es_dependency": "An Elasticsearch client dependency is unavailable",
    "es_unknown": "Elasticsearch query failed for an unknown reason",
}


class ElasticsearchQueryError(Exception):
    """A sanitized Elasticsearch failure carrying only a static reason."""

    def __init__(self, message: str, *, reason: str = "es_unknown"):
        super().__init__(message)
        self.message = message
        self.reason = reason


def es_error_payload(reason: str) -> Dict[str, str]:
    """Static error envelope -- never the underlying exception text."""
    return {"error": _ES_REASON_MESSAGES[reason], "reason": reason}


def classify_es_failure(exc: BaseException) -> str:
    """Map the installed client's exception hierarchy onto ES_REASONS."""
    from elasticsearch import (
        ApiError,
        AuthenticationException,
        AuthorizationException,
        ConnectionError as EsConnectionError,
        TransportError,
    )

    from elastic_transport import ConnectionTimeout

    if isinstance(exc, (AuthenticationException, AuthorizationException)):
        return "es_auth"
    if isinstance(exc, ConnectionTimeout):
        return "es_timeout"
    if isinstance(exc, ApiError):
        status = getattr(exc, "meta", None)
        status = getattr(status, "status", None)
        if status == 404:
            return "es_not_found"
        if status in (400, 409):
            return "es_validation"
        return "es_unknown"
    if isinstance(exc, EsConnectionError):
        return "es_connection"
    if isinstance(exc, (ImportError, ModuleNotFoundError)):
        return "es_dependency"
    if isinstance(exc, TransportError):
        # The transport wraps lower-level failures: a timeout raised inside
        # a transport error must still classify as es_timeout.
        for wrapped in (exc.__cause__, exc.__context__):
            if wrapped is not None:
                sub = classify_es_failure(wrapped)
                if sub not in ("es_unknown", "es_dependency"):
                    return sub
        return "es_connection"
    return "es_unknown"


# ---------------------------------------------------------------------------
# Canonical transaction schema
# ---------------------------------------------------------------------------

CATEGORY_MAPPING = {
    # Living
    "Food & Drink": ("Expense",),
    "Housing": ("Expense",),
    "Utilities": ("Expense",),
    "Household": ("Expense",),
    "Transport": ("Expense",),
    "Personal Care": ("Expense",),

    # Lifestyle
    "Eating Out / Snacks": ("Expense",),
    "Entertainment": ("Expense",),
    "Shopping": ("Expense",),
    "Subscription": ("Expense",),
    "Other Outcome": ("Expense",),

    # Financial
    "Loan": ("Expense",),
    "Bank Fee": ("Expense",),
    "Investment": ("Expense",),
    "Insurance": ("Expense",),
    "Tax": ("Expense",),
    "Salary": ("Income",),
    "Freelance": ("Income",),
    "Investment Return": ("Income",),
    "Other Income": ("Income",),

    # Internal
    "Bank Transfer": ("Transfer",),
}

PARENT_CATEGORY_MAP = {
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

REQUIRED_FIELDS = {
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

ALLOWED_CURRENCIES = {"IDR"}


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def validate_date(value: Any, field_name: str, transaction_id: str = "") -> None:
    """Validate strict ISO date format: YYYY-MM-DD."""

    prefix = f"{transaction_id}: " if transaction_id else ""

    if not isinstance(value, str):
        raise ValueError(f"{prefix}{field_name} must be a string")

    try:
        parsed_date = date.fromisoformat(value)
    except ValueError:
        raise ValueError(
            f"{prefix}{field_name} must use YYYY-MM-DD format"
        ) from None

    if parsed_date.isoformat() != value:
        raise ValueError(
            f"{prefix}{field_name} must use strict YYYY-MM-DD format"
        )


def generate_doc_id(doc: Dict[str, Any]) -> str:
    transaction_id = doc.get("transaction_id")

    if not isinstance(transaction_id, str) or not transaction_id:
        raise ValueError("Missing or invalid transaction_id")

    if transaction_id != transaction_id.strip():
        raise ValueError("Missing or invalid transaction_id")

    if len(transaction_id.encode("utf-8")) > 512:
        raise ValueError(
            "transaction_id exceeds Elasticsearch _id limit"
        )

    return transaction_id


def validate_transaction(doc: Dict[str, Any]) -> None:
    """Validate one transaction against the canonical data contract.

    Every error message is prefixed with the ``transaction_id`` so an
    operator ingesting a large file can locate the offending record.
    """

    if not isinstance(doc, dict):
        raise ValueError("Transaction must be a JSON object")

    transaction_id = doc.get("transaction_id")
    if not isinstance(transaction_id, str) or not transaction_id:
        transaction_id = "<missing transaction_id>"
    tid = f"{transaction_id}: "

    # --- structural: required / extra fields ---
    for field in sorted(REQUIRED_FIELDS):
        if field not in doc:
            raise ValueError(f"{tid}Missing required field '{field}'")

    for field in sorted(doc.keys() - REQUIRED_FIELDS):
        raise ValueError(f"{tid}Extra field '{field}' is not in the schema")

    transaction_id = generate_doc_id(doc)

    # --- dates ---
    validate_date(doc["transaction_date"], "transaction_date", transaction_id)
    if doc["pay_cycle"] is not None:
        validate_date(doc["pay_cycle"], "pay_cycle", transaction_id)

    # --- category / type / parent ---
    category = doc["category"]
    transaction_type = doc["type"]
    parent_category = doc["parent_category"]

    if not isinstance(category, str) or not category.strip():
        raise ValueError(f"{tid}invalid category")

    if not isinstance(transaction_type, str) or not transaction_type.strip():
        raise ValueError(f"{tid}invalid type")

    if not isinstance(parent_category, str) or not parent_category.strip():
        raise ValueError(f"{tid}invalid parent_category")

    if category not in CATEGORY_MAPPING:
        raise ValueError(f"{tid}unknown category '{category}'")

    if transaction_type not in CATEGORY_MAPPING[category]:
        raise ValueError(
            f"{tid}category '{category}' does not allow type "
            f"'{transaction_type}'"
        )

    if parent_category not in PARENT_CATEGORY_MAP:
        raise ValueError(
            f"{tid}unknown parent category '{parent_category}'"
        )

    if category not in PARENT_CATEGORY_MAP[parent_category]:
        raise ValueError(
            f"{tid}category '{category}' does not belong to parent "
            f"'{parent_category}'"
        )

    # --- accounts ---
    source_account = doc["source_account"]
    destination_account = doc["destination_account"]

    if not isinstance(source_account, str) or not source_account.strip():
        raise ValueError(
            f"{tid}source_account must be a non-empty string"
        )

    # --- amount / signed_amount ---
    amount = doc["amount"]
    signed_amount = doc["signed_amount"]

    if isinstance(amount, bool) or not isinstance(amount, int):
        raise ValueError(f"{tid}amount must be an integer")

    if amount < 0:
        raise ValueError(f"{tid}amount must be non-negative")

    if isinstance(signed_amount, bool) or not isinstance(signed_amount, int):
        raise ValueError(f"{tid}signed_amount must be an integer")

    if abs(signed_amount) != amount:
        raise ValueError(
            f"{tid}signed_amount magnitude must equal amount"
        )

    # description may be a string or None; every other type is rejected.
    description = doc["description"]
    if description is not None and not isinstance(description, str):
        raise ValueError(f"{tid}description must be a string or null")

    # --- currency ---
    if doc["currency"] not in ALLOWED_CURRENCIES:
        raise ValueError(f"{tid}currency must be IDR")

    # --- type-specific rules ---
    if transaction_type != "Transfer" and destination_account is not None:
        raise ValueError(
            f"{tid}non-Transfer transaction must not have destination_account"
        )

    if transaction_type == "Income":
        if signed_amount < 0:
            raise ValueError(
                f"{tid}Income requires a non-negative signed_amount"
            )

    elif transaction_type == "Expense":
        if signed_amount > 0:
            raise ValueError(
                f"{tid}Expense requires a non-positive signed_amount"
            )

    elif transaction_type == "Transfer":
        if amount == 0:
            raise ValueError(
                f"{tid}Transfer amount must be greater than zero"
            )
        if signed_amount >= 0:
            raise ValueError(
                f"{tid}Transfer requires a negative signed_amount"
            )
        if (
            not isinstance(destination_account, str)
            or not destination_account.strip()
        ):
            raise ValueError(
                f"{tid}Transfer requires a non-empty destination_account"
            )
        if source_account == destination_account:
            raise ValueError(
                f"{tid}destination_account must differ from source_account"
            )


# ---------------------------------------------------------------------------
# Synchronous client (ingest / CLI scripts)
# ---------------------------------------------------------------------------

def create_es_client() -> Optional[Elasticsearch]:
    client = None

    try:
        if ES_API_KEY:
            client = Elasticsearch(
                hosts=[ES_HOST],
                api_key=ES_API_KEY,
            )
        else:
            client = Elasticsearch(hosts=[ES_HOST])

        if not client.ping():
            raise ConnectionError("Could not connect to Elasticsearch")

        logger.info("Elasticsearch connection successful")
        return client

    except Exception as exc:
        logger.error(
            "Elasticsearch connection failed (%s)",
            type(exc).__name__,
        )

        if client is not None:
            try:
                client.close()
            except Exception:
                logger.warning("Failed to close Elasticsearch client")

        return None


# ---------------------------------------------------------------------------
# Asynchronous client (MCP tools)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def get_async_es_client() -> AsyncGenerator[AsyncElasticsearch, None]:
    options: Dict[str, Any] = {"hosts": [ES_HOST]}
    if ES_API_KEY:
        options["api_key"] = ES_API_KEY
    client = AsyncElasticsearch(**options)

    try:
        yield client
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# Bulk-action generator
# ---------------------------------------------------------------------------

def generate_actions(
    filepath: str,
    index_name: str,
) -> Iterator[Dict[str, Any]]:

    with open(filepath, "r", encoding="utf-8") as file:
        data = json.load(file)

    if not isinstance(data, list):
        raise ValueError("JSON root must be a list of transactions")

    if not data:
        raise ValueError("Transaction dataset is empty")

    seen_ids = set()
    validated_documents = []

    # Phase 1: Validate the entire dataset before yielding anything.
    for row_number, doc in enumerate(data, start=1):
        if not isinstance(doc, dict):
            raise ValueError(
                f"Invalid transaction at row {row_number}: expected object"
            )

        try:
            validate_transaction(doc)
            transaction_id = generate_doc_id(doc)

            if transaction_id in seen_ids:
                raise ValueError(
                    f"Duplicate transaction_id: {transaction_id}"
                )

            seen_ids.add(transaction_id)
            validated_documents.append(doc)

        except ValueError as exc:
            raise ValueError(
                f"Validation failed at row {row_number}: {exc}"
            ) from exc

    # Phase 2: Yield actions only after every document is valid.
    for doc in validated_documents:
        yield {
            "_index": index_name,
            "_id": doc["transaction_id"],
            "_source": doc,
        }


# ---------------------------------------------------------------------------
# Async query wrapper (used by MCP tool functions)
# ---------------------------------------------------------------------------

async def query_elasticsearch(
    query: Dict[str, Any],
) -> Dict[str, Any]:
    """Run a search and return the response body.

    Any failure raises :class:`ElasticsearchQueryError` with a sanitized
    static message -- the underlying exception text (URLs, keys, query
    contents) never reaches the caller.  The original exception is kept as
    ``__cause__`` for in-process debugging only.
    """
    try:
        async with get_async_es_client() as client:
            response = await client.search(
                index=ES_INDEX,
                body=query,
            )
            return response.body

    except Exception as exc:
        reason = classify_es_failure(exc)
        logger.error(
            "Elasticsearch query failed (reason=%s)",
            reason,
        )
        raise ElasticsearchQueryError(
            es_error_payload(reason)["error"],
            reason=reason,
        ) from exc
