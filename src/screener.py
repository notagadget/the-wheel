"""
screener.py — Equity screening logic for wheel trading.

Filters underlying tickers by IV rank and earnings proximity,
ranks candidates by IV metrics, and flags warnings.
"""

from datetime import date, datetime, timedelta
from typing import Optional
from src.db import get_conn


def has_earnings_soon(earnings_date: Optional[str], dte_window: int = 7) -> bool:
    """
    Check if earnings are within dte_window days from today.

    Args:
        earnings_date: ISO date string (YYYY-MM-DD) or None
        dte_window: days ahead to look (default 7)

    Returns:
        True if earnings fall within [today, today + dte_window)
    """
    if not earnings_date:
        return False

    try:
        earnings = datetime.strptime(earnings_date, "%Y-%m-%d").date()
        today = date.today()
        delta = (earnings - today).days
        return 0 <= delta < dte_window
    except (ValueError, TypeError):
        return False


def get_screening_candidates(
    min_iv_rank: float = 50.0,
    exclude_earnings_window: int = 7,
    max_results: Optional[int] = None
) -> list[dict]:
    """
    Get ranked list of tickers suitable for wheel entry.

    Filters by:
    - wheel_eligible = 1 (hard quality gate, set via eligibility.py)
    - IV rank >= min_iv_rank (default 50% — elevated IV timing signal)
    - No active cycles in SHORT_PUT or LONG_STOCK state
    - Optionally excludes tickers with earnings within window

    Returns list of dicts with keys:
    - underlying_id, ticker, iv_rank_cached, iv_current, earnings_date, notes,
      eligible_strategy, last_reviewed, active_cycles, has_earnings_soon

    Sorted by iv_rank descending (highest IV rank first).
    """
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT
                u.underlying_id,
                u.ticker,
                u.iv_rank_cached,
                u.iv_pct_cached,
                u.iv_current,
                u.earnings_date,
                u.notes,
                u.iv_updated,
                u.eligible_strategy,
                u.last_reviewed,
                COUNT(CASE WHEN c.state IN ('SHORT_PUT', 'LONG_STOCK')
                      THEN 1 END)  AS active_cycles
            FROM underlying u
            LEFT JOIN cycle c ON c.underlying_id = u.underlying_id
            WHERE u.wheel_eligible = 1
              AND (u.iv_rank_cached >= ? OR u.iv_rank_cached IS NULL)
            GROUP BY u.underlying_id
            ORDER BY u.iv_rank_cached DESC NULLS LAST, u.ticker
        """, (min_iv_rank,)).fetchall()

    candidates = []
    for row in rows:
        earnings_soon = has_earnings_soon(row["earnings_date"], exclude_earnings_window)

        if row["active_cycles"] > 0:
            continue

        if earnings_soon:
            continue

        candidates.append({
            "underlying_id": row["underlying_id"],
            "ticker": row["ticker"],
            "iv_rank_cached": row["iv_rank_cached"],
            "iv_pct_cached": row["iv_pct_cached"],
            "iv_current": row["iv_current"],
            "earnings_date": row["earnings_date"],
            "notes": row["notes"],
            "iv_updated": row["iv_updated"],
            "eligible_strategy": row["eligible_strategy"],
            "last_reviewed": row["last_reviewed"],
            "has_earnings_soon": earnings_soon,
        })

    if max_results:
        candidates = candidates[:max_results]

    return candidates


def get_screening_candidates_by_strategy(
    strategy: str,
    min_iv_rank: float = 40.0,
    exclude_earnings_window: int = 30,
    max_results: Optional[int] = None,
) -> list[dict]:
    """
    Like get_screening_candidates() but filtered to a single strategy.

    Uses a lower default min_iv_rank (40 vs 50) because strategy eligibility
    already provides quality filtering.
    Uses a longer default earnings window (30 days) appropriate for 30-45 DTE cycles.
    """
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT
                u.underlying_id,
                u.ticker,
                u.iv_rank_cached,
                u.iv_pct_cached,
                u.iv_current,
                u.earnings_date,
                u.notes,
                u.iv_updated,
                u.eligible_strategy,
                u.last_reviewed,
                COUNT(CASE WHEN c.state IN ('SHORT_PUT', 'LONG_STOCK')
                      THEN 1 END)  AS active_cycles
            FROM underlying u
            LEFT JOIN cycle c ON c.underlying_id = u.underlying_id
            WHERE u.wheel_eligible = 1
              AND u.eligible_strategy = ?
              AND (u.iv_rank_cached >= ? OR u.iv_rank_cached IS NULL)
            GROUP BY u.underlying_id
            ORDER BY u.iv_rank_cached DESC NULLS LAST, u.ticker
        """, (strategy, min_iv_rank)).fetchall()

    candidates = []
    for row in rows:
        earnings_soon = has_earnings_soon(row["earnings_date"], exclude_earnings_window)

        if row["active_cycles"] > 0:
            continue

        if earnings_soon:
            continue

        candidates.append({
            "underlying_id": row["underlying_id"],
            "ticker": row["ticker"],
            "iv_rank_cached": row["iv_rank_cached"],
            "iv_pct_cached": row["iv_pct_cached"],
            "iv_current": row["iv_current"],
            "earnings_date": row["earnings_date"],
            "notes": row["notes"],
            "iv_updated": row["iv_updated"],
            "eligible_strategy": row["eligible_strategy"],
            "last_reviewed": row["last_reviewed"],
            "has_earnings_soon": earnings_soon,
        })

    if max_results:
        candidates = candidates[:max_results]

    return candidates


def get_all_watchlist(include_inactive: bool = False) -> list[dict]:
    """
    Get all tickers in watchlist, optionally filtering out inactive cycles.

    Args:
        include_inactive: if False, exclude tickers already in SHORT_PUT/LONG_STOCK state

    Returns list of dicts with watchlist data.
    """
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT
                u.underlying_id,
                u.ticker,
                u.iv_rank_cached,
                u.iv_pct_cached,
                u.iv_current,
                u.earnings_date,
                u.notes,
                u.iv_updated,
                COUNT(CASE WHEN c.state IN ('SHORT_PUT', 'LONG_STOCK')
                      THEN 1 END)  AS active_cycles
            FROM underlying u
            LEFT JOIN cycle c ON c.underlying_id = u.underlying_id
            GROUP BY u.underlying_id
            ORDER BY u.iv_rank_cached DESC NULLS LAST, u.ticker
        """).fetchall()

    watchlist = []
    for row in rows:
        if not include_inactive and row["active_cycles"] > 0:
            continue

        watchlist.append({
            "underlying_id": row["underlying_id"],
            "ticker": row["ticker"],
            "iv_rank_cached": row["iv_rank_cached"],
            "iv_pct_cached": row["iv_pct_cached"],
            "iv_current": row["iv_current"],
            "earnings_date": row["earnings_date"],
            "notes": row["notes"],
            "iv_updated": row["iv_updated"],
            "active_cycles": row["active_cycles"],
        })

    return watchlist
