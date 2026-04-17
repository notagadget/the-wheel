"""
3_Position.py — Drill into a single cycle.

Shows:
- Cycle state, cost basis, P&L
- Trade ledger for this cycle
- Roll history
- Action forms: record assignment, open CC, roll, close, expiration, called away
"""

import streamlit as st
from datetime import date
from src.db import get_conn
from src.cost_basis import get_cycle_summary, audit_cycle
from src.state_machine import (
    record_assignment, open_short_call, roll_position,
    close_position, record_expiration, record_called_away,
)
from src.ui_helpers import (
    fmt_dollar, state_badge, trades_to_dataframe, color_pnl_column
)

st.set_page_config(page_title="Position", layout="wide")
st.title("Position detail")


# ---------------------------------------------------------------------------
# Cycle selector
# ---------------------------------------------------------------------------

with get_conn() as conn:
    active_rows = conn.execute(
        "SELECT cycle_id, underlying_id, state, lot_id "
        "FROM cycle WHERE state != 'CLOSED' ORDER BY opened_at DESC"
    ).fetchall()

if not active_rows:
    st.info("No active cycles. Open one via the Screener.")
    st.stop()

options = {
    f"{r['underlying_id']} — {state_badge(r['state'])} (id={r['cycle_id']})": r["cycle_id"]
    for r in active_rows
}

default_label = None
if "selected_cycle_id" in st.session_state:
    for label, cid in options.items():
        if cid == st.session_state["selected_cycle_id"]:
            default_label = label
            break

selected_label = st.selectbox(
    "Select cycle",
    list(options.keys()),
    index=list(options.keys()).index(default_label) if default_label else 0,
)
cycle_id = options[selected_label]


# ---------------------------------------------------------------------------
# Cycle summary header
# ---------------------------------------------------------------------------

summary = get_cycle_summary(cycle_id)
audit = audit_cycle(cycle_id)

col1, col2, col3, col4, col5 = st.columns(5)
with col1:
    st.metric("State", state_badge(summary.state))
with col2:
    st.metric("Cost basis", fmt_dollar(summary.cost_basis))
with col3:
    st.metric("Assign price", fmt_dollar(summary.assignment_price))
with col4:
    st.metric("Total premium", fmt_dollar(summary.total_premium))
with col5:
    st.metric("Net P&L", fmt_dollar(summary.net_pnl_to_date))

if not audit.match:
    st.error(
        f"⚠️ Premium accounting mismatch: DB={fmt_dollar(audit.db_total_premium)}, "
        f"computed={fmt_dollar(audit.computed_total_premium)}, "
        f"delta={fmt_dollar(audit.delta)}"
    )

st.divider()


# ---------------------------------------------------------------------------
# Trade ledger for this cycle
# ---------------------------------------------------------------------------

st.subheader("Trade ledger")

with get_conn() as conn:
    trades = conn.execute(
        "SELECT * FROM trade WHERE cycle_id=? ORDER BY filled_at",
        (cycle_id,)
    ).fetchall()
    trade_dicts = [dict(t) for t in trades]

if trade_dicts:
    df = trades_to_dataframe(trade_dicts)
    display_df = df.drop(columns=["trade_id"])
    st.dataframe(
        color_pnl_column(display_df, "Net credit"),
        width="stretch",
        hide_index=True,
    )
else:
    st.write("No trades recorded yet.")

# Roll history
with get_conn() as conn:
    rolls = conn.execute(
        "SELECT * FROM roll_event WHERE cycle_id=? ORDER BY rolled_at",
        (cycle_id,)
    ).fetchall()

if rolls:
    st.subheader("Roll history")
    import pandas as pd
    roll_data = []
    for r in rolls:
        roll_data.append({
            "Date":         r["rolled_at"][:10] if r["rolled_at"] else "—",
            "Old strike":   fmt_dollar(r["old_strike"]),
            "New strike":   fmt_dollar(r["new_strike"]),
            "Old expiry":   r["old_expiration"] or "—",
            "New expiry":   r["new_expiration"] or "—",
            "Net credit":   fmt_dollar(r["net_credit"]),
        })
    st.dataframe(pd.DataFrame(roll_data), width="stretch", hide_index=True)

