"""
tradier.py — Tradier REST client for order execution and options data.

Supports sandbox and live environments. Environment is selected by
TRADIER_ENV=sandbox|live (default: sandbox) read from environment or
st.secrets["TRADIER_ENV"].

API key is read from TRADIER_API_KEY env var or st.secrets["TRADIER_API_KEY"].
Never hardcode credentials.

Endpoints used:
  Orders:         POST /v1/accounts/{id}/orders
  Options chain:  GET  /v1/markets/options/chains
  Historical IV:  GET  /v1/markets/history (underlying price as IV proxy via options)
  Quotes:         GET  /v1/markets/quotes
  Expirations:    GET  /v1/markets/options/expirations

See docs/architecture.md for how this module fits into the system.
"""

import os
import requests
from typing import Optional
from datetime import date, timedelta

import streamlit as st


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SANDBOX_BASE = "https://sandbox.tradier.com"
LIVE_BASE    = "https://api.tradier.com"

SANDBOX_STREAM = "https://sandbox.tradier.com"  # sandbox has no separate stream host


class TradierError(Exception):
    pass

class TradierAuthError(TradierError):
    pass

class TradierOrderError(TradierError):
    pass


def _get_config() -> tuple[str, str, str]:
    """
    Returns (base_url, api_key, account_id).
    Reads from environment variables first, falls back to st.secrets if available.
    """
    def _get(key: str) -> Optional[str]:
        val = os.environ.get(key)
        if val:
            return val
        try:
            import streamlit as st
            return st.secrets.get(key)
        except Exception:
            return None

    env        = (_get("TRADIER_ENV") or "sandbox").lower()
    api_key    = _get("TRADIER_API_KEY")
    account_id = _get("TRADIER_ACCOUNT_ID")

    if not api_key:
        raise TradierAuthError(
            "TRADIER_API_KEY not set. Add to environment or .streamlit/secrets.toml."
        )
    if not account_id:
        raise TradierAuthError(
            "TRADIER_ACCOUNT_ID not set. Add to environment or .streamlit/secrets.toml."
        )

    base_url = SANDBOX_BASE if env == "sandbox" else LIVE_BASE
    return base_url, api_key, account_id


def _source_from_env() -> str:
    env = (os.environ.get("TRADIER_ENV") or "sandbox").lower()
    return "TRADIER_SANDBOX" if env == "sandbox" else "TRADIER_LIVE"


def _headers(api_key: str) -> dict:
    return {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }


def _get(path: str, params: dict = None) -> dict:
    base, api_key, _ = _get_config()
    resp = requests.get(f"{base}{path}", headers=_headers(api_key), params=params, timeout=10)
    if resp.status_code == 401:
        raise TradierAuthError("Tradier auth failed — check TRADIER_API_KEY.")
    resp.raise_for_status()
    return resp.json()


def _post(path: str, data: dict) -> dict:
    base, api_key, _ = _get_config()
    resp = requests.post(f"{base}{path}", headers=_headers(api_key), data=data, timeout=10)
    if resp.status_code == 401:
        raise TradierAuthError("Tradier auth failed — check TRADIER_API_KEY.")
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Order execution
# ---------------------------------------------------------------------------

def submit_option_order(
    *,
    symbol: str,
    option_symbol: str,     # OCC format: e.g. RKLB250321P00070000
    side: str,              # buy_to_open | sell_to_open | buy_to_close | sell_to_close
    quantity: int,
    order_type: str = "limit",
    price: Optional[float] = None,
    duration: str = "day",
) -> str:
    """
    Submit an options order. Returns Tradier order_id string.
    Raises TradierOrderError on rejection.
    """
    _, _, account_id = _get_config()

    payload = {
        "class":         "option",
        "symbol":        symbol,
        "option_symbol": option_symbol,
        "side":          side,
        "quantity":      str(quantity),
        "type":          order_type,
        "duration":      duration,
    }
    if order_type == "limit" and price is not None:
        payload["price"] = f"{price:.2f}"

    resp = _post(f"/v1/accounts/{account_id}/orders", payload)

    order = resp.get("order", {})
    if order.get("status") == "ok":
        return str(order["id"])

    raise TradierOrderError(f"Order rejected: {resp}")


def get_order_status(order_id: str) -> dict:
    """
    Fetch order status. Returns dict with keys: status, avg_fill_price, quantity.
    status values: open | partially_filled | filled | expired | canceled | rejected
    """
    _, _, account_id = _get_config()
    resp = _get(f"/v1/accounts/{account_id}/orders/{order_id}")
    order = resp.get("order", {})
    return {
        "status":          order.get("status"),
        "avg_fill_price":  order.get("avg_fill_price"),
        "quantity":        order.get("quantity"),
        "exec_quantity":   order.get("exec_quantity"),
        "raw":             order,
    }


# ---------------------------------------------------------------------------
# Options chain
# ---------------------------------------------------------------------------

