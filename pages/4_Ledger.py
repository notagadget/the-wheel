"""
4_Ledger.py — Full trade ledger, unified paper + manual.

Shows all trades across all cycles. Filterable by ticker, source,
trade type, fill status, and date range.
"""

import streamlit as st
import pandas as pd
from datetime import date, timedelta
from src.db import get_conn
from src.ui_helpers import trades_to_dataframe, color_pnl_column, fmt_dollar

st.set_page_config(page_title="Ledger", layout="wide")
st.title("Trade ledger")


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

with st.expander("Filters", expanded=True):
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        with get_conn() as conn:
            tickers = [r[0] for r in conn.execute(
                "SELECT DISTINCT underlying_id FROM trade ORDER BY underlying_id"
            ).fetchall()]
        selected_tickers = st.multiselect("Ticker", tickers)
    with col2:
        selected_sources = st.multiselect(
            "Source",
            ["TRADIER_SANDBOX", "TRADIER_LIVE", "MANUAL"],
            default=["TRADIER_SANDBOX", "TRADIER_LIVE", "MANUAL"],
            format_func=lambda s: {"TRADIER_SANDBOX": "Sandbox", "TRADIER_LIVE": "Live", "MANUAL": "Manual"}[s],
        )
    with col3:
        selected_statuses = st.multiselect(
            "Fill status", ["CONFIRMED", "PENDING", "REJECTED"],
            default=["CONFIRMED", "PENDING"]
        )
    with col4:
        date_range = st.date_input(
            "Date range",
            value=(date.today() - timedelta(days=90), date.today()),
        )

# Build query
conditions = []
params = []

if selected_tickers:
    placeholders = ",".join("?" * len(selected_tickers))
    conditions.append(f"t.underlying_id IN ({placeholders})")
    params.extend(selected_tickers)

if selected_sources:
    placeholders = ",".join("?" * len(selected_sources))
    conditions.append(f"t.source IN ({placeholders})")
    params.extend(selected_sources)

if selected_statuses:
    placeholders = ",".join("?" * len(selected_statuses))
    conditions.append(f"t.fill_status IN ({placeholders})")
    params.extend(selected_statuses)

if isinstance(date_range, tuple) and len(date_range) == 2:
    conditions.append("DATE(t.filled_at) BETWEEN ? AND ?")
    params.extend([str(date_range[0]), str(date_range[1])])

where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

query = f"""
    SELECT t.*, c.state as cycle_state
    FROM trade t
    JOIN cycle c ON c.cycle_id = t.cycle_id
    {where}
    ORDER BY t.filled_at DESC
    LIMIT 500
"""

with get_conn() as conn:
    rows = conn.execute(query, params).fetchall()

trade_dicts = [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Summary metrics
# ---------------------------------------------------------------------------

if trade_dicts:
    total_credit = sum(t["net_credit"] for t in trade_dicts if t["fill_status"] == "CONFIRMED")
    total_commission = sum(t["commission"] for t in trade_dicts if t["fill_status"] == "CONFIRMED")
    pending_count = sum(1 for t in trade_dicts if t["fill_status"] == "PENDING")
    rejected_count = sum(1 for t in trade_dicts if t["fill_status"] == "REJECTED")

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Trades shown", len(trade_dicts))
    with col2:
        st.metric("Net credit (confirmed)", fmt_dollar(total_credit))
    with col3:
        st.metric("Total commissions", fmt_dollar(total_commission))
    with col4:
        pending_str = f"{pending_count} pending, {rejected_count} rejected"
        st.metric("Unconfirmed", pending_str)

st.divider()


# ---------------------------------------------------------------------------
# Trade table
# ---------------------------------------------------------------------------

if not trade_dicts:
    st.info("No trades match the selected filters.")
    st.page_link("pages/2_Screener.py", label="Open a position →", icon="🔍")
else:
    df = trades_to_dataframe(trade_dicts)
    # Add cycle state column for context
    df.insert(1, "Cycle state", [t.get("cycle_state", "") for t in trade_dicts])
    display_df = df.drop(columns=["trade_id"])

    st.dataframe(
        color_pnl_column(display_df, "Net credit"),
        width="stretch",
        hide_index=True,
    )
    st.caption(f"Showing up to 500 most recent trades. Adjust filters to narrow.")


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

if trade_dicts:
    export_df = pd.DataFrame(trade_dicts)
    csv = export_df.to_csv(index=False).encode("utf-8")
    st.download_button(
        "Export to CSV",
        data=csv,
        file_name=f"wheel_trades_{date.today()}.csv",
        mime="text/csv",
    )
