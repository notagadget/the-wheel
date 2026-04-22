"""
scanner.py — Evaluates tickers against all wheel strategy criteria.

Pure screening logic, no DB writes. Returns criterion results as dicts.
Uses Tradier for quotes and price history; Massive for company info only.
"""

import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional
from functools import lru_cache
from datetime import date, timedelta

from src.eligibility import STRATEGIES
from src import tradier, massive


def _format_si(value: float) -> str:
    """Format a number using SI units (K, M, B)."""
    if value is None:
        return "—"
    abs_val = abs(value)
    if abs_val >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f}B"
    if abs_val >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if abs_val >= 1_000:
        return f"{value / 1_000:.1f}K"
    return f"{value:.0f}"


def _compute_sma(bars: list[dict], window: int = 200) -> float | None:
    """Compute simple moving average from the last `window` closing prices."""
    closes = [b["close"] for b in bars if b.get("close") is not None]
    if len(closes) < window:
        return None
    return sum(closes[-window:]) / window


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


def _fetch_common_data(symbol: str, quotes_cache: dict | None = None) -> dict:
    """
    Fetch data needed by all strategies: quote, ticker details, daily bars.
    quotes_cache: pre-fetched quotes dict to avoid redundant API calls.
    Returns error key if the fetch fails, otherwise price/name/market_cap_b/daily_bars/avg_volume.
    Also includes _fetch_profile with per-call timings.
    """
    fetch_profile = {}

    try:
        t = time.time()
        if quotes_cache and symbol in quotes_cache:
            quote = quotes_cache[symbol]
        else:
            quote = tradier.get_quote(symbol)
        fetch_profile["quote_ms"] = (time.time() - t) * 1000
        price = quote.get("last")

        t = time.time()
        ticker_details = massive.get_ticker_details(symbol)
        fetch_profile["ticker_details_ms"] = (time.time() - t) * 1000
    except (tradier.TradierError, massive.MassiveError) as e:
        return {"error": str(e), "_fetch_profile": fetch_profile}

    t = time.time()
    daily_bars = _get_daily_bars_tradier(symbol, days=300)
    fetch_profile["daily_bars_ms"] = (time.time() - t) * 1000

    t = time.time()
    avg_volume = massive.compute_avg_volume(daily_bars)
    fetch_profile["avg_volume_ms"] = (time.time() - t) * 1000

    return {
        "error": None,
        "price": price,
        "market_cap_b": ticker_details.get("market_cap_b"),
        "name": ticker_details.get("name"),
        "daily_bars": daily_bars,
        "avg_volume": avg_volume,
        "_fetch_profile": fetch_profile,
    }


