"""Tests for src/screener.py — equity screening logic."""

import pytest
from datetime import date, timedelta

import src.db as db_module
from src import screener, state_machine as sm


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    monkeypatch.setattr(db_module, "_initialized", False)


def _seed(
    ticker: str,
    iv_rank: float = None,
    earnings_date: str = None,
    wheel_eligible: int = 1,
    strategy: str = "FUNDAMENTAL",
):
    from src.db import get_conn
    from datetime import date
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO underlying "
            "(underlying_id, ticker, iv_rank_cached, earnings_date, "
            " wheel_eligible) VALUES (?,?,?,?,?)",
            (ticker, ticker, iv_rank, earnings_date, wheel_eligible),
        )
        if wheel_eligible and strategy:
            conn.execute(
                "INSERT OR IGNORE INTO underlying_strategy "
                "(underlying_id, strategy, added_date) VALUES (?,?,?)",
                (ticker, strategy, date.today().isoformat()),
            )


def _open_put(ticker: str, iv_rank: float = 80.0) -> str:
    _seed(ticker, iv_rank)
    result = sm.open_short_put(
        underlying_id=ticker, strike=10.0, expiration="2099-12-19",
        contracts=1, price_per_share=0.50, source="MANUAL",
    )
    return result["cycle_id"]


def _open_long_stock(ticker: str, iv_rank: float = 80.0) -> str:
    cycle_id = _open_put(ticker, iv_rank)
    sm.record_assignment(cycle_id=cycle_id, fill_price=10.0, source="MANUAL")
    return cycle_id


# ---------------------------------------------------------------------------
# has_earnings_soon
# ---------------------------------------------------------------------------

def test_has_earnings_soon_none():
    assert screener.has_earnings_soon(None) is False


def test_has_earnings_soon_today():
    assert screener.has_earnings_soon(date.today().isoformat()) is True


def test_has_earnings_soon_within_window():
    soon = (date.today() + timedelta(days=5)).isoformat()
    assert screener.has_earnings_soon(soon, dte_window=7) is True


def test_has_earnings_soon_at_window_boundary_is_excluded():
    # window is [today, today+dte_window) — the boundary day itself is excluded
    boundary = (date.today() + timedelta(days=7)).isoformat()
    assert screener.has_earnings_soon(boundary, dte_window=7) is False


def test_has_earnings_soon_past_date():
    past = (date.today() - timedelta(days=1)).isoformat()
    assert screener.has_earnings_soon(past) is False


def test_has_earnings_soon_invalid_format():
    assert screener.has_earnings_soon("not-a-date") is False


# ---------------------------------------------------------------------------
# get_screening_candidates
# ---------------------------------------------------------------------------

def test_filters_by_min_iv_rank():
    _seed("HIGH", iv_rank=75.0)
    _seed("LOW",  iv_rank=30.0)
    results = screener.get_screening_candidates(min_iv_rank=50.0)
    tickers = {r["ticker"] for r in results}
    assert "HIGH" in tickers
    assert "LOW" not in tickers


def test_excludes_tickers_with_short_put():
    _open_put("BUSY", iv_rank=80.0)
    results = screener.get_screening_candidates(min_iv_rank=50.0)
    assert not any(r["ticker"] == "BUSY" for r in results)


def test_excludes_tickers_with_long_stock():
    _open_long_stock("HELD", iv_rank=80.0)
    results = screener.get_screening_candidates(min_iv_rank=50.0)
    assert not any(r["ticker"] == "HELD" for r in results)


def test_excludes_earnings_within_window():
    soon = (date.today() + timedelta(days=3)).isoformat()
    _seed("EARN", iv_rank=80.0, earnings_date=soon)
    results = screener.get_screening_candidates(min_iv_rank=50.0, exclude_earnings_window=7)
    assert not any(r["ticker"] == "EARN" for r in results)


def test_does_not_exclude_earnings_outside_window():
    far = (date.today() + timedelta(days=30)).isoformat()
    _seed("SAFE", iv_rank=80.0, earnings_date=far)
    results = screener.get_screening_candidates(min_iv_rank=50.0, exclude_earnings_window=7)
    assert any(r["ticker"] == "SAFE" for r in results)


def test_sorted_by_iv_rank_descending():
    _seed("A", iv_rank=60.0)
    _seed("B", iv_rank=90.0)
    _seed("C", iv_rank=75.0)
    results = screener.get_screening_candidates(min_iv_rank=50.0)
    iv_ranks = [r["iv_rank_cached"] for r in results]
    assert iv_ranks == sorted(iv_ranks, reverse=True)


