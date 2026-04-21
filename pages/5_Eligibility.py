"""Eligibility page — manage wheel_eligible flag and strategy assignment."""

import time
import threading
import traceback
import streamlit as st
from src.eligibility import (
    STRATEGIES,
    add_underlying,
    get_eligible_underlyings,
    get_ineligible_underlyings,
    remove_underlying,
    update_eligibility,
)
from src.scanner import scan_universe
from src.massive import MassiveAuthError, get_sp500_tickers
from src.db import get_conn
from src.ui_helpers import format_si

st.title("Wheel Eligibility")


def _format_criterion_cell(crit_data: dict) -> dict:
    """Extract and format criterion cell data for display.

    Returns a dict with keys: value, threshold, passed, note.
    """
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

    passed = crit_data.get("passed")

    return {
        "value": value_str,
        "threshold": threshold_str,
        "passed": passed,
        "note": note,
    }


def _format_criterion_name(key: str) -> str:
    return key.replace("_", " ").capitalize()


def _render_criterion(crit_name: str, crit_data: dict):
    formatted = _format_criterion_cell(crit_data)

    if formatted["passed"] is True:
        icon = "✅"
    elif formatted["passed"] is False:
        icon = "❌"
    else:
        icon = "⚠️"

    st.write(f"{icon} **{_format_criterion_name(crit_name)}**: {formatted['value']} (threshold: {formatted['threshold']})")
    if formatted["note"]:
        st.caption(formatted["note"])


