"""
Tests for poller.py.

Uses a real in-process SQLite DB. Tradier calls are mocked.
Covers:
- Confirmed fill updates trade and corrects premium
- Rejected/canceled order calls reject_fill
- Stale PENDING trade (>24h) is auto-rejected without Tradier call
- Auth error aborts the poll cycle early
- Orders still open/partially_filled are left PENDING
- start_poller() is idempotent
"""

import pytest
import time
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

import src.db as db_module
from src import state_machine as sm
from src import poller as pol
from src.poller import _poll_once, _get_pending_trades


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    monkeypatch.setattr(db_module, "_initialized", False)
    yield


def _get_trade(trade_id):
    from src.db import get_conn
    with get_conn() as conn:
        return dict(conn.execute(
            "SELECT * FROM trade WHERE trade_id=?", (trade_id,)
        ).fetchone())


def _get_cycle(cycle_id):
    from src.db import get_conn
    with get_conn() as conn:
        return dict(conn.execute(
            "SELECT * FROM cycle WHERE cycle_id=?", (cycle_id,)
        ).fetchone())


def _open_pending_put(price=1.50) -> dict:
    """Open a SHORT_PUT with TRADIER_SANDBOX source so it's PENDING."""
    with patch("src.state_machine._try_submit", return_value="ORD-001"):
        return sm.open_short_put(
            underlying_id="XYZ", strike=50.0, expiration="2025-03-21",
            contracts=1, price_per_share=price, source="TRADIER_SANDBOX",
        )


# ---------------------------------------------------------------------------
# _get_pending_trades
# ---------------------------------------------------------------------------

def test_no_pending_initially():
    sm.open_short_put(
        underlying_id="XYZ", strike=50.0, expiration="2025-03-21",
        contracts=1, price_per_share=1.50, source="MANUAL",
    )
    assert _get_pending_trades() == []


def test_pending_returned_for_tradier_source():
    r = _open_pending_put()
    pending = _get_pending_trades()
    assert len(pending) == 1
    assert pending[0]["trade_id"] == r["trade_id"]
    assert pending[0]["broker_order_id"] == "ORD-001"


# ---------------------------------------------------------------------------
# _poll_once — confirmed fill
# ---------------------------------------------------------------------------

def test_poll_confirms_filled_order():
    r = _open_pending_put(price=1.50)
    cycle_before = _get_cycle(r["cycle_id"])["total_premium"]

    with patch("src.poller.get_order_status", return_value={
        "status": "filled",
        "avg_fill_price": 1.50,
    }):
        stats = _poll_once()

    assert stats["confirmed"] == 1
    assert stats["rejected"] == 0
    assert _get_trade(r["trade_id"])["fill_status"] == "CONFIRMED"
    # Premium unchanged when actual == optimistic price
    assert _get_cycle(r["cycle_id"])["total_premium"] == pytest.approx(cycle_before)


def test_poll_corrects_premium_on_price_difference():
    """If actual fill price differs from optimistic, total_premium is corrected."""
    r = _open_pending_put(price=1.50)

    with patch("src.poller.get_order_status", return_value={
        "status": "filled",
        "avg_fill_price": 1.40,   # filled slightly lower
    }):
        _poll_once()

    trade = _get_trade(r["trade_id"])
    assert trade["fill_status"] == "CONFIRMED"
    assert trade["price_per_share"] == pytest.approx(1.40)
    # net_credit corrected: 1 * 100 * 1.40 = 140
    assert trade["net_credit"] == pytest.approx(140.0)
    assert _get_cycle(r["cycle_id"])["total_premium"] == pytest.approx(140.0)


# ---------------------------------------------------------------------------
# _poll_once — rejected/canceled
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tradier_status", ["rejected", "canceled", "expired"])
def test_poll_rejects_on_terminal_status(tradier_status):
    r = _open_pending_put()
    before_premium = _get_cycle(r["cycle_id"])["total_premium"]

    with patch("src.poller.get_order_status", return_value={"status": tradier_status}):
        stats = _poll_once()

    assert stats["rejected"] == 1
    assert _get_trade(r["trade_id"])["fill_status"] == "REJECTED"
    # Premium reversed
    assert _get_cycle(r["cycle_id"])["total_premium"] == pytest.approx(
        before_premium - 150.0
    )


