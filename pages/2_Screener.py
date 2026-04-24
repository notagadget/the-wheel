"""
2_Screener.py — Open a new covered short put position.

Shows:
- Form to open a new CSP (creates cycle via state_machine.open_short_put)
- Lists eligible tickers from the Eligibility page
- Shows strategy context for the selected ticker
"""

import streamlit as st
from datetime import date
from src.state_machine import open_short_put
from src.ui_helpers import fmt_dollar, fmt_pct
from src.eligibility import get_eligible_underlyings, STRATEGY_LABELS

st.set_page_config(page_title="New Position", layout="wide")
st.title("New Position")


# ---------------------------------------------------------------------------
# Open new CSP
# ---------------------------------------------------------------------------

eligible = get_eligible_underlyings()

if not eligible:
    st.info("No eligible tickers yet.")
    st.page_link("pages/5_Eligibility.py", label="Go to Eligibility to scan or add tickers →")
    st.stop()

st.subheader("Open new position (sell CSP)")

ticker_options = [u["ticker"] for u in eligible]
eligible_by_ticker = {u["ticker"]: u for u in eligible}

with st.form("open_csp"):
    col1, col2 = st.columns(2)
    with col1:
        ticker = st.selectbox("Underlying", ticker_options)

        # Show strategy context for selected ticker
        if ticker and ticker in eligible_by_ticker:
            u = eligible_by_ticker[ticker]
            strat_labels = [STRATEGY_LABELS.get(s, s) for s in u.get("strategies", [])]
            context_parts = []
            if strat_labels:
                context_parts.append(f"Passes: {', '.join(strat_labels)}")
            if u.get("iv_rank_cached") is not None:
                context_parts.append(f"IV Rank: {u['iv_rank_cached']:.1f}%")
            if u.get("last_reviewed"):
                context_parts.append(f"Last reviewed: {u['last_reviewed'][:10]}")
            if context_parts:
                st.caption(" · ".join(context_parts))

        strike = st.number_input("Strike", min_value=0.01, step=0.50, format="%.2f")
        contracts = st.number_input("Contracts", min_value=1, max_value=100, value=1)
    with col2:
        expiration = st.date_input("Expiration", min_value=date.today())
        price_per_share = st.number_input(
            "Premium (per share)", min_value=0.01, step=0.01, format="%.2f"
        )
        source = st.radio(
            "Source",
            ["MANUAL", "TRADIER_SANDBOX"],
            format_func=lambda s: {"MANUAL": "Manual entry", "TRADIER_SANDBOX": "Sandbox fill"}[s],
            horizontal=True,
        )
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
