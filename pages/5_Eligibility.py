"""Eligibility page — manage wheel_eligible flag and strategy assignment."""

import streamlit as st
from src.eligibility import (
    STRATEGIES,
    get_eligible_underlyings,
    get_ineligible_underlyings,
    remove_underlying,
    update_eligibility,
)
from src.scanner import scan_universe
from src.massive import MassiveAuthError
from src.db import get_conn
from src.ui_helpers import format_si

st.title("Wheel Eligibility")


def _render_criterion(crit_name: str, crit_data: dict):
    if crit_data.get("passed") is True:
        icon = "✅"
    elif crit_data.get("passed") is False:
        icon = "❌"
    else:
        icon = "⚠️"

    value = crit_data.get("value")
    threshold = crit_data.get("threshold")
    note = crit_data.get("note", "")

    if isinstance(value, dict):
        value_str = ", ".join(f"{k}={v}" for k, v in value.items() if v is not None)
    elif isinstance(value, float):
        value_str = format_si(value) if value > 1_000 else f"{value:.2f}"
    else:
        value_str = str(value) if value is not None else "—"

    if isinstance(threshold, (int, float)) and threshold > 1_000:
        threshold_str = format_si(threshold)
    else:
        threshold_str = str(threshold) if threshold is not None else "—"

    st.write(f"{icon} **{crit_name}**: {value_str} (threshold: {threshold_str})")
    if note:
        st.caption(note)


def _render_scan_result(result: dict):
    """Render a single scan result showing all strategy evaluations."""
    symbol = result["symbol"]
    name = result.get("name") or ""
    price = result.get("price")
    market_cap_b = result.get("market_cap_b")
    strategies = result.get("strategies", {})
    passes_any = result.get("passes_any")

    header = symbol
    if name:
        header += f" — {name}"
    if price:
        header += f" @ ${price:.2f}"
    if market_cap_b:
        header += f" (${market_cap_b:.1f}B)"

    with st.expander(header, expanded=passes_any):
        if result.get("error"):
            st.error(result["error"])
            return

        for strat_name, strat_data in strategies.items():
            strat_passes = strat_data.get("passes_all")
            icon = "✅" if strat_passes else "❌"
            desc = STRATEGIES[strat_name]["description"]
            st.markdown(f"**{icon} {strat_name}** — _{desc}_")

            for crit_name, crit_data in strat_data.get("criteria", {}).items():
                _render_criterion(crit_name, crit_data)

            st.divider()

        passing_strategies = [s for s, d in strategies.items() if d.get("passes_all")]
        strategy_options = passing_strategies if passing_strategies else list(STRATEGIES.keys())

        col1, col2 = st.columns([3, 1])
        with col1:
            selected_strategies = st.multiselect(
                "Add with strategies",
                options=strategy_options,
                default=passing_strategies[:1] if passing_strategies else [],
                key=f"add_strategy_{symbol}",
            )
        with col2:
            st.write("")  # vertical alignment
            if st.button("✨ Add to watchlist", key=f"add_scan_{symbol}"):
                if not selected_strategies:
                    st.warning("Select at least one strategy.")
                else:
                    _add_from_scan(symbol, selected_strategies)
                    st.rerun()


def _add_from_scan(symbol: str, strategies: list[str]):
    """Add ticker to underlying and mark eligible with one or more strategies."""
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO underlying (ticker) VALUES (?)",
            (symbol,),
        )
    update_eligibility(
        ticker=symbol,
        eligible=True,
        strategies=strategies,
        quality_notes=f"Added via scanner ({', '.join(strategies)})",
    )


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
        # Sort by conviction desc, then ticker
        eligible.sort(key=lambda r: (-r["conviction"], r["ticker"]))

        st.caption(f"{len(eligible)} ticker(s)")
        header_cols = st.columns([2, 3, 2, 3, 2])
        header_cols[0].caption("Ticker")
        header_cols[1].caption("Strategies")
        header_cols[2].caption("IV Rank")
        header_cols[3].caption("Notes")
        header_cols[4].caption("")

        for row in eligible:
            cols = st.columns([2, 3, 2, 3, 2])
            cols[0].write(row["ticker"])
            badges = " ".join(f"`{s}`" for s in sorted(row["strategies"]))
            cols[1].markdown(badges or "—")
            cols[2].write(
                f"{row['iv_rank_cached']:.1f}%" if row["iv_rank_cached"] is not None else "—"
            )
            cols[3].write(row["quality_notes"] or "")
            if cols[4].button("Mark ineligible", key=f"inelig_{row['ticker']}"):
                update_eligibility(
                    ticker=row["ticker"],
                    eligible=False,
                    strategies=None,
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
                    strategies_input = st.multiselect(
                        "Strategies",
                        options=list(STRATEGIES.keys()),
                        default=[],
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
                        if eligible_input and not strategies_input:
                            st.error("Select at least one strategy.")
                        else:
                            try:
                                update_eligibility(
                                    ticker=row["ticker"],
                                    eligible=eligible_input,
                                    strategies=strategies_input if eligible_input else None,
                                    quality_notes=notes_input or None,
                                )
                                st.success(f"Saved {row['ticker']}")
                                st.rerun()
                            except ValueError as e:
                                st.error(str(e))

                if st.button("🗑 Remove", key=f"remove_{row['ticker']}"):
                    try:
                        remove_underlying(row["ticker"])
                        st.rerun()
                    except ValueError as e:
                        st.error(str(e))

# ---------------------------------------------------------------------------
# Tab 3 — Scan
# ---------------------------------------------------------------------------
with tab_scan:
    st.subheader("Strategy Scanner")

    try:
        from src.massive import _get_api_key
        _get_api_key()
    except MassiveAuthError as e:
        st.error(f"🔑 {str(e)}")
        st.stop()

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

            progress_bar = st.progress(0, text="Starting scan...")

            def progress_callback(i, total, symbol):
                pct = min((i + 1) / total, 0.99) if total > 0 else 0
                progress_bar.progress(pct, text=f"Scanning {symbol}... ({i + 1}/{total})")

            try:
                results = scan_universe(tickers=tickers, progress_callback=progress_callback)
                progress_bar.progress(1.0, text="Scan complete!")
                st.session_state["scan_results"] = results
                st.rerun()
            except Exception as e:
                st.error(f"Scan failed: {str(e)}")

    if st.session_state.get("scan_results"):
        results = st.session_state["scan_results"]

        full_passes = [r for r in results if r.get("passes_any")]
        partials = [r for r in results if not r.get("error") and not r.get("passes_any")]
        errors = [r for r in results if r.get("error")]

        col1, col2, col3 = st.columns(3)
        col1.metric("✅ Full Pass", len(full_passes))
        col2.metric("⚠️ Partial", len(partials))
        col3.metric("❌ Error", len(errors))

        if full_passes:
            st.subheader("✅ Full Passes")
            for result in full_passes:
                _render_scan_result(result)

        if partials:
            st.subheader("⚠️ Partial Matches")
            for result in partials:
                _render_scan_result(result)

        if errors:
            st.subheader("❌ Errors")
            for result in errors:
                with st.expander(f"{result['symbol']} — {result.get('error')}"):
                    st.error(result.get("error"))
