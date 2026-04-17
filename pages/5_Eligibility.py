"""Eligibility page — manage wheel_eligible flag and strategy assignment."""

import streamlit as st
from src.eligibility import (
    STRATEGIES,
    get_eligible_underlyings,
    get_ineligible_underlyings,
    update_eligibility,
)
from src.scanner import scan_ticker, scan_universe
from src.massive import MassiveAuthError
from src.db import get_conn

st.title("Wheel Eligibility")

tab_eligible, tab_review, tab_scan = st.tabs(["Eligible", "Review Queue", "Scan"])

# ---------------------------------------------------------------------------
# Tab 1 — Eligible tickers
# ---------------------------------------------------------------------------
with tab_eligible:
    st.subheader("Eligible Tickers")

    strategy_filter = st.selectbox(
        "Filter by strategy",
        options=["All"] + list(STRATEGIES.keys()),
        index=0,
    )

    eligible = get_eligible_underlyings(
        strategy=None if strategy_filter == "All" else strategy_filter
    )

    if not eligible:
        st.info("No eligible tickers. Add some in the Review Queue tab.")
    else:
        grouped: dict[str, list] = {}
        for row in eligible:
            s = row["eligible_strategy"] or "—"
            grouped.setdefault(s, []).append(row)

        for strat, rows in sorted(grouped.items()):
            desc = STRATEGIES[strat]["description"] if strat in STRATEGIES else ""
            st.markdown(f"**{strat}** — _{desc}_")

            for row in rows:
                cols = st.columns([2, 2, 2, 3, 2])
                cols[0].write(row["ticker"])
                cols[1].write(
                    f"{row['iv_rank_cached']:.1f}%" if row["iv_rank_cached"] is not None else "—"
                )
                cols[2].write(row["last_reviewed"] or "—")
                cols[3].write(row["quality_notes"] or "")
                if cols[4].button("Mark ineligible", key=f"inelig_{row['ticker']}"):
                    update_eligibility(
                        ticker=row["ticker"],
                        eligible=False,
                        strategy=None,
                        quality_notes=row["quality_notes"],
                    )
                    st.rerun()

# ---------------------------------------------------------------------------
# Tab 2 — Review Queue
# ---------------------------------------------------------------------------
with tab_review:
    st.subheader("Review Queue")

    pending = get_ineligible_underlyings()

    if not pending:
        st.success("No tickers pending review.")
    else:
        st.caption(f"{len(pending)} ticker(s) awaiting eligibility decision.")

        for row in pending:
            with st.expander(row["ticker"], expanded=False):
                with st.form(key=f"form_{row['ticker']}"):
                    eligible_input = st.checkbox("Mark as eligible", value=False)
                    strategy_input = st.selectbox(
                        "Strategy",
                        options=list(STRATEGIES.keys()),
                        help="\n".join(
                            f"**{k}**: {v['description']}" for k, v in STRATEGIES.items()
                        ),
                    )
                    notes_input = st.text_input(
                        "Notes",
                        value=row["quality_notes"] or "",
                        placeholder="Reason for decision",
                    )
                    submitted = st.form_submit_button("Save")
                    if submitted:
                        try:
                            update_eligibility(
                                ticker=row["ticker"],
                                eligible=eligible_input,
                                strategy=strategy_input if eligible_input else None,
                                quality_notes=notes_input or None,
                            )
                            st.success(f"Saved {row['ticker']}")
                            st.rerun()
                        except ValueError as e:
                            st.error(str(e))

