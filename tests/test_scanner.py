"""Tests for src/scanner.py and src/massive.py — equity scanning logic."""

import pytest
from unittest.mock import patch, MagicMock
from datetime import date, timedelta

from src import scanner, massive


# ---------------------------------------------------------------------------
# compute_rsi tests
# ---------------------------------------------------------------------------

def test_compute_rsi_insufficient_data():
    """RSI with fewer than period+1 bars returns None."""
    bars = [{"close": 100}, {"close": 101}]
    result = massive.compute_rsi(bars, period=14)
    assert result is None


def test_compute_rsi_sufficient_data():
    """RSI with enough bars returns float in 0-100."""
    # Create bars with gradual increase
    bars = [{"close": float(100 + i)} for i in range(16)]
    result = massive.compute_rsi(bars, period=14)
    assert result is not None
    assert isinstance(result, float)
    assert 0 <= result <= 100


def test_compute_rsi_all_gains():
    """RSI with only gains returns 100.0."""
    bars = [{"close": float(100 + i)} for i in range(16)]
    result = massive.compute_rsi(bars, period=14)
    assert result == 100.0


def test_compute_rsi_all_losses():
    """RSI with only losses returns near 0."""
    bars = [{"close": float(100 - i)} for i in range(16)]
    result = massive.compute_rsi(bars, period=14)
    assert result is not None
    assert 0 <= result < 20  # Near zero but not exactly due to Wilder's smoothing


def test_compute_rsi_mixed():
    """RSI with mixed gains/losses returns value in range."""
    # Zigzag pattern
    closes = [100, 102, 101, 103, 102, 104, 103, 105, 104, 106, 105, 107, 106, 108, 107, 109]
    bars = [{"close": c} for c in closes]
    result = massive.compute_rsi(bars, period=14)
    assert result is not None
    assert 40 < result < 100  # More gains than losses, so RSI > 50


# ---------------------------------------------------------------------------
# compute_avg_volume tests
# ---------------------------------------------------------------------------

def test_compute_avg_volume_correct():
    """compute_avg_volume returns correct average."""
    bars = [
        {"volume": 1_000_000},
        {"volume": 2_000_000},
        {"volume": 3_000_000},
    ]
    result = massive.compute_avg_volume(bars)
    assert result == 2_000_000


def test_compute_avg_volume_empty():
    """compute_avg_volume with no valid volumes returns None."""
    bars = [{"volume": None}, {"volume": None}]
    result = massive.compute_avg_volume(bars)
    assert result is None


def test_compute_avg_volume_skips_none():
    """compute_avg_volume skips None volumes."""
    bars = [
        {"volume": 1_000_000},
        {"volume": None},
        {"volume": 3_000_000},
    ]
    result = massive.compute_avg_volume(bars)
    assert result == 2_000_000


def test_compute_avg_volume_no_volumes():
    """compute_avg_volume with empty list returns None."""
    result = massive.compute_avg_volume([])
    assert result is None


# ---------------------------------------------------------------------------
# get_prev_close tests
# ---------------------------------------------------------------------------

@patch("src.massive._request")
def test_get_prev_close_success(mock_request):
    """get_prev_close returns correct fields."""
    mock_request.return_value = {
        "results": [{
            "c": 150.5,
            "o": 149.0,
            "h": 151.0,
            "l": 148.0,
            "v": 5_000_000,
        }]
    }
    result = massive.get_prev_close("AAPL")
    assert result["symbol"] == "AAPL"
    assert result["close"] == 150.5
    assert result["open"] == 149.0
    assert result["high"] == 151.0
    assert result["low"] == 148.0
    assert result["volume"] == 5_000_000


@patch("src.massive._request")
def test_get_prev_close_empty(mock_request):
    """get_prev_close raises MassiveNotFoundError if results empty."""
    mock_request.return_value = {"results": []}
    with pytest.raises(massive.MassiveNotFoundError):
        massive.get_prev_close("UNKNOWN")


