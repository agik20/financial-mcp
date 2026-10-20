"""Centralised configuration constants for the financial-mcp package.

All Elasticsearch connection settings, index names, and data file paths
are defined here so that other modules import a single source of truth.
"""

import os
from typing import Optional

# --- Elasticsearch -----------------------------------------------------------
ES_HOST: str = os.getenv("ES_HOST", "http://localhost:9200")
ES_INDEX: str = "financial-transactions"
ES_API_KEY: Optional[str] = os.getenv("ES_API_KEY")

# --- Data files ---------------------------------------------------------------
# NOTE: the filenames intentionally contain a *double* space
#       ("Finance  -") -- do NOT "fix" this.
_PACKAGE_DIR: str = os.path.dirname(__file__)

_DATA_DIR: str = os.path.normpath(os.path.join(_PACKAGE_DIR, "..", "data"))

DATA_FILE_PATH: str = os.path.join(
    _DATA_DIR, "Personal Finance  - Transactions.json",
)
DATA_CSV_PATH: str = os.path.join(
    _DATA_DIR, "Personal Finance  - Transactions.csv",
)
