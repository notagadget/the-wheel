"""
poller.py — Async poller for Tradier order fill confirmation.

Runs as a background thread. On each tick:
1. Fetches all trades with fill_status=PENDING
2. Queries Tradier for each order's status
3. Calls state_machine.confirm_fill() or state_machine.reject_fill()

Usage (from app.py or a Streamlit page):
    from src.poller import start_poller
    start_poller()   # idempotent — safe to call on every Streamlit rerun

The poller runs in a daemon thread and stops when the process exits.
It is intentionally simple: no retry backoff, no dead-letter queue.
Failed orders surface in the UI as REJECTED and require manual review.

Threading note: SQLite with WAL mode handles concurrent reads safely.
The poller only calls state_machine functions which use get_conn()
transactions — no direct DB writes outside of those functions.
"""

import logging
import threading
import time
from typing import Optional

from src.db import get_conn
from src.state_machine import confirm_fill, reject_fill
from src.tradier import get_order_status, TradierError, TradierAuthError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_INTERVAL_SECONDS = 30   # how often to poll
MAX_PENDING_AGE_HOURS    = 24   # reject orders older than this automatically

_poller_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()


# ---------------------------------------------------------------------------
# Core polling logic
# ---------------------------------------------------------------------------

def _get_pending_trades() -> list[dict]:
    """Fetch all PENDING trades that have a broker_order_id."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT trade_id, broker_order_id, filled_at, source
            FROM trade
            WHERE fill_status = 'PENDING'
              AND broker_order_id IS NOT NULL
            ORDER BY filled_at ASC
        """).fetchall()
    return [dict(r) for r in rows]


def _is_expired(filled_at_str: str) -> bool:
    """Return True if a PENDING trade is older than MAX_PENDING_AGE_HOURS."""
    from datetime import datetime, timezone, timedelta
    try:
        filled_at = datetime.fromisoformat(filled_at_str)
        if filled_at.tzinfo is None:
            filled_at = filled_at.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - filled_at
        return age > timedelta(hours=MAX_PENDING_AGE_HOURS)
    except Exception:
        return False


def _poll_once() -> dict:
    """
    Single poll cycle. Returns summary dict for logging/testing.
    {"checked": int, "confirmed": int, "rejected": int, "errors": int}
    """
    pending = _get_pending_trades()
    stats = {"checked": len(pending), "confirmed": 0, "rejected": 0, "errors": 0}

    for trade in pending:
        trade_id       = trade["trade_id"]
        order_id       = trade["broker_order_id"]
        filled_at      = trade["filled_at"]

        # Auto-reject stale orders
        if _is_expired(filled_at):
            logger.warning(
                "Trade %d (order %s) expired after %dh — auto-rejecting.",
                trade_id, order_id, MAX_PENDING_AGE_HOURS
            )
            try:
                reject_fill(
                    trade_id=trade_id,
                    notes=f"Auto-rejected: no fill after {MAX_PENDING_AGE_HOURS}h",
                )
                stats["rejected"] += 1
            except Exception as e:
                logger.error("Failed to auto-reject trade %d: %s", trade_id, e)
                stats["errors"] += 1
            continue

        # Query Tradier
        try:
            status = get_order_status(order_id)
        except TradierAuthError:
            # Auth error affects all trades — stop this poll cycle
            logger.error("Tradier auth error during poll — check credentials.")
            stats["errors"] += 1
            break
        except TradierError as e:
            logger.warning("Tradier error for order %s: %s", order_id, e)
            stats["errors"] += 1
            continue
        except Exception as e:
            logger.error("Unexpected error polling order %s: %s", order_id, e)
            stats["errors"] += 1
            continue

        order_status = status.get("status")

        if order_status == "filled":
            avg_price = status.get("avg_fill_price")
            try:
                confirm_fill(
                    trade_id=trade_id,
                    broker_order_id=order_id,
                    actual_price_per_share=float(avg_price) if avg_price else None,
                )
                logger.info(
                    "Trade %d confirmed (order %s, fill=%.4f).",
                    trade_id, order_id, avg_price or 0
                )
                stats["confirmed"] += 1
            except Exception as e:
                logger.error("confirm_fill failed for trade %d: %s", trade_id, e)
                stats["errors"] += 1

        elif order_status in ("canceled", "rejected", "expired"):
            try:
                reject_fill(
                    trade_id=trade_id,
                    notes=f"Tradier order status: {order_status}",
                )
                logger.warning(
                    "Trade %d rejected (order %s, status=%s).",
                    trade_id, order_id, order_status
                )
                stats["rejected"] += 1
            except Exception as e:
                logger.error("reject_fill failed for trade %d: %s", trade_id, e)
                stats["errors"] += 1

        elif order_status in ("open", "partially_filled", "pending"):
            # Still working — leave as PENDING
            logger.debug("Order %s still %s, skipping.", order_id, order_status)

        else:
            logger.warning("Unknown order status %r for order %s.", order_status, order_id)

    return stats


# ---------------------------------------------------------------------------
# Background thread
# ---------------------------------------------------------------------------

def _run_loop(interval: int) -> None:
    logger.info("Poller started (interval=%ds).", interval)
    while not _stop_event.is_set():
        try:
            stats = _poll_once()
            if stats["checked"] > 0:
                logger.info(
                    "Poll complete — checked=%d confirmed=%d rejected=%d errors=%d",
                    stats["checked"], stats["confirmed"],
                    stats["rejected"], stats["errors"]
                )
        except Exception as e:
            logger.error("Unhandled poller error: %s", e)
        _stop_event.wait(timeout=interval)
    logger.info("Poller stopped.")


def start_poller(interval: int = DEFAULT_INTERVAL_SECONDS) -> None:
    """
    Start the background poller thread. Idempotent — safe to call on every
    Streamlit rerun. Does nothing if the poller is already running.
    """
    global _poller_thread
    if _poller_thread is not None and _poller_thread.is_alive():
        return
    _stop_event.clear()
    _poller_thread = threading.Thread(
        target=_run_loop,
        args=(interval,),
        daemon=True,
        name="tradier-poller",
    )
    _poller_thread.start()


def stop_poller() -> None:
    """Signal the poller to stop. Blocks until the thread exits."""
    _stop_event.set()
    if _poller_thread is not None:
        _poller_thread.join(timeout=5)


def poller_status() -> dict:
    """Return current poller state for dashboard display."""
    running = _poller_thread is not None and _poller_thread.is_alive()
    pending_count = 0
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM trade WHERE fill_status='PENDING'"
            ).fetchone()
            pending_count = row[0] if row else 0
    except Exception:
        pass
    return {
        "running":       running,
        "pending_trades": pending_count,
        "interval_s":    DEFAULT_INTERVAL_SECONDS,
    }