# ---------------------------------------------------------------------------
# _poll_once — still open
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tradier_status", ["open", "partially_filled", "pending"])
def test_poll_leaves_pending_for_open_orders(tradier_status):
    r = _open_pending_put()

    with patch("src.poller.get_order_status", return_value={"status": tradier_status}):
        stats = _poll_once()

    assert stats["confirmed"] == 0
    assert stats["rejected"] == 0
    assert _get_trade(r["trade_id"])["fill_status"] == "PENDING"


# ---------------------------------------------------------------------------
# _poll_once — stale auto-reject
# ---------------------------------------------------------------------------

def test_poll_auto_rejects_stale_pending(monkeypatch):
    """Trade older than MAX_PENDING_AGE_HOURS is auto-rejected without Tradier call."""
    r = _open_pending_put()

    # Backdate the trade's filled_at to 25 hours ago
    old_time = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    from src.db import get_conn
    with get_conn() as conn:
        conn.execute(
            "UPDATE trade SET filled_at=? WHERE trade_id=?",
            (old_time, r["trade_id"])
        )

    mock_tradier = MagicMock()
    with patch("src.poller.get_order_status", mock_tradier):
        stats = _poll_once()

    mock_tradier.assert_not_called()
    assert stats["rejected"] == 1
    assert _get_trade(r["trade_id"])["fill_status"] == "REJECTED"


# ---------------------------------------------------------------------------
# _poll_once — auth error aborts early
# ---------------------------------------------------------------------------

def test_poll_aborts_on_auth_error():
    from src.tradier import TradierAuthError
    _open_pending_put()
    # Open a second pending trade
    with patch("src.state_machine._try_submit", return_value="ORD-002"):
        sm.open_short_put(
            underlying_id="AAA", strike=30.0, expiration="2025-03-21",
            contracts=1, price_per_share=0.80, source="TRADIER_SANDBOX",
        )

    with patch("src.poller.get_order_status", side_effect=TradierAuthError("bad key")):
        stats = _poll_once()

    assert stats["errors"] >= 1
    assert stats["confirmed"] == 0


# ---------------------------------------------------------------------------
# _poll_once — Tradier error on single order continues
# ---------------------------------------------------------------------------

def test_poll_continues_after_single_tradier_error():
    from src.tradier import TradierError
    r1 = _open_pending_put()
    with patch("src.state_machine._try_submit", return_value="ORD-002"):
        r2 = sm.open_short_put(
            underlying_id="AAA", strike=30.0, expiration="2025-03-21",
            contracts=1, price_per_share=0.80, source="TRADIER_SANDBOX",
        )

    def _side_effect(order_id):
        if order_id == "ORD-001":
            raise TradierError("timeout")
        return {"status": "filled", "avg_fill_price": 0.80}

    with patch("src.poller.get_order_status", side_effect=_side_effect):
        stats = _poll_once()

    assert stats["errors"] == 1
    assert stats["confirmed"] == 1
    assert _get_trade(r1["trade_id"])["fill_status"] == "PENDING"   # errored, unchanged
    assert _get_trade(r2["trade_id"])["fill_status"] == "CONFIRMED"


# ---------------------------------------------------------------------------
# start_poller idempotency
# ---------------------------------------------------------------------------

def test_start_poller_is_idempotent():
    pol.start_poller(interval=3600)  # long interval so it doesn't actually poll
    thread_id = pol._poller_thread.ident
    pol.start_poller(interval=3600)
    assert pol._poller_thread.ident == thread_id  # same thread, not a new one
    pol.stop_poller()


def test_poller_status_reflects_state():
    pol.stop_poller()
    status = pol.poller_status()
    assert status["running"] is False

    pol.start_poller(interval=3600)
    status = pol.poller_status()
    assert status["running"] is True
    pol.stop_poller()
