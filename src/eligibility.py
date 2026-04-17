"""
eligibility.py — Wheel-eligibility gate for underlying tickers.

Single authority for setting wheel_eligible, eligible_strategy, quality_notes,
and last_reviewed on underlying rows. Analogous to how state_machine.py owns
cycle state — no other module may UPDATE wheel_eligible directly.
"""

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
    Optionally filter by eligible_strategy.
    """
    with get_conn() as conn:
        if strategy is not None:
            rows = conn.execute("""
                SELECT underlying_id, ticker, eligible_strategy, quality_notes,
                       last_reviewed, iv_rank_cached
                FROM underlying
                WHERE wheel_eligible = 1 AND eligible_strategy = ?
                ORDER BY ticker
            """, (strategy,)).fetchall()
        else:
            rows = conn.execute("""
                SELECT underlying_id, ticker, eligible_strategy, quality_notes,
                       last_reviewed, iv_rank_cached
                FROM underlying
                WHERE wheel_eligible = 1
                ORDER BY ticker
            """).fetchall()

    return [dict(r) for r in rows]


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
    strategy: str | None,
    quality_notes: str | None,
) -> None:
    """
    Set wheel_eligible, eligible_strategy, quality_notes, and last_reviewed for a ticker.

    Raises ValueError if eligible=True and strategy is None or not in STRATEGIES.
    When eligible=False, clears eligible_strategy regardless of what is passed.
    """
    if eligible:
        if strategy is None:
            raise ValueError("strategy is required when eligible=True")
        if strategy not in STRATEGIES:
            raise ValueError(
                f"Invalid strategy {strategy!r}. "
                f"Must be one of: {', '.join(STRATEGIES)}"
            )
        set_strategy = strategy
    else:
        set_strategy = None

    with get_conn() as conn:
        conn.execute("""
            UPDATE underlying
            SET wheel_eligible    = ?,
                eligible_strategy = ?,
                quality_notes     = ?,
                last_reviewed     = ?
            WHERE ticker = ?
        """, (
            1 if eligible else 0,
            set_strategy,
            quality_notes,
            date.today().isoformat(),
            ticker,
        ))


def get_strategy_description(strategy: str) -> str:
    """Return the description string for a strategy. Raises KeyError if unknown."""
    return STRATEGIES[strategy]["description"]
