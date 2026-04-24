"""
yfinance_data.py — Supplemental market data via yfinance.

Used for data not available via Tradier/Massive:
- Institutional ownership percentage (from SEC 13F filings)
- Sector classification
"""

import yfinance as yf
import streamlit as st
from functools import lru_cache


@st.cache_data(ttl=86400)
def get_institutional_ownership_pct(symbol: str) -> float | None:
    """
    Return % of shares held by institutions (0–100 scale).
    Source: yfinance major_holders (aggregated from SEC 13F filings).
    Returns None on any failure.
    """
    try:
        ticker = yf.Ticker(symbol)
        holders = ticker.major_holders
        if holders is None or holders.empty:
            return None
        # Row 2 is "% of Shares Held by Institutions"
        # DataFrame has columns [0, 1]; row index 2
        pct_str = holders.iloc[2, 0]
        val = float(str(pct_str).replace("%", "").strip())
        # yfinance may return a decimal fraction (0.65) or a percentage (65.0)
        if val <= 1.0:
            val *= 100
        return round(val, 2)
    except Exception:
        return None


@st.cache_data(ttl=86400)
def get_fundamentals(symbol: str) -> dict:
    """
    Return FCF and D/E ratio from yfinance.
    D/E is normalized to a ratio (yfinance returns it ×100).
    Both may be None on failure.
    """
    try:
        info = yf.Ticker(symbol).info
        fcf = info.get("freeCashflow")
        de_raw = info.get("debtToEquity")
        de = de_raw / 100 if de_raw is not None else None
        return {"free_cash_flow": fcf, "debt_to_equity": de}
    except Exception:
        return {"free_cash_flow": None, "debt_to_equity": None}


@lru_cache(maxsize=256)
def get_sector(symbol: str) -> str | None:
    """
    Return sector classification from yfinance.
    Returns None on any exception.
    """
    try:
        ticker = yf.Ticker(symbol)
        return ticker.info.get("sector")
    except Exception:
        return None