# ---------------------------------------------------------------------------
# get_ticker_details tests
# ---------------------------------------------------------------------------

@patch("src.massive._request")
def test_get_ticker_details_success(mock_request):
    """get_ticker_details converts market cap to billions correctly."""
    mock_request.return_value = {
        "results": {
            "name": "Apple Inc.",
            "market_cap": 3_000_000_000_000,  # 3 trillion
            "primary_exchange": "XNYS",
        }
    }
    result = massive.get_ticker_details("AAPL")
    assert result["symbol"] == "AAPL"
    assert result["name"] == "Apple Inc."
    assert result["market_cap_b"] == 3000.0  # 3 trillion / 1e9 = 3000 billion
    assert result["exchange"] == "XNYS"


@patch("src.massive._request")
def test_get_ticker_details_empty(mock_request):
    """get_ticker_details raises MassiveNotFoundError if results empty."""
    mock_request.return_value = {"results": None}
    with pytest.raises(massive.MassiveNotFoundError):
        massive.get_ticker_details("UNKNOWN")


@patch("src.massive._request")
def test_get_ticker_details_no_market_cap(mock_request):
    """get_ticker_details handles missing market_cap."""
    mock_request.return_value = {
        "results": {
            "name": "Test Corp",
            "market_cap": None,
            "primary_exchange": "XNYS",
        }
    }
    result = massive.get_ticker_details("TEST")
    assert result["market_cap_b"] == 0.0


# ---------------------------------------------------------------------------
# get_sma tests
# ---------------------------------------------------------------------------

@patch("src.massive._request")
def test_get_sma_success(mock_request):
    """get_sma returns first value."""
    mock_request.return_value = {
        "results": {
            "values": [{"value": 150.25}, {"value": 149.5}]
        }
    }
    result = massive.get_sma("AAPL", window=200)
    assert result == 150.25


@patch("src.massive._request")
def test_get_sma_not_found(mock_request):
    """get_sma catches MassiveNotFoundError and returns None."""
    mock_request.side_effect = massive.MassiveNotFoundError("Not found")
    result = massive.get_sma("UNKNOWN")
    assert result is None


@patch("src.massive._request")
def test_get_sma_empty(mock_request):
    """get_sma returns None if values empty."""
    mock_request.return_value = {
        "results": {"values": []}
    }
    result = massive.get_sma("AAPL")
    assert result is None


# ---------------------------------------------------------------------------
# scan_ticker tests
# ---------------------------------------------------------------------------

@patch("src.scanner._get_daily_bars_tradier")
@patch("src.tradier.get_quote")
@patch("src.massive.get_daily_bars")
@patch("src.massive.get_sma")
@patch("src.massive.compute_rsi")
@patch("src.massive.compute_avg_volume")
@patch("src.massive.get_ticker_details")
def test_scan_ticker_technical_all_pass(
    mock_ticker_details,
    mock_avg_volume,
    mock_rsi,
    mock_sma,
    mock_daily_bars,
    mock_quote,
    mock_tradier_bars,
):
    """scan_ticker TECHNICAL strategy with all criteria passing."""
    mock_quote.return_value = {"last": 50.0}
    mock_ticker_details.return_value = {"name": "Test Corp", "market_cap_b": 5.0, "exchange": "XNYS"}
    mock_tradier_bars.return_value = [{"close": 50.0, "volume": 500_000}]
    mock_daily_bars.return_value = [{"close": float(50 + i)} for i in range(16)]
    mock_avg_volume.return_value = 500_000
    mock_sma.return_value = 45.0  # Price 50 > SMA 45
    mock_rsi.return_value = 50.0  # RSI between 35-65

    result = scanner.scan_ticker("AAPL", "TECHNICAL")

    assert result["symbol"] == "AAPL"
    assert result["strategy"] == "TECHNICAL"
    assert result["error"] is None
    assert result["passes_all"] is True
    assert result["price"] == 50.0
    assert result["market_cap_b"] == 5.0


