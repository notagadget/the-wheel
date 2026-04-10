"""
market_data.py — IV rank and percentile computation via Tradier.

Fetches historical IV, computes both IVR and IV percentile,
and caches results in underlying.iv_rank_cached / iv_pct_cached.

IV Rank:       (current_IV - 52w_low) / (52w_high - 52w_low) * 100
IV Percentile: % of days in the past year where IV was below current IV

Both are stored; which one the screener displays is a session preference
(see pages/2_Screener.py).
"""

from datetime import datetime, timezone
from typing import Optional
from src.db import get_conn
from src.tradier import get_historical_iv, get_options_chain, get_expirations, TradierError


# ---------------------------------------------------------------------------
# IV computation
# ---------------------------------------------------------------------------

def compute_iv_metrics(iv_series: list[dict], current_iv: float) -> dict:
    """
    Given a list of {date, iv} dicts and a current IV value,
    return IV rank and IV percentile.

    Args:
        iv_series:  historical IV values, oldest first
        current_iv: today's IV (not included in series)

    Returns dict with keys: iv_rank, iv_percentile, iv_52w_high, iv_52w_low
    """
    if not iv_series:
        return {
            "iv_rank":       None,
            "iv_percentile": None,
            "iv_52w_high":   None,
            "iv_52w_low":    None,
        }

    values = [d["iv"] for d in iv_series]
    high = max(values)
    low  = min(values)

    iv_rank = None
    if high != low:
        iv_rank = round((current_iv - low) / (high - low) * 100, 1)
        iv_rank = max(0.0, min(100.0, iv_rank))  # clamp to [0, 100]

    days_below = sum(1 for v in values if v < current_iv)
    iv_pct = round(days_below / len(values) * 100, 1)

    return {
        "iv_rank":       iv_rank,
        "iv_percentile": iv_pct,
        "iv_52w_high":   round(high, 4),
        "iv_52w_low":    round(low, 4),
    }


def get_current_iv(symbol: str) -> Optional[float]:
    """
    Fetch current 30-day IV from the nearest-expiry ATM options chain.
    Returns None if unavailable (sandbox limitations, no options listed).
    """
    try:
        expirations = get_expirations(symbol)
        if not expirations:
            return None

        # Use the nearest expiration with at least 7 days out
        from datetime import date
        today = date.today()
        valid_exps = [
            e for e in sorted(expirations)
            if (datetime.strptime(e, "%Y-%m-%d").date() - today).days >= 7
        ]
        if not valid_exps:
            return None

        chain = get_options_chain(symbol, valid_exps[0], option_type="put")
        if not chain:
            return None

        # Get ATM option: option with strike closest to current price
        from src.tradier import get_quote
        quote = get_quote(symbol)
        last_price = quote.get("last")
        if not last_price:
            return None

        atm = min(chain, key=lambda o: abs((o["strike"] or 0) - last_price))
        return atm.get("implied_volatility")

    except TradierError:
        return None


# ---------------------------------------------------------------------------
# Cache update
# ---------------------------------------------------------------------------

def refresh_iv_for_ticker(symbol: str) -> dict:
    """
    Fetch IV history + current IV for symbol, compute IVR + IV percentile,
    and update underlying table.

    Returns the computed metrics dict, or raises on Tradier error.
    """
    iv_series = get_historical_iv(symbol, days=365)
    current_iv = get_current_iv(symbol)

    if current_iv is None and iv_series:
        # Fall back to most recent historical value as current
        current_iv = iv_series[-1]["iv"]

    if current_iv is None:
        raise ValueError(f"Could not determine current IV for {symbol}")

    metrics = compute_iv_metrics(iv_series, current_iv)
    now = datetime.now(timezone.utc).isoformat()

    with get_conn() as conn:
        conn.execute("""
            UPDATE underlying SET
                iv_rank_cached = ?,
                iv_pct_cached  = ?,
                iv_current     = ?,
                iv_52w_high    = ?,
                iv_52w_low     = ?,
                iv_updated     = ?
            WHERE underlying_id = ?
        """, (
            metrics["iv_rank"],
            metrics["iv_percentile"],
            current_iv,
            metrics["iv_52w_high"],
            metrics["iv_52w_low"],
            now,
            symbol,
        ))

    return {**metrics, "iv_current": current_iv, "symbol": symbol, "updated_at": now}


def refresh_all_watchlist() -> list[dict]:
    """
    Refresh IV metrics for every ticker in the underlying table.
    Returns list of result dicts (one per ticker).
    Errors per ticker are caught and included as {"symbol": ..., "error": ...}.
    """
    with get_conn() as conn:
        tickers = [
            r["underlying_id"] for r in conn.execute(
                "SELECT underlying_id FROM underlying"
            ).fetchall()
        ]

    results = []
    for ticker in tickers:
        try:
            result = refresh_iv_for_ticker(ticker)
            results.append(result)
        except Exception as e:
            results.append({"symbol": ticker, "error": str(e)})

    return results
