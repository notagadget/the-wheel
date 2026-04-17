"""Tests for src/market_data.py — IV computation and DB cache update."""

import pytest
from datetime import date, timedelta
from unittest.mock import patch

import src.db as db_module
from src import market_data as md


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    monkeypatch.setattr(db_module, "_initialized", False)


def _seed(ticker: str):
    from src.db import get_conn
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO underlying (underlying_id, ticker) VALUES (?,?)",
            (ticker, ticker),
        )


_SERIES = [{"date": f"2024-{i:02d}-01", "iv": float(i * 5)} for i in range(1, 13)]
# ivs: 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60  — low=5, high=60


# ---------------------------------------------------------------------------
# compute_iv_metrics
# ---------------------------------------------------------------------------

def test_compute_iv_metrics_normal():
    result = md.compute_iv_metrics(_SERIES, current_iv=40.0)
    # iv_rank = (40 - 5) / (60 - 5) * 100 = 63.6...
    assert result["iv_rank"] == pytest.approx(63.6, abs=0.1)
    # 7 values below 40 (5..35) out of 12
    assert result["iv_percentile"] == pytest.approx(7 / 12 * 100, abs=0.1)
    assert result["iv_52w_high"] == 60.0
    assert result["iv_52w_low"] == 5.0


def test_compute_iv_metrics_empty_series():
    result = md.compute_iv_metrics([], current_iv=30.0)
    assert result["iv_rank"] is None
    assert result["iv_percentile"] is None
    assert result["iv_52w_high"] is None
    assert result["iv_52w_low"] is None


def test_compute_iv_metrics_flat_history():
    """When all historical IVs are equal, range is zero so iv_rank is None."""
    flat = [{"date": "2024-01-01", "iv": 30.0}, {"date": "2024-02-01", "iv": 30.0}]
    result = md.compute_iv_metrics(flat, current_iv=30.0)
    assert result["iv_rank"] is None


def test_compute_iv_metrics_clamps_to_bounds():
    result_high = md.compute_iv_metrics(_SERIES, current_iv=9999.0)
    assert result_high["iv_rank"] == 100.0

    result_low = md.compute_iv_metrics(_SERIES, current_iv=0.0)
    assert result_low["iv_rank"] == 0.0


# ---------------------------------------------------------------------------
# get_current_iv
# ---------------------------------------------------------------------------

_FUTURE_EXP = (date.today() + timedelta(days=30)).isoformat()
_CHAIN = [
    {"strike": 8.0, "implied_volatility": 0.45},
    {"strike": 9.0, "implied_volatility": 0.40},
]


def test_get_current_iv_picks_atm():
    # last=8.10 → nearest strike=8.0
    # get_quote is imported locally inside get_current_iv, so patch at source
    with (
        patch("src.market_data.get_expirations", return_value=[_FUTURE_EXP]),
        patch("src.market_data.get_options_chain", return_value=_CHAIN),
        patch("src.tradier.get_quote", return_value={"last": 8.10}),
    ):
        iv = md.get_current_iv("RKLB")
    assert iv == 0.45


def test_get_current_iv_no_expirations():
    with patch("src.market_data.get_expirations", return_value=[]):
        assert md.get_current_iv("RKLB") is None


def test_get_current_iv_all_expirations_too_soon():
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    with patch("src.market_data.get_expirations", return_value=[yesterday]):
        assert md.get_current_iv("RKLB") is None


def test_get_current_iv_empty_chain():
    with (
        patch("src.market_data.get_expirations", return_value=[_FUTURE_EXP]),
        patch("src.market_data.get_options_chain", return_value=[]),
    ):
        assert md.get_current_iv("RKLB") is None


def test_get_current_iv_no_quote_price():
    with (
        patch("src.market_data.get_expirations", return_value=[_FUTURE_EXP]),
        patch("src.market_data.get_options_chain", return_value=_CHAIN),
        patch("src.tradier.get_quote", return_value={"last": None}),
    ):
        assert md.get_current_iv("RKLB") is None


def test_get_current_iv_tradier_error_returns_none():
    from src.tradier import TradierError
    with patch("src.market_data.get_expirations", side_effect=TradierError("boom")):
        assert md.get_current_iv("RKLB") is None


# ---------------------------------------------------------------------------
# refresh_iv_for_ticker
# ---------------------------------------------------------------------------

def test_refresh_iv_for_ticker_updates_db():
    _seed("RKLB")
    with (
        patch("src.market_data.get_historical_iv", return_value=_SERIES),
        patch("src.market_data.get_current_iv", return_value=40.0),
    ):
        result = md.refresh_iv_for_ticker("RKLB")

    assert result["symbol"] == "RKLB"
    assert result["iv_current"] == 40.0
    assert result["iv_rank"] == pytest.approx(63.6, abs=0.1)

    from src.db import get_conn
    with get_conn() as conn:
        row = conn.execute(
            "SELECT iv_rank_cached, iv_current FROM underlying WHERE underlying_id=?",
            ("RKLB",),
        ).fetchone()
    assert row["iv_current"] == 40.0
    assert row["iv_rank_cached"] == result["iv_rank"]


def test_refresh_iv_for_ticker_falls_back_to_last_historical():
    """If get_current_iv returns None, use the last iv_series value."""
    _seed("RKLB")
    with (
        patch("src.market_data.get_historical_iv", return_value=_SERIES),
        patch("src.market_data.get_current_iv", return_value=None),
    ):
        result = md.refresh_iv_for_ticker("RKLB")
    assert result["iv_current"] == 60.0  # last iv in _SERIES


def test_refresh_iv_for_ticker_raises_when_no_iv_at_all():
    _seed("RKLB")
    with (
        patch("src.market_data.get_historical_iv", return_value=[]),
        patch("src.market_data.get_current_iv", return_value=None),
    ):
        with pytest.raises(ValueError, match="current IV"):
            md.refresh_iv_for_ticker("RKLB")


# ---------------------------------------------------------------------------
# refresh_all_watchlist
# ---------------------------------------------------------------------------

def test_refresh_all_watchlist_aggregates_results():
    _seed("RKLB")
    _seed("PLTR")
    with (
        patch("src.market_data.get_historical_iv", return_value=_SERIES),
        patch("src.market_data.get_current_iv", return_value=40.0),
    ):
        results = md.refresh_all_watchlist()
    assert {r["symbol"] for r in results} == {"RKLB", "PLTR"}
    assert all("error" not in r for r in results)


def test_refresh_all_watchlist_captures_per_ticker_errors():
    _seed("RKLB")
    _seed("BADTICKER")

    def _fail_if_bad(symbol, days=365):
        if symbol == "BADTICKER":
            raise ValueError("no data available")
        return _SERIES

    with (
        patch("src.market_data.get_historical_iv", side_effect=_fail_if_bad),
        patch("src.market_data.get_current_iv", return_value=40.0),
    ):
        results = md.refresh_all_watchlist()

    ok  = [r for r in results if "error" not in r]
    err = [r for r in results if "error" in r]
    assert len(ok) == 1
    assert len(err) == 1
    assert err[0]["symbol"] == "BADTICKER"