@patch("src.scanner._get_daily_bars_tradier")
@patch("src.tradier.get_quote")
@patch("src.massive.get_daily_bars")
@patch("src.massive.get_sma")
@patch("src.massive.compute_rsi")
@patch("src.massive.compute_avg_volume")
@patch("src.massive.get_ticker_details")
def test_scan_ticker_price_below_min(
    mock_ticker_details,
    mock_avg_volume,
    mock_rsi,
    mock_sma,
    mock_daily_bars,
    mock_quote,
    mock_tradier_bars,
):
    """scan_ticker fails when price is below minimum."""
    mock_quote.return_value = {"last": 5.0}  # Below min_price 10.0
    mock_ticker_details.return_value = {"name": "Test", "market_cap_b": 5.0, "exchange": "XNYS"}
    mock_tradier_bars.return_value = [{"close": 5.0, "volume": 500_000}]
    mock_daily_bars.return_value = [{"close": 5.0}]
    mock_avg_volume.return_value = 500_000
    mock_sma.return_value = 4.0
    mock_rsi.return_value = 50.0

    result = scanner.scan_ticker("LOW", "TECHNICAL")

    assert result["passes_all"] is False
    assert result["criteria"]["min_price"]["passed"] is False


@patch("src.scanner._get_daily_bars_tradier")
@patch("src.tradier.get_quote")
@patch("src.massive.get_daily_bars")
@patch("src.massive.get_sma")
@patch("src.massive.compute_rsi")
@patch("src.massive.compute_avg_volume")
@patch("src.massive.get_ticker_details")
def test_scan_ticker_price_above_max(
    mock_ticker_details,
    mock_avg_volume,
    mock_rsi,
    mock_sma,
    mock_daily_bars,
    mock_quote,
    mock_tradier_bars,
):
    """scan_ticker fails when price is above maximum."""
    mock_quote.return_value = {"last": 200.0}  # Above max_price 150.0
    mock_ticker_details.return_value = {"name": "Test", "market_cap_b": 5.0, "exchange": "XNYS"}
    mock_tradier_bars.return_value = [{"close": 200.0, "volume": 500_000}]
    mock_daily_bars.return_value = [{"close": 200.0}]
    mock_avg_volume.return_value = 500_000
    mock_sma.return_value = 190.0
    mock_rsi.return_value = 50.0

    result = scanner.scan_ticker("HIGH", "TECHNICAL")

    assert result["passes_all"] is False
    assert result["criteria"]["max_price"]["passed"] is False


@patch("src.scanner._get_daily_bars_tradier")
@patch("src.tradier.get_quote")
@patch("src.massive.get_daily_bars")
@patch("src.massive.get_sma")
@patch("src.massive.compute_rsi")
@patch("src.massive.compute_avg_volume")
@patch("src.massive.get_ticker_details")
def test_scan_ticker_below_200dma(
    mock_ticker_details,
    mock_avg_volume,
    mock_rsi,
    mock_sma,
    mock_daily_bars,
    mock_quote,
    mock_tradier_bars,
):
    """scan_ticker TECHNICAL fails when price below 200-day SMA."""
    mock_quote.return_value = {"last": 50.0}
    mock_ticker_details.return_value = {"name": "Test", "market_cap_b": 5.0, "exchange": "XNYS"}
    mock_tradier_bars.return_value = [{"close": 50.0, "volume": 500_000}]
    mock_daily_bars.return_value = [{"close": 50.0}]
    mock_avg_volume.return_value = 500_000
    mock_sma.return_value = 55.0  # Price 50 < SMA 55
    mock_rsi.return_value = 50.0

    result = scanner.scan_ticker("AAPL", "TECHNICAL")

    assert result["passes_all"] is False
    assert result["criteria"]["above_200dma"]["passed"] is False


