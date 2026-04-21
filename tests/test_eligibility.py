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


def _seed(ticker: str, wheel_eligible: int = 0, strategies: list[str] | None = None):
    from src.db import get_conn
    strategies = strategies or []
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO underlying "
            "(underlying_id, ticker, wheel_eligible) VALUES (?,?,?)",
            (ticker, ticker, wheel_eligible),
        )
        for s in strategies:
            conn.execute(
                "INSERT OR IGNORE INTO underlying_strategy "
                "(underlying_id, strategy, added_date) VALUES (?,?,?)",
                (ticker, s, date.today().isoformat()),
            )


# ---------------------------------------------------------------------------
# update_eligibility
# ---------------------------------------------------------------------------

def test_update_eligibility_sets_all_fields():
    _seed("AAPL")
    eligibility.update_eligibility("AAPL", True, ["FUNDAMENTAL"], "Strong FCF")

    from src.db import get_conn
    with get_conn() as conn:
        row = conn.execute(
            "SELECT wheel_eligible, last_reviewed "
            "FROM underlying WHERE ticker = 'AAPL'"
        ).fetchone()
        strats = conn.execute(
            "SELECT strategy, quality_notes FROM underlying_strategy "
            "WHERE underlying_id = 'AAPL'"
        ).fetchall()

    assert row["wheel_eligible"] == 1
    assert row["last_reviewed"] == date.today().isoformat()
    assert {r["strategy"] for r in strats} == {"FUNDAMENTAL"}
    assert strats[0]["quality_notes"] == "Strong FCF"


def test_update_eligibility_multi_strategy():
    _seed("AAPL")
    eligibility.update_eligibility("AAPL", True, ["FUNDAMENTAL", "TECHNICAL"], "Dual signal")

    from src.db import get_conn
    with get_conn() as conn:
        strats = conn.execute(
            "SELECT strategy, quality_notes FROM underlying_strategy "
            "WHERE underlying_id = 'AAPL'"
        ).fetchall()

    assert {r["strategy"] for r in strats} == {"FUNDAMENTAL", "TECHNICAL"}
    assert all(r["quality_notes"] == "Dual signal" for r in strats)


def test_update_eligibility_replaces_strategies():
    _seed("AAPL", wheel_eligible=1, strategies=["FUNDAMENTAL", "TECHNICAL"])
    eligibility.update_eligibility("AAPL", True, ["VOL_PREMIUM"], "Re-tagged")

    from src.db import get_conn
    with get_conn() as conn:
        strats = conn.execute(
            "SELECT strategy FROM underlying_strategy WHERE underlying_id = 'AAPL'"
        ).fetchall()

    assert {r["strategy"] for r in strats} == {"VOL_PREMIUM"}


def test_update_eligibility_requires_strategy_when_eligible():
    _seed("MSFT")
    with pytest.raises(ValueError, match="at least one strategy"):
        eligibility.update_eligibility("MSFT", True, [], None)

    with pytest.raises(ValueError, match="at least one strategy"):
        eligibility.update_eligibility("MSFT", True, None, None)


def test_update_eligibility_clears_strategy_when_ineligible():
    _seed("TSLA", wheel_eligible=1, strategies=["TECHNICAL"])
    eligibility.update_eligibility("TSLA", False, None, "Too volatile")

    from src.db import get_conn
    with get_conn() as conn:
        row = conn.execute(
            "SELECT wheel_eligible, notes FROM underlying WHERE ticker = 'TSLA'"
        ).fetchone()
        strats = conn.execute(
            "SELECT COUNT(*) AS cnt FROM underlying_strategy WHERE underlying_id = 'TSLA'"
        ).fetchone()

    assert row["wheel_eligible"] == 0
    assert row["notes"] == "Too volatile"
    assert strats["cnt"] == 0


def test_update_eligibility_rejects_invalid_strategy():
    _seed("GME")
    with pytest.raises(ValueError, match="Invalid strategy"):
        eligibility.update_eligibility("GME", True, ["MEME"], None)


# ---------------------------------------------------------------------------
# remove_strategy
# ---------------------------------------------------------------------------