def _render_scan_result_table(result: dict, collapsed: bool = False, index: int = 0):
    """Render scan result as an HTML card with strategy tables."""
    symbol = result["symbol"]

    if result.get("error"):
        st.error(result["error"])
        return

    name = result.get("name") or ""
    price = result.get("price")
    market_cap_b = result.get("market_cap_b")
    strategies = result.get("strategies", {})
    passes_any = result.get("passes_any")

    passing_count = sum(1 for s, d in strategies.items() if d.get("passes_all"))
    summary_text = f"{passing_count} strategy passes" if passing_count > 1 else (
        "1 strategy passes" if passing_count == 1 else "0 strategies pass"
    )
    badge_bg = "#2ecc71" if passes_any else "#e74c3c"

    header_text = symbol
    if name:
        header_text += f" — {name}"
    if price:
        header_text += f" @ ${price:.2f}"
    if market_cap_b:
        header_text += f" (${market_cap_b:.1f}B)"

    strategy_count = sum(1 for d in strategies.values() if d.get("passes_all"))
    label = f"{header_text} — {strategy_count} strateg{'y' if strategy_count == 1 else 'ies'} pass"

    expanded_state = False if collapsed else passes_any
    with st.expander(label, expanded=expanded_state):
        html_parts = []

        html_parts.append(
            '<div style="'
            'display: flex; '
            'justify-content: space-between; '
            'align-items: center; '
            'margin-bottom: 0.5rem;'
            'font-size: 0.875rem;'
            '">'
        )
        html_parts.append(f'<div style="font-weight: bold;">{header_text}</div>')
        html_parts.append(
            f'<div style="'
            f'background-color: {badge_bg}; '
            f'color: white; '
            f'padding: 0.15rem 0.4rem; '
            f'border-radius: 0.2rem; '
            f'font-size: 0.75rem;'
            f'">{summary_text}</div>'
        )
        html_parts.append('</div>')

        first_strategy = True
        for strat_name, strat_data in strategies.items():
            strat_passes = strat_data.get("passes_all")
            icon = "✅" if strat_passes else "❌"
            desc = STRATEGIES[strat_name]["description"]

            html_parts.append(
                f'<div style="'
                f'margin-top: 0.5rem; '
                f'margin-bottom: 0.3rem; '
                f'font-weight: bold;'
                f'font-size: 0.875rem;'
                f'">{icon} {strat_name} — <em>{desc}</em></div>'
            )

            html_parts.append(
                '<table style="'
                'width: 100%; '
                'border-collapse: collapse; '
                'font-size: 0.8rem; '
                'table-layout: fixed;'
                '">'
            )
            html_parts.append(
                '<colgroup>'
                '<col style="width: 28%;">'
                '<col style="width: 16%;">'
                '<col style="width: 20%;">'
                '<col style="width: 10%;">'
                '<col style="width: 26%;">'
                '</colgroup>'
            )

            if first_strategy:
                html_parts.append('<thead>')
                html_parts.append(
                    '<tr style="border-bottom: 0.5px solid var(--color-border-tertiary, #ddd);">'
                )
                for header in ["Criterion", "Value", "Threshold", "Status", "Note"]:
                    html_parts.append(
                        f'<th style="'
                        f'text-align: left; '
                        f'padding: 0.25rem 0.35rem; '
                        f'font-weight: bold;'
                        f'font-size: 0.75rem;'
                        f'">{header}</th>'
                    )
                html_parts.append('</tr>')
                html_parts.append('</thead>')

            html_parts.append('<tbody>')
            for crit_name, crit_data in strat_data.get("criteria", {}).items():
                formatted = _format_criterion_cell(crit_data)

                if formatted["passed"] is True:
                    status_icon = "✅"
                    status_color = "#2ecc71"
                elif formatted["passed"] is False:
                    status_icon = "❌"
                    status_color = "#e74c3c"
                else:
                    status_icon = "⚠️"
                    status_color = "#f39c12"

                html_parts.append(
                    '<tr style="border-bottom: 0.5px solid var(--color-border-tertiary, #ddd);">'
                )
                html_parts.append(f'<td style="padding: 0.2rem 0.35rem;">{_format_criterion_name(crit_name)}</td>')
                html_parts.append(f'<td style="padding: 0.2rem 0.35rem;">{formatted["value"]}</td>')
                html_parts.append(f'<td style="padding: 0.2rem 0.35rem;">{formatted["threshold"]}</td>')
                html_parts.append(
                    f'<td style="padding: 0.2rem 0.35rem; color: {status_color}; font-weight: bold;">{status_icon}</td>'
                )
                html_parts.append(
                    f'<td style="padding: 0.2rem 0.35rem; font-size: 0.7rem; color: gray;">{formatted["note"]}</td>'
                )
                html_parts.append('</tr>')

            html_parts.append('</tbody>')
            html_parts.append('</table>')

            first_strategy = False

        html_content = "\n".join(html_parts)
        st.html(html_content)

        passing_strategies = [s for s, d in strategies.items() if d.get("passes_all")]
        strategy_options = passing_strategies if passing_strategies else list(STRATEGIES.keys())

        col1, col2 = st.columns([3, 1])
        with col1:
            selected_strategies = st.multiselect(
                "Add with strategies",
                options=strategy_options,
                default=passing_strategies[:1] if passing_strategies else [],
                key=f"add_strategy_{symbol}_{index}",
            )
        with col2:
            st.write("")
            if st.button("✨ Add to watchlist", key=f"add_scan_{symbol}_{index}"):
                if not selected_strategies:
                    st.warning("Select at least one strategy.")
                else:
                    _add_from_scan(symbol, selected_strategies)
                    st.rerun()


def _render_scan_result(result: dict, collapsed: bool = False, index: int = 0):
    """Render a single scan result showing all strategy evaluations."""
    _render_scan_result_table(result, collapsed=collapsed, index=index)


def _render_timing_stats(ts: dict):
    with st.expander("⏱ Scan Performance", expanded=False):
        col_t1, col_t2, col_t3, col_t4 = st.columns(4)
        col_t1.metric("Total Time", f"{ts['total_ms']/1000:.1f}s")
        col_t2.metric("Avg/Ticker", f"{ts['avg_per_ticker_ms']:.0f}ms")
        col_t3.metric("Min", f"{ts['min_per_ticker_ms']:.0f}ms")
        col_t4.metric("Max", f"{ts['max_per_ticker_ms']:.0f}ms")

        if ts.get("batch_quote_ms"):
            st.caption(f"Batch quotes: {ts['batch_quote_ms']:.0f}ms")

        if ts.get("api_breakdown"):
            st.caption("Common data fetch timing (avg per ticker):")
            breakdown = ts["api_breakdown"]
            col_api1, col_api2, col_api3, col_api4 = st.columns(4)
            col_api1.metric("get_quote", f"{breakdown.get('quote_ms', 0):.0f}ms")
            col_api2.metric("ticker_details", f"{breakdown.get('ticker_details_ms', 0):.0f}ms")
            col_api3.metric("daily_bars", f"{breakdown.get('daily_bars_ms', 0):.0f}ms")
            col_api4.metric("avg_volume", f"{breakdown.get('avg_volume_ms', 0):.0f}ms")

        if ts.get("strategy_breakdown"):
            st.caption("Strategy evaluation timing (avg per ticker):")
            for strat_name in sorted(ts["strategy_breakdown"].keys()):
                calls = ts["strategy_breakdown"][strat_name]
                if calls:
                    st.write(f"**{strat_name}**")
                    cols = st.columns(len(calls))
                    for i, (call_name, ms_val) in enumerate(sorted(calls.items())):
                        cols[i].metric(call_name.replace("_ms", ""), f"{ms_val:.0f}ms")