@patch("src.scanner._get_daily_bars_tradier")
@patch("src.tradier.get_quote")
@patch("src.massive.get_daily_bars")
@patch("src.massive.get_sma")
@patch("src.massive.compute_rsi")
@patch("src.massive.compute_avg_volume")
@patch("src.massive.get_ticker_details")
def test_scan_ticker_fundamental_manual_criteria(
    mock_ticker_details,
    mock_avg_volume,
    mock_rsi,
    mock_sma,
    mock_daily_bars,
    mock_quote,
    mock_tradier_bars,
):
    """scan_ticker FUNDAMENTAL strategy has manual criteria (passed=None)."""
    mock_quote.return_value = {"last": 50.0}
    mock_ticker_details.return_value = {"name": "Test", "market_cap_b": 5.0, "exchange": "XNYS"}
    mock_tradier_bars.return_value = [{"close": 50.0, "volume": 500_000}]
    mock_daily_bars.return_value = [{"close": 50.0}]
    mock_avg_volume.return_value = 500_000

    result = scanner.scan_ticker("AAPL", "FUNDAMENTAL")

    assert result["criteria"]["requires_positive_cashflow"]["passed"] is None
    assert "verify manually" in result["criteria"]["requires_positive_cashflow"]["note"]
    assert result["criteria"]["max_debt_equity"]["passed"] is None


@patch("src.tradier.get_historical_iv")
@patch("src.market_data.get_current_iv")
@patch("src.scanner._get_daily_bars_tradier")
@patch("src.tradier.get_quote")
@patch("src.massive.get_daily_bars")
@patch("src.massive.get_sma")
@patch("src.massive.compute_rsi")
@patch("src.massive.compute_avg_volume")
@patch("src.massive.get_ticker_details")
def test_scan_ticker_vol_premium_manual_criteria(
    mock_ticker_details,
    mock_avg_volume,
    mock_rsi,
    mock_sma,
    mock_daily_bars,
    mock_quote,
    mock_tradier_bars,
    mock_current_iv,
    mock_historical_iv,
):
    """scan_ticker VOL_PREMIUM strategy has manual IV criteria."""
    mock_quote.return_value = {"last": 50.0}
    mock_ticker_details.return_value = {"name": "Test", "market_cap_b": 5.0, "exchange": "XNYS"}
    mock_tradier_bars.return_value = [{"close": 50.0, "volume": 500_000}]
    mock_daily_bars.return_value = [{"close": 50.0}]
    mock_avg_volume.return_value = 500_000
    mock_current_iv.return_value = None
    mock_historical_iv.return_value = None

    result = scanner.scan_ticker("AAPL", "VOL_PREMIUM")

    assert result["criteria"]["min_iv_hv_ratio"]["passed"] is None
    assert "Tradier" in result["criteria"]["min_iv_hv_ratio"]["note"]
    assert result["criteria"]["min_iv_rank"]["passed"] is None


@patch("src.scanner._get_daily_bars_tradier")
@patch("src.tradier.get_quote")
@patch("src.massive.get_daily_bars")
@patch("src.massive.get_sma")
@patch("src.massive.compute_rsi")
@patch("src.massive.compute_avg_volume")
@patch("src.massive.get_ticker_details")
def test_scan_ticker_etf_component_manual_criteria(
    mock_ticker_details,
    mock_avg_volume,
    mock_rsi,
    mock_sma,
    mock_daily_bars,
    mock_quote,
    mock_tradier_bars,
):
    """scan_ticker ETF_COMPONENT strategy has manual institutional ownership criteria."""
    mock_quote.return_value = {"last": 50.0}
    mock_ticker_details.return_value = {"name": "Test", "market_cap_b": 5.0, "exchange": "XNYS"}
    mock_tradier_bars.return_value = [{"close": 50.0, "volume": 1_000_000}]
    mock_daily_bars.return_value = [{"close": 50.0}]
    mock_avg_volume.return_value = 1_000_000

    result = scanner.scan_ticker("AAPL", "ETF_COMPONENT")

    assert result["criteria"]["min_institutional_ownership_pct"]["passed"] is None
    assert "13F filings" in result["criteria"]["min_institutional_ownership_pct"]["note"]


