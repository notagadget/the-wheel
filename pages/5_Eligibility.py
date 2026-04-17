"""Eligibility page — manage wheel_eligible flag and strategy assignment."""

import streamlit as st
from src.eligibility import (
    STRATEGIES,
    get_eligible_underlyings,
    get_ineligible_underlyings,
    update_eligibility,
)

st.title("Wheel Eligibility")

tab_eligible, tab_review = st.tabs(["Eligible", "Review Queue"])

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
