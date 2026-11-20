"""Tests for the prompt templates in financial_mcp.server."""

import pytest

from financial_mcp.server import monthly_report


class TestMonthlyReport:
    def test_invalid_month_raises(self):
        for bad in ("2026-13", "2026-00", "2026-32", "jan-26", "2026"):
            with pytest.raises(ValueError, match="YYYY-MM"):
                monthly_report(bad)

    def test_empty_month_uses_current_month(self):
        prompt = monthly_report()
        assert "bulan ini" in prompt
        assert "get_financial_summary" in prompt

    def test_valid_month_uses_correct_range(self):
        prompt = monthly_report("2026-02")
        assert "start_date='2026-02-01'" in prompt
        assert "end_date='2026-02-28'" in prompt

    def test_february_2024_leap(self):
        prompt = monthly_report("2024-02")
        assert "end_date='2024-02-29'" in prompt

    def test_april_30_day_month(self):
        prompt = monthly_report("2026-04")
        assert "end_date='2026-04-30'" in prompt

    def test_december_end_of_year(self):
        prompt = monthly_report("2026-12")
        assert "start_date='2026-12-01'" in prompt
        assert "end_date='2026-12-31'" in prompt

    def test_prompt_mentions_tools(self):
        prompt = monthly_report("2026-01")
        assert "query_transactions" in prompt
        assert "get_spending_by_category" in prompt
        assert "aggregation='daily'" in prompt

    def test_unicode_digits_rejected(self):
        """F-011: the regex uses an explicit ASCII digit class [0-9].
        Arabic-Indic digits must not pass."""
        for bad in ("٢٠٢٦-٠٢", "٢٠٢٦-١٢", "２０２６-０２"):
            with pytest.raises(ValueError, match="YYYY-MM"):
                monthly_report(bad)
