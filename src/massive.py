"""
massive.py — Massive.com REST client for stock screening data.

Pure data fetching, no DB writes. Auth via MASSIVE_API_KEY (env first, then st.secrets).
Base URL: https://api.massive.com
All requests use Authorization: Bearer <key> header.
"""

import os
import requests
from typing import Optional
from datetime import date, timedelta
import streamlit as st


class MassiveError(Exception):
    pass


class MassiveAuthError(MassiveError):
    pass


class MassiveNotFoundError(MassiveError):
    pass


def _get_api_key() -> str:
    """Read API key from environment first, fall back to st.secrets."""
    key = os.environ.get("MASSIVE_API_KEY")
    if key:
        return key
    try:
        import streamlit as st
        key = st.secrets.get("MASSIVE_API_KEY")
        if key:
            return key
    except Exception:
        pass
    raise MassiveAuthError(
        "MASSIVE_API_KEY not set. Add to environment or .streamlit/secrets.toml."
    )


def _headers() -> dict:
    """Return auth headers for Massive.com API."""
    api_key = _get_api_key()
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _request(method: str, path: str, params: dict = None) -> dict:
    """Make HTTP request to Massive.com, handle auth and errors."""
    base_url = "https://api.massive.com"
    url = f"{base_url}{path}"

    try:
        resp = requests.request(
            method, url, headers=_headers(), params=params, timeout=10
        )
    except Exception as e:
        raise MassiveError(f"Request failed: {str(e)}")

    if resp.status_code == 401:
        raise MassiveAuthError("Massive auth failed — check MASSIVE_API_KEY.")
    if resp.status_code == 403:
        raise MassiveAuthError(
            "Massive API access forbidden — verify subscription level."
        )
    if resp.status_code == 404:
        raise MassiveNotFoundError(f"Not found: {path}")

    try:
        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        raise MassiveError(f"HTTP {resp.status_code}: {str(e)}")

    return resp.json()


def get_prev_close(symbol: str) -> dict:
    """
    Get previous day's close for a symbol.

    GET /v2/aggs/ticker/{symbol}/prev with adjusted=true.
    Returns {symbol, close, open, high, low, volume}.
    Raises MassiveNotFoundError if results is empty.
    """
    data = _request("GET", f"/v2/aggs/ticker/{symbol}/prev", {"adjusted": "true"})

    if not data.get("results"):
        raise MassiveNotFoundError(f"No prev close data for {symbol}")

    result = data["results"][0]
    return {
        "symbol": symbol,
        "close": result.get("c"),
        "open": result.get("o"),
        "high": result.get("h"),
        "low": result.get("l"),
        "volume": result.get("v"),
    }


@st.cache_data(ttl=86400)
def get_ticker_details(symbol: str) -> dict:
    """
    Get company details for a symbol.

    GET /v3/reference/tickers/{symbol}.
    Returns {symbol, name, market_cap_b, exchange}.
    market_cap_b = market_cap / 1e9.
    Raises MassiveNotFoundError if results is empty or falsy.
    """
    data = _request("GET", f"/v3/reference/tickers/{symbol}")

    if not data.get("results"):
        raise MassiveNotFoundError(f"No ticker details for {symbol}")

    result = data["results"]
    market_cap = result.get("market_cap")
    market_cap_b = (market_cap / 1e9) if market_cap else 0.0

    return {
        "symbol": symbol,
        "name": result.get("name", ""),
        "market_cap_b": market_cap_b,
        "exchange": result.get("primary_exchange", ""),
    }


@st.cache_data(ttl=86400)
def get_sma(symbol: str, window: int = 200) -> Optional[float]:
    """
    Get Simple Moving Average for a symbol.

    GET /v1/indicators/sma/{symbol} with params:
    timespan=day, window=<window>, series_type=close, order=desc, limit=1, adjusted=true.
    Returns results.values[0].value or None if empty.
    Catches MassiveNotFoundError and returns None.
    """
    try:
        data = _request(
            "GET",
            f"/v1/indicators/sma/{symbol}",
            {
                "timespan": "day",
                "window": str(window),
                "series_type": "close",
                "order": "desc",
                "limit": "1",
                "adjusted": "true",
            },
        )
    except MassiveNotFoundError:
        return None

    if not data.get("results", {}).get("values"):
        return None

    values = data["results"]["values"]
    if values:
        return float(values[0].get("value"))
    return None


