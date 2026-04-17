"""Tests for src/eligibility.py — wheel eligibility gate."""

import pytest
from datetime import date

import src.db as db_module
from src import eligibility


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    monkeypatch.setattr(db_module, "_initialized", False)


def _seed(ticker: str, wheel_eligible: int = 0, strategy: str = None):
    from src.db import get_conn
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO underlying "
            "(underlying_id, ticker, wheel_eligible, eligible_strategy) VALUES (?,?,?,?)",
            (ticker, ticker, wheel_eligible, strategy),
        )


# ---------------------------------------------------------------------------
# update_eligibility
# ---------------------------------------------------------------------------

def test_update_eligibility_sets_all_fields():
    _seed("AAPL")
    eligibility.update_eligibility("AAPL", True, "FUNDAMENTAL", "Strong FCF")

    from src.db import get_conn
    with get_conn() as conn:
        row = conn.execute(
            "SELECT wheel_eligible, eligible_strategy, quality_notes, last_reviewed "
            "FROM underlying WHERE ticker = 'AAPL'"
        ).fetchone()

    assert row["wheel_eligible"] == 1
    assert row["eligible_strategy"] == "FUNDAMENTAL"
    assert row["quality_notes"] == "Strong FCF"
    assert row["last_reviewed"] == date.today().isoformat()


def test_update_eligibility_requires_strategy_when_eligible():
    _seed("MSFT")
    with pytest.raises(ValueError, match="strategy is required"):
        eligibility.update_eligibility("MSFT", True, None, None)


def test_update_eligibility_clears_strategy_when_ineligible():
    _seed("TSLA", wheel_eligible=1, strategy="TECHNICAL")
    eligibility.update_eligibility("TSLA", False, None, "Too volatile")

    from src.db import get_conn
    with get_conn() as conn:
        row = conn.execute(
            "SELECT wheel_eligible, eligible_strategy, quality_notes "
            "FROM underlying WHERE ticker = 'TSLA'"
        ).fetchone()

    assert row["wheel_eligible"] == 0
    assert row["eligible_strategy"] is None
    assert row["quality_notes"] == "Too volatile"


def test_update_eligibility_rejects_invalid_strategy():
    _seed("GME")
    with pytest.raises(ValueError, match="Invalid strategy"):
        eligibility.update_eligibility("GME", True, "MEME", None)


# ---------------------------------------------------------------------------
# get_eligible_underlyings
# ---------------------------------------------------------------------------

def test_get_eligible_underlyings_filters_correctly():
    _seed("ELIG", wheel_eligible=1, strategy="FUNDAMENTAL")
    _seed("INELIG", wheel_eligible=0)

    results = eligibility.get_eligible_underlyings()
    tickers = {r["ticker"] for r in results}

    assert "ELIG" in tickers
    assert "INELIG" not in tickers


def test_get_eligible_underlyings_filters_by_strategy():
    _seed("FUND", wheel_eligible=1, strategy="FUNDAMENTAL")
    _seed("TECH", wheel_eligible=1, strategy="TECHNICAL")

    results = eligibility.get_eligible_underlyings(strategy="FUNDAMENTAL")
    tickers = {r["ticker"] for r in results}

    assert "FUND" in tickers
    assert "TECH" not in tickers


def test_get_eligible_underlyings_sorted_by_ticker():
    _seed("ZZZ", wheel_eligible=1, strategy="VOL_PREMIUM")
    _seed("AAA", wheel_eligible=1, strategy="ETF_COMPONENT")
    _seed("MMM", wheel_eligible=1, strategy="TECHNICAL")

    results = eligibility.get_eligible_underlyings()
    tickers = [r["ticker"] for r in results]
    assert tickers == sorted(tickers)


# ---------------------------------------------------------------------------
# get_ineligible_underlyings
# ---------------------------------------------------------------------------

def test_get_ineligible_underlyings_returns_only_ineligible():
    _seed("GOOD", wheel_eligible=1, strategy="FUNDAMENTAL")
    _seed("BAD", wheel_eligible=0)
    _seed("UGLY", wheel_eligible=0)

    results = eligibility.get_ineligible_underlyings()
    tickers = {r["ticker"] for r in results}

    assert "BAD" in tickers
    assert "UGLY" in tickers
    assert "GOOD" not in tickers


# ---------------------------------------------------------------------------
# get_strategy_description
# ---------------------------------------------------------------------------

def test_get_strategy_description_returns_correct_string():
    desc = eligibility.get_strategy_description("FUNDAMENTAL")
    assert "long-term" in desc.lower() or "profitable" in desc.lower()


def test_get_strategy_description_raises_for_unknown():
    with pytest.raises(KeyError):
        eligibility.get_strategy_description("UNKNOWN")