def _evaluate_strategy(symbol: str, strategy: str, common_data: dict, hiv_cache: dict | None = None) -> dict:
    """
    Evaluate a single strategy given pre-fetched common data.
    hiv_cache: pre-fetched historical IV dict to avoid redundant API calls.
    Returns {criteria, passes_all, _strategy_profile} with timing breakdown.
    """
    strat_config = STRATEGIES[strategy]
    price = common_data["price"]
    market_cap_b = common_data["market_cap_b"]
    avg_volume = common_data["avg_volume"]
    strat_profile = {}

    criteria = {}

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
    criteria["min_avg_volume"] = {
        "passed": avg_volume >= strat_config["min_avg_volume"] if avg_volume else None,
        "value": avg_volume,
        "threshold": strat_config["min_avg_volume"],
        "note": "",
    }

    if strategy == "TECHNICAL":
        # SMA computed locally from already-fetched Tradier bars — no extra API call
        sma_200 = _compute_sma(common_data["daily_bars"], window=200)
        criteria["above_200dma"] = {
            "passed": price > sma_200 if price and sma_200 else None,
            "value": sma_200,
            "threshold": "SMA-200",
            "note": "Insufficient bar data for SMA-200" if not sma_200 else "",
        }

        rsi_min = strat_config.get("rsi_min", 30.0)
        # Use last 30 bars from already-fetched Tradier data — no extra API call
        daily_bars_rsi = list(common_data["daily_bars"][-30:]) if common_data["daily_bars"] else []
        rsi = massive.compute_rsi(daily_bars_rsi, period=14) if daily_bars_rsi else None
        criteria["rsi"] = {
            "passed": rsi >= rsi_min if rsi else None,
            "value": rsi,
            "threshold": rsi_min,
            "note": "Unable to compute RSI" if rsi is None else "",
        }

    elif strategy == "FUNDAMENTAL":
        t = time.time()
        from src.yfinance_data import get_fundamentals as _get_fundamentals
        fundamentals = _get_fundamentals(symbol)
        strat_profile["fundamentals_ms"] = (time.time() - t) * 1000

        fcf = fundamentals.get("free_cash_flow")
        fcf_display = fcf
        fcf_note = "FCF unavailable" if fcf is None else ""

        criteria["requires_positive_cashflow"] = {
            "passed": fcf > 0 if fcf is not None else None,
            "value": fcf_display,
            "threshold": "Positive",
            "note": fcf_note,
        }

        de_ratio = fundamentals.get("debt_to_equity")
        max_de = strat_config.get("max_debt_equity", 1.5)
        criteria["max_debt_equity"] = {
            "passed": de_ratio <= max_de if de_ratio is not None else None,
            "value": de_ratio,
            "threshold": max_de,
            "note": "D/E unavailable" if de_ratio is None else "",
        }

    elif strategy == "ETF_COMPONENT":
        from src.yfinance_data import get_institutional_ownership_pct
        min_inst = strat_config.get("min_institutional_ownership_pct", 60.0)
        t = time.time()
        inst_pct = get_institutional_ownership_pct(symbol)
        strat_profile["institutional_ownership_ms"] = (time.time() - t) * 1000
        criteria["min_institutional_ownership_pct"] = {
            "passed": inst_pct >= min_inst if inst_pct is not None else None,
            "value": inst_pct,
            "threshold": min_inst,
            "note": "yfinance unavailable" if inst_pct is None else "",
        }

    elif strategy == "VOL_PREMIUM":
        min_iv_hv = strat_config.get("min_iv_hv_ratio", 1.2)
        iv_hv_ratio = None
        note = ""
        current_iv = None
        current_hv = None
        hv_series = None
        try:
            from src.market_data import get_current_iv
            t = time.time()
            current_iv = get_current_iv(symbol)
            strat_profile["current_iv_ms"] = (time.time() - t) * 1000
            if current_iv:
                current_iv_pct = current_iv * 100
                t = time.time()
                if hiv_cache and symbol in hiv_cache:
                    hv_series = hiv_cache[symbol]
                else:
                    hv_series = tradier.get_historical_iv(symbol, days=365)
                strat_profile["historical_iv_ms"] = (time.time() - t) * 1000
                if hv_series:
                    current_hv = hv_series[-1]["iv"]
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

        min_iv_rank = strat_config.get("min_iv_rank", 40.0)
        iv_rank = None
        iv_rank_note = ""
        if current_iv and hv_series:
            try:
                from src.market_data import compute_iv_metrics
                current_iv_pct = current_iv * 100
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

    passes_all = all(
        crit["passed"] is True
        for crit in criteria.values()
        if crit["passed"] is not None
    ) and any(crit["passed"] is not None for crit in criteria.values())

    return {"criteria": criteria, "passes_all": passes_all, "_strategy_profile": strat_profile}


def scan_ticker(
    symbol: str,
    quotes_cache: dict | None = None,
    hiv_cache: dict | None = None,
    skip_strategies: set[str] | None = None,
) -> dict:
    """
    Scan a single ticker against all strategies.

    Fetches market data once, then evaluates every strategy in STRATEGIES.
    quotes_cache: pre-fetched quotes dict to avoid redundant API calls.
    hiv_cache: pre-fetched historical IV dict to avoid redundant API calls.
    Returns dict with keys:
    - symbol, name, price, market_cap_b, error
    - strategies: {strategy_name: {criteria, passes_all}}
    - passes_any: True if at least one strategy has passes_all=True
    - _timing: {fetch_ms, evaluate_ms, total_ms} for debugging
    """
    start = time.time()

    fetch_start = time.time()
    common_data = _fetch_common_data(symbol, quotes_cache=quotes_cache)
    fetch_ms = (time.time() - fetch_start) * 1000
    fetch_profile = common_data.pop("_fetch_profile", {})

    if common_data.get("error"):
        return {
            "symbol": symbol,
            "error": common_data["error"],
            "strategies": {},
            "passes_any": False,
            "name": None,
            "price": None,
            "market_cap_b": None,
            "_timing": {"fetch_ms": fetch_ms, "evaluate_ms": 0, "total_ms": (time.time() - start) * 1000, "fetch_profile": fetch_profile},
        }

    eval_start = time.time()
    strategies = {
        strategy: _evaluate_strategy(symbol, strategy, common_data, hiv_cache=hiv_cache)
        for strategy in STRATEGIES
        if not skip_strategies or strategy not in skip_strategies
    }
    eval_ms = (time.time() - eval_start) * 1000

    total_ms = (time.time() - start) * 1000

    return {
        "symbol": symbol,
        "error": None,
        "strategies": strategies,
        "passes_any": any(s["passes_all"] for s in strategies.values()),
        "name": common_data["name"],
        "price": common_data["price"],
        "market_cap_b": common_data["market_cap_b"],
        "_timing": {"fetch_ms": fetch_ms, "evaluate_ms": eval_ms, "total_ms": total_ms, "fetch_profile": fetch_profile},
    }


