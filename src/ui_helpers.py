"""
ui_helpers.py — shared display utilities for Streamlit pages.

No business logic here — purely formatting and DataFrame construction.
"""

import pandas as pd


# State badge labels
_STATE_LABELS = {
    "SHORT_PUT":   "Short Put",
    "LONG_STOCK":  "Long Stock",
    "SHORT_CALL":  "Short Call",
    "CLOSED":      "Closed",
}


def fmt_dollar(value) -> str:
    """Format a number as a dollar string with two decimal places."""
    if value is None:
        return "—"
    return f"${value:,.2f}"


def state_badge(state: str) -> str:
    """Return a human-readable label for a cycle state."""
    return _STATE_LABELS.get(state, state)


def cycles_to_dataframe(cycles) -> pd.DataFrame:
    """
    Convert a list of CycleSummary objects to a display DataFrame.
    Keeps cycle_id as a column so callers can drop it if desired.
    """
    rows = []
    for c in cycles:
        rows.append({
            "cycle_id":   c.cycle_id,
            "Ticker":     c.underlying_id,
            "State":      state_badge(c.state),
            "Opened":     c.opened_at[:10] if c.opened_at else "—",
            "Premium":    fmt_dollar(c.total_premium),
            "Cost Basis": fmt_dollar(c.cost_basis),
            "Shares":     c.shares_held if c.shares_held else "—",
            "P&L to date": c.net_pnl_to_date,
            "Rolls":      c.roll_count,
        })
    return pd.DataFrame(rows)


def color_pnl_column(df: pd.DataFrame, column: str) -> pd.DataFrame.style:
    """
    Return a Styler that colors a numeric P&L column green/red,
    and formats it as dollars.
    """
    def _color(val):
        if val is None or not isinstance(val, (int, float)):
            return ""
        return "color: green" if val >= 0 else "color: red"

    def _fmt(val):
        if val is None or not isinstance(val, (int, float)):
            return "—"
        return f"${val:,.2f}"

    return df.style.map(_color, subset=[column]).format(_fmt, subset=[column])