def test_remove_strategy_clears_wheel_eligible_when_last():
    _seed("AAPL", wheel_eligible=1, strategies=["FUNDAMENTAL"])
    eligibility.remove_strategy("AAPL", "FUNDAMENTAL")

    from src.db import get_conn
    with get_conn() as conn:
        row = conn.execute(
            "SELECT wheel_eligible FROM underlying WHERE ticker = 'AAPL'"
        ).fetchone()

    assert row["wheel_eligible"] == 0


def test_remove_strategy_keeps_eligible_when_others_remain():
    _seed("AAPL", wheel_eligible=1, strategies=["FUNDAMENTAL", "TECHNICAL"])
    eligibility.remove_strategy("AAPL", "FUNDAMENTAL")

    from src.db import get_conn
    with get_conn() as conn:
        row = conn.execute(
            "SELECT wheel_eligible FROM underlying WHERE ticker = 'AAPL'"
        ).fetchone()
        strats = conn.execute(
            "SELECT strategy FROM underlying_strategy WHERE underlying_id = 'AAPL'"
        ).fetchall()

    assert row["wheel_eligible"] == 1
    assert {r["strategy"] for r in strats} == {"TECHNICAL"}


def test_remove_strategy_noop_for_unknown_ticker():
    eligibility.remove_strategy("DOESNOTEXIST", "FUNDAMENTAL")  # should not raise


# ---------------------------------------------------------------------------
# conviction_score
# ---------------------------------------------------------------------------

def test_conviction_score_zero_for_ineligible():
    _seed("GME", wheel_eligible=0)
    assert eligibility.conviction_score("GME") == 0


def test_conviction_score_single():
    _seed("AAPL", wheel_eligible=1, strategies=["FUNDAMENTAL"])
    assert eligibility.conviction_score("AAPL") == 1


def test_conviction_score_multi():
    _seed("AAPL", wheel_eligible=1, strategies=["FUNDAMENTAL", "TECHNICAL", "VOL_PREMIUM"])
    assert eligibility.conviction_score("AAPL") == 3


# ---------------------------------------------------------------------------
# get_eligible_underlyings
# ---------------------------------------------------------------------------

def test_get_eligible_underlyings_filters_correctly():
    _seed("ELIG", wheel_eligible=1, strategies=["FUNDAMENTAL"])
    _seed("INELIG", wheel_eligible=0)

    results = eligibility.get_eligible_underlyings()
    tickers = {r["ticker"] for r in results}

    assert "ELIG" in tickers
    assert "INELIG" not in tickers


def test_get_eligible_underlyings_returns_strategies_list():
    _seed("AAPL", wheel_eligible=1, strategies=["FUNDAMENTAL", "TECHNICAL"])
    results = eligibility.get_eligible_underlyings()
    row = next(r for r in results if r["ticker"] == "AAPL")

    assert set(row["strategies"]) == {"FUNDAMENTAL", "TECHNICAL"}
    assert row["conviction"] == 2


def test_get_eligible_underlyings_filters_by_strategy():
    _seed("FUND", wheel_eligible=1, strategies=["FUNDAMENTAL"])
    _seed("TECH", wheel_eligible=1, strategies=["TECHNICAL"])
    _seed("BOTH", wheel_eligible=1, strategies=["FUNDAMENTAL", "TECHNICAL"])

    results = eligibility.get_eligible_underlyings(strategy="FUNDAMENTAL")
    tickers = {r["ticker"] for r in results}

    assert "FUND" in tickers
    assert "BOTH" in tickers   # has FUNDAMENTAL
    assert "TECH" not in tickers


def test_get_eligible_underlyings_sorted_by_ticker():
    _seed("ZZZ", wheel_eligible=1, strategies=["VOL_PREMIUM"])
    _seed("AAA", wheel_eligible=1, strategies=["ETF_COMPONENT"])
    _seed("MMM", wheel_eligible=1, strategies=["TECHNICAL"])

    results = eligibility.get_eligible_underlyings()
    tickers = [r["ticker"] for r in results]
    assert tickers == sorted(tickers)


# ---------------------------------------------------------------------------
# get_ineligible_underlyings
# ---------------------------------------------------------------------------

def test_get_ineligible_underlyings_returns_only_ineligible():
    _seed("GOOD", wheel_eligible=1, strategies=["FUNDAMENTAL"])
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
