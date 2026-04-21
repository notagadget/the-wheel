"""
Tests for state_machine.py.

Covers:
- Every valid transition in the transition table
- Every invalid transition (must raise InvalidTransitionError)
- cost_basis arithmetic via the full worked example from docs/cost-basis-rules.md
- Optimistic fill: TRADIER_SANDBOX writes PENDING, confirm_fill/reject_fill work correctly
"""

import pytest
from unittest.mock import patch

import src.db as db_module

from src import state_machine as sm
from src.state_machine import (
    InvalidTransitionError,
    CycleClosedError,
    CycleNotFoundError,
)


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    monkeypatch.setattr(db_module, "_initialized", False)


def _open_put(underlying="XYZ", strike=50.0, price=1.50, source="MANUAL") -> dict:
    return sm.open_short_put(
        underlying_id=underlying, strike=strike, expiration="2025-03-21",
        contracts=1, price_per_share=price, source=source,
    )


def _get_cycle(cycle_id):
    from src.db import get_conn
    with get_conn() as conn:
        return dict(conn.execute(
            "SELECT * FROM cycle WHERE cycle_id=?", (cycle_id,)
        ).fetchone())


def _get_trade(trade_id):
    from src.db import get_conn
    with get_conn() as conn:
        return dict(conn.execute(
            "SELECT * FROM trade WHERE trade_id=?", (trade_id,)
        ).fetchone())


# ---------------------------------------------------------------------------
# open_short_put
# ---------------------------------------------------------------------------

def test_open_short_put_manual():
    result = _open_put()
    assert result["cycle_id"] == 1
    cycle = _get_cycle(1)
    assert cycle["state"] == "SHORT_PUT"
    assert cycle["total_premium"] == pytest.approx(150.0)
    trade = _get_trade(result["trade_id"])
    assert trade["trade_type"] == "SELL_PUT"
    assert trade["fill_status"] == "CONFIRMED"


def test_open_short_put_tradier_sets_pending():
    with patch("src.state_machine._try_submit", return_value="ORD-001"):
        result = _open_put(source="TRADIER_SANDBOX")
    trade = _get_trade(result["trade_id"])
    assert trade["fill_status"] == "PENDING"


def test_open_short_put_tradier_error_still_writes_pending():
    """Tradier error returns None from _try_submit; record still written as PENDING."""
    with patch("src.state_machine._try_submit", return_value=None):
        result = _open_put(source="TRADIER_SANDBOX")
    trade = _get_trade(result["trade_id"])
    assert trade["fill_status"] == "PENDING"
    assert trade["broker_order_id"] is None


# ---------------------------------------------------------------------------
# record_assignment
# ---------------------------------------------------------------------------

def test_record_assignment_transitions_to_long_stock():
    r = _open_put(strike=50.0, price=1.50)
    sm.record_assignment(cycle_id=r["cycle_id"], fill_price=50.0, source="MANUAL")
    cycle = _get_cycle(r["cycle_id"])
    assert cycle["state"] == "LONG_STOCK"
    assert cycle["assignment_price"] == pytest.approx(50.0)
    assert cycle["shares_held"] == 100
    assert cycle["cost_basis"] == pytest.approx(48.50)


def test_record_assignment_wrong_state():
    r = _open_put()
    sm.record_assignment(cycle_id=r["cycle_id"], fill_price=50.0, source="MANUAL")
    with pytest.raises(InvalidTransitionError):
        sm.record_assignment(cycle_id=r["cycle_id"], fill_price=50.0, source="MANUAL")


# ---------------------------------------------------------------------------
# open_short_call
# ---------------------------------------------------------------------------

def test_open_short_call_transitions_to_short_call():
    r = _open_put()
    sm.record_assignment(cycle_id=r["cycle_id"], fill_price=50.0, source="MANUAL")
    sm.open_short_call(
        cycle_id=r["cycle_id"], strike=52.0, expiration="2025-04-18",
        contracts=1, price_per_share=0.90, source="MANUAL",
    )
    cycle = _get_cycle(r["cycle_id"])
    assert cycle["state"] == "SHORT_CALL"
    assert cycle["total_premium"] == pytest.approx(240.0)