@patch("src.tradier.get_quote")
def test_scan_ticker_massive_error(mock_quote):
    """scan_ticker returns error dict on TradierError."""
    from src.tradier import TradierError
    mock_quote.side_effect = TradierError("Connection failed")

    result = scanner.scan_ticker("AAPL", "TECHNICAL")

    assert result["error"] is not None
    assert result["passes_all"] is False
    assert result["criteria"] == {}


def test_scan_ticker_invalid_strategy():
    """scan_ticker raises ValueError for unknown strategy."""
    with pytest.raises(ValueError, match="Unknown strategy"):
        scanner.scan_ticker("AAPL", "UNKNOWN_STRATEGY")


# ---------------------------------------------------------------------------
# scan_universe tests
# ---------------------------------------------------------------------------

@patch("src.scanner.scan_ticker")
def test_scan_universe_returns_results(mock_scan_ticker):
    """scan_universe returns results for all tickers."""
    mock_scan_ticker.side_effect = [
        {
            "symbol": "AAPL",
            "strategy": "TECHNICAL",
            "passes_all": True,
            "criteria": {"a": {"passed": True}},
            "error": None,
        },
        {
            "symbol": "MSFT",
            "strategy": "TECHNICAL",
            "passes_all": False,
            "criteria": {"a": {"passed": False}},
            "error": None,
        },
    ]

    results = scanner.scan_universe("TECHNICAL", tickers=["AAPL", "MSFT"])

    assert len(results) == 2
    assert results[0]["symbol"] == "AAPL"
    assert results[1]["symbol"] == "MSFT"


@patch("src.scanner.scan_ticker")
def test_scan_universe_sorts_full_passes_first(mock_scan_ticker):
    """scan_universe sorts passes_all=True first."""
    mock_scan_ticker.side_effect = [
        {
            "symbol": "PARTIAL",
            "passes_all": False,
            "criteria": {"a": {"passed": True}},
            "error": None,
        },
        {
            "symbol": "FULLPASS",
            "passes_all": True,
            "criteria": {},
            "error": None,
        },
    ]

    results = scanner.scan_universe("TECHNICAL", tickers=["PARTIAL", "FULLPASS"])

    assert results[0]["symbol"] == "FULLPASS"
    assert results[1]["symbol"] == "PARTIAL"


@patch("src.scanner.scan_ticker")
def test_scan_universe_sorts_errors_last(mock_scan_ticker):
    """scan_universe sorts errors last."""
    mock_scan_ticker.side_effect = [
        {
            "symbol": "ERROR",
            "error": "Connection failed",
            "passes_all": False,
        },
        {
            "symbol": "OK",
            "passes_all": True,
            "error": None,
            "criteria": {},
        },
    ]

    results = scanner.scan_universe("TECHNICAL", tickers=["ERROR", "OK"])

    assert results[0]["symbol"] == "OK"
    assert results[1]["symbol"] == "ERROR"


@patch("src.scanner.scan_ticker")
def test_scan_universe_calls_progress_callback(mock_scan_ticker):
    """scan_universe calls progress_callback with correct args."""
    mock_scan_ticker.return_value = {"symbol": "AAPL", "passes_all": True, "error": None, "criteria": {}}

    callback_calls = []

    def progress_cb(i, total, symbol):
        callback_calls.append((i, total, symbol))

    scanner.scan_universe("TECHNICAL", tickers=["AAPL", "MSFT"], progress_callback=progress_cb)

    assert len(callback_calls) == 2
    assert callback_calls[0] == (0, 2, "AAPL")
    assert callback_calls[1] == (1, 2, "MSFT")
