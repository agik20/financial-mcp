"""Pydantic models used by the MCP tool signatures."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, field_validator


class QueryTransactionParams(BaseModel):
    """Parameters accepted by :func:`query_transactions` and
    :func:`get_spending_by_category`.

    Attributes:
        start_date: Inclusive start date in ``YYYY-MM-DD`` format.
        end_date: Inclusive end date in ``YYYY-MM-DD`` format.
        type: Transaction type -- ``"Expense"`` or ``"Income"``.
        category: Category filter, e.g. ``"Food & Drink"``, ``"Loan"``.
        source_name: Source / account filter, e.g. ``"BNI"``.
        aggregation: Group results by ``"daily"``, ``"weekly"``, or
            ``"monthly"``.
        limit: Maximum number of raw records to return (default 50).
    """

    start_date: Optional[str] = None
    end_date: Optional[str] = None
    type: Optional[str] = None
    category: Optional[str] = None
    source_name: Optional[str] = None
    aggregation: Optional[str] = None
    limit: Optional[int] = 50

    @field_validator("start_date", "end_date")
    @classmethod
    def validate_date(cls, v: Optional[str]) -> Optional[str]:
        """Ensure dates are in ``YYYY-MM-DD`` format."""
        if v is None:
            return v
        try:
            datetime.strptime(v, "%Y-%m-%d")
            return v
        except ValueError:
            raise ValueError("Invalid date format. Use YYYY-MM-DD")

    @field_validator("type")
    @classmethod
    def validate_type(cls, v: Optional[str]) -> Optional[str]:
        """Ensure ``type`` is ``Expense`` or ``Income`` when provided."""
        if v is not None and v not in ("Expense", "Income"):
            raise ValueError("type must be 'Expense' or 'Income'")
        return v

    @field_validator("aggregation")
    @classmethod
    def validate_aggregation(cls, v: Optional[str]) -> Optional[str]:
        """Ensure ``aggregation`` is one of the supported intervals."""
        valid = ("daily", "weekly", "monthly", None)
        if v not in valid:
            raise ValueError(f"aggregation must be one of: {valid[:-1]}")
        return v