def test_open_short_call_wrong_state():
    r = _open_put()
    with pytest.raises(InvalidTransitionError):
        sm.open_short_call(
            cycle_id=r["cycle_id"], strike=52.0, expiration="2025-04-18",
            contracts=1, price_per_share=0.90, source="MANUAL",
        )


# ---------------------------------------------------------------------------
# roll_position
# ---------------------------------------------------------------------------

def test_roll_put_stays_in_short_put():
    r = _open_put(price=1.50)
    sm.roll_position(
        cycle_id=r["cycle_id"], close_price_per_share=2.00,
        open_strike=48.0, open_expiration="2025-04-18",
        open_price_per_share=1.80, contracts=1, source="MANUAL",
    )
    cycle = _get_cycle(r["cycle_id"])
    assert cycle["state"] == "SHORT_PUT"
    assert cycle["total_premium"] == pytest.approx(130.0)


def test_roll_creates_roll_event():
    from src.db import get_conn
    r = _open_put()
    result = sm.roll_position(
        cycle_id=r["cycle_id"], close_price_per_share=2.00,
        open_strike=48.0, open_expiration="2025-04-18",
        open_price_per_share=1.80, contracts=1, source="MANUAL",
    )
    with get_conn() as conn:
        event = conn.execute(
            "SELECT * FROM roll_event WHERE roll_group_id=?",
            (result["roll_group_id"],)
        ).fetchone()
    assert event is not None
    assert event["net_credit"] == pytest.approx(-20.0)


def test_roll_wrong_state():
    r = _open_put()
    sm.record_assignment(cycle_id=r["cycle_id"], fill_price=50.0, source="MANUAL")
    with pytest.raises(InvalidTransitionError):
        sm.roll_position(
            cycle_id=r["cycle_id"], close_price_per_share=1.0,
            open_strike=50.0, open_expiration="2025-04-18",
            open_price_per_share=1.5, contracts=1, source="MANUAL",
        )


# ---------------------------------------------------------------------------
# record_expiration
# ---------------------------------------------------------------------------

def test_expiration_put_closes_cycle():
    r = _open_put()
    result = sm.record_expiration(cycle_id=r["cycle_id"])
    assert result["new_state"] == "CLOSED"
    assert _get_cycle(r["cycle_id"])["state"] == "CLOSED"
    assert _get_cycle(r["cycle_id"])["total_premium"] == pytest.approx(150.0)


def test_expiration_call_returns_to_long_stock():
    r = _open_put()
    sm.record_assignment(cycle_id=r["cycle_id"], fill_price=50.0, source="MANUAL")
    sm.open_short_call(
        cycle_id=r["cycle_id"], strike=52.0, expiration="2025-04-18",
        contracts=1, price_per_share=0.90, source="MANUAL",
    )
    result = sm.record_expiration(cycle_id=r["cycle_id"])
    assert result["new_state"] == "LONG_STOCK"


# ---------------------------------------------------------------------------
# close_position
# ---------------------------------------------------------------------------

def test_close_put_early():
    r = _open_put(price=1.50)
    sm.close_position(cycle_id=r["cycle_id"], price_per_share=0.50, source="MANUAL")
    cycle = _get_cycle(r["cycle_id"])
    assert cycle["state"] == "CLOSED"
    assert cycle["total_premium"] == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# record_called_away
# ---------------------------------------------------------------------------