def _add_from_scan(symbol: str, strategies: list[str]):
    """Add ticker to underlying and mark eligible with one or more strategies."""
    add_underlying(symbol)
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
                        value=row["notes"] or "",
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
    st.html("""
<style>
  [data-testid="stTable"] table { font-size: 11px; }
  [data-testid="stTable"] th,
  [data-testid="stTable"] td { padding: 3px 6px; line-height: 1.2; }
  [data-testid="stTable"] th,
  [data-testid="stTable"] td { border-color: rgba(255,255,255,0.08); }
  [data-testid="stExpander"] summary { padding: 6px 10px; font-size: 12px; }
  [data-testid="stExpander"] { border: 1px solid rgba(255,255,255,0.1) !important; border-radius: 6px; }
</style>
""")

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

    fast_mode = st.checkbox(
        "⚡ Fast Mode (skip VOL_PREMIUM — saves ~1s/ticker)",
        value=False,
        help="Skips volatility analysis. Use for quick screening; run full scan to confirm candidates.",
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

    # scan_state is a plain mutable dict — mutations from the background thread
    # are visible to the main thread because both share the same object reference.
    # Direct st.session_state writes from a thread don't work (disconnected context).
    if "scan_state" not in st.session_state:
        st.session_state["scan_state"] = {"running": False, "stop_event": None}

    scan = st.session_state["scan_state"]

    col_run, col_stop = st.columns([3, 1])

    with col_run:
        run_clicked = st.button(
            "▶ Run Scan",
            type="primary",
            disabled=scan["running"],
        )

    with col_stop:
        stop_clicked = st.button(
            "⏹ Stop",
            disabled=not scan["running"],
        )

    if stop_clicked:
        if scan.get("stop_event"):
            scan["stop_event"].set()
        scan["running"] = False
        st.rerun()

    if run_clicked:
        if universe_choice == "Custom list" and not tickers:
            st.error("Please enter at least one ticker.")
        else:
            if tickers:
                ticker_count = len(tickers)
            else:
                ticker_count = len(get_sp500_tickers())

            stop_event = threading.Event()
            scan.clear()
            scan.update({
                "running": True,
                "stop_event": stop_event,
                "progress": (0, ticker_count, ""),
                "results": None,
                "error": None,
            })

            def _run_scan():
                def progress_callback(i, total, symbol):
                    scan["progress"] = (i, total, symbol)

                try:
                    results, timing_stats = scan_universe(
                        tickers=tickers,
                        progress_callback=progress_callback,
                        stop_event=stop_event,
                        skip_strategies={"VOL_PREMIUM"} if fast_mode else None,
                    )
                    scan["results"] = results
                    scan["timing_stats"] = timing_stats
                except Exception as e:
                    scan["error"] = f"{type(e).__name__}: {str(e)}\n\n{traceback.format_exc()}"
                finally:
                    scan["running"] = False

            threading.Thread(target=_run_scan, daemon=True).start()

    if scan.get("error"):
        st.error(f"Scan failed: {scan['error']}")

    if scan["running"]:
        done, total, symbol = scan.get("progress", (0, 0, ""))
        pct = min(done / total, 0.99) if total > 0 else 0
        st.progress(pct, text=f"Scanning… ({done}/{total}) last: {symbol}")
        time.sleep(0.5)
        st.rerun()

    if scan.get("results"):
        results = scan["results"]

        full_passes = [r for r in results if r.get("passes_any")]
        partials = [r for r in results if not r.get("error") and not r.get("passes_any")]
        errors = [r for r in results if r.get("error")]

        col1, col2, col3 = st.columns(3)
        col1.metric("✅ Full Pass", len(full_passes))
        col2.metric("⚠️ Partial", len(partials))
        col3.metric("❌ Error", len(errors))

        if "timing_stats" in scan:
            _render_timing_stats(scan["timing_stats"])

        with st.expander("🔍 Filter & Sort", expanded=True):
            _fc1, _fc2 = st.columns(2)
            with _fc1:
                sort_by = st.selectbox(
                    "Sort by",
                    options=[
                        "Symbol (A→Z)", "Symbol (Z→A)",
                        "Price ↑", "Price ↓",
                        "Market Cap ↑", "Market Cap ↓",
                        "Strategies passing ↓",
                    ],
                    key="scan_sort_by",
                )
            with _fc2:
                show_groups = st.multiselect(
                    "Show groups",
                    options=["Full Passes", "Partial Matches", "Errors"],
                    default=["Full Passes", "Partial Matches", "Errors"],
                    key="scan_show_groups",
                )
            _fc3, _fc4, _fc5 = st.columns(3)
            with _fc3:
                required_strats = st.multiselect(
                    "Must pass strategies",
                    options=list(STRATEGIES.keys()),
                    default=[],
                    key="scan_required_strats",
                )
            with _fc4:
                price_min = st.number_input(
                    "Min price ($)", min_value=0.0, value=0.0, step=5.0, format="%.0f",
                    key="scan_price_min",
                )
            with _fc5:
                price_max = st.number_input(
                    "Max price ($)", min_value=0.0, value=0.0, step=5.0, format="%.0f",
                    help="0 = no limit", key="scan_price_max",
                )

        _sort_key_map = {
            "Symbol (A→Z)": (lambda r: r.get("symbol", ""), False),
            "Symbol (Z→A)": (lambda r: r.get("symbol", ""), True),
            "Price ↑": (lambda r: r.get("price") or 0.0, False),
            "Price ↓": (lambda r: r.get("price") or 0.0, True),
            "Market Cap ↑": (lambda r: r.get("market_cap_b") or 0.0, False),
            "Market Cap ↓": (lambda r: r.get("market_cap_b") or 0.0, True),
            "Strategies passing ↓": (
                lambda r: sum(1 for d in r.get("strategies", {}).values() if d.get("passes_all")),
                True,
            ),
        }
        _sk, _rev = _sort_key_map[sort_by]

        def _filter_and_sort(result_list):
            out = result_list
            if required_strats:
                out = [
                    r for r in out
                    if all(r.get("strategies", {}).get(s, {}).get("passes_all") for s in required_strats)
                ]
            if price_min > 0:
                out = [r for r in out if (r.get("price") or 0) >= price_min]
            if price_max > 0:
                out = [r for r in out if (r.get("price") or 0) <= price_max]
            return sorted(out, key=_sk, reverse=_rev)

        full_passes_shown = _filter_and_sort(full_passes) if "Full Passes" in show_groups else []
        partials_shown = _filter_and_sort(partials) if "Partial Matches" in show_groups else []
        errors_shown = errors if "Errors" in show_groups else []

        if full_passes_shown:
            st.subheader(f"✅ Full Passes ({len(full_passes_shown)})")
            for i, result in enumerate(full_passes_shown):
                _render_scan_result(result, collapsed=True, index=i)
        elif "Full Passes" in show_groups and full_passes:
            st.info("No full passes match the current filters.")

        if partials_shown:
            st.subheader(f"⚠️ Partial Matches ({len(partials_shown)})")
            for i, result in enumerate(partials_shown):
                _render_scan_result(result, collapsed=True, index=i)
        elif "Partial Matches" in show_groups and partials:
            st.info("No partial matches match the current filters.")

        if errors_shown:
            st.subheader(f"❌ Errors ({len(errors_shown)})")
            for result in errors_shown:
                with st.expander(f"{result['symbol']} — {result.get('error')}"):
                    st.error(result.get("error"))
