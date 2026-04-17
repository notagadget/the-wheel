"""
scanner.py — Evaluates tickers against wheel strategy criteria.

Pure screening logic, no DB writes. Returns criterion results as dicts.
Uses Tradier for quotes and price history; Massive for company info only.
"""

import time
from typing import Callable, Optional
from functools import lru_cache
from datetime import date, timedelta

from src.eligibility import STRATEGIES
from src import tradier, massive


def _format_si(value: float) -> str:
    """Format a number using SI units (K, M, B)."""
    if value is None:
        return "—"
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f}B"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return f"{value:.0f}"


@lru_cache(maxsize=256)
def _get_daily_bars_tradier(symbol: str, days: int) -> list[dict]:
    """Fetch daily bars from Tradier (cheaper than Massive)."""
    to_date = date.today().isoformat()
    from_date = (date.today() - timedelta(days=days)).isoformat()

    try:
        resp = tradier._get("/v1/markets/history", {
            "symbol": symbol,
            "interval": "daily",
            "start": from_date,
            "end": to_date,
        })
    except Exception:
        return []

    history = resp.get("history", {}).get("day", [])
    if isinstance(history, dict):
        history = [history]

    return [
        {
            "date": bar.get("date"),
            "open": float(bar.get("open")) if bar.get("open") else None,
            "high": float(bar.get("high")) if bar.get("high") else None,
            "low": float(bar.get("low")) if bar.get("low") else None,
            "close": float(bar.get("close")) if bar.get("close") else None,
            "volume": float(bar.get("volume")) if bar.get("volume") else None,
        }
        for bar in history
    ]


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
        # Use Tradier for quote (faster, included in subscription)
        quote = tradier.get_quote(symbol)
        price = quote.get("last")

        # Use Massive only for company details (name, market cap)
        ticker_details = massive.get_ticker_details(symbol)
    except (tradier.TradierError, massive.MassiveError) as e:
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
    daily_bars = _get_daily_bars_tradier(symbol, days=45)
    avg_volume = massive.compute_avg_volume(daily_bars)
    criteria["min_avg_volume"] = {
        "passed": avg_volume >= strat_config["min_avg_volume"] if avg_volume else None,
        "value": avg_volume,
        "threshold": strat_config["min_avg_volume"],
        "note": "",
    }

    # Strategy-specific criteria
    if strategy == "TECHNICAL":
        # Get 200-day SMA
        sma_200 = massive.get_sma(symbol, window=200)
        criteria["above_200dma"] = {
            "passed": price > sma_200 if price and sma_200 else None,
            "value": sma_200,
            "threshold": "price > 200-day SMA",
            "note": f"Current SMA: ${sma_200:.2f}" if sma_200 else "Unable to fetch SMA",
        }

        # Compute RSI from daily bars
        rsi_min = strat_config.get("rsi_min", 30.0)
        daily_bars_rsi = massive.get_daily_bars(symbol, days=30)
        rsi = massive.compute_rsi(daily_bars_rsi, period=14) if daily_bars_rsi else None
        criteria["rsi"] = {
            "passed": rsi >= rsi_min if rsi else None,
            "value": rsi,
            "threshold": rsi_min,
            "note": f"RSI(14): {rsi:.1f}" if rsi else "Unable to compute RSI",
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
        # Compute IV/HV ratio: current IV ÷ current (realized) HV
        # Note: get_current_iv returns decimal (0.35), get_historical_iv returns % (35.0)
        # Convert current IV to percentage for consistent units
        min_iv_hv = strat_config.get("min_iv_hv_ratio", 1.2)
        iv_hv_ratio = None
        note = ""
        current_iv = None
        current_hv = None
        hv_series = None
        try:
            from src.market_data import get_current_iv
            current_iv = get_current_iv(symbol)
            if current_iv:
                current_iv_pct = current_iv * 100  # convert decimal to percentage
                hv_series = tradier.get_historical_iv(symbol, days=365)
                if hv_series:
                    current_hv = hv_series[-1]["iv"]  # most recent HV (already in %)
                    if current_hv and current_hv > 0:
                        iv_hv_ratio = round(current_iv_pct / current_hv, 2)
        except Exception as e:
            note = f"Error: {str(e)}"

        if iv_hv_ratio:
            note = f"IV: {current_iv_pct:.2f}%, HV: {current_hv:.2f}%"
        elif not note:
            note = "Unable to fetch IV/HV data from Tradier"

        criteria["min_iv_hv_ratio"] = {
            "passed": iv_hv_ratio >= min_iv_hv if iv_hv_ratio else None,
            "value": iv_hv_ratio,
            "threshold": min_iv_hv,
            "note": note,
        }
        # Compute IV rank: (current_iv - 52w_low) / (52w_high - 52w_low) * 100
        min_iv_rank = strat_config.get("min_iv_rank", 40.0)
        iv_rank = None
        iv_rank_note = ""
        if current_iv and hv_series:
            try:
                from src.market_data import compute_iv_metrics
                current_iv_pct = current_iv * 100  # convert decimal to percentage
                metrics = compute_iv_metrics(hv_series, current_iv_pct)
                iv_rank = metrics.get("iv_rank")
                iv_52w_high = metrics.get("iv_52w_high")
                iv_52w_low = metrics.get("iv_52w_low")
                if iv_rank is not None:
                    iv_rank_note = f"IV rank: {iv_rank:.1f}% (52w: {iv_52w_low:.2f}–{iv_52w_high:.2f})"
            except Exception as e:
                iv_rank_note = f"Error: {str(e)}"
        else:
            iv_rank_note = "Unable to fetch IV history from Tradier"

        criteria["min_iv_rank"] = {
            "passed": iv_rank >= min_iv_rank if iv_rank is not None else None,
            "value": iv_rank,
            "threshold": min_iv_rank,
            "note": iv_rank_note,
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

        time.sleep(1.0)  # Rate limit (Tradier: 250 req/hr ≈ 4 req/sec)

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
