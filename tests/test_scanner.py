"""Tests for src/scanner.py and src/massive.py — equity scanning logic."""

import pytest
from unittest.mock import patch, MagicMock
from datetime import date, timedelta

from src import scanner, massive
from src import yfinance_data


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

_COMMON_MOCKS = dict(
    mock_ticker_details={"name": "Test Corp", "market_cap_b": 5.0, "exchange": "XNYS"},
    mock_tradier_bars=[{"close": 50.0, "volume": 500_000}],
    mock_avg_volume=500_000,
)


@patch("src.scanner._get_daily_bars_tradier")
@patch("src.tradier.get_quote")
@patch("src.massive.get_daily_bars")
@patch("src.massive.get_sma")
@patch("src.massive.compute_rsi")
@patch("src.massive.compute_avg_volume")
@patch("src.massive.get_ticker_details")
def test_scan_ticker_returns_all_strategies(
    mock_ticker_details,
    mock_avg_volume,
    mock_rsi,
    mock_sma,
    mock_daily_bars,
    mock_quote,
    mock_tradier_bars,
):
    """scan_ticker evaluates all strategies and returns them in result."""
    mock_quote.return_value = {"last": 50.0}
    mock_ticker_details.return_value = {"name": "Test Corp", "market_cap_b": 5.0, "exchange": "XNYS"}
    mock_tradier_bars.return_value = [{"close": 50.0, "volume": 500_000}]
    mock_daily_bars.return_value = [{"close": float(50 + i)} for i in range(16)]
    mock_avg_volume.return_value = 500_000
    mock_sma.return_value = 45.0
    mock_rsi.return_value = 50.0

    from src.eligibility import STRATEGIES
    result = scanner.scan_ticker("AAPL")

    assert result["symbol"] == "AAPL"
    assert result["error"] is None
    assert result["price"] == 50.0
    assert result["market_cap_b"] == 5.0
    assert set(result["strategies"].keys()) == set(STRATEGIES.keys())
    assert "passes_any" in result


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
    """TECHNICAL strategy passes when price > SMA-200, RSI in range, volume/price/cap ok."""
    mock_quote.return_value = {"last": 50.0}
    mock_ticker_details.return_value = {"name": "Test Corp", "market_cap_b": 5.0, "exchange": "XNYS"}
    # 200 bars at 45.0 → SMA-200 = 45.0, price 50.0 > SMA ✓
    mock_tradier_bars.return_value = [{"close": 45.0, "volume": 500_000}] * 200
    mock_daily_bars.return_value = [{"close": float(50 + i)} for i in range(16)]
    mock_avg_volume.return_value = 500_000
    mock_sma.return_value = 45.0
    mock_rsi.return_value = 50.0  # RSI between 35-65

    result = scanner.scan_ticker("AAPL")

    technical = result["strategies"]["TECHNICAL"]
    assert technical["passes_all"] is True
    assert result["passes_any"] is True


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
    """All strategies fail min_price when price is below the lowest threshold (10.0)."""
    mock_quote.return_value = {"last": 5.0}
    mock_ticker_details.return_value = {"name": "Test", "market_cap_b": 5.0, "exchange": "XNYS"}
    mock_tradier_bars.return_value = [{"close": 5.0, "volume": 500_000}]
    mock_daily_bars.return_value = [{"close": 5.0}]
    mock_avg_volume.return_value = 500_000
    mock_sma.return_value = 4.0
    mock_rsi.return_value = 50.0

    result = scanner.scan_ticker("LOW")

    assert result["passes_any"] is False
    for strat_data in result["strategies"].values():
        assert strat_data["criteria"]["min_price"]["passed"] is False


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
    """TECHNICAL fails above_200dma when price < SMA-200."""
    mock_quote.return_value = {"last": 50.0}
    mock_ticker_details.return_value = {"name": "Test", "market_cap_b": 5.0, "exchange": "XNYS"}
    # 200 bars at 55.0 → SMA-200 = 55.0, price 50.0 < SMA ✗
    mock_tradier_bars.return_value = [{"close": 55.0, "volume": 500_000}] * 200
    mock_daily_bars.return_value = [{"close": 50.0}]
    mock_avg_volume.return_value = 500_000
    mock_sma.return_value = 55.0
    mock_rsi.return_value = 50.0

    result = scanner.scan_ticker("AAPL")

    technical = result["strategies"]["TECHNICAL"]
    assert technical["passes_all"] is False
    assert technical["criteria"]["above_200dma"]["passed"] is False


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
    """FUNDAMENTAL strategy has manual criteria (passed=None) for cashflow and debt/equity."""
    mock_quote.return_value = {"last": 50.0}
    mock_ticker_details.return_value = {"name": "Test", "market_cap_b": 5.0, "exchange": "XNYS"}
    mock_tradier_bars.return_value = [{"close": 50.0, "volume": 500_000}]
    mock_daily_bars.return_value = [{"close": 50.0}]
    mock_avg_volume.return_value = 500_000
    mock_sma.return_value = 45.0
    mock_rsi.return_value = 50.0

    result = scanner.scan_ticker("AAPL")

    fundamental = result["strategies"]["FUNDAMENTAL"]
    assert fundamental["criteria"]["requires_positive_cashflow"]["passed"] is None
    assert "verify manually" in fundamental["criteria"]["requires_positive_cashflow"]["note"]
    assert fundamental["criteria"]["max_debt_equity"]["passed"] is None


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
    """VOL_PREMIUM strategy has None criteria when IV data is unavailable."""
    mock_quote.return_value = {"last": 50.0}
    mock_ticker_details.return_value = {"name": "Test", "market_cap_b": 5.0, "exchange": "XNYS"}
    mock_tradier_bars.return_value = [{"close": 50.0, "volume": 500_000}]
    mock_daily_bars.return_value = [{"close": 50.0}]
    mock_avg_volume.return_value = 500_000
    mock_sma.return_value = 45.0
    mock_rsi.return_value = 50.0
    mock_current_iv.return_value = None
    mock_historical_iv.return_value = None

    result = scanner.scan_ticker("AAPL")

    vol = result["strategies"]["VOL_PREMIUM"]
    assert vol["criteria"]["min_iv_hv_ratio"]["passed"] is None
    assert "Tradier" in vol["criteria"]["min_iv_hv_ratio"]["note"]
    assert vol["criteria"]["min_iv_rank"]["passed"] is None