@st.cache_data(ttl=86400)
def get_daily_bars(symbol: str, days: int = 30) -> tuple:
    """
    Get daily bars for a symbol over the last N days.

    GET /v2/aggs/ticker/{symbol}/range/1/day/{from_date}/{to_date}.
    to_date = today, from_date = today - days.
    Params: adjusted=true, sort=asc, limit=days+10.
    Returns tuple of dicts {date, open, high, low, close, volume}.
    Timestamp t is Unix ms — convert to ISO date string.
    Returns () on MassiveNotFoundError.
    """
    to_date = date.today().isoformat()
    from_date = (date.today() - timedelta(days=days)).isoformat()

    try:
        data = _request(
            "GET",
            f"/v2/aggs/ticker/{symbol}/range/1/day/{from_date}/{to_date}",
            {
                "adjusted": "true",
                "sort": "asc",
                "limit": str(days + 10),
            },
        )
    except MassiveNotFoundError:
        return ()

    if not data.get("results"):
        return ()

    bars = []
    for result in data["results"]:
        # t is Unix ms, convert to ISO date
        ts_ms = result.get("t")
        if ts_ms:
            bar_date = date.fromtimestamp(ts_ms / 1000).isoformat()
        else:
            bar_date = None

        bars.append({
            "date": bar_date,
            "open": result.get("o"),
            "high": result.get("h"),
            "low": result.get("l"),
            "close": result.get("c"),
            "volume": result.get("v"),
        })

    return tuple(bars)


def compute_rsi(bars: list[dict], period: int = 14) -> Optional[float]:
    """
    Compute RSI (Relative Strength Index) using Wilder's smoothing.

    Requires period+1 bars minimum (oldest first, must have 'close').
    Returns None if insufficient data.
    Returns 100.0 if avg_loss == 0.
    Returns rounded to 2 decimal places.
    """
    if len(bars) < period + 1:
        return None

    # Extract closes
    closes = [bar.get("close") for bar in bars]
    if None in closes or len(closes) < period + 1:
        return None

    # Compute deltas
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]

    # Separate gains and losses
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]

    # Seed with simple average of first 'period' gains/losses
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    # Wilder's smoothing for remaining periods
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return round(rsi, 2)


def compute_avg_volume(bars: list[dict]) -> Optional[float]:
    """
    Compute average volume across bars.

    Skips None volumes. Returns None if no valid volumes.
    """
    volumes = [bar.get("volume") for bar in bars if bar.get("volume") is not None]
    if not volumes:
        return None
    return sum(volumes) / len(volumes)


@st.cache_data(ttl=86400)
def get_fundamentals(symbol: str) -> dict:
    """
    Get FCF and debt/equity ratio for a symbol from the Ratios endpoint.

    GET /stocks/financials/v1/ratios?ticker=<symbol>&limit=1.
    Returns {free_cash_flow, debt_to_equity}. Both may be None if unavailable.
    """
    try:
        data = _request(
            "GET",
            "/stocks/financials/v1/ratios",
            {"ticker": symbol, "limit": 1},
        )
    except MassiveError:
        return {"free_cash_flow": None, "debt_to_equity": None}

    results = data.get("results", [])
    if not results:
        return {"free_cash_flow": None, "debt_to_equity": None}

    result = results[0]
    return {
        "free_cash_flow": result.get("free_cash_flow"),
        "debt_to_equity": result.get("debt_to_equity"),
    }


def get_sp500_tickers() -> list[str]:
    """
    Return static list of ~100 large-cap US tickers spanning major sectors.

    Representative names across Technology, Financials, Healthcare, Consumer,
    Industrials, Energy, Utilities/REITs, Communication, Materials, plus
    mid-caps with good options liquidity.
    """
    return [
        # Technology
        "AAPL", "MSFT", "NVDA", "GOOGL", "GOOG", "META", "TSLA", "AVGO", "ASML", "NFLX",
        "CRM", "ADBE", "PAYC", "WDAY", "SNPS", "CDNS", "VEEV", "NET", "CRWD", "OKTA",
        # Financials
        "JPM", "BAC", "WFC", "GS", "MS", "BLK", "SCHW", "COIN", "ICE", "SOFI",
        # Healthcare
        "JNJ", "UNH", "MRK", "PFE", "ABBV", "LLY", "AMGN", "VRTX", "REGN", "ISRG",
        # Consumer
        "WMT", "PG", "KO", "PEP", "MCD", "SBUX", "CMG", "NKE", "LULU", "ULTA",
        # Industrials
        "BA", "CAT", "GE", "DE", "RTX", "LMT", "NOC", "GD", "TDG", "ODFL",
        # Energy
        "XOM", "CVX", "COP", "MPC", "PSX", "EOG", "APA", "KMI",
        # Utilities & REITs
        "DUK", "NEE", "SO", "AEP", "EXC", "PEG", "SRE", "EQIX", "WELL", "PSA",
        # Communication
        "T", "VZ", "CMCSA", "EA", "TTWO", "RBLX",
        # Materials
        "NEM", "FCX", "APD", "ECL", "VMC", "SLB",
        # Mid-caps with strong options liquidity
        "PLTR", "RIOT", "F", "C", "GM", "SOFI", "AMD", "MU", "QCOM", "VLO",
        # Additional core holdings
        "BRK.B", "QQQ", "SPY",
    ]