def scan_universe(
    tickers: Optional[list[str]] = None,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    stop_event: Optional[threading.Event] = None,
    skip_strategies: set[str] | None = None,
    max_workers: int = 5,
) -> tuple[list[dict], dict]:
    """
    Scan a universe of tickers against all strategies.

    Defaults tickers to get_sp500_tickers().
    Calls progress_callback(i, total, symbol) as each ticker completes (may
    arrive out of submission order).
    If stop_event is set, pending tickers are skipped and in-flight ones finish.
    Sorts results: passes_any=True first, then by most criteria passing, errors last.

    max_workers caps parallel ticker scans. Concurrent Tradier requests are
    further gated by a semaphore inside tradier._get (see src/tradier.py).

    Returns: (results, timing_stats) where timing_stats has keys:
      total_ms, avg_per_ticker_ms, min_per_ticker_ms, max_per_ticker_ms
    """
    if tickers is None:
        tickers = massive.get_sp500_tickers()

    start_time = time.time()

    # Batch-fetch all quotes at once before scanning
    batch_quote_start = time.time()
    quotes_cache = tradier.get_quotes(tickers)
    batch_quote_ms = (time.time() - batch_quote_start) * 1000

    results: list[dict] = []
    progress_lock = threading.Lock()
    completed_count = 0
    total = len(tickers)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_symbol = {
            pool.submit(
                scan_ticker,
                symbol,
                quotes_cache=quotes_cache,
                hiv_cache=None,
                skip_strategies=skip_strategies,
            ): symbol
            for symbol in tickers
        }

        for future in as_completed(future_to_symbol):
            symbol = future_to_symbol[future]
            if stop_event and stop_event.is_set():
                # Cancel anything that hasn't started yet; in-flight futures finish.
                for f in future_to_symbol:
                    if not f.done():
                        f.cancel()
                break
            try:
                result = future.result()
            except Exception as e:
                result = {
                    "symbol": symbol,
                    "error": f"scan failed: {e}",
                    "strategies": {},
                    "passes_any": False,
                    "name": None,
                    "price": None,
                    "market_cap_b": None,
                }
            results.append(result)

            with progress_lock:
                completed_count += 1
                if progress_callback:
                    progress_callback(completed_count, total, symbol)

    # Aggregate timings & profiles serially after all workers return
    timings: list[float] = []
    fetch_profiles = {
        "quote_ms": [],
        "ticker_details_ms": [],
        "daily_bars_ms": [],
        "avg_volume_ms": [],
    }
    strategy_profiles: dict = {}

    for result in results:
        if "_timing" in result:
            timings.append(result["_timing"]["total_ms"])
            profile = result["_timing"].get("fetch_profile", {})
            for key, val in profile.items():
                if key in fetch_profiles:
                    fetch_profiles[key].append(val)
        for strategy_name, strategy_result in result.get("strategies", {}).items():
            strat_prof = strategy_result.get("_strategy_profile", {})
            if strat_prof:
                if strategy_name not in strategy_profiles:
                    strategy_profiles[strategy_name] = {}
                for key, val in strat_prof.items():
                    if key not in strategy_profiles[strategy_name]:
                        strategy_profiles[strategy_name][key] = []
                    strategy_profiles[strategy_name][key].append(val)

    elapsed = (time.time() - start_time) * 1000

    # Calculate API call averages
    api_stats = {}
    for key, values in fetch_profiles.items():
        if values:
            api_stats[key] = sum(values) / len(values)

    # Calculate strategy call averages
    strategy_stats = {}
    for strat_name, calls in strategy_profiles.items():
        strategy_stats[strat_name] = {}
        for call_name, values in calls.items():
            if values:
                strategy_stats[strat_name][call_name] = sum(values) / len(values)

    timing_stats = {
        "total_ms": elapsed,
        "avg_per_ticker_ms": sum(timings) / len(timings) if timings else 0,
        "min_per_ticker_ms": min(timings) if timings else 0,
        "max_per_ticker_ms": max(timings) if timings else 0,
        "api_breakdown": api_stats,
        "strategy_breakdown": strategy_stats,
        "batch_quote_ms": batch_quote_ms,
    }

    def sort_key(r):
        if r.get("error"):
            return (2, 0)
        if r.get("passes_any"):
            return (0, 999)
        total_passed = sum(
            sum(1 for c in s.get("criteria", {}).values() if c.get("passed") is True)
            for s in r.get("strategies", {}).values()
        )
        return (1, -total_passed)

    results.sort(key=sort_key)
    return results, timing_stats