st.divider()


# ---------------------------------------------------------------------------
# Action panel — only show valid actions for current state
# ---------------------------------------------------------------------------

st.subheader("Actions")

state = summary.state


def _action_form(title, key, fields_fn, submit_label, on_submit):
    with st.expander(title):
        with st.form(key):
            fields_fn()
            if st.form_submit_button(submit_label, type="primary"):
                try:
                    on_submit()
                    st.success("Recorded.")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))


# --- SHORT_PUT actions ---
if state == "SHORT_PUT":

    # Record assignment
    with st.expander("Record assignment"):
        with st.form("assignment"):
            fill_price = st.number_input("Fill price ($/sh)", min_value=0.01, step=0.01, format="%.2f")
            src = st.radio("Source", ["MANUAL", "TRADIER_SANDBOX"], horizontal=True)
            commission = st.number_input("Commission", min_value=0.0, value=0.0, step=0.01)
            if st.form_submit_button("Record assignment", type="primary"):
                try:
                    record_assignment(
                        cycle_id=cycle_id, fill_price=float(fill_price),
                        source=src, commission=float(commission)
                    )
                    st.success("Assignment recorded.")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))

    # Roll put
    with st.expander("Roll put"):
        with st.form("roll_put"):
            col1, col2 = st.columns(2)
            with col1:
                close_price = st.number_input("Close price (debit/sh)", min_value=0.01, step=0.01, format="%.2f")
            with col2:
                open_price = st.number_input("Open price (credit/sh)", min_value=0.01, step=0.01, format="%.2f")
            col3, col4 = st.columns(2)
            with col3:
                new_strike = st.number_input("New strike", min_value=0.01, step=0.50, format="%.2f")
            with col4:
                new_exp = st.date_input("New expiration", min_value=date.today())
            contracts = st.number_input("Contracts", min_value=1, value=1)
            src = st.radio("Source", ["MANUAL", "TRADIER_SANDBOX"], horizontal=True, key="roll_src")
            if close_price and open_price:
                net = (open_price - close_price) * contracts * 100
                st.caption(f"Net roll: **{fmt_dollar(net)}**")
            if st.form_submit_button("Submit roll", type="primary"):
                try:
                    roll_position(
                        cycle_id=cycle_id,
                        close_price_per_share=float(close_price),
                        open_strike=float(new_strike),
                        open_expiration=str(new_exp),
                        open_price_per_share=float(open_price),
                        contracts=int(contracts),
                        source=src,
                    )
                    st.success("Roll recorded.")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))

    # Close early
    with st.expander("Close early (buy to close)"):
        with st.form("close_put"):
            price = st.number_input("Close price (debit/sh)", min_value=0.01, step=0.01, format="%.2f")
            src = st.radio("Source", ["MANUAL", "TRADIER_SANDBOX"], horizontal=True, key="close_src")
            commission = st.number_input("Commission", min_value=0.0, value=0.0, step=0.01)
            if st.form_submit_button("Close position", type="primary"):
                try:
                    close_position(
                        cycle_id=cycle_id, price_per_share=float(price),
                        source=src, commission=float(commission)
                    )
                    st.success("Position closed.")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))

    # Expiration
    with st.expander("Record expiration (expires worthless)"):
        with st.form("expire_put"):
            notes = st.text_input("Notes (optional)")
            if st.form_submit_button("Record expiration", type="primary"):
                try:
                    record_expiration(cycle_id=cycle_id, notes=notes or None)
                    st.success("Expiration recorded.")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))


