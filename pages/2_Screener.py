"""
2_Screener.py — Equity screening and cycle entry.

Shows:
- Watchlist of tracked underlyings with cached IV rank
- Earnings calendar warning flag
- Form to open a new CSP (creates cycle via state_machine.open_short_put)
"""

import streamlit as st
from datetime import date
from src.db import get_conn
from src.state_machine import open_short_put
from src.ui_helpers import fmt_dollar, fmt_pct
from src.market_data import refresh_all_watchlist, refresh_iv_for_ticker
from src.eligibility import add_underlying

st.set_page_config(page_title="Screener", layout="wide")
st.title("Screener")


# ---------------------------------------------------------------------------
# Watchlist
# ---------------------------------------------------------------------------

col1, col2 = st.columns([3, 1])
with col1:
    st.subheader("Watchlist")
with col2:
    if st.button("🔄 Refresh all IV"):
        with st.spinner("Fetching IV data..."):
            results = refresh_all_watchlist()
            successes = [r for r in results if "error" not in r]
            errors = [r for r in results if "error" in r]
            st.success(f"✓ Updated {len(successes)} tickers")
            if errors:
                st.warning(f"⚠️ {len(errors)} tickers failed to update")
            st.rerun()

with get_conn() as conn:
    underlyings = conn.execute(
        "SELECT u.underlying_id, u.ticker, u.iv_rank_cached, u.iv_updated, u.notes, "
        "COUNT(c.cycle_id) FILTER (WHERE c.state != 'CLOSED') AS active_cycles "
        "FROM underlying u "
        "LEFT JOIN cycle c ON c.underlying_id = u.underlying_id "
        "GROUP BY u.underlying_id "
        "ORDER BY u.iv_rank_cached DESC NULLS LAST"
    ).fetchall()

if underlyings:
    rows = []
    for u in underlyings:
        rows.append({
            "Ticker":       u["ticker"],
            "IV Rank":      fmt_pct(u["iv_rank_cached"]),
            "Active cycles": u["active_cycles"] or 0,
            "Updated":      u["iv_updated"][:10] if u["iv_updated"] else "—",
            "Notes":        u["notes"] or "—",
        })
    import pandas as pd
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    st.caption("IV Rank sourced from Tradier IV history (cached). Use 🔄 Refresh all IV to update.")
else:
    st.info("No tickers in watchlist yet. Add one below.")

st.divider()


# ---------------------------------------------------------------------------
# Add ticker to watchlist
# ---------------------------------------------------------------------------

with st.expander("Add ticker to watchlist"):
    with st.form("add_ticker"):
        new_ticker = st.text_input("Ticker", placeholder="e.g. RKLB").upper().strip()
        notes = st.text_area("Notes (why Wheel-eligible?)", height=80)
        submitted = st.form_submit_button("Add")
        if submitted and new_ticker:
            add_underlying(new_ticker, notes or None)
            st.success(f"{new_ticker} added to watchlist.")
            st.rerun()

st.divider()


# ---------------------------------------------------------------------------
# Open new CSP
# ---------------------------------------------------------------------------

st.subheader("Open new position (sell CSP)")

ticker_options = [u["ticker"] for u in underlyings] if underlyings else []

with st.form("open_csp"):
    col1, col2 = st.columns(2)
    with col1:
        ticker = st.selectbox("Underlying", ticker_options) if ticker_options else \
                 st.text_input("Underlying (add to watchlist first)")
        strike = st.number_input("Strike", min_value=0.01, step=0.50, format="%.2f")
        contracts = st.number_input("Contracts", min_value=1, max_value=100, value=1)
    with col2:
        expiration = st.date_input("Expiration", min_value=date.today())
        price_per_share = st.number_input(
            "Premium (per share)", min_value=0.01, step=0.01, format="%.2f"
        )
        source = st.radio("Source", ["MANUAL", "TRADIER_SANDBOX"], horizontal=True)
        commission = st.number_input("Commission ($)", min_value=0.0, value=0.0, step=0.01)

    # Preview before submit
    if strike and price_per_share and contracts:
        net = contracts * 100 * price_per_share
        st.caption(
            f"Net credit: **{fmt_dollar(net)}** | "
            f"Cost basis if assigned: **{fmt_dollar(strike - net/100)}**/sh"
        )

    submitted = st.form_submit_button("Open position", type="primary")

    if submitted:
        if not ticker:
            st.error("Select or enter a ticker.")
        elif strike <= 0 or price_per_share <= 0:
            st.error("Strike and premium must be positive.")
        else:
            try:
                result = open_short_put(
                    underlying_id=ticker,
                    strike=float(strike),
                    expiration=str(expiration),
                    contracts=int(contracts),
                    price_per_share=float(price_per_share),
                    source=source,
                    commission=float(commission),
                )
                st.success(
                    f"Cycle {result['cycle_id']} opened. "
                    f"Trade {result['trade_id']} recorded."
                )
                st.session_state["selected_cycle_id"] = result["cycle_id"]
            except Exception as e:
                st.error(f"Failed to open position: {e}")