@patch("src.yfinance_data.get_institutional_ownership_pct")
@patch("src.scanner._get_daily_bars_tradier")
@patch("src.tradier.get_quote")
@patch("src.massive.get_daily_bars")
@patch("src.massive.get_sma")
@patch("src.massive.compute_rsi")
@patch("src.massive.compute_avg_volume")
@patch("src.massive.get_ticker_details")
def test_scan_ticker_etf_component_passes(
    mock_ticker_details,
    mock_avg_volume,
    mock_rsi,
    mock_sma,
    mock_daily_bars,
    mock_quote,
    mock_tradier_bars,
    mock_inst_ownership,
):
    """ETF_COMPONENT strategy passes when institutional ownership meets threshold."""
    mock_quote.return_value = {"last": 50.0}
    mock_ticker_details.return_value = {"name": "Test", "market_cap_b": 5.0, "exchange": "XNYS"}
    mock_tradier_bars.return_value = [{"close": 50.0, "volume": 1_000_000}]
    mock_daily_bars.return_value = [{"close": 50.0}]
    mock_avg_volume.return_value = 1_000_000
    mock_sma.return_value = 45.0
    mock_rsi.return_value = 50.0
    mock_inst_ownership.return_value = 72.45

    result = scanner.scan_ticker("AAPL")

    etf = result["strategies"]["ETF_COMPONENT"]
    assert etf["criteria"]["min_institutional_ownership_pct"]["passed"] is True
    assert etf["criteria"]["min_institutional_ownership_pct"]["value"] == 72.45
    assert "72.5% institutional" in etf["criteria"]["min_institutional_ownership_pct"]["note"]


