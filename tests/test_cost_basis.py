"""
Tests for cost_basis.py.

Covers:
- CycleSummary retrieval before and after assignment
- PositionPnL computation at various current prices
- Realized P&L aggregation
- audit_cycle: clean cycle passes, tampered total_premium fails
"""

import pytest
from unittest.mock import patch

import src.db as db_module
from src import state_machine as sm
from src import cost_basis as cb


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    monkeypatch.setattr(db_module, "_initialized", False)


def _open_and_assign(strike=50.0, put_price=1.50, assign_price=50.0):
    r = sm.open_short_put(
        underlying_id="XYZ", strike=strike, expiration="2025-03-21",
        contracts=1, price_per_share=put_price, source="MANUAL",
    )
    sm.record_assignment(cycle_id=r["cycle_id"], fill_price=assign_price, source="MANUAL")
    return r["cycle_id"]


# ---------------------------------------------------------------------------
# get_cycle_summary
# ---------------------------------------------------------------------------

def test_summary_before_assignment():
    r = sm.open_short_put(
        underlying_id="XYZ", strike=50.0, expiration="2025-03-21",
        contracts=1, price_per_share=1.50, source="MANUAL",
    )
    s = cb.get_cycle_summary(r["cycle_id"])
    assert s.state == "SHORT_PUT"
    assert s.total_premium == pytest.approx(150.0)
    assert s.cost_basis is None
    assert s.assignment_price is None


def test_summary_after_assignment():
    cid = _open_and_assign(strike=50.0, put_price=1.50, assign_price=50.0)
    s = cb.get_cycle_summary(cid)
    assert s.state == "LONG_STOCK"
    assert s.cost_basis == pytest.approx(48.50)
    assert s.shares_held == 100


def test_summary_not_found():
    with pytest.raises(ValueError):
        cb.get_cycle_summary(999)


# ---------------------------------------------------------------------------
# list_active_cycles
# ---------------------------------------------------------------------------

def test_list_active_excludes_closed():
    r1 = sm.open_short_put(
        underlying_id="AAA", strike=50.0, expiration="2025-03-21",
        contracts=1, price_per_share=1.00, source="MANUAL",
    )
    r2 = sm.open_short_put(
        underlying_id="BBB", strike=30.0, expiration="2025-03-21",
        contracts=1, price_per_share=0.80, source="MANUAL",
    )
    sm.record_expiration(cycle_id=r1["cycle_id"])  # closes AAA

    active = cb.list_active_cycles()
    ids = [s.cycle_id for s in active]
    assert r1["cycle_id"] not in ids
    assert r2["cycle_id"] in ids


# ---------------------------------------------------------------------------
# get_position_pnl
# ---------------------------------------------------------------------------

def test_position_pnl_at_breakeven():
    cid = _open_and_assign(strike=50.0, put_price=1.50, assign_price=50.0)
    # cost_basis = 48.50; at 48.50 unrealized = 0
    pnl = cb.get_position_pnl(cid, current_price=48.50)
    assert pnl.unrealized_pnl == pytest.approx(0.0)
    assert pnl.cost_basis == pytest.approx(48.50)


def test_position_pnl_above_cost_basis():
    cid = _open_and_assign(strike=50.0, put_price=1.50, assign_price=50.0)
    pnl = cb.get_position_pnl(cid, current_price=52.00)
    # (52.00 - 48.50) * 100 = 350
    assert pnl.unrealized_pnl == pytest.approx(350.0)


def test_position_pnl_below_cost_basis():
    cid = _open_and_assign(strike=50.0, put_price=1.50, assign_price=50.0)
    pnl = cb.get_position_pnl(cid, current_price=45.00)
    # (45.00 - 48.50) * 100 = -350
    assert pnl.unrealized_pnl == pytest.approx(-350.0)


def test_position_pnl_wrong_state():
    r = sm.open_short_put(
        underlying_id="XYZ", strike=50.0, expiration="2025-03-21",
        contracts=1, price_per_share=1.50, source="MANUAL",
    )
    with pytest.raises(ValueError, match="LONG_STOCK or SHORT_CALL"):
        cb.get_position_pnl(r["cycle_id"], current_price=50.0)