# --- LONG_STOCK actions ---
elif state == "LONG_STOCK":

    with st.expander("Sell covered call"):
        with st.form("open_cc"):
            col1, col2 = st.columns(2)
            with col1:
                strike = st.number_input("Strike", min_value=0.01, step=0.50, format="%.2f")
                contracts = st.number_input("Contracts", min_value=1, value=1)
            with col2:
                expiration = st.date_input("Expiration", min_value=date.today())
                price = st.number_input("Premium (credit/sh)", min_value=0.01, step=0.01, format="%.2f")
            src = st.radio("Source", ["MANUAL", "TRADIER_SANDBOX"], horizontal=True)
            commission = st.number_input("Commission", min_value=0.0, value=0.0, step=0.01)
            if summary.cost_basis and strike and price:
                new_basis = summary.cost_basis - price
                st.caption(
                    f"New cost basis after this CC: **{fmt_dollar(new_basis)}**/sh"
                )
            if st.form_submit_button("Sell covered call", type="primary"):
                try:
                    open_short_call(
                        cycle_id=cycle_id, strike=float(strike),
                        expiration=str(expiration), contracts=int(contracts),
                        price_per_share=float(price), source=src,
                        commission=float(commission)
                    )
                    st.success("Covered call recorded.")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))


# --- SHORT_CALL actions ---
elif state == "SHORT_CALL":

    with st.expander("Record called away"):
        with st.form("called_away"):
            fill_price = st.number_input("Fill price ($/sh)", min_value=0.01, step=0.01, format="%.2f")
            src = st.radio("Source", ["MANUAL", "TRADIER_SANDBOX"], horizontal=True)
            commission = st.number_input("Commission", min_value=0.0, value=0.0, step=0.01)
            if summary.cost_basis and fill_price:
                est_pnl = (fill_price - summary.cost_basis) * (summary.shares_held or 100)
                st.caption(f"Estimated realized P&L: **{fmt_dollar(est_pnl)}**")
            if st.form_submit_button("Record called away", type="primary"):
                try:
                    result = record_called_away(
                        cycle_id=cycle_id, fill_price=float(fill_price),
                        source=src, commission=float(commission)
                    )
                    st.success(f"Called away. Realized P&L: {fmt_dollar(result['realized_pnl'])}")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))

    with st.expander("Roll call"):
        with st.form("roll_call"):
            col1, col2 = st.columns(2)
            with col1:
                close_price = st.number_input("Close price (debit/sh)", min_value=0.01, step=0.01, format="%.2f")
            with col2:
                open_price = st.number_input("Open price (credit/sh)", min_value=0.01, step=0.01, format="%.2f")
            col3, col4 = st.columns(2)
            with col3:
                new_strike = st.number_input("New strike", min_value=0.01, step=0.50, format="%.2f")
            with col4:
                new_exp = st.date_input("New expiration", min_value=date.today())
            contracts = st.number_input("Contracts", min_value=1, value=1)
            src = st.radio("Source", ["MANUAL", "TRADIER_SANDBOX"], horizontal=True, key="roll_call_src")
            if close_price and open_price:
                net = (open_price - close_price) * contracts * 100
                st.caption(f"Net roll: **{fmt_dollar(net)}**")
            if st.form_submit_button("Submit roll", type="primary"):
                try:
                    roll_position(
                        cycle_id=cycle_id,
                        close_price_per_share=float(close_price),
                        open_strike=float(new_strike),
                        open_expiration=str(new_exp),
                        open_price_per_share=float(open_price),
                        contracts=int(contracts),
                        source=src,
                    )
                    st.success("Roll recorded.")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))

    with st.expander("Record expiration (expires worthless)"):
        with st.form("expire_call"):
            notes = st.text_input("Notes (optional)")
            if st.form_submit_button("Record expiration", type="primary"):
                try:
                    record_expiration(cycle_id=cycle_id, notes=notes or None)
                    st.success("Expiration recorded.")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))

    with st.expander("Close early (buy to close)"):
        with st.form("close_call"):
            price = st.number_input("Close price (debit/sh)", min_value=0.01, step=0.01, format="%.2f")
            src = st.radio("Source", ["MANUAL", "TRADIER_SANDBOX"], horizontal=True, key="close_call_src")
            commission = st.number_input("Commission", min_value=0.0, value=0.0, step=0.01)
            if st.form_submit_button("Close position", type="primary"):
                try:
                    close_position(
                        cycle_id=cycle_id, price_per_share=float(price),
                        source=src, commission=float(commission)
                    )
                    st.success("Position closed.")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))
