"""
yfinance_data.py — Supplemental market data via yfinance.

Used for data not available via Tradier/Massive:
- Institutional ownership percentage (from SEC 13F filings)
"""

import yfinance as yf
import streamlit as st


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
        return round(float(str(pct_str).replace("%", "").strip()), 2)
    except Exception:
        return None