@st.cache_data(ttl=300)
def get_options_chain(
    symbol: str,
    expiration: str,        # YYYY-MM-DD
    option_type: str = "put",  # put | call
) -> tuple:
    """
    Fetch options chain for a symbol/expiration (cached).
    Returns tuple of option dicts with keys:
      symbol, strike, bid, ask, last, volume, open_interest,
      implied_volatility, delta, gamma, theta
    """
    resp = _get("/v1/markets/options/chains", {
        "symbol":     symbol,
        "expiration": expiration,
        "greeks":     "true",
    })

    options = resp.get("options", {}).get("option", [])
    if isinstance(options, dict):
        options = [options]  # single result comes back as dict

    return tuple(
        {
            "symbol":             o.get("symbol"),
            "option_type":        o.get("option_type"),
            "strike":             o.get("strike"),
            "bid":                o.get("bid"),
            "ask":                o.get("ask"),
            "last":               o.get("last"),
            "volume":             o.get("volume"),
            "open_interest":      o.get("open_interest"),
            "implied_volatility": o.get("greeks", {}).get("mid_iv"),
            "delta":              o.get("greeks", {}).get("delta"),
            "gamma":              o.get("greeks", {}).get("gamma"),
            "theta":              o.get("greeks", {}).get("theta"),
        }
        for o in options
        if o.get("option_type") == option_type
    )


@st.cache_data(ttl=86400)
def get_expirations(symbol: str) -> tuple:
    """Returns tuple of available expiration dates (YYYY-MM-DD) for a symbol (cached)."""
    resp = _get("/v1/markets/options/expirations", {
        "symbol":           symbol,
        "includeAllRoots":  "true",
        "strikes":          "false",
    })
    dates = resp.get("expirations", {}).get("date", [])
    if isinstance(dates, str):
        dates = [dates]
    return tuple(dates)


# ---------------------------------------------------------------------------
# IV data (used by market_data.py)
# ---------------------------------------------------------------------------

@st.cache_data(ttl=86400)
def get_historical_iv(symbol: str, days: int = 365) -> tuple:
    """
    Fetch historical daily options data to derive IV history.
    Returns list of {date, iv} dicts sorted oldest-first.

    Tradier doesn't expose a direct daily-IV endpoint, so we pull
    the at-the-money option IV for each historical date using the
    /v1/markets/timesales endpoint as a proxy for underlying price,
    then fetch the nearest-expiry ATM option IV.

    Practical note: for IVR calculation, Tradier's /v1/markets/history
    doesn't include IV directly. The cleanest available approach is to
    use the options chain's implied_volatility field for current IV,
    and approximate historical IV from the underlying's HV (historical
    volatility) as a fallback when historical option IV isn't available.

    This returns current IV only if historical data is unavailable via
    the sandbox. In production, supplement with a data provider that
    offers historical IV series (e.g. ORATS, Polygon options).
    """
    start = (date.today() - timedelta(days=days)).isoformat()
    end   = date.today().isoformat()

    # Get underlying price history as HV basis
    resp = _get("/v1/markets/history", {
        "symbol":   symbol,
        "interval": "daily",
        "start":    start,
        "end":      end,
    })

    history = resp.get("history", {}).get("day", [])
    if isinstance(history, dict):
        history = [history]

    # Compute 30-day rolling HV from close prices as IV proxy
    closes = [float(d["close"]) for d in history if d.get("close")]
    if len(closes) < 31:
        return ()

    import math
    result = []
    for i in range(30, len(closes)):
        window = closes[i-30:i]
        log_returns = [math.log(window[j] / window[j-1]) for j in range(1, len(window))]
        mean = sum(log_returns) / len(log_returns)
        variance = sum((r - mean) ** 2 for r in log_returns) / (len(log_returns) - 1)
        hv_30 = math.sqrt(variance * 252) * 100  # annualized %
        result.append({
            "date": history[i]["date"],
            "iv":   round(hv_30, 4),
        })

    return tuple(result)


@st.cache_data(ttl=300)
def get_quote(symbol: str) -> dict:
    """Fetch current quote for a symbol (cached)."""
    resp = _get("/v1/markets/quotes", {"symbols": symbol, "greeks": "false"})
    quotes = resp.get("quotes", {}).get("quote", {})
    if isinstance(quotes, list):
        quotes = quotes[0] if quotes else {}
    return {
        "symbol": quotes.get("symbol"),
        "last":   quotes.get("last"),
        "bid":    quotes.get("bid"),
        "ask":    quotes.get("ask"),
        "volume": quotes.get("volume"),
    }


def get_quotes(symbols: list[str]) -> dict[str, dict]:
    """
    Fetch quotes for multiple symbols in one batch request.
    Returns dict mapping symbol -> {symbol, last, bid, ask, volume}.
    Much faster than calling get_quote() for each symbol individually.
    """
    if not symbols:
        return {}

    symbols_str = ",".join(symbols)
    resp = _get("/v1/markets/quotes", {"symbols": symbols_str, "greeks": "false"})
    quotes_data = resp.get("quotes", {}).get("quote", [])

    # Handle single quote vs list of quotes
    if isinstance(quotes_data, dict):
        quotes_data = [quotes_data]

    result = {}
    for quote in quotes_data:
        symbol = quote.get("symbol")
        if symbol:
            result[symbol] = {
                "symbol": symbol,
                "last": quote.get("last"),
                "bid": quote.get("bid"),
                "ask": quote.get("ask"),
                "volume": quote.get("volume"),
            }

    return result


# ---------------------------------------------------------------------------
# Convenience: source string for DB records
# ---------------------------------------------------------------------------

def current_source() -> str:
    """Returns 'TRADIER_SANDBOX' or 'TRADIER_LIVE' based on current env config."""
    return _source_from_env()