def test_max_results_limit():
    for i in range(5):
        _seed(f"TK{i}", iv_rank=float(60 + i))
    results = screener.get_screening_candidates(min_iv_rank=50.0, max_results=3)
    assert len(results) == 3


def test_includes_null_iv_rank_tickers():
    """Tickers with no IV data yet (NULL) should still appear."""
    _seed("FRESH", iv_rank=None)
    results = screener.get_screening_candidates(min_iv_rank=50.0)
    assert any(r["ticker"] == "FRESH" for r in results)


def test_result_has_expected_keys():
    _seed("RKLB", iv_rank=70.0)
    results = screener.get_screening_candidates(min_iv_rank=50.0)
    assert len(results) == 1
    expected_keys = {
        "underlying_id", "ticker", "iv_rank_cached", "iv_pct_cached",
        "iv_current", "earnings_date", "notes", "iv_updated", "has_earnings_soon",
        "strategies", "conviction", "last_reviewed",
    }
    assert expected_keys.issubset(results[0].keys())


# ---------------------------------------------------------------------------
# get_all_watchlist
# ---------------------------------------------------------------------------

def test_watchlist_returns_all_tickers():
    _seed("A", iv_rank=70.0)
    _seed("B", iv_rank=40.0)
    results = screener.get_all_watchlist(include_inactive=True)
    assert {r["ticker"] for r in results} == {"A", "B"}


def test_watchlist_excludes_active_by_default():
    _seed("FREE", iv_rank=70.0)
    _open_put("BUSY", iv_rank=80.0)
    results = screener.get_all_watchlist(include_inactive=False)
    tickers = {r["ticker"] for r in results}
    assert "FREE" in tickers
    assert "BUSY" not in tickers


def test_watchlist_include_inactive_shows_all():
    _open_put("BUSY", iv_rank=80.0)
    results = screener.get_all_watchlist(include_inactive=True)
    assert any(r["ticker"] == "BUSY" for r in results)


# ---------------------------------------------------------------------------
# wheel_eligible pre-filter integration
# ---------------------------------------------------------------------------

def test_screener_excludes_ineligible_tickers():
    """Ineligible tickers must not appear in screening results even with high IV rank."""
    _seed("GOOD", iv_rank=80.0, wheel_eligible=1)
    _seed("BAD",  iv_rank=80.0, wheel_eligible=0)

    results = screener.get_screening_candidates(min_iv_rank=50.0)
    tickers = {r["ticker"] for r in results}

    assert "GOOD" in tickers
    assert "BAD" not in tickers


def test_result_includes_strategies_and_conviction():
    _seed("RKLB", iv_rank=70.0, strategy="FUNDAMENTAL")
    results = screener.get_screening_candidates(min_iv_rank=50.0)
    assert len(results) == 1
    r = results[0]
    assert r["strategies"] == ["FUNDAMENTAL"]
    assert r["conviction"] == 1


# ---------------------------------------------------------------------------
# get_screening_candidates_by_strategy
# ---------------------------------------------------------------------------

def test_by_strategy_filters_to_matching_tickers():
    _seed("FUND", iv_rank=70.0, strategy="FUNDAMENTAL")
    _seed("TECH", iv_rank=70.0, strategy="TECHNICAL")

    results = screener.get_screening_candidates_by_strategy("FUNDAMENTAL", min_iv_rank=40.0)
    tickers = {r["ticker"] for r in results}

    assert "FUND" in tickers
    assert "TECH" not in tickers


def test_by_strategy_ticker_with_two_strategies_appears_in_both():
    from datetime import date
    from src.db import get_conn

    _seed("BOTH", iv_rank=70.0, strategy="FUNDAMENTAL")
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO underlying_strategy "
            "(underlying_id, strategy, added_date) VALUES (?,?,?)",
            ("BOTH", "TECHNICAL", date.today().isoformat()),
        )

    fund_results = screener.get_screening_candidates_by_strategy("FUNDAMENTAL", min_iv_rank=40.0)
    tech_results = screener.get_screening_candidates_by_strategy("TECHNICAL", min_iv_rank=40.0)

    assert any(r["ticker"] == "BOTH" for r in fund_results)
    assert any(r["ticker"] == "BOTH" for r in tech_results)

    # conviction should be 2 in both
    both_row = next(r for r in fund_results if r["ticker"] == "BOTH")
    assert both_row["conviction"] == 2
    assert set(both_row["strategies"]) == {"FUNDAMENTAL", "TECHNICAL"}
