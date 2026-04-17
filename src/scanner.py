"""
scanner.py — Evaluates tickers against wheel strategy criteria.

Pure screening logic, no DB writes. Returns criterion results as dicts.
"""

import time
from typing import Callable, Optional

from src.eligibility import STRATEGIES
from src import massive


def scan_ticker(symbol: str, strategy: str) -> dict:
    """
    Scan a single ticker against a strategy's criteria.

    Returns dict with keys:
    - symbol, strategy, error (if failed), criteria, passes_all, name, price, market_cap_b

    Each criterion result is {passed: bool|None, value: any, threshold: any, note: str}.
    passed=None means manual review required.
    Raises ValueError for unknown strategy.
    On MassiveError fetching prev_close or ticker_details, returns early with error dict.
    """
    if strategy not in STRATEGIES:
        raise ValueError(f"Unknown strategy: {strategy}")

    strat_config = STRATEGIES[strategy]

    # Fetch data upfront — if basic data fails, return error
    try:
        prev_close_data = massive.get_prev_close(symbol)
        ticker_details = massive.get_ticker_details(symbol)
    except massive.MassiveError as e:
        return {
            "symbol": symbol,
            "strategy": strategy,
            "error": str(e),
            "criteria": {},
            "passes_all": False,
            "name": None,
            "price": None,
            "market_cap_b": None,
        }

    price = prev_close_data.get("close")
    market_cap_b = ticker_details.get("market_cap_b")
    name = ticker_details.get("name")

    criteria = {}

    # Common criteria (all strategies)
    criteria["min_price"] = {
        "passed": price >= strat_config["min_price"] if price else None,
        "value": price,
        "threshold": strat_config["min_price"],
        "note": "",
    }
    criteria["max_price"] = {
        "passed": price <= strat_config["max_price"] if price else None,
        "value": price,
        "threshold": strat_config["max_price"],
        "note": "",
    }
    criteria["min_market_cap_b"] = {
        "passed": market_cap_b >= strat_config["min_market_cap_b"] if market_cap_b else None,
        "value": market_cap_b,
        "threshold": strat_config["min_market_cap_b"],
        "note": "",
    }

    # Compute min_avg_volume (45 calendar days ≈ 30 trading days)
    daily_bars = massive.get_daily_bars(symbol, days=45)
    avg_volume = massive.compute_avg_volume(daily_bars)
    criteria["min_avg_volume"] = {
        "passed": avg_volume >= strat_config["min_avg_volume"] if avg_volume else None,
        "value": avg_volume,
        "threshold": strat_config["min_avg_volume"],
        "note": "",
    }

    # Strategy-specific criteria
    if strategy == "TECHNICAL":
        # above_200dma
        sma_200 = massive.get_sma(symbol, window=200)
        criteria["above_200dma"] = {
            "passed": price > sma_200 if (price and sma_200) else None,
            "value": {"price": price, "sma_200": sma_200},
            "threshold": "price > 200-day SMA",
            "note": "",
        }

        # rsi
        bars_for_rsi = massive.get_daily_bars(symbol, days=60)
        rsi = massive.compute_rsi(bars_for_rsi, period=14)
        rsi_min = strat_config.get("rsi_min", 30.0)
        rsi_max = strat_config.get("rsi_max", 70.0)
        criteria["rsi"] = {
            "passed": rsi_min <= rsi <= rsi_max if rsi is not None else None,
            "value": rsi,
            "threshold": f"{rsi_min}–{rsi_max}",
            "note": "",
        }

    elif strategy == "FUNDAMENTAL":
        criteria["requires_positive_cashflow"] = {
            "passed": None,
            "value": None,
            "threshold": "Positive FCF",
            "note": "Requires Massive paid plan — verify manually via financials.",
        }
        criteria["max_debt_equity"] = {
            "passed": None,
            "value": None,
            "threshold": strat_config.get("max_debt_equity", 1.5),
            "note": "Requires Massive paid plan — verify manually via financials.",
        }

    elif strategy == "ETF_COMPONENT":
        criteria["min_institutional_ownership_pct"] = {
            "passed": None,
            "value": None,
            "threshold": strat_config.get("min_institutional_ownership_pct", 60.0),
            "note": "Not available via Massive — verify manually (13F filings, etc.).",
        }

    elif strategy == "VOL_PREMIUM":
        criteria["min_iv_hv_ratio"] = {
            "passed": None,
            "value": None,
            "threshold": strat_config.get("min_iv_hv_ratio", 1.2),
            "note": "Sourced from Tradier — check iv_rank_cached after adding to watchlist.",
        }
        criteria["min_iv_rank"] = {
            "passed": None,
            "value": None,
            "threshold": strat_config.get("min_iv_rank", 40.0),
            "note": "Sourced from Tradier — check iv_rank_cached after adding to watchlist.",
        }

    # passes_all: True only if every criterion where passed is not None is True
    passes_all = all(
        crit["passed"] is True
        for crit in criteria.values()
        if crit["passed"] is not None
    ) and any(crit["passed"] is not None for crit in criteria.values())

    return {
        "symbol": symbol,
        "strategy": strategy,
        "error": None,
        "criteria": criteria,
        "passes_all": passes_all,
        "name": name,
        "price": price,
        "market_cap_b": market_cap_b,
    }


def scan_universe(
    strategy: str,
    tickers: Optional[list[str]] = None,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
) -> list[dict]:
    """
    Scan a universe of tickers against a strategy.

    Defaults tickers to get_sp500_tickers().
    Calls progress_callback(i, total, symbol) if provided.
    Sleeps 0.25s between calls (free tier rate limit).
    Sorts results: passes_all=True first, then by descending count of passed=True,
    errors last.
    """
    if tickers is None:
        tickers = massive.get_sp500_tickers()

    results = []
    for i, symbol in enumerate(tickers):
        if progress_callback:
            progress_callback(i, len(tickers), symbol)

        try:
            result = scan_ticker(symbol, strategy)
            results.append(result)
        except ValueError:
            # Unknown strategy — should not happen if called correctly
            raise

        time.sleep(0.25)  # Rate limit

    # Sort: passes_all=True first, then by passed count desc, errors last
    def sort_key(r):
        if r.get("error"):
            return (2, 0)  # errors last
        if r.get("passes_all"):
            return (0, 999)  # full passes first (high sort value)
        # Partial: count True criteria
        passed_count = sum(
            1 for crit in r.get("criteria", {}).values()
            if crit.get("passed") is True
        )
        return (1, -passed_count)  # partials by passed count desc

    results.sort(key=sort_key)
    return results
