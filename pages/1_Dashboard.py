"""
1_Dashboard.py — Portfolio overview.

Shows:
- Aggregate P&L (realized + unrealized)
- Active positions table with cost basis and state
- Pending fills alert (TRADIER_SANDBOX trades with fill_status=PENDING)
- Quick-link to drill into any position
"""

import streamlit as st
import pandas as pd
from src.cost_basis import list_active_cycles, get_realized_pnl_summary, audit_all_active
from src.db import get_conn
from src.ui_helpers import cycles_to_dataframe, fmt_dollar, state_badge, color_pnl_column
from src.poller import poller_status

st.set_page_config(page_title="Dashboard", layout="wide")
st.title("Dashboard")


# ---------------------------------------------------------------------------
# Fetch data
# ---------------------------------------------------------------------------

realized = get_realized_pnl_summary()
active = list_active_cycles()
_status = poller_status()


# ---------------------------------------------------------------------------
# P&L summary metrics (top — primary signal)
# ---------------------------------------------------------------------------

col1, col2, col3, col4 = st.columns(4)

with col1:
    st.metric("Active cycles", len(active))
with col2:
    st.metric("Realized P&L", fmt_dollar(realized["total_realized"]))
with col3:
    st.metric("Closed cycles", realized["cycle_count"])
with col4:
    st.metric("Avg P&L / cycle", fmt_dollar(realized["avg_per_cycle"]))


# ---------------------------------------------------------------------------
# Pending fills alert (action-required — shown prominently only when non-zero)
# ---------------------------------------------------------------------------

def _get_pending_fills() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT t.trade_id, t.cycle_id, t.underlying_id, t.trade_type, "
            "t.filled_at, t.broker_order_id "
            "FROM trade t WHERE t.fill_status='PENDING' "
            "ORDER BY t.filled_at DESC LIMIT 50"
        ).fetchall()
    return [dict(r) for r in rows]


pending = _get_pending_fills()
if pending:
    st.warning(f"⏳ {len(pending)} paper order(s) awaiting fill confirmation.")
    with st.expander("View pending fills"):
        st.dataframe(pd.DataFrame(pending), width="stretch", hide_index=True)


# ---------------------------------------------------------------------------
# Data integrity alerts
# ---------------------------------------------------------------------------

mismatches = audit_all_active()
if mismatches:
    st.error(
        f"⚠️ {len(mismatches)} active cycle(s) have premium accounting mismatches. "
        "Check the Ledger page."
    )


# ---------------------------------------------------------------------------
# Active positions table
# ---------------------------------------------------------------------------

st.subheader("Active positions")

if not active:
    st.info("No active cycles yet.")
    st.page_link("pages/2_Positions.py", label="Open a position →", icon="➕")
else:
    df = cycles_to_dataframe(active)
    display_df = df.drop(columns=["cycle_id"])

    st.dataframe(
        color_pnl_column(display_df, "P&L to date"),
        width="stretch",
        hide_index=True,
    )

    st.caption("Click a row then use the Positions page to drill in.")

    tickers = [f"{c.underlying_id} (id={c.cycle_id})" for c in active]
    selected = st.selectbox("Drill into position", ["—"] + tickers)
    if selected != "—":
        cycle_id = int(selected.split("id=")[1].rstrip(")"))
        st.session_state["selected_cycle_id"] = cycle_id
        st.info(f"cycle_id {cycle_id} selected — navigate to **Positions** in the sidebar.")


# ---------------------------------------------------------------------------
# Closed cycles (collapsed by default)
# ---------------------------------------------------------------------------

with st.expander("Closed cycles"):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM cycle_summary WHERE state='CLOSED' "
            "ORDER BY closed_at DESC LIMIT 50"
        ).fetchall()
    if rows:
        closed_data = []
        for r in rows:
            closed_data.append({
                "Ticker":       r["underlying_id"],
                "Opened":       r["opened_at"][:10] if r["opened_at"] else "—",
                "Closed":       r["closed_at"][:10] if r["closed_at"] else "—",
                "Realized P&L": fmt_dollar(r["realized_pnl"]),
                "Premium":      fmt_dollar(r["total_premium"]),
                "Rolls":        (r["roll_count"] or 0),
            })
        st.dataframe(pd.DataFrame(closed_data), width="stretch", hide_index=True)
    else:
        st.write("No closed cycles yet.")


# ---------------------------------------------------------------------------
# System status (footer — diagnostic, not trading signal)
# ---------------------------------------------------------------------------

st.divider()
status_cols = st.columns(2)
with status_cols[0]:
    if _status["running"]:
        st.caption("🟢 Poller running")
    else:
        st.caption("🔴 Poller stopped")
with status_cols[1]:
    st.caption(f"Poll interval: {_status['interval_s']}s")
