"""Convert the personal-finance CSV file to JSON.

Reads ``Personal Finance  - Transactions.csv`` (double space in the
filename is intentional), transforms each row into the schema expected
by Elasticsearch, and writes the result to
``Personal Finance  - Transactions.json``.

Usage::

    python -m financial_mcp.converter
"""

import json
from datetime import datetime
from typing import Any, Dict, Optional

import pandas as pd

from financial_mcp.config import DATA_CSV_PATH, DATA_FILE_PATH

# Support Excel as an alternative input
SHEET_NAME: int = 0  # for .xlsx


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def clean_currency(value: Any) -> Optional[int]:
    """Strip currency formatting and return an integer.

    Args:
        value: Raw cell value, e.g. ``"Rp 1.234,56"``.

    Returns:
        The integer value, or ``None`` when the cell is missing / NaN.
    """
    if pd.isna(value):
        return None
    return int(
        str(value)
        .replace("Rp", "")
        .replace(",", "")
        .strip()
    )


def parse_date(date_val: Any) -> Optional[pd.Timestamp]:
    """Parse a date cell into a :class:`pandas.Timestamp`.

    Args:
        date_val: Raw cell value.

    Returns:
        A ``pd.Timestamp``, or ``None`` when the value is NaN.
    """
    if pd.isna(date_val):
        return None
    return pd.to_datetime(date_val)


def transform_row(row: pd.Series) -> Dict[str, Any]:
    """Transform a single CSV row into the Elasticsearch document schema.

    Args:
        row: A pandas Series representing one row of the input file.

    Returns:
        A dictionary with keys ``type``, ``category``, ``sourceName``,
        ``unit``, ``value``, ``balance``, ``description``, and the
        time-related fields.
    """
    date = parse_date(row["Date"])

    return {
        "type": row.get("Type"),
        "category": row.get("Category"),
        "parent-category": row.get("Parent"),
        "sourceName": row.get("Account"),
        "unit": "IDR",
        "value": clean_currency(row.get("Signed Amount")),
        "description": row.get("Detail Transaction"),
        # Time fields
        "creationDate": (
            date.isoformat() if date is not None else None
        ),
        "day": (
            date.strftime("%Y-%m-%d") if date is not None else None
        ),
        "dayOfWeek": (
            date.strftime("%A") if date is not None else None
        ),
        "hour": date.hour if date is not None else 0,
        # Custom
        "payCycle": (
            pd.to_datetime(row.get("Pay Cycle")).strftime("%Y-%m-%d")
            if not pd.isna(row.get("Pay Cycle"))
            else None
        ),
    }


# ---------------------------------------------------------------------------
# Main conversion
# ---------------------------------------------------------------------------
def convert_csv_to_json() -> None:
    """Read the CSV (or Excel) file and write the transformed JSON.

    The input file is selected from :data:`DATA_CSV_PATH` (config).
    If the file ends with ``.xlsx`` it is read with
    :func:`pandas.read_excel`; otherwise with :func:`pandas.read_csv`.

    Rows that raise an exception during transformation are logged and
    skipped.
    """
    input_file = DATA_CSV_PATH
    output_file = DATA_FILE_PATH

    # -- Load data -----------------------------------------------------------
    if input_file.endswith(".csv"):
        df = pd.read_csv(input_file)
    else:
        df = pd.read_excel(input_file, sheet_name=SHEET_NAME)

    # -- Transform -----------------------------------------------------------
    records: list[Dict[str, Any]] = []
    for _, row in df.iterrows():
        try:
            transformed = transform_row(row)
            records.append(transformed)
        except Exception as e:
            print(f"Error processing row: {row}")
            print(e)

    # -- Save ----------------------------------------------------------------
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2)

    print(f"Done. Output saved to {output_file}")


if __name__ == "__main__":
    convert_csv_to_json()
