"""
eligibility.py — Wheel-eligibility gate for underlying tickers.

Single authority for setting wheel_eligible, strategy tags, per-strategy notes,
and last_reviewed on underlying rows. Analogous to how state_machine.py owns
cycle state — no other module may UPDATE wheel_eligible directly.

Multi-strategy: a ticker may qualify under multiple STRATEGIES simultaneously.
The canonical store is underlying_strategy. Per-strategy rationale lives in
underlying_strategy.quality_notes; generic ticker notes live in underlying.notes.
"""

import sqlite3
from datetime import date
from src.db import get_conn

STRATEGIES: dict[str, dict] = {
    "FUNDAMENTAL": {
        "description": "Profitable, low-debt companies you'd hold long-term.",
        "min_price": 10.0,
        "max_price": 150.0,
        "min_market_cap_b": 2.0,
        "min_avg_volume": 500_000,
        "requires_positive_cashflow": True,
        "max_debt_equity": 1.5,
        "excluded_sectors": ["Financial Services", "Utilities", "Real Estate"],
    },
    "TECHNICAL": {
        "description": "Stocks above 200-day MA with defined support levels.",
        "min_price": 10.0,
        "max_price": 150.0,
        "min_market_cap_b": 1.0,
        "min_avg_volume": 300_000,
        "min_pct_above_200dma": 3.0,
        "rsi_min": 30.0,
        "rsi_max": 65.0,
    },
    "ETF_COMPONENT": {
        "description": "Top-50 holdings of major sector ETFs (institutional support).",
        "min_price": 10.0,
        "max_price": 200.0,
        "min_market_cap_b": 2.0,
        "min_avg_volume": 1_000_000,
        "min_institutional_ownership_pct": 60.0,
        "min_pct_above_200dma": 3.0,
    },
    "VOL_PREMIUM": {
        "description": "Stocks where IV chronically exceeds realized HV (vol risk premium).",
        "min_price": 10.0,
        "max_price": 100.0,
        "min_market_cap_b": 1.0,
        "min_avg_volume": 500_000,
        "min_iv_hv_ratio": 1.3,
        "min_iv_rank": 50.0,
    },
}

STRATEGY_LABELS = {
    "FUNDAMENTAL": "Fundamental",
    "TECHNICAL": "Technical",
    "ETF_COMPONENT": "ETF",
    "VOL_PREMIUM": "Vol Premium",
}


def get_eligible_underlyings(strategy: str | None = None) -> list[dict]:
    """
    Return all underlyings where wheel_eligible = 1, sorted by ticker.
    Each row includes strategies: list[str], conviction: int, and
    quality_notes (combined across strategies, pipe-separated).
    Optionally filter to tickers that have at least one row for strategy.
    """
    with get_conn() as conn:
        if strategy is not None:
            rows = conn.execute("""
                SELECT u.underlying_id, u.ticker, u.last_reviewed, u.iv_rank_cached,
                       GROUP_CONCAT(us.strategy, ',') AS strategies_csv,
                       GROUP_CONCAT(COALESCE(us.quality_notes, ''), '|') AS notes_csv
                FROM underlying u
                JOIN underlying_strategy us ON us.underlying_id = u.underlying_id
                WHERE u.wheel_eligible = 1
                  AND u.underlying_id IN (
                      SELECT underlying_id FROM underlying_strategy WHERE strategy = ?
                  )
                GROUP BY u.underlying_id
                ORDER BY u.ticker
            """, (strategy,)).fetchall()
        else:
            rows = conn.execute("""
                SELECT u.underlying_id, u.ticker, u.last_reviewed, u.iv_rank_cached,
                       GROUP_CONCAT(us.strategy, ',') AS strategies_csv,
                       GROUP_CONCAT(COALESCE(us.quality_notes, ''), '|') AS notes_csv
                FROM underlying u
                JOIN underlying_strategy us ON us.underlying_id = u.underlying_id
                WHERE u.wheel_eligible = 1
                GROUP BY u.underlying_id
                ORDER BY u.ticker
            """).fetchall()

    result = []
    for row in rows:
        d = dict(row)
        csv = d.pop("strategies_csv") or ""
        d["strategies"] = csv.split(",") if csv else []
        d["conviction"] = len(d["strategies"])
        notes_raw = d.pop("notes_csv") or ""
        distinct_notes = []
        for n in notes_raw.split("|"):
            n = n.strip()
            if n and n not in distinct_notes:
                distinct_notes.append(n)
        d["quality_notes"] = " | ".join(distinct_notes) if distinct_notes else None
        result.append(d)
    return result