@patch("src.yfinance_data.get_institutional_ownership_pct")
@patch("src.scanner._get_daily_bars_tradier")
@patch("src.tradier.get_quote")
@patch("src.massive.get_daily_bars")
@patch("src.massive.get_sma")
@patch("src.massive.compute_rsi")
@patch("src.massive.compute_avg_volume")
@patch("src.massive.get_ticker_details")
def test_scan_ticker_etf_component_fails_ownership(
    mock_ticker_details,
    mock_avg_volume,
    mock_rsi,
    mock_sma,
    mock_daily_bars,
    mock_quote,
    mock_tradier_bars,
    mock_inst_ownership,
):
    """ETF_COMPONENT strategy fails when institutional ownership below threshold."""
    mock_quote.return_value = {"last": 50.0}
    mock_ticker_details.return_value = {"name": "Test", "market_cap_b": 5.0, "exchange": "XNYS"}
    mock_tradier_bars.return_value = [{"close": 50.0, "volume": 1_000_000}]
    mock_daily_bars.return_value = [{"close": 50.0}]
    mock_avg_volume.return_value = 1_000_000
    mock_sma.return_value = 45.0
    mock_rsi.return_value = 50.0
    mock_inst_ownership.return_value = 45.0  # Below 60.0 threshold

    result = scanner.scan_ticker("AAPL")

    etf = result["strategies"]["ETF_COMPONENT"]
    assert etf["criteria"]["min_institutional_ownership_pct"]["passed"] is False
    assert etf["criteria"]["min_institutional_ownership_pct"]["value"] == 45.0


@patch("src.yfinance_data.get_institutional_ownership_pct")
@patch("src.scanner._get_daily_bars_tradier")
@patch("src.tradier.get_quote")
@patch("src.massive.get_daily_bars")
@patch("src.massive.get_sma")
@patch("src.massive.compute_rsi")
@patch("src.massive.compute_avg_volume")
@patch("src.massive.get_ticker_details")
def test_scan_ticker_etf_component_unavailable(
    mock_ticker_details,
    mock_avg_volume,
    mock_rsi,
    mock_sma,
    mock_daily_bars,
    mock_quote,
    mock_tradier_bars,
    mock_inst_ownership,
):
    """ETF_COMPONENT strategy has None criteria when yfinance unavailable."""
    mock_quote.return_value = {"last": 50.0}
    mock_ticker_details.return_value = {"name": "Test", "market_cap_b": 5.0, "exchange": "XNYS"}
    mock_tradier_bars.return_value = [{"close": 50.0, "volume": 1_000_000}]
    mock_daily_bars.return_value = [{"close": 50.0}]
    mock_avg_volume.return_value = 1_000_000
    mock_sma.return_value = 45.0
    mock_rsi.return_value = 50.0
    mock_inst_ownership.return_value = None

    result = scanner.scan_ticker("AAPL")

    etf = result["strategies"]["ETF_COMPONENT"]
    assert etf["criteria"]["min_institutional_ownership_pct"]["passed"] is None
    assert "yfinance unavailable" in etf["criteria"]["min_institutional_ownership_pct"]["note"]


@patch("src.yfinance_data.get_institutional_ownership_pct")
@patch("src.scanner._get_daily_bars_tradier")
@patch("src.tradier.get_quote")
@patch("src.massive.compute_avg_volume")
@patch("src.massive.get_ticker_details")
def test_scan_ticker_skip_strategies(
    mock_ticker_details,
    mock_avg_volume,
    mock_quote,
    mock_tradier_bars,
    mock_inst_ownership,
):
    """skip_strategies excludes the specified strategies from results."""
    mock_quote.return_value = {"last": 50.0}
    mock_ticker_details.return_value = {"name": "Test", "market_cap_b": 5.0, "exchange": "XNYS"}
    mock_tradier_bars.return_value = [{"close": 50.0, "volume": 500_000}]
    mock_avg_volume.return_value = 500_000
    mock_inst_ownership.return_value = 72.0

    result = scanner.scan_ticker("AAPL", skip_strategies={"VOL_PREMIUM"})

    assert "VOL_PREMIUM" not in result["strategies"]
    assert "TECHNICAL" in result["strategies"]
    assert "ETF_COMPONENT" in result["strategies"]


@patch("src.tradier.get_quote")
def test_scan_ticker_fetch_error(mock_quote):
    """scan_ticker returns error dict when quote fetch fails."""
    from src.tradier import TradierError
    mock_quote.side_effect = TradierError("Connection failed")

    result = scanner.scan_ticker("AAPL")

    assert result["error"] is not None
    assert result["passes_any"] is False
    assert result["strategies"] == {}


# ---------------------------------------------------------------------------
# scan_universe tests
# ---------------------------------------------------------------------------

