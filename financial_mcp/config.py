"""Centralised configuration constants for the financial-mcp package.

All Elasticsearch connection settings, index names, and data file paths
are defined here so that other modules import a single source of truth.
"""

import os
import time
from typing import Optional

# --- Elasticsearch -----------------------------------------------------------
ES_HOST: str = os.getenv("ES_HOST", "http://localhost:9200")
ES_API_KEY: Optional[str] = os.getenv("ES_API_KEY")

# The application queries the stable alias below -- never a physical index.
ES_INDEX: str = "financial-transactions-active"

# Versioned physical indexes are named with the prefix below.
ES_INDEX_PREFIX: str = "financial-transactions"

# Legacy concrete index that predates the alias. It may hold data the owner
# wants to keep; ingestion never deletes it automatically.
LEGACY_ES_INDEX: str = "financial-transactions"


def generate_physical_index_name(
    existing: Optional[set] = None,
) -> str:
    """Return a deterministic, collision-resistant physical index name.

    The name is ``{prefix}-{YYYYMMDD-HHMMSS}-{seq}``.  The timestamp suffix
    means two ingestion runs started in the same second still get distinct
    names, and a set of already-known names (e.g. live indices or aliases) can
    be checked before creation so a stale name is never reused.

    Args:
        existing: Optional set of index names already in the cluster.  If the
            generated name collides with one of them, the sequence is bumped
            until it is unique.

    Returns:
        A physical index name that does not collide with *existing*.
    """
    base = f"{ES_INDEX_PREFIX}-{time.strftime('%Y%m%d-%H%M%S')}"
    existing = existing or set()
    candidate = base
    seq = 0
    while candidate in existing:
        seq += 1
        candidate = f"{base}-{seq:02d}"
    return candidate

# --- Data files ---------------------------------------------------------------
# NOTE: the filenames intentionally contain a *double* space
#       ("Finance  -") -- do NOT "fix" this.
_PACKAGE_DIR: str = os.path.dirname(__file__)

_DATA_DIR: str = os.path.normpath(os.path.join(_PACKAGE_DIR, "..", "data"))

DATA_FILE_PATH: str = os.path.join(
    _DATA_DIR, "Financ  - Transactions.json",
)
DATA_CSV_PATH: str = os.path.join(
    _DATA_DIR, "Financ - Transactions.csv",
)