def get_ineligible_underlyings() -> list[dict]:
    """Return all underlyings where wheel_eligible = 0, sorted by ticker."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT underlying_id, ticker, notes,
                   last_reviewed, iv_rank_cached
            FROM underlying
            WHERE wheel_eligible = 0
            ORDER BY ticker
        """).fetchall()

    return [dict(r) for r in rows]


def update_eligibility(
    ticker: str,
    eligible: bool,
    strategies: list[str] | None,
    quality_notes: str | None,
) -> None:
    """
    Set wheel_eligible and strategy tags for a ticker.

    When eligible=True: strategies must be a non-empty list of valid STRATEGIES keys.
    Fully replaces any existing strategy tags (delete-then-insert).
    quality_notes is stored per-strategy on underlying_strategy.

    When eligible=False: clears all underlying_strategy rows, sets wheel_eligible=0,
    and stores quality_notes on underlying.notes for retention.
    """
    if eligible:
        if not strategies:
            raise ValueError("at least one strategy is required when eligible=True")
        for s in strategies:
            if s not in STRATEGIES:
                raise ValueError(
                    f"Invalid strategy {s!r}. "
                    f"Must be one of: {', '.join(STRATEGIES)}"
                )
        today = date.today().isoformat()

    with get_conn() as conn:
        row = conn.execute(
            "SELECT underlying_id FROM underlying WHERE ticker = ?", (ticker,)
        ).fetchone()
        if row is None:
            raise ValueError(f"Ticker {ticker!r} not found.")
        underlying_id = row["underlying_id"]

        if eligible:
            conn.execute("""
                UPDATE underlying
                SET wheel_eligible = 1,
                    last_reviewed  = ?
                WHERE underlying_id = ?
            """, (today, underlying_id))

            conn.execute(
                "DELETE FROM underlying_strategy WHERE underlying_id = ?",
                (underlying_id,),
            )
            for strategy in strategies:
                conn.execute("""
                    INSERT INTO underlying_strategy
                        (underlying_id, strategy, quality_notes, added_date)
                    VALUES (?, ?, ?, ?)
                """, (underlying_id, strategy, quality_notes, today))
        else:
            conn.execute("""
                UPDATE underlying
                SET wheel_eligible = 0,
                    notes          = ?,
                    last_reviewed  = ?
                WHERE underlying_id = ?
            """, (quality_notes, date.today().isoformat(), underlying_id))

            conn.execute(
                "DELETE FROM underlying_strategy WHERE underlying_id = ?",
                (underlying_id,),
            )


def remove_strategy(ticker: str, strategy: str) -> None:
    """
    Remove a single strategy tag from a ticker.
    If no strategy tags remain, sets wheel_eligible=0 automatically.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT underlying_id FROM underlying WHERE ticker = ?", (ticker,)
        ).fetchone()
        if row is None:
            return
        underlying_id = row["underlying_id"]

        conn.execute(
            "DELETE FROM underlying_strategy WHERE underlying_id = ? AND strategy = ?",
            (underlying_id, strategy),
        )

        remaining = conn.execute(
            "SELECT COUNT(*) AS cnt FROM underlying_strategy WHERE underlying_id = ?",
            (underlying_id,),
        ).fetchone()

        if remaining["cnt"] == 0:
            conn.execute(
                "UPDATE underlying SET wheel_eligible = 0 WHERE underlying_id = ?",
                (underlying_id,),
            )


def conviction_score(ticker: str) -> int:
    """Return the number of strategy tags for a ticker (0–4)."""
    with get_conn() as conn:
        row = conn.execute("""
            SELECT COUNT(*) AS cnt
            FROM underlying_strategy us
            JOIN underlying u ON u.underlying_id = us.underlying_id
            WHERE u.ticker = ?
        """, (ticker,)).fetchone()
    return row["cnt"] if row else 0


def remove_underlying(ticker: str) -> None:
    """
    Delete an underlying row by ticker.

    Raises ValueError if any cycle rows reference this underlying (FK constraint).
    """
    with get_conn() as conn:
        try:
            conn.execute("DELETE FROM underlying WHERE ticker = ?", (ticker,))
        except sqlite3.IntegrityError:
            raise ValueError(
                f"Cannot remove {ticker}: active or historical cycles exist."
            )


def add_underlying(ticker: str, notes: str | None = None) -> None:
    """Insert an underlying row if not already present. Idempotent."""
    ticker = ticker.upper().strip()
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO underlying (underlying_id, ticker, notes) "
            "VALUES (?, ?, ?)",
            (ticker, ticker, notes),
        )


def get_strategy_description(strategy: str) -> str:
    """Return the description string for a strategy. Raises KeyError if unknown."""
    return STRATEGIES[strategy]["description"]