def test_called_away_computes_realized_pnl():
    r = _open_put(strike=50.0, price=1.50)
    sm.record_assignment(cycle_id=r["cycle_id"], fill_price=50.0, source="MANUAL")
    sm.open_short_call(
        cycle_id=r["cycle_id"], strike=52.0, expiration="2025-04-18",
        contracts=1, price_per_share=0.90, source="MANUAL",
    )
    result = sm.record_called_away(
        cycle_id=r["cycle_id"], fill_price=52.0, source="MANUAL"
    )
    # cost_basis = 50 - (150+90)/100 = 47.60; pnl = (52 - 47.60) * 100 = 440
    assert result["realized_pnl"] == pytest.approx(440.0)
    assert _get_cycle(r["cycle_id"])["state"] == "CLOSED"


def test_called_away_wrong_state():
    r = _open_put()
    with pytest.raises(InvalidTransitionError):
        sm.record_called_away(cycle_id=r["cycle_id"], fill_price=52.0, source="MANUAL")


# ---------------------------------------------------------------------------
# CLOSED cycle guard
# ---------------------------------------------------------------------------

def test_closed_cycle_rejects_mutations():
    r = _open_put()
    sm.record_expiration(cycle_id=r["cycle_id"])
    with pytest.raises(CycleClosedError):
        sm.record_assignment(cycle_id=r["cycle_id"], fill_price=50.0, source="MANUAL")


# ---------------------------------------------------------------------------
# Full worked example from docs/cost-basis-rules.md
# ---------------------------------------------------------------------------

def test_cost_basis_worked_example():
    r = sm.open_short_put(
        underlying_id="XYZ", strike=50.0, expiration="2025-02-21",
        contracts=1, price_per_share=1.50, source="MANUAL",
    )
    cid = r["cycle_id"]
    assert _get_cycle(cid)["total_premium"] == pytest.approx(150.0)

    sm.roll_position(
        cycle_id=cid, close_price_per_share=2.00, open_strike=48.0,
        open_expiration="2025-03-21", open_price_per_share=1.80,
        contracts=1, source="MANUAL",
    )
    assert _get_cycle(cid)["total_premium"] == pytest.approx(130.0)

    sm.record_assignment(cycle_id=cid, fill_price=48.0, source="MANUAL")
    assert _get_cycle(cid)["cost_basis"] == pytest.approx(46.70)

    sm.open_short_call(
        cycle_id=cid, strike=49.0, expiration="2025-04-18",
        contracts=1, price_per_share=0.90, source="MANUAL",
    )
    assert _get_cycle(cid)["cost_basis"] == pytest.approx(45.80)

    sm.record_expiration(cycle_id=cid)
    assert _get_cycle(cid)["total_premium"] == pytest.approx(220.0)

    sm.open_short_call(
        cycle_id=cid, strike=49.0, expiration="2025-05-16",
        contracts=1, price_per_share=0.70, source="MANUAL",
    )
    assert _get_cycle(cid)["cost_basis"] == pytest.approx(45.10)

    result = sm.record_called_away(cycle_id=cid, fill_price=49.0, source="MANUAL")
    assert result["realized_pnl"] == pytest.approx(390.0)
    assert _get_cycle(cid)["state"] == "CLOSED"


# ---------------------------------------------------------------------------
# confirm_fill / reject_fill
# ---------------------------------------------------------------------------

def test_confirm_fill_updates_status():
    with patch("src.state_machine._try_submit", return_value="ORD-1"):
        r = _open_put(source="TRADIER_SANDBOX")
    sm.confirm_fill(trade_id=r["trade_id"], broker_order_id="ORD-REAL")
    assert _get_trade(r["trade_id"])["fill_status"] == "CONFIRMED"
    assert _get_trade(r["trade_id"])["broker_order_id"] == "ORD-REAL"


def test_reject_fill_reverses_premium():
    with patch("src.state_machine._try_submit", return_value="ORD-1"):
        r = _open_put(source="TRADIER_SANDBOX")
    before = _get_cycle(r["cycle_id"])["total_premium"]
    sm.reject_fill(trade_id=r["trade_id"])
    after = _get_cycle(r["cycle_id"])["total_premium"]
    assert after == pytest.approx(before - 150.0)
    assert _get_trade(r["trade_id"])["fill_status"] == "REJECTED"