# ---------------------------------------------------------------------------
# Tab 3 — Scan
# ---------------------------------------------------------------------------
with tab_scan:
    st.subheader("Strategy Scanner")

    # Check for auth error upfront
    try:
        # Just try to get the API key to validate auth early
        from src.massive import _get_api_key
        _get_api_key()
    except MassiveAuthError as e:
        st.error(f"🔑 {str(e)}")
        st.stop()

    col1, col2 = st.columns(2)

    with col1:
        strategy = st.selectbox(
            "Select strategy",
            options=list(STRATEGIES.keys()),
        )

    strat_info = STRATEGIES.get(strategy, {})
    st.caption(strat_info.get("description", ""))

    universe_choice = st.radio(
        "Scan universe",
        options=["S&P 500 representative (~100 tickers)", "Custom list"],
        index=0,
    )

    if universe_choice == "Custom list":
        custom_input = st.text_area(
            "Enter tickers (comma or newline-separated)",
            placeholder="AAPL, MSFT, GOOGL",
            height=100,
        )
        if custom_input:
            tickers = [
                t.strip().upper()
                for t in custom_input.replace(",", "\n").split("\n")
                if t.strip()
            ]
        else:
            tickers = []
    else:
        tickers = None  # Use default S&P 500

    if st.button("▶ Run Scan", type="primary"):
        if universe_choice == "Custom list" and not tickers:
            st.error("Please enter at least one ticker.")
        else:
            st.session_state["scan_results"] = None
            st.session_state["scan_strategy"] = strategy

            progress_bar = st.progress(0, text="Starting scan...")

            def progress_callback(i, total, symbol):
                pct = min((i + 1) / total, 0.99) if total > 0 else 0
                progress_bar.progress(pct, text=f"Scanning {symbol}... ({i + 1}/{total})")

            try:
                results = scan_universe(strategy, tickers=tickers, progress_callback=progress_callback)
                progress_bar.progress(1.0, text="Scan complete!")
                st.session_state["scan_results"] = results
                st.session_state["scan_strategy"] = strategy
                st.rerun()
            except Exception as e:
                st.error(f"Scan failed: {str(e)}")

    # Display results if available
    if st.session_state.get("scan_results"):
        results = st.session_state["scan_results"]
        strat = st.session_state.get("scan_strategy", strategy)

        # Separate results
        full_passes = [r for r in results if r.get("passes_all")]
        partials = [r for r in results if not r.get("error") and not r.get("passes_all")]
        errors = [r for r in results if r.get("error")]

        # Show counts
        col1, col2, col3 = st.columns(3)
        col1.metric("✅ Full Pass", len(full_passes))
        col2.metric("⚠️ Partial", len(partials))
        col3.metric("❌ Error", len(errors))

        # Full passes section
        if full_passes:
            st.subheader("✅ Full Passes")
            for result in full_passes:
                _render_scan_result(result, strat)

        # Partial section
        if partials:
            st.subheader("⚠️ Partial Matches")
            for result in partials:
                _render_scan_result(result, strat)

        # Errors section
        if errors:
            st.subheader("❌ Errors")
            for result in errors:
                with st.expander(f"{result['symbol']} — {result.get('error')}"):
                    st.error(result.get("error"))


def _render_scan_result(result: dict, strategy: str):
    """Render a single scan result with criterion details."""
    symbol = result["symbol"]
    name = result.get("name") or ""
    price = result.get("price")
    market_cap_b = result.get("market_cap_b")
    criteria = result.get("criteria", {})
    passes_all = result.get("passes_all")

    # Format header
    header = f"{symbol}"
    if name:
        header += f" — {name}"
    if price:
        header += f" @ ${price:.2f}"
    if market_cap_b:
        header += f" (${market_cap_b:.1f}B)"

    with st.expander(header, expanded=passes_all):
        # Render criteria table
        for crit_name, crit_data in criteria.items():
            passed = crit_data.get("passed")
            value = crit_data.get("value")
            threshold = crit_data.get("threshold")
            note = crit_data.get("note", "")

            # Icon
            if passed is True:
                icon = "✅"
            elif passed is False:
                icon = "❌"
            else:
                icon = "⚠️"

            # Format value
            if isinstance(value, dict):
                value_str = ", ".join(f"{k}={v}" for k, v in value.items() if v is not None)
            elif isinstance(value, float):
                value_str = f"{value:.2f}"
            else:
                value_str = str(value) if value is not None else "—"

            # Format threshold
            threshold_str = str(threshold) if threshold is not None else "—"

            st.write(
                f"{icon} **{crit_name}**: {value_str} (threshold: {threshold_str})"
            )
            if note:
                st.caption(note)

        # Add to watchlist button
        if not result.get("error"):
            if st.button(
                "✨ Add to watchlist & mark eligible",
                key=f"add_scan_{symbol}",
            ):
                _add_from_scan(symbol, strategy)
                st.rerun()


def _add_from_scan(symbol: str, strategy: str):
    """Add ticker to underlying and mark eligible."""
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO underlying (ticker) VALUES (?)",
            (symbol,),
        )
    update_eligibility(
        ticker=symbol,
        eligible=True,
        strategy=strategy,
        quality_notes=f"Added via scanner ({strategy})",
    )
