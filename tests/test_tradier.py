"""Tests for src/tradier.py — Tradier REST client."""

import pytest
from unittest.mock import patch, MagicMock

from src.tradier import (
    TradierAuthError, TradierOrderError,
    _get_config, _source_from_env, _headers,
    submit_option_order, get_order_status,
    get_options_chain, get_expirations,
    get_historical_iv, get_quote, current_source,
    SANDBOX_BASE, LIVE_BASE,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def tradier_env(monkeypatch):
    monkeypatch.setenv("TRADIER_API_KEY", "test-key")
    monkeypatch.setenv("TRADIER_ACCOUNT_ID", "ACC123")
    monkeypatch.setenv("TRADIER_ENV", "sandbox")


def _resp(json_data, status=200):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = json_data
    r.raise_for_status = MagicMock()
    return r


# ---------------------------------------------------------------------------
# _get_config
# ---------------------------------------------------------------------------

def test_get_config_sandbox(monkeypatch):
    monkeypatch.setenv("TRADIER_ENV", "sandbox")
    base, key, acct = _get_config()
    assert base == SANDBOX_BASE
    assert key == "test-key"
    assert acct == "ACC123"


def test_get_config_live(monkeypatch):
    monkeypatch.setenv("TRADIER_ENV", "live")
    base, _, _ = _get_config()
    assert base == LIVE_BASE


def test_get_config_missing_key_raises(monkeypatch):
    import streamlit as st
    monkeypatch.delenv("TRADIER_API_KEY", raising=False)
    monkeypatch.setattr(st, "secrets", {})
    with pytest.raises(TradierAuthError, match="TRADIER_API_KEY"):
        _get_config()


def test_get_config_missing_account_raises(monkeypatch):
    import streamlit as st
    monkeypatch.delenv("TRADIER_ACCOUNT_ID", raising=False)
    monkeypatch.setattr(st, "secrets", {})
    with pytest.raises(TradierAuthError, match="TRADIER_ACCOUNT_ID"):
        _get_config()


# ---------------------------------------------------------------------------
# _source_from_env / _headers / current_source
# ---------------------------------------------------------------------------

def test_source_from_env_sandbox(monkeypatch):
    monkeypatch.setenv("TRADIER_ENV", "sandbox")
    assert _source_from_env() == "TRADIER_SANDBOX"


def test_source_from_env_live(monkeypatch):
    monkeypatch.setenv("TRADIER_ENV", "live")
    assert _source_from_env() == "TRADIER_LIVE"


def test_headers_format():
    h = _headers("my-key")
    assert h["Authorization"] == "Bearer my-key"
    assert h["Accept"] == "application/json"


def test_current_source_sandbox(monkeypatch):
    monkeypatch.setenv("TRADIER_ENV", "sandbox")
    assert current_source() == "TRADIER_SANDBOX"


def test_current_source_live(monkeypatch):
    monkeypatch.setenv("TRADIER_ENV", "live")
    assert current_source() == "TRADIER_LIVE"


# ---------------------------------------------------------------------------
# submit_option_order
# ---------------------------------------------------------------------------

def test_submit_option_order_success():
    with patch("requests.post", return_value=_resp({"order": {"status": "ok", "id": 99001}})):
        order_id = submit_option_order(
            symbol="RKLB",
            option_symbol="RKLB250321P00007000",
            side="sell_to_open",
            quantity=1,
            order_type="limit",
            price=0.50,
        )
    assert order_id == "99001"


def test_submit_option_order_rejected_raises():
    with patch("requests.post", return_value=_resp({"order": {"status": "error"}})):
        with pytest.raises(TradierOrderError):
            submit_option_order(
                symbol="RKLB",
                option_symbol="RKLB250321P00007000",
                side="sell_to_open",
                quantity=1,
            )


def test_submit_option_order_401_raises():
    r = MagicMock()
    r.status_code = 401
    r.raise_for_status = MagicMock()
    r.json.return_value = {}
    with patch("requests.post", return_value=r):
        with pytest.raises(TradierAuthError):
            submit_option_order(
                symbol="RKLB",
                option_symbol="RKLB250321P00007000",
                side="sell_to_open",
                quantity=1,
            )


# ---------------------------------------------------------------------------
# get_order_status
# ---------------------------------------------------------------------------

def test_get_order_status_returns_filled():
    data = {"order": {"status": "filled", "avg_fill_price": 0.48, "quantity": 1, "exec_quantity": 1}}
    with patch("requests.get", return_value=_resp(data)):
        result = get_order_status("99001")
    assert result["status"] == "filled"
    assert result["avg_fill_price"] == 0.48
    assert result["exec_quantity"] == 1
    assert "raw" in result


# ---------------------------------------------------------------------------
# get_options_chain
# ---------------------------------------------------------------------------

_OPTION_PUT = {
    "symbol": "RKLB250321P00008000", "option_type": "put", "strike": 8.0,
    "bid": 0.5, "ask": 0.6, "last": 0.55, "volume": 100, "open_interest": 200,
    "greeks": {"mid_iv": 0.35, "delta": -0.3, "gamma": 0.01, "theta": -0.02},
}
_OPTION_CALL = {
    "symbol": "RKLB250321C00009000", "option_type": "call", "strike": 9.0,
    "bid": 0.4, "ask": 0.5, "last": 0.45, "volume": 50, "open_interest": 100,
    "greeks": {"mid_iv": 0.28, "delta": 0.3, "gamma": 0.01, "theta": -0.02},
}


def test_get_options_chain_filters_by_type():
    data = {"options": {"option": [_OPTION_PUT, _OPTION_CALL]}}
    with patch("requests.get", return_value=_resp(data)):
        result = get_options_chain("RKLB", "2025-03-21", option_type="put")
    assert len(result) == 1
    assert result[0]["symbol"] == _OPTION_PUT["symbol"]
    assert result[0]["implied_volatility"] == 0.35


def test_get_options_chain_single_result_as_dict():
    """Tradier returns a dict (not list) when only one option matches."""
    data = {"options": {"option": _OPTION_PUT}}
    with patch("requests.get", return_value=_resp(data)):
        result = get_options_chain("RKLB", "2025-03-21", option_type="put")
    assert len(result) == 1


# ---------------------------------------------------------------------------
# get_expirations
# ---------------------------------------------------------------------------

def test_get_expirations_list():
    data = {"expirations": {"date": ["2025-03-21", "2025-04-18"]}}
    with patch("requests.get", return_value=_resp(data)):
        assert get_expirations("RKLB") == ["2025-03-21", "2025-04-18"]


def test_get_expirations_single_string():
    """Single expiration date comes back as a plain string, not a list."""
    data = {"expirations": {"date": "2025-03-21"}}
    with patch("requests.get", return_value=_resp(data)):
        assert get_expirations("RKLB") == ["2025-03-21"]


# ---------------------------------------------------------------------------
# get_historical_iv
# ---------------------------------------------------------------------------

def _make_history(n: int, start: float = 100.0) -> list[dict]:
    """Generate price history with alternating up/down moves so HV > 0."""
    import math
    history, price = [], start
    for i in range(n):
        history.append({"date": f"2024-{(i//28)+1:02d}-{(i%28)+1:02d}", "close": str(price)})
        price *= math.exp(0.02 if i % 2 == 0 else -0.015)
    return history


def test_get_historical_iv_sufficient_data():
    data = {"history": {"day": _make_history(90)}}
    with patch("requests.get", return_value=_resp(data)):
        result = get_historical_iv("RKLB", days=365)
    assert len(result) == 60  # 90 - 30 window
    assert result[0]["iv"] > 0


def test_get_historical_iv_insufficient_data():
    data = {"history": {"day": _make_history(20)}}
    with patch("requests.get", return_value=_resp(data)):
        assert get_historical_iv("RKLB", days=365) == []


def test_get_historical_iv_single_day_as_dict():
    """Single day returned as dict — should fall through as insufficient data."""
    data = {"history": {"day": {"date": "2025-01-01", "close": "100.0"}}}
    with patch("requests.get", return_value=_resp(data)):
        assert get_historical_iv("RKLB", days=365) == []


# ---------------------------------------------------------------------------
# get_quote
# ---------------------------------------------------------------------------

def test_get_quote_dict_response():
    data = {"quotes": {"quote": {"symbol": "RKLB", "last": 8.42, "bid": 8.40, "ask": 8.45, "volume": 1000}}}
    with patch("requests.get", return_value=_resp(data)):
        result = get_quote("RKLB")
    assert result["symbol"] == "RKLB"
    assert result["last"] == 8.42


def test_get_quote_list_response():
    """Some multi-symbol Tradier responses return a list."""
    data = {"quotes": {"quote": [{"symbol": "RKLB", "last": 8.42, "bid": 8.40, "ask": 8.45, "volume": 1000}]}}
    with patch("requests.get", return_value=_resp(data)):
        result = get_quote("RKLB")
    assert result["symbol"] == "RKLB"
