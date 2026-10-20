"""FastMCP server entry-point for the financial-transactions MCP.

Registers all four MCP tools and three prompts, then runs the server
over stdio when executed directly::

    python -m financial_mcp.server
"""

from mcp.server.fastmcp import FastMCP

from financial_mcp.tools.analytics import register_analytics_tools
from financial_mcp.tools.transactions import register_transaction_tools

# Create the FastMCP instance
mcp = FastMCP("financial-transactions")

# Register tools
register_transaction_tools(mcp)
register_analytics_tools(mcp)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------
@mcp.prompt()
def monthly_report(month: str = "") -> str:
    """Generate a monthly financial report.

    Args:
        month: Month in ``YYYY-MM`` format.  When empty the current
            month is used.

    Returns:
        A prompt string instructing the LLM to call the appropriate
        MCP tools.
    """
    if month:
        start = f"{month}-01"
        end = f"{month}-31"
        return (
            f"Buatkan laporan keuangan bulanan untuk periode {month}. "
            f"Gunakan tool query_transactions dengan start_date='{start}' "
            f"end_date='{end}' aggregation='daily', "
            f"lalu get_spending_by_category untuk breakdown per kategori. "
            f"Tampilkan total pemasukan, pengeluaran, net, dan "
            f"kategori terbesar."
        )
    return (
        "Buatkan laporan keuangan bulan ini. "
        "Gunakan get_financial_summary untuk overview, "
        "lalu get_spending_by_category untuk detail per kategori."
    )


@mcp.prompt()
def spending_analysis(start_date: str, end_date: str) -> str:
    """Analyze spending patterns between two dates.

    Args:
        start_date: Start date in ``YYYY-MM-DD`` format.
        end_date: End date in ``YYYY-MM-DD`` format.

    Returns:
        A prompt string for the LLM to call weekly-aggregation and
        category tools.
    """
    return (
        f"Analisis pola pengeluaran dari {start_date} sampai {end_date}. "
        f"Gunakan query_transactions dengan aggregation='weekly' "
        f"untuk tren mingguan, "
        f"dan get_spending_by_category untuk breakdown kategori. "
        f"Identifikasi kategori terbesar dan berikan rekomendasi."
    )


@mcp.prompt()
def cashflow_check() -> str:
    """Quick cashflow health check.

    Returns:
        A prompt string instructing the LLM to call summary and
        monthly-trend tools.
    """
    return (
        "Lakukan pengecekan cashflow: gunakan get_financial_summary "
        "untuk overview, "
        "lalu query_transactions dengan aggregation='monthly' "
        "untuk tren bulanan. "
        "Hitung apakah saldo cenderung naik atau turun, "
        "dan identifikasi bulan dengan pengeluaran tertinggi."
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    mcp.run(transport="stdio")
