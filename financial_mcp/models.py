"""Pydantic models used by the MCP tool signatures."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, field_validator, model_validator


class QueryTransactionParams(BaseModel):
    """Parameters accepted by :func:`query_transactions` and
    :func:`get_spending_by_category`.

    Attributes:
        start_date: Inclusive start date in ``YYYY-MM-DD`` format.
        end_date: Inclusive end date in ``YYYY-MM-DD`` format.
        type: Transaction type -- ``"Expense"``, ``"Income"``, or
            ``"Transfer"``.
        category: Category filter, e.g. ``"Food & Drink"``, ``"Loan"``.
        source_account: Source / account filter, e.g. ``"BNI"``.
        source_name: Legacy alias for :attr:`source_account`.  Kept so
            existing MCP clients that still pass ``source_name`` do not
            break; a :meth:`model_validator` folds the two together
            (``source_account`` wins when both are supplied).
        aggregation: Group results by ``"daily"``, ``"weekly"``, or
            ``"monthly"``.
        limit: Maximum number of raw records to return (default 50).
    """

    start_date: Optional[str] = None
    end_date: Optional[str] = None
    type: Optional[str] = None
    category: Optional[str] = None
    source_account: Optional[str] = None
    # Legacy alias kept for backwards compatibility with existing MCP
    # clients that were written against the old parameter name.
    source_name: Optional[str] = None
    aggregation: Optional[str] = None
    limit: Optional[int] = 50

    @model_validator(mode="after")
    def _fold_source_name(self) -> "QueryTransactionParams":
        if self.source_name and not self.source_account:
            self.source_account = self.source_name
            self.source_name = None
        return self

    @property
    def effective_source_account(self) -> Optional[str]:
        """The source-account filter to actually query on."""
        return self.source_account

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
        """Ensure ``type`` is one of the canonical values when provided."""
        valid = ("Expense", "Income", "Transfer")
        if v is not None and v not in valid:
            raise ValueError(f"type must be one of: {valid}")
        return v

    @field_validator("aggregation")
    @classmethod
    def validate_aggregation(cls, v: Optional[str]) -> Optional[str]:
        """Ensure ``aggregation`` is one of the supported intervals."""
        valid = ("daily", "weekly", "monthly", None)
        if v not in valid:
            raise ValueError(f"aggregation must be one of: {valid[:-1]}")
        return v