# ---------------------------------------------------------------------------
# get_realized_pnl_summary
# ---------------------------------------------------------------------------

def test_realized_pnl_summary_all():
    # Cycle 1: called away for profit
    cid1 = _open_and_assign(strike=50.0, put_price=1.50, assign_price=50.0)
    sm.open_short_call(
        cycle_id=cid1, strike=52.0, expiration="2025-04-18",
        contracts=1, price_per_share=0.90, source="MANUAL",
    )
    sm.record_called_away(cycle_id=cid1, fill_price=52.0, source="MANUAL")
    # realized = (52 - (50 - 2.40)) * 100 = (52 - 47.60) * 100 = 440

    # Cycle 2: put expires worthless
    r2 = sm.open_short_put(
        underlying_id="XYZ", strike=48.0, expiration="2025-05-16",
        contracts=1, price_per_share=1.00, source="MANUAL",
    )
    sm.record_expiration(cycle_id=r2["cycle_id"])
    # realized_pnl = 0.0 (no assignment, set in record_expiration)

    summary = cb.get_realized_pnl_summary()
    assert summary["cycle_count"] == 2
    assert summary["total_realized"] == pytest.approx(440.0)
    assert summary["avg_per_cycle"] == pytest.approx(220.0)


def test_realized_pnl_summary_by_underlying():
    cid = _open_and_assign(strike=50.0, put_price=1.50, assign_price=50.0)
    sm.open_short_call(
        cycle_id=cid, strike=52.0, expiration="2025-04-18",
        contracts=1, price_per_share=0.90, source="MANUAL",
    )
    sm.record_called_away(cycle_id=cid, fill_price=52.0, source="MANUAL")

    result = cb.get_realized_pnl_summary("XYZ")
    assert result["cycle_count"] == 1

    empty = cb.get_realized_pnl_summary("NONE")
    assert empty["total_realized"] == pytest.approx(0.0)
    assert empty["cycle_count"] == 0


# ---------------------------------------------------------------------------
# audit_cycle
# ---------------------------------------------------------------------------

def test_audit_clean_cycle():
    r = sm.open_short_put(
        underlying_id="XYZ", strike=50.0, expiration="2025-03-21",
        contracts=1, price_per_share=1.50, source="MANUAL",
    )
    sm.roll_position(
        cycle_id=r["cycle_id"],
        close_price_per_share=2.00, open_strike=48.0,
        open_expiration="2025-04-18", open_price_per_share=1.80,
        contracts=1, source="MANUAL",
    )
    audit = cb.audit_cycle(r["cycle_id"])
    assert audit.match is True
    assert audit.delta == pytest.approx(0.0)


def test_audit_detects_tampered_total_premium(tmp_path, monkeypatch):
    """Simulate a direct UPDATE to total_premium bypassing state machine."""
    r = sm.open_short_put(
        underlying_id="XYZ", strike=50.0, expiration="2025-03-21",
        contracts=1, price_per_share=1.50, source="MANUAL",
    )
    # Tamper directly
    from src.db import get_conn
    with get_conn() as conn:
        conn.execute(
            "UPDATE cycle SET total_premium = 999.0 WHERE cycle_id=?",
            (r["cycle_id"],)
        )

    audit = cb.audit_cycle(r["cycle_id"])
    assert audit.match is False
    assert audit.delta == pytest.approx(999.0 - 150.0)


def test_audit_all_active_returns_only_mismatches():
    r1 = sm.open_short_put(
        underlying_id="AAA", strike=50.0, expiration="2025-03-21",
        contracts=1, price_per_share=1.50, source="MANUAL",
    )
    r2 = sm.open_short_put(
        underlying_id="BBB", strike=30.0, expiration="2025-03-21",
        contracts=1, price_per_share=1.00, source="MANUAL",
    )
    # Tamper only r2
    from src.db import get_conn
    with get_conn() as conn:
        conn.execute(
            "UPDATE cycle SET total_premium = 500.0 WHERE cycle_id=?",
            (r2["cycle_id"],)
        )

    mismatches = cb.audit_all_active()
    assert len(mismatches) == 1
    assert mismatches[0].cycle_id == r2["cycle_id"]
