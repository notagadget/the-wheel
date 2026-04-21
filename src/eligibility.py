"""
eligibility.py — Wheel-eligibility gate for underlying tickers.

Single authority for setting wheel_eligible, eligible_strategy, quality_notes,
and last_reviewed on underlying rows. Analogous to how state_machine.py owns
cycle state — no other module may UPDATE wheel_eligible directly.

Multi-strategy: a ticker may qualify under multiple STRATEGIES simultaneously.
The canonical store is underlying_strategy; underlying.eligible_strategy is kept
as a backward-compat column (set to strategies[0]) until all reads are migrated.
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
    },
    "TECHNICAL": {
        "description": "Stocks above 200-day MA with defined support levels.",
        "min_price": 10.0,
        "max_price": 150.0,
        "min_market_cap_b": 1.0,
        "min_avg_volume": 300_000,
        "above_200dma": True,
        "rsi_min": 35.0,
        "rsi_max": 65.0,
    },
    "ETF_COMPONENT": {
        "description": "Top-50 holdings of major sector ETFs (institutional support).",
        "min_price": 10.0,
        "max_price": 200.0,
        "min_market_cap_b": 2.0,
        "min_avg_volume": 1_000_000,
        "min_institutional_ownership_pct": 60.0,
    },
    "VOL_PREMIUM": {
        "description": "Stocks where IV chronically exceeds realized HV (vol risk premium).",
        "min_price": 10.0,
        "max_price": 100.0,
        "min_market_cap_b": 0.5,
        "min_avg_volume": 200_000,
        "min_iv_hv_ratio": 1.2,
        "min_iv_rank": 40.0,
    },
}


def get_eligible_underlyings(strategy: str | None = None) -> list[dict]:
    """
    Return all underlyings where wheel_eligible = 1, sorted by ticker.
    Each row includes strategies: list[str] and conviction: int.
    Optionally filter to tickers that have at least one row for strategy.
    """
    with get_conn() as conn:
        if strategy is not None:
            rows = conn.execute("""
                SELECT u.underlying_id, u.ticker, u.quality_notes,
                       u.last_reviewed, u.iv_rank_cached,
                       GROUP_CONCAT(us.strategy, ',') AS strategies_csv
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
                SELECT u.underlying_id, u.ticker, u.quality_notes,
                       u.last_reviewed, u.iv_rank_cached,
                       GROUP_CONCAT(us.strategy, ',') AS strategies_csv
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
        result.append(d)
    return result


def get_ineligible_underlyings() -> list[dict]:
    """Return all underlyings where wheel_eligible = 0, sorted by ticker."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT underlying_id, ticker, eligible_strategy, quality_notes,
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
    Also sets underlying.eligible_strategy = strategies[0] for backward compat.

    When eligible=False: clears all underlying_strategy rows and sets wheel_eligible=0.
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
                SET wheel_eligible    = 1,
                    eligible_strategy = ?,
                    quality_notes     = ?,
                    last_reviewed     = ?
                WHERE underlying_id = ?
            """, (strategies[0], quality_notes, today, underlying_id))

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
                SET wheel_eligible    = 0,
                    eligible_strategy = NULL,
                    quality_notes     = ?,
                    last_reviewed     = ?
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
            "SELECT strategy FROM underlying_strategy WHERE underlying_id = ? ORDER BY strategy LIMIT 1",
            (underlying_id,),
        ).fetchone()

        if remaining is None:
            conn.execute(
                "UPDATE underlying SET wheel_eligible = 0, eligible_strategy = NULL WHERE underlying_id = ?",
                (underlying_id,),
            )
        else:
            # Keep eligible_strategy in sync with first remaining (backward compat)
            conn.execute(
                "UPDATE underlying SET eligible_strategy = ? WHERE underlying_id = ?",
                (remaining["strategy"], underlying_id),
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
