"""Safe staging-index ingestion for the financial-transactions pipeline.

This module never deletes or overwrites the index that is currently live.
Instead it:

1. Validates the entire JSON dataset *before* touching Elasticsearch.
2. Creates a brand-new versioned physical index (a "staging" index).
3. Bulk-indexes every validated document into the staging index.
4. Verifies the staging index (count + spot fetch + cluster health).
5. Atomically switches the ``financial-transactions-active`` alias from its
   previous target to the staging index in a single ``update_aliases`` call.
6. Performs a final verification of the alias target and document count.

If any step *before* the alias switch fails, the active alias is untouched
and the freshly created staging index is cleaned up.  The previous physical
index is never deleted by this module.

If the alias switch succeeds but final verification fails, both physical
indexes are preserved and :func:`rollback_alias` is available to atomically
move the alias back to the previous index.

Concurrency safety: a staging-name collision with any existing concrete
index aborts the run before creation; a concurrent run that created the
same staging name between our probe and creation is detected by the
``indices.exists`` guard in :func:`_create_staging_index`.

Usage::

    python -m financial_mcp.ingest
"""

import json
import logging
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Iterator, Literal

from elasticsearch import Elasticsearch, helpers
from elasticsearch import exceptions as es_exceptions

from financial_mcp.config import (
    DATA_FILE_PATH,
    ES_INDEX,
    LEGACY_ES_INDEX,
    generate_physical_index_name,
)
from financial_mcp.es_client import (
    INDEX_MAPPING,
    create_es_client,
    generate_actions,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Alias transition lock (cross-process serialization)
# ---------------------------------------------------------------------------
# The alias switch (update_aliases) is atomic within a single API call,
# but the *precondition check* and the *alias update* are two separate
# API calls.  A concurrent process could change the alias state between
# them.  A file lock serializes the entire check-then-update window
# across processes on the same host.
#
# Lock file location: a well-known path derived from ES_HOST.
# The lock is advisory (flock/mlock) -- a well-behaved process will
# acquire it; a misbehaving process that ignores the lock is NOT
# protected against.  This is a mitigation, not a guarantee.

def _alias_lock_path() -> str:
    """Return the absolute path of the alias lock file."""
    lock_dir = os.path.join(tempfile.gettempdir(), "financial-mcp")
    os.makedirs(lock_dir, exist_ok=True)
    return os.path.join(lock_dir, "alias.lock")

@contextmanager
def _alias_transition_lock() -> Iterator[None]:
    """Acquire an exclusive lock covering the alias check-then-update window.

    Uses ``fcntl.flock`` (POSIX) or ``msvcrt.locking`` (Windows) to
    serialize concurrent alias transitions on the same host.

    The lock is held for the duration of the ``with`` block.  It is
    released automatically when the block exits (including on exception).

    This mitigates the race between precondition check and alias update.
    It does NOT protect against a process that deliberately bypasses
    the lock -- that is an operational, not code, failure mode.
    """
    lock_path = _alias_lock_path()
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl  # type: ignore[import]
            fcntl.flock(fd, fcntl.LOCK_EX)  # type: ignore[attr-defined]
        yield
    finally:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl  # type: ignore[import]
            fcntl.flock(fd, fcntl.LOCK_UN)  # type: ignore[attr-defined]
        os.close(fd)


def _is_definitive_es_rejection(exc: BaseException) -> bool:
    """Return True when *exc* is a definitively safe-to-classify rejection.

    A definitive rejection means the server received the request and
    explicitly refused it, with a response that proves the alias update
    was **not** applied.  In elasticsearch-py 8.x these are specific
    4xx :class:`~elasticsearch.exceptions.ApiError` subclasses:

    - 400 (BadRequestError)
    - 403 (AuthorizationException)
    - 404 (NotFoundError)

    All other statuses are classified as **ambiguous** and the caller
    must treat the outcome as ``update_outcome_unknown``:

    - 408 (Request Timeout): the timeout may originate from a proxy or
      gateway in front of Elasticsearch; the update may have been applied
      before the timeout was returned.
    - 429 (Too Many Requests): the server may have applied the update
      before returning the rate-limit response.
    - 5xx (500–599): the server-side failure may have occurred after
      the alias update was applied.
    - Unknown status or missing metadata: fail safe as ambiguous.
    - Transport errors (timeout, connection reset, SSL error) and any
      non-Elasticsearch exception: the request may have reached the
      server and been applied before the client received the error.
    """
    if not isinstance(exc, es_exceptions.ApiError):
        return False
    status = getattr(getattr(exc, "meta", None), "status", None)
    return status in (400, 403, 404)


def _is_resource_already_exists(exc: es_exceptions.BadRequestError) -> bool:
    """Return True if *exc* is the specific ``resource_already_exists_exception``.

    In Elasticsearch 8.x the sync client wraps the HTTP 400 response in a
    ``BadRequestError``.  The parsed JSON body is available as ``exc.body``.
    The response structure is::

        {
            "error": {
                "type": "resource_already_exists_exception",
                "reason": "index [my-index] already exists",
                "root_cause": [...]
            },
            "status": 400
        }

    This function inspects ``exc.body["error"]["type"]`` for the exact
    string ``"resource_already_exists_exception"``.  No string matching
    against free-form reason text is used.

    Fails closed: if the response body has no structured error field, the
    function returns ``False`` and the caller re-raises the exception,
    treating it as a genuine creation failure (not an ownership conflict).
    """
    body: Any = getattr(exc, "body", None)
    if not isinstance(body, dict):
        return False
    error = body.get("error")
    if not isinstance(error, dict):
        return False
    error_type = error.get("type", "")
    return error_type == "resource_already_exists_exception"


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------
@dataclass
class IngestResult:
    """Structured outcome of an ingestion run.

    The object makes every stage's result explicit so callers can log or
    report without parsing free-form text.  A run is considered successful
    only when :attr:`success` is ``True`` -- which is only set after the
    final verification passes.
    """

    success: bool = False
    staging_index: str = ""
    previous_active: Optional[str] = None
    previous_index_kept: bool = False
    expected_docs: int = 0
    indexed_docs: int = 0
    bulk_failed: int = 0
    error: Optional[str] = None

    # Stages that ran cleanly.
    stages: List[str] = field(default_factory=list)


@dataclass
class SwitchOutcome:
    """Structured result of an alias-switch attempt.

    ``state`` captures *where* the attempt stopped:

    - ``"confirmed"``: update + postcondition both succeeded; the alias
      now targets the staging index.
    - ``"postcondition_failed"``: update succeeded but the postcondition
      read showed a target other than staging, or the postcondition read
      itself raised.  The alias may point to staging, to another index,
      or have been re-pointed by a third process.  The caller must
      **not** auto-delete the staging index.
    - ``"not_attempted"``: the initial alias read (``_resolve_alias_target``)
      raised, so ``update_aliases`` was never called.  The staging index
      was never referenced by the alias.  The caller *may* clean up the
      staging index.
    - ``"update_failed"``: ``update_aliases`` raised a **definitive
      Elasticsearch rejection** — an HTTP 400, 403, or 404
      :class:`~elasticsearch.exceptions.ApiError` subclass.  The
      server explicitly refused the request; the update was **not**
      applied server-side.  The caller *may* clean up the staging index.
    - ``"update_outcome_unknown"``: ``update_aliases`` raised an
      **ambiguous exception** — HTTP 408 (timeout), HTTP 429 (rate
      limit), any 5xx :class:`~elasticsearch.exceptions.ApiError`
      (the server may have applied the update before the error was
      returned), a :class:`~elasticsearch.exceptions.TransportError`
      subclass (connection timeout, reset, SSL error), or any non-ES
      exception.  The server *may* have applied the update before the
      client received the error.  The caller must **not** auto-delete
      the staging index.  The operator must inspect the alias manually.
    """
    state: Literal[
        "confirmed",
        "postcondition_failed",
        "not_attempted",
        "update_failed",
        "update_outcome_unknown",
    ]
    previous_active: Optional[str] = None
    detail: str = ""


# ---------------------------------------------------------------------------
# Step 1 -- Preflight
# ---------------------------------------------------------------------------
def preflight(data_path: str = DATA_FILE_PATH) -> int:
    """Validate the entire JSON dataset and return the expected document count.

    No Elasticsearch state is touched here.  A :class:`FileNotFoundError`,
    :class:`ValueError` (schema / duplicate / bad root), or :class:`json`
    error is raised and the caller is expected to abort before creating any
    index.
    """
    docs = _load_dataset(data_path)
    expected = len(docs)
    # Force the full validation pass: iterating the generator yields *zero*
    # items until every record has been validated; only then does the
    # generator start producing actions.  We break after the first action --
    # the validation work has already completed.
    for _action in generate_actions(data_path, "__preflight__"):
        break
    return expected


def _load_dataset(data_path: str) -> List[Dict[str, Any]]:
    """Load and top-level-shape-check the JSON dataset.

    Returns the parsed list (guaranteed non-empty).  Raises on any shape
    violation so that preflight never proceeds with a bad root.
    """
    with open(data_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(
            f"Transaction JSON root must be a list, got {type(data).__name__}"
        )
    if not data:
        raise ValueError("Transaction JSON is empty; ingestion aborted")
    for _i, doc in enumerate(data):
        if not isinstance(doc, dict):
            raise ValueError(
                f"Transaction entry at index {_i} is not a JSON object"
            )
    return data


# ---------------------------------------------------------------------------
# Step 2 -- Connect + state probe
# ---------------------------------------------------------------------------
def _resolve_alias_target(client: Elasticsearch) -> Optional[str]:
    """Return the physical index currently targeted by *alias*, or ``None``.

    The alias may target at most one index in this architecture.  If it
    targets two or more, the state is ambiguous and we raise :class:`RuntimeError`
    so the operator can fix it manually -- we never silently merge.
    """
    try:
        mapping = client.indices.get_alias(name=ES_INDEX)
    except es_exceptions.NotFoundError:
        return None

    if not mapping:
        return None
    # mapping: {physical_index_name: {"<alias>": {...}}}
    if len(mapping) > 1:
        raise RuntimeError(
            f"Alias {ES_INDEX!r} targets multiple indexes: {list(mapping)}; "
            "manual resolution required before ingestion"
        )
    only_key = next(iter(mapping))
    if ES_INDEX in mapping[only_key]:
        return only_key
    return None


def _existing_index_names(client: Elasticsearch) -> set:
    """Return the set of concrete index names in the cluster.

    Only concrete indexes appear here (aliases are resolved away by the
    ``get()`` API).  This set is used to prevent staging-name collisions
    with anything already in the cluster.
    """
    try:
        resp = client.indices.get(index="*")
        return set(resp.keys())
    except es_exceptions.NotFoundError:
        return set()


# ---------------------------------------------------------------------------
# Step 3 -- Create staging index
# ---------------------------------------------------------------------------
def _create_staging_index(client: Elasticsearch, staging: str) -> bool:
    """Create the staging index and report whether this process owns it.

    Returns:
        ``True``  -- this process successfully called ``indices.create`` and
            owns the index.  Safe to delete on cleanup.
        ``False`` -- the index already existed when we tried to create it
            (concurrent run).  This process did **not** create it and
            must never delete it.

    The ownership flag is only set after ``indices.create`` returns
    successfully.  A race between the ``exists`` probe and ``create`` is
    still possible (another process creates the same name in that window),
    but ``indices.create`` will raise ``ResourceAlreadyExistsError`` in that
    case, so the function will return ``False`` and the caller will never
    delete an index it did not create.
    """
    if client.indices.exists(index=staging):
        logger.warning(
            "Staging index %r already exists -- another ingestion may be in "
            "flight.  Aborting to protect concurrent runs.",
            staging,
        )
        return False

    try:
        client.indices.create(
            index=staging,
            settings=INDEX_MAPPING["settings"],
            mappings=INDEX_MAPPING["mappings"],
        )
    except es_exceptions.BadRequestError as exc:
        # ES 8.x raises HTTP 400 (BadRequestError) when the index already
        # exists.  The response body contains a structured error:
        #   { "error": { "type": "resource_already_exists_exception",
        #                 "reason": "index [x] already exists" } }
        # We must only treat this specific error type as an ownership
        # conflict.  Any other 400 error (invalid mapping, malformed
        # request, etc.) must propagate as a genuine failure.
        if _is_resource_already_exists(exc):
            logger.warning(
                "Staging index %r was created by a concurrent run between "
                "probe and create.  This process does NOT own it.",
                staging,
            )
            return False
        # Not an "already exists" error -- re-raise as a genuine failure
        raise

    logger.info("Created staging index %s", staging)
    return True


# ---------------------------------------------------------------------------
# Step 4 -- Bulk ingestion
# ---------------------------------------------------------------------------
def _bulk_ingest(client: Elasticsearch, staging: str, data_path: str,
                 expected: int) -> int:
    """Bulk-index all validated documents into *staging* and return the
    success count.

    Partial success is never treated as complete: any non-empty ``failed``
    list from :func:`helpers.bulk` raises, and the caller cleans up.
    """
    actions = generate_actions(data_path, staging)
    success, failed = helpers.bulk(client, actions)
    # ``failed`` is a list of ``{"index": {...error...}}`` dicts from
    # helpers.bulk; it can also be an int in edge cases (ES 8.x changed the
    # return type in some versions), so normalise both forms.
    failed_items = failed if isinstance(failed, list) else [failed] * int(failed)
    if failed_items:
        raise RuntimeError(
            f"bulk ingestion reported {len(failed_items)} failures "
            f"(success={success}). Aborting; staging index will be cleaned up."
        )
    if success != expected:
        raise RuntimeError(
            f"bulk indexed {success} docs, expected {expected}. Aborting."
        )
    return success


# ---------------------------------------------------------------------------
# Step 5 -- Staging verification
# ---------------------------------------------------------------------------
def _verify_staging(client: Elasticsearch, staging: str,
                    expected: int) -> None:
    """Verify the staging index before the alias switch.

    Acceptance conditions (all must hold):

    1. The index exists (``indices.get`` returns metadata for it).
    2. The index state is ``open`` (confirmed via ``cat.indices``;
       a closed index cannot serve queries).
    3. Document count equals *expected*.
    4. A spot-fetch returns exactly one document.
    5. Cluster health for the index: ``status`` is ``green`` or ``yellow``
       and ``unassigned_shards`` is 0.

    When any condition cannot be confirmed the function raises
    :class:`RuntimeError` and the caller cleans up the staging index.

    Note on index state:
        ``client.indices.get()`` returns metadata for both open AND closed
        indexes.  The ``cat.indices`` API exposes a per-index ``state``
        field (``open`` or ``close``) that is not available via
        ``indices.get``.  A closed index will pass the existence check but
        will fail ``count`` and ``search`` calls, so we reject it early
        with a clear error message rather than letting it fail silently.
    """
    client.indices.refresh(index=staging)

    # 1. Index must exist in the cluster (metadata is retrievable).
    metadata = client.indices.get(index=staging)
    if staging not in metadata:
        raise RuntimeError(
            f"staging index {staging!r} not found after creation -- "
            "refusing to activate"
        )

    # 2. Index must be open (a closed index cannot serve queries).
    #    The CAT indices API with format="json" returns a JSON *array* of
    #    index objects.  Each object has:
    #      - "index":  the index name
    #      - "status": "open" or "close"  (this is the index state)
    #      - "health": "green", "yellow", "red", or "unavailable"
    #
    #    Note: the field is called "status" in the JSON response, NOT
    #    "state" (the text/CSV format labels the column "status" too).
    #
    #    Fail-closed policy: ANY response shape we cannot parse with full
    #    confidence is treated as a verification failure.  We never default
    #    an unknown status to "open".
    cat_resp = client.cat.indices(index=staging, format="json")
    # The client returns an ObjectApiResponse; .body is the parsed JSON.
    # For format="json" the body is a *list* of index objects.
    cat_body: Any = getattr(cat_resp, "body", cat_resp)

    if not isinstance(cat_body, list):
        raise RuntimeError(
            f"cat.indices returned an unexpected response type "
            f"({type(cat_body).__name__}, expected list) for index {staging!r} "
            "-- failing closed; refusing to activate"
        )

    # Match entries by the "index" field (the index name).
    # An entry is a dict; malformed entries (non-dict) are rejected.
    matching: List[Dict[str, Any]] = []
    for entry in cat_body:
        if not isinstance(entry, dict):
            raise RuntimeError(
                f"cat.indices returned a malformed entry "
                f"({type(entry).__name__}) in the response for index "
                f"{staging!r} -- failing closed; refusing to activate"
            )
        if entry.get("index") == staging:
            matching.append(entry)

    if len(matching) == 0:
        raise RuntimeError(
            f"cat.indices returned no entry for staging index {staging!r} "
            "-- failing closed; refusing to activate"
        )
    if len(matching) > 1:
        raise RuntimeError(
            f"cat.indices returned {len(matching)} entries matching "
            f"staging index {staging!r} -- duplicate entries; failing "
            "closed; refusing to activate"
        )

    entry = matching[0]
    status = entry.get("status")
    if not isinstance(status, str) or status == "":
        raise RuntimeError(
            f"cat.indices entry for {staging!r} has no 'status' field or it "
            f"is not a string -- failing closed; refusing to activate"
        )
    if status != "open":
        raise RuntimeError(
            f"staging index {staging!r} is in status {status!r} "
            f"(expected 'open') -- refusing to activate a closed index"
        )

    # 3. Document count.
    count = client.count(index=staging)["count"]
    if count != expected:
        raise RuntimeError(
            f"staging count {count} != expected {expected} -- refusing to "
            "activate a partial index"
        )

    # 4. Spot-fetch a representative document to prove the data is queryable.
    sample = client.search(
        index=staging,
        body={"query": {"match_all": {}}, "size": 1},
    ).body
    if sample["hits"]["total"]["value"] != 1:
        raise RuntimeError(
            f"spot-fetch expected 1 doc, got {sample['hits']['total']['value']}"
        )

    # 5. Cluster health for the specific index.
    health = client.cluster.health(index=staging).body
    status = health.get("status", "unknown")
    unassigned = health.get("unassigned_shards", -1)
    if unassigned != 0:
        raise RuntimeError(
            f"staging index has {unassigned} unassigned shards -- "
            "refusing to activate an unhealthy index"
        )
    if status not in ("green", "yellow"):
        raise RuntimeError(
            f"staging index cluster health status is {status!r} "
            f"(expected green or yellow) -- refusing to activate"
        )

    logger.info("Staging index %s verified (%d docs, state=open)", staging, expected)


# ---------------------------------------------------------------------------
# Step 6 -- Atomic alias switch
# ---------------------------------------------------------------------------
def _atomic_alias_switch(
    client: Elasticsearch, staging: str,
) -> SwitchOutcome:
    """Attempt to switch the alias to *staging*.

    The complete check-then-update sequence is executed while holding the
    cross-process alias transition lock:

    1. Read the current alias target (``previous_active``).
       - If this raises (transient API error or multi-target RuntimeError),
         no update was attempted: returns ``state="not_attempted"``.
    2. Build the ``update_aliases`` actions.
    3. Call ``update_aliases``.
       - If this raises (any exception), the update did not complete:
         returns ``state="update_failed"``.
    4. Re-read the alias target and verify it now points to *staging*.
       - If it doesn't, the alias state is ambiguous:
         returns ``state="postcondition_failed"``.
       - If it does, the switch is confirmed:
         returns ``state="confirmed"``.

    The postcondition check is done *inside* the lock so that a concurrent
    process cannot change the alias between our update and our verification.

    ``remove`` and ``add`` are bundled into one request so Elasticsearch
    applies them atomically.  If the previous active index is the legacy
    concrete index, it is left in place -- the alias is removed from it but
    the index itself is not deleted.

    Note on lock scope:
        This is a *local file lock* that coordinates cooperating processes
        on the **same host** only.  It does NOT provide multi-host safety;
        a cluster spanning multiple machines would require a distributed
        lock (e.g. an ES-based mutex) which is out of scope for this
        single-node deployment.
    """
    with _alias_transition_lock():
        # 1. Read current alias target while holding the lock.
        try:
            previous_active = _resolve_alias_target(client)
        except Exception as exc:
            logger.warning(
                "Pre-switch alias read failed: %s -- update not attempted", exc,
            )
            return SwitchOutcome(
                state="not_attempted",
                previous_active=None,
                detail=str(exc),
            )

        # 2. Build the update actions
        actions: List[Dict[str, Any]] = []
        if previous_active:
            actions.append(
                {"remove": {"index": previous_active, "alias": ES_INDEX}}
            )
        actions.append({"add": {"index": staging, "alias": ES_INDEX}})

        # 3. Execute the atomic alias update
        try:
            client.indices.update_aliases(actions=actions)
        except Exception as exc:
            if _is_definitive_es_rejection(exc):
                # 400/403/404: the server explicitly rejected the request —
                # the update was NOT applied.  Safe to treat as a clean failure.
                logger.error(
                    "update_aliases definitively rejected: %s: %s",
                    type(exc).__name__, exc,
                )
                return SwitchOutcome(
                    state="update_failed",
                    previous_active=previous_active,
                    detail=f"definitive rejection: {exc}",
                )
            # 408, 429, 5xx ApiError, transport error, connection reset, or
            # non-ES exception: the request MAY have been applied
            # server-side.  We cannot confirm, so the staging index must
            # NOT be auto-deleted.
            logger.error(
                "update_aliases outcome UNKNOWN: %s: %s -- the request may "
                "have been applied before the client received the error. "
                "Staging index will be PRESERVED; inspect the alias manually.",
                type(exc).__name__, exc,
            )
            return SwitchOutcome(
                state="update_outcome_unknown",
                previous_active=previous_active,
                detail=f"outcome unknown: {type(exc).__name__}: {exc}",
            )

        # 4. Postcondition: verify the alias now targets staging
        try:
            post_target = _resolve_alias_target(client)
        except Exception as exc:
            # Transient API error during the postcondition read:
            # we know update_aliases succeeded but cannot confirm the
            # alias state -- treat as ambiguous, do NOT auto-delete staging.
            logger.warning(
                "Post-switch alias re-read failed: %s -- alias state ambiguous",
                exc,
            )
            return SwitchOutcome(
                state="postcondition_failed",
                previous_active=previous_active,
                detail=f"postcondition read raised {type(exc).__name__}: {exc}",
            )

        if post_target != staging:
            logger.warning(
                "Post-switch verification failed: alias %r targets %r, "
                "expected %r -- alias state ambiguous",
                ES_INDEX, post_target, staging,
            )
            return SwitchOutcome(
                state="postcondition_failed",
                previous_active=previous_active,
                detail=(
                    f"alias {ES_INDEX!r} targets {post_target!r}, "
                    f"expected {staging!r}"
                ),
            )

    logger.info(
        "Alias %s switched: %s -> %s",
        ES_INDEX, previous_active or "(none)", staging,
    )
    return SwitchOutcome(
        state="confirmed",
        previous_active=previous_active,
    )


# ---------------------------------------------------------------------------
# Step 7 -- Final verification
# ---------------------------------------------------------------------------
def _final_verify(client: Elasticsearch, staging: str, expected: int) -> None:
    target = _resolve_alias_target(client)
    if target != staging:
        raise RuntimeError(
            f"post-switch alias target is {target!r}, expected {staging!r}"
        )
    count = client.count(index=ES_INDEX)["count"]
    if count != expected:
        raise RuntimeError(
            f"alias count {count} != expected {expected} after switch"
        )


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------
def run_ingest(
    data_path: str = DATA_FILE_PATH,
) -> IngestResult:
    """Execute the full staging ingestion workflow.

    The synchronous Elasticsearch client is always closed in a ``finally``
    block, on both success and failure.  No index or alias is modified on
    the failure path.
    """
    result = IngestResult()

    # --- Step 1: Preflight ---
    try:
        result.expected_docs = preflight(data_path)
        result.stages.append("preflight")
        logger.info("Preflight OK: %d docs ready", result.expected_docs)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        result.error = f"preflight failed: {exc}"
        logger.error(result.error)
        return result

    client: Optional[Elasticsearch] = create_es_client()
    if client is None:
        result.error = "could not connect to Elasticsearch"
        logger.error(result.error)
        return result

    staging = ""
    switch_succeeded = False
    owns_staging = False
    try:
        # --- Step 2: Connect + probe state ---
        # Read the current alias target for the name-collision check.
        # The authoritative previous-target value for result.previous_active
        # is set later, from the lock-protected read in Step 6.
        existing = _existing_index_names(client)

        # --- Step 3: Create staging index ---
        # owns_staging is True only when THIS process successfully called
        # indices.create.  If the index already existed (concurrent run),
        # owns_staging stays False and we will never delete it.
        staging = generate_physical_index_name(existing)
        owns_staging = _create_staging_index(client, staging)
        if not owns_staging:
            raise RuntimeError(
                f"staging index {staging!r} already existed -- concurrent "
                "run detected; aborting to protect both runs"
            )
        result.staging_index = staging
        result.stages.append("create_staging")

        # --- Step 4: Bulk ingest ---
        indexed = _bulk_ingest(client, staging, data_path, result.expected_docs)
        result.indexed_docs = indexed
        result.stages.append("bulk_ingest")

        # --- Step 5: Verify staging ---
        _verify_staging(client, staging, result.expected_docs)
        result.stages.append("verify_staging")

        # --- Step 6: Atomic alias switch ---
        # _atomic_alias_switch does the full critical section under the
        # lock: read previous target, update, and verify postcondition.
        # It returns a SwitchOutcome; run_ingest branches on outcome.state.
        outcome = _atomic_alias_switch(client, staging)
        result.previous_active = outcome.previous_active
        previous = outcome.previous_active

        if outcome.state == "confirmed":
            switch_succeeded = True
            result.stages.append("alias_switch")
            result.previous_index_kept = True

            # --- Step 7: Final verification ---
            _final_verify(client, staging, result.expected_docs)
            result.stages.append("final_verify")
            result.success = True
            logger.info(
                "Ingestion complete: alias %s -> %s (%d docs; previous=%s kept=%s)",
                ES_INDEX, staging, result.indexed_docs,
                previous or "(none)", result.previous_index_kept,
            )
            return result

        # outcome.state is one of: "not_attempted", "update_failed",
        # "update_outcome_unknown", "postcondition_failed"  -- switch was
        # NOT confirmed.
        #
        #   not_attempted:
        #     The alias read failed before update_aliases was ever called.
        #     The staging index was never referenced by the alias.
        #     Safe to delete the staging index we own.
        #   update_failed:
        #     update_aliases raised a DEFINITIVE Elasticsearch rejection
        #     (HTTP 400, 403, or 404 from the server).  The update was
        #     NOT applied.  Safe to delete the staging index we own.
        #   update_outcome_unknown:
        #     update_aliases raised an AMBIGUOUS exception: HTTP 408
        #     (timeout), HTTP 429 (rate limit), any 5xx ApiError, a
        #     TRANSPORT error (connection reset, SSL error), or a
        #     non-ES exception.  The request MAY have reached the
        #     server and been applied.  The alias may now target the
        #     staging index.  We must NOT delete it.
        #   postcondition_failed:
        #     update_aliases succeeded but the postcondition read showed a
        #     target other than staging (or the read itself raised).
        #     The alias may or may not point to staging.  We must NOT
        #     delete it.
        can_cleanup_staging = outcome.state in ("not_attempted", "update_failed")

        if can_cleanup_staging and owns_staging and staging and staging != LEGACY_ES_INDEX:
            _safe_delete(client, staging, is_own_staging=True)
            logger.info(
                "Cleaned up staging index %s; active alias untouched",
                staging,
            )
        else:
            logger.info(
                "NOT deleting staging index %s -- alias state is ambiguous "
                "(outcome=%s); the index may be referenced by the alias. "
                "Inspect manually before deleting.",
                staging or "(not created)", outcome.state,
            )

        preserved_reason = {
            "update_outcome_unknown": (
                "update outcome unknown -- transport error; the request "
                "may have been applied server-side"
            ),
            "postcondition_failed": (
                "alias state ambiguous -- postcondition check failed"
            ),
        }.get(outcome.state, "state unknown")

        result.error = (
            f"alias switch state={outcome.state} "
            f"(previous={previous!r}, detail={outcome.detail!r}). "
            f"Staging index {staging!r} "
            f"{'deleted' if can_cleanup_staging else f'PRESERVED ({preserved_reason})'}; "
            f"previous index {previous!r} preserved. "
            f"Inspect the alias before deleting the staging index. "
            f"Use rollback_alias() to restore the alias if needed."
        )
        result.previous_index_kept = True
        logger.error(result.error)
        return result
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        logger.exception("Ingestion aborted")

        if switch_succeeded:
            # The alias switch has already been confirmed (state="confirmed"),
            # so both the previous and new indexes exist.
            # We do NOT delete either one.
            result.previous_index_kept = True
            try:
                current = _resolve_alias_target(client)
            except Exception:
                current = None

            if current != staging:
                result.error = (
                    f"{result.error} -- alias {ES_INDEX!r} targets "
                    f"{current!r}, not the staging index {staging!r}. "
                    f"Use rollback_alias() to restore the previous index "
                    f"if needed. Both indexes are preserved."
                )
            else:
                result.error = (
                    f"{result.error} -- alias {ES_INDEX!r} still targets "
                    f"the new staging index {staging!r}. To roll back, run "
                    f"rollback_alias() with previous_index="
                    f"{previous!r}. Both indexes are preserved; no automatic "
                    f"deletion is performed."
                )
            logger.error(
                "Post-switch verification failed; alias targets %r. "
                "Previous index %s is preserved for rollback.",
                current, previous or "(none)",
            )
        else:
            # Failure BEFORE the alias switch (preflight, create, bulk,
            # verify_staging, or the switch attempt itself failed before
            # update was confirmed).  Clean up the staging index only when
            # this process owns it AND the alias state is unambiguous.
            result.previous_index_kept = True
            if owns_staging and staging and staging != LEGACY_ES_INDEX:
                _safe_delete(client, staging, is_own_staging=True)
                logger.info(
                    "Cleaned up staging index %s; active alias untouched",
                    staging,
                )
            else:
                logger.info(
                    "Not deleting staging index %s -- not owned by this "
                    "process (concurrent run or creation failure)",
                    staging or "(not created)",
                )
        return result
    finally:
        client.close()


def rollback_alias(
    client: Elasticsearch,
    new_index: str,
    previous_index: Optional[str],
) -> None:
    """Atomically move the ``financial-transactions-active`` alias back to
    *previous_index* after a failed ingestion.

    Precondition checks (all must pass before the alias is moved):

    1. ``previous_index`` is not ``None`` (there is something to restore).
    2. ``new_index`` and ``previous_index`` are different (no-op guard).
    3. Both physical indexes exist in the cluster.
    4. The alias currently targets ``new_index`` (the expected state).

    Postcondition:

    - The alias targets ``previous_index`` after the atomic switch.
    - Neither physical index is ever deleted.

    Args:
        client: A connected Elasticsearch client.
        new_index: The staging index that the alias currently (or
            incorrectly) points to.
        previous_index: The physical index to restore the alias to, or
            ``None`` if there was no previous index (first ingestion).

    Raises:
        RuntimeError: when any precondition check fails, when
            *previous_index* is ``None``, or the rollback operation fails.
    """
    # --- Precondition 1: must have a previous index to restore to ---
    if previous_index is None:
        raise RuntimeError(
            "No previous index to roll back to -- this was the first "
            "ingestion. The alias currently targets the new index; "
            "verify it before deleting."
        )

    # --- Precondition 2: new and previous must differ (no-op guard) ---
    if new_index == previous_index:
        raise RuntimeError(
            f"new_index and previous_index are both {new_index!r} -- "
            "nothing to roll back; alias already targets the correct index."
        )

    # --- Precondition 3–5 under cross-process lock ---
    # The lock serialises the check-then-update window so that two
    # concurrent rollback_alias calls (or a rollback racing with
    # _atomic_alias_switch) cannot both succeed in moving the alias.
    with _alias_transition_lock():
        # --- Precondition 3: both indexes must exist ---
        existing = _existing_index_names(client)
        if new_index not in existing:
            raise RuntimeError(
                f"new_index {new_index!r} does not exist in the cluster -- "
                "cannot roll back to a missing index. "
                "The alias may have already been rolled back or the index was deleted."
            )
        if previous_index not in existing:
            raise RuntimeError(
                f"previous_index {previous_index!r} does not exist in the cluster -- "
                "cannot restore the alias to a missing index. "
                "Create the index manually or use a different rollback target."
            )

        # --- Precondition 4: alias must currently target new_index ---
        current_target = _resolve_alias_target(client)
        if current_target != new_index:
            raise RuntimeError(
                f"Alias {ES_INDEX!r} currently targets {current_target!r}, "
                f"not the expected {new_index!r}.  Refusing to roll back in an "
                "unexpected state -- inspect the alias manually before proceeding."
            )

        # --- Execute the atomic alias move ---
        actions: List[Dict[str, Any]] = [
            {"remove": {"index": new_index, "alias": ES_INDEX}},
            {"add": {"index": previous_index, "alias": ES_INDEX}},
        ]
        client.indices.update_aliases(actions=actions)

        # --- Postcondition: verify the alias now targets previous_index ---
        post_target = _resolve_alias_target(client)
        if post_target != previous_index:
            raise RuntimeError(
                f"Post-rollback verification failed: alias {ES_INDEX!r} targets "
                f"{post_target!r}, expected {previous_index!r}. "
                "Both indexes are preserved; investigate manually."
            )

    logger.info(
        "Rollback: alias %s moved back to %s (new index %s preserved)",
        ES_INDEX, previous_index, new_index,
    )


def _safe_delete(client: Elasticsearch, index: str,
                 is_own_staging: bool = True) -> None:
    """Delete a newly-created staging index if it still exists.

    Only called on the specific index *this* run created (the caller
    passes ``is_own_staging=True`` for that case).  A concurrent run that
    created an index with the same name *after* our probe is protected by
    the ``_create_staging_index`` exists-check; this function never deletes
    an index it did not create.

    A deletion failure is logged but not re-raised: the purpose is cleanup
    of a *newly-created* staging index, and a stale empty index is far less
    dangerous than an unhandled exception here.
    """
    if not is_own_staging:
        logger.warning(
            "Refusing to delete %s: not this run's staging index", index,
        )
        return
    try:
        if client.indices.exists(index=index):
            client.indices.delete(index=index)
    except es_exceptions.NotFoundError:
        pass
    except Exception:
        logger.exception(
            "Could not clean up staging index %s -- leaving it in place",
            index,
        )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
def main() -> None:
    logging.basicConfig(level=logging.INFO)
    result = run_ingest()
    if result.success:
        print(
            f"OK  alias={ES_INDEX} "
            f"staging={result.staging_index} "
            f"previous={result.previous_active or '(none)'} "
            f"docs={result.indexed_docs}"
        )
    else:
        print(f"FAIL {result.error}")
        # Exit non-zero so CI / operators notice.
        raise SystemExit(1)


if __name__ == "__main__":
    main()