def _make_result(symbol, passes_any, error=None, passed_criteria=0):
    strategies = {}
    if not error:
        strategies = {
            "TECHNICAL": {
                "passes_all": passes_any,
                "criteria": {f"c{i}": {"passed": True} for i in range(passed_criteria)},
            }
        }
    return {
        "symbol": symbol,
        "passes_any": passes_any,
        "error": error,
        "strategies": strategies,
    }


@patch("src.scanner.scan_ticker")
def test_scan_universe_returns_results(mock_scan_ticker):
    """scan_universe returns results for all tickers."""
    mock_scan_ticker.side_effect = [
        _make_result("AAPL", passes_any=True),
        _make_result("MSFT", passes_any=False),
    ]

    results = scanner.scan_universe(tickers=["AAPL", "MSFT"])

    assert len(results) == 2
    mock_scan_ticker.assert_any_call("AAPL")
    mock_scan_ticker.assert_any_call("MSFT")


@patch("src.scanner.scan_ticker")
def test_scan_universe_sorts_full_passes_first(mock_scan_ticker):
    """scan_universe sorts passes_any=True first."""
    mock_scan_ticker.side_effect = [
        _make_result("PARTIAL", passes_any=False, passed_criteria=1),
        _make_result("FULLPASS", passes_any=True),
    ]

    results = scanner.scan_universe(tickers=["PARTIAL", "FULLPASS"])

    assert results[0]["symbol"] == "FULLPASS"
    assert results[1]["symbol"] == "PARTIAL"


@patch("src.scanner.scan_ticker")
def test_scan_universe_sorts_errors_last(mock_scan_ticker):
    """scan_universe sorts errors last."""
    mock_scan_ticker.side_effect = [
        _make_result("ERROR", passes_any=False, error="Connection failed"),
        _make_result("OK", passes_any=True),
    ]

    results = scanner.scan_universe(tickers=["ERROR", "OK"])

    assert results[0]["symbol"] == "OK"
    assert results[1]["symbol"] == "ERROR"


@patch("src.scanner.scan_ticker")
def test_scan_universe_calls_progress_callback(mock_scan_ticker):
    """scan_universe calls progress_callback with correct args."""
    mock_scan_ticker.return_value = _make_result("AAPL", passes_any=True)

    callback_calls = []

    def progress_cb(i, total, symbol):
        callback_calls.append((i, total, symbol))

    scanner.scan_universe(tickers=["AAPL", "MSFT"], progress_callback=progress_cb)

    assert len(callback_calls) == 2
    assert callback_calls[0] == (0, 2, "AAPL")
    assert callback_calls[1] == (1, 2, "MSFT")


# ---------------------------------------------------------------------------
# yfinance_data tests
# ---------------------------------------------------------------------------

@patch("src.yfinance_data.yf.Ticker")
def test_get_institutional_ownership_pct_success(mock_ticker_cls):
    """get_institutional_ownership_pct returns correct institutional ownership %."""
    import pandas as pd
    from src.yfinance_data import get_institutional_ownership_pct

    mock_df = pd.DataFrame(["5.07%", "15.23%", "72.45%", "84.68%"])
    mock_ticker_cls.return_value.major_holders = mock_df

    result = get_institutional_ownership_pct("RKLB")
    assert result == 72.45


@patch("src.yfinance_data.yf.Ticker")
def test_get_institutional_ownership_pct_no_holders(mock_ticker_cls):
    """get_institutional_ownership_pct returns None when major_holders is None."""
    from src.yfinance_data import get_institutional_ownership_pct
    mock_ticker_cls.return_value.major_holders = None
    assert get_institutional_ownership_pct("RKLB") is None


@patch("src.yfinance_data.yf.Ticker")
def test_get_institutional_ownership_pct_empty_df(mock_ticker_cls):
    """get_institutional_ownership_pct returns None when major_holders is empty."""
    import pandas as pd
    from src.yfinance_data import get_institutional_ownership_pct
    mock_ticker_cls.return_value.major_holders = pd.DataFrame()
    assert get_institutional_ownership_pct("RKLB") is None


@patch("src.yfinance_data.yf.Ticker")
def test_get_institutional_ownership_pct_exception(mock_ticker_cls):
    """get_institutional_ownership_pct returns None on any exception."""
    from src.yfinance_data import get_institutional_ownership_pct
    mock_ticker_cls.side_effect = Exception("Connection error")
    assert get_institutional_ownership_pct("RKLB") is None
