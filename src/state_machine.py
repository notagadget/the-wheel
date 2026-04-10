"""
state_machine.py — sole authority on cycle state transitions.

Rules:
- All DB writes that change cycle.state go through this module only.
- cycle.cost_basis is VIRTUAL — never set it directly.
- total_premium is updated here after every credit/debit leg.
- Optimistic fills: DB record written immediately with fill_status=PENDING
  for TRADIER_SANDBOX/TRADIER_LIVE sources. confirm_fill() / reject_fill()
  called by poller (src/poller.py) when Tradier confirms or rejects.
- MANUAL source: DB record written with fill_status=CONFIRMED immediately.

See docs/state-machine.md for the full transition table.
See docs/cost-basis-rules.md for total_premium accounting.
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

from src.db import get_conn
from src.tradier import submit_option_order, current_source, TradierError


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class WheelError(Exception):
    pass

class InvalidTransitionError(WheelError):
    pass

class CycleNotFoundError(WheelError):
    pass

class CycleClosedError(WheelError):
    pass


TRADIER_SOURCES = {"TRADIER_SANDBOX", "TRADIER_LIVE"}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_cycle(conn, cycle_id: int) -> dict:
    row = conn.execute(
        "SELECT * FROM cycle WHERE cycle_id = ?", (cycle_id,)
    ).fetchone()
    if row is None:
        raise CycleNotFoundError(f"cycle_id {cycle_id} not found")
    return dict(row)


def _assert_state(cycle: dict, expected: str) -> None:
    if cycle["state"] == "CLOSED":
        raise CycleClosedError(f"cycle {cycle['cycle_id']} is already CLOSED")
    if cycle["state"] != expected:
        raise InvalidTransitionError(
            f"cycle {cycle['cycle_id']} is in state {cycle['state']!r}, "
            f"expected {expected!r}"
        )


def _fill_status(source: str) -> str:
    return "PENDING" if source in TRADIER_SOURCES else "CONFIRMED"


def _write_trade(conn, *, cycle_id, underlying_id, trade_type, leg_role,
                 contracts, price_per_share, net_credit, filled_at,
                 source, commission=0.0, expiration=None, strike=None,
                 roll_group_id=None, broker_order_id=None,
                 fill_status="CONFIRMED", notes=None) -> int:
    cur = conn.execute("""
        INSERT INTO trade (
            cycle_id, underlying_id, trade_type, leg_role,
            roll_group_id, expiration, strike, contracts,
            price_per_share, net_credit, commission,
            filled_at, source, broker_order_id, fill_status, notes
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        cycle_id, underlying_id, trade_type, leg_role,
        roll_group_id, expiration, strike, contracts,
        price_per_share, net_credit, commission,
        filled_at, source, broker_order_id, fill_status, notes,
    ))
    return cur.lastrowid


def _update_total_premium(conn, cycle_id: int, delta: float) -> None:
    conn.execute(
        "UPDATE cycle SET total_premium = total_premium + ? WHERE cycle_id = ?",
        (delta, cycle_id)
    )


def _set_cycle_state(conn, cycle_id: int, state: str,
                     extra_fields: Optional[dict] = None) -> None:
    fields = {"state": state}
    if extra_fields:
        fields.update(extra_fields)
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE cycle SET {set_clause} WHERE cycle_id = ?",
        (*fields.values(), cycle_id)
    )


def _try_submit(symbol, side, option_symbol, contracts, price, source) -> Optional[str]:
    """Submit to Tradier; return order_id or None on error (optimistic)."""
    if source not in TRADIER_SOURCES:
        return None
    try:
        return submit_option_order(
            symbol=symbol,
            option_symbol=option_symbol or "",
            side=side,
            quantity=contracts,
            order_type="limit",
            price=price,
        )
    except TradierError:
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def open_short_put(
    *,
    underlying_id: str,
    strike: float,
    expiration: str,
    contracts: int,
    price_per_share: float,
    source: str,
    commission: float = 0.0,
    option_symbol: Optional[str] = None,
    lot_id: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict:
    """
    Start a new Wheel cycle by selling a cash-secured put.
    Returns {"cycle_id": int, "trade_id": int}.
    """
    net_credit = contracts * 100 * price_per_share
    filled_at = _now()

    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO underlying (underlying_id, ticker) VALUES (?,?)",
            (underlying_id, underlying_id)
        )
        cur = conn.execute("""
            INSERT INTO cycle (underlying_id, state, lot_id, total_premium, opened_at)
            VALUES (?, 'SHORT_PUT', ?, ?, ?)
        """, (underlying_id, lot_id, net_credit, filled_at))
        cycle_id = cur.lastrowid

        broker_order_id = _try_submit(
            underlying_id, "sell_to_open", option_symbol, contracts, price_per_share, source
        )

        trade_id = _write_trade(
            conn,
            cycle_id=cycle_id, underlying_id=underlying_id,
            trade_type="SELL_PUT", leg_role="OPEN",
            contracts=contracts, price_per_share=price_per_share,
            net_credit=net_credit, filled_at=filled_at, source=source,
            commission=commission, expiration=expiration, strike=strike,
            broker_order_id=broker_order_id,
            fill_status=_fill_status(source), notes=notes,
        )

    return {"cycle_id": cycle_id, "trade_id": trade_id}


def record_assignment(
    *,
    cycle_id: int,
    fill_price: float,
    source: str,
    commission: float = 0.0,
    notes: Optional[str] = None,
) -> dict:
    """
    Record stock assignment. Transitions SHORT_PUT → LONG_STOCK.
    Returns {"trade_id": int}.
    """
    with get_conn() as conn:
        cycle = _get_cycle(conn, cycle_id)
        _assert_state(cycle, "SHORT_PUT")

        contracts = conn.execute(
            "SELECT contracts FROM trade WHERE cycle_id=? AND leg_role='OPEN' "
            "ORDER BY filled_at LIMIT 1",
            (cycle_id,)
        ).fetchone()["contracts"]
        shares_held = contracts * 100

        trade_id = _write_trade(
            conn,
            cycle_id=cycle_id, underlying_id=cycle["underlying_id"],
            trade_type="BUY_STOCK", leg_role="ASSIGNMENT",
            contracts=contracts, price_per_share=fill_price,
            net_credit=-(fill_price * shares_held),
            filled_at=_now(), source=source, commission=commission,
            fill_status="CONFIRMED", notes=notes,
        )

        _set_cycle_state(conn, cycle_id, "LONG_STOCK", {
            "assignment_price": fill_price,
            "shares_held":      shares_held,
        })

    return {"trade_id": trade_id}


def open_short_call(
    *,
    cycle_id: int,
    strike: float,
    expiration: str,
    contracts: int,
    price_per_share: float,
    source: str,
    commission: float = 0.0,
    option_symbol: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict:
    """
    Sell a covered call. Transitions LONG_STOCK → SHORT_CALL.
    Returns {"trade_id": int}.
    """
    net_credit = contracts * 100 * price_per_share

    with get_conn() as conn:
        cycle = _get_cycle(conn, cycle_id)
        _assert_state(cycle, "LONG_STOCK")

        broker_order_id = _try_submit(
            cycle["underlying_id"], "sell_to_open", option_symbol, contracts, price_per_share, source
        )

        trade_id = _write_trade(
            conn,
            cycle_id=cycle_id, underlying_id=cycle["underlying_id"],
            trade_type="SELL_CALL", leg_role="OPEN",
            contracts=contracts, price_per_share=price_per_share,
            net_credit=net_credit, filled_at=_now(), source=source,
            commission=commission, expiration=expiration, strike=strike,
            broker_order_id=broker_order_id,
            fill_status=_fill_status(source), notes=notes,
        )

        _update_total_premium(conn, cycle_id, net_credit)
        _set_cycle_state(conn, cycle_id, "SHORT_CALL")

    return {"trade_id": trade_id}


def roll_position(
    *,
    cycle_id: int,
    close_price_per_share: float,
    open_strike: float,
    open_expiration: str,
    open_price_per_share: float,
    contracts: int,
    source: str,
    close_commission: float = 0.0,
    open_commission: float = 0.0,
    close_option_symbol: Optional[str] = None,
    open_option_symbol: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict:
    """
    Roll an open option. Writes ROLL_CLOSE + ROLL_OPEN + roll_event.
    State unchanged. Returns {"roll_group_id", "close_trade_id", "open_trade_id"}.
    """
    with get_conn() as conn:
        cycle = _get_cycle(conn, cycle_id)
        if cycle["state"] not in ("SHORT_PUT", "SHORT_CALL"):
            raise InvalidTransitionError(
                f"roll_position requires SHORT_PUT or SHORT_CALL, got {cycle['state']!r}"
            )

        is_put    = cycle["state"] == "SHORT_PUT"
        close_type = "BUY_PUT"  if is_put else "BUY_CALL"
        open_type  = "SELL_PUT" if is_put else "SELL_CALL"

        current_leg = conn.execute(
            "SELECT strike, expiration FROM trade "
            "WHERE cycle_id=? AND leg_role IN ('OPEN','ROLL_OPEN') "
            "ORDER BY filled_at DESC LIMIT 1",
            (cycle_id,)
        ).fetchone()
        old_strike     = current_leg["strike"]     if current_leg else None
        old_expiration = current_leg["expiration"] if current_leg else None

        roll_group_id = str(uuid.uuid4())
        filled_at     = _now()
        fs            = _fill_status(source)

        _try_submit(cycle["underlying_id"], "buy_to_close",  close_option_symbol, contracts, close_price_per_share, source)
        _try_submit(cycle["underlying_id"], "sell_to_open",  open_option_symbol,  contracts, open_price_per_share,  source)

        close_net = -(contracts * 100 * close_price_per_share)
        close_trade_id = _write_trade(
            conn,
            cycle_id=cycle_id, underlying_id=cycle["underlying_id"],
            trade_type=close_type, leg_role="ROLL_CLOSE",
            roll_group_id=roll_group_id, contracts=contracts,
            price_per_share=-close_price_per_share, net_credit=close_net,
            filled_at=filled_at, source=source, commission=close_commission,
            expiration=old_expiration, strike=old_strike,
            fill_status=fs, notes=notes,
        )

        open_net = contracts * 100 * open_price_per_share
        open_trade_id = _write_trade(
            conn,
            cycle_id=cycle_id, underlying_id=cycle["underlying_id"],
            trade_type=open_type, leg_role="ROLL_OPEN",
            roll_group_id=roll_group_id, contracts=contracts,
            price_per_share=open_price_per_share, net_credit=open_net,
            filled_at=filled_at, source=source, commission=open_commission,
            expiration=open_expiration, strike=open_strike,
            fill_status=fs, notes=notes,
        )

        net_roll = open_net + close_net
        _update_total_premium(conn, cycle_id, net_roll)

        conn.execute("""
            INSERT INTO roll_event
                (roll_group_id, cycle_id, old_expiration, new_expiration,
                 old_strike, new_strike, net_credit, rolled_at, notes)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (roll_group_id, cycle_id, old_expiration, open_expiration,
              old_strike, open_strike, net_roll, filled_at, notes))

    return {
        "roll_group_id":   roll_group_id,
        "close_trade_id":  close_trade_id,
        "open_trade_id":   open_trade_id,
    }


def record_expiration(*, cycle_id: int, notes: Optional[str] = None) -> dict:
    """
    Record option expiring worthless. No premium change.
    SHORT_PUT → CLOSED, SHORT_CALL → LONG_STOCK.
    Returns {"trade_id": int, "new_state": str}.
    """
    with get_conn() as conn:
        cycle = _get_cycle(conn, cycle_id)
        if cycle["state"] not in ("SHORT_PUT", "SHORT_CALL"):
            raise InvalidTransitionError(
                f"record_expiration requires SHORT_PUT or SHORT_CALL, got {cycle['state']!r}"
            )

        is_put     = cycle["state"] == "SHORT_PUT"
        new_state  = "CLOSED" if is_put else "LONG_STOCK"
        expire_type = "BUY_PUT" if is_put else "BUY_CALL"

        current_leg = conn.execute(
            "SELECT contracts, strike, expiration FROM trade "
            "WHERE cycle_id=? AND leg_role IN ('OPEN','ROLL_OPEN') "
            "ORDER BY filled_at DESC LIMIT 1",
            (cycle_id,)
        ).fetchone()

        trade_id = _write_trade(
            conn,
            cycle_id=cycle_id, underlying_id=cycle["underlying_id"],
            trade_type=expire_type, leg_role="EXPIRATION",
            contracts=current_leg["contracts"] if current_leg else 1,
            price_per_share=0.0, net_credit=0.0,
            filled_at=_now(), source="MANUAL",
            strike=current_leg["strike"] if current_leg else None,
            expiration=current_leg["expiration"] if current_leg else None,
            fill_status="CONFIRMED", notes=notes,
        )

        extra = {"closed_at": _now(), "realized_pnl": 0.0} if new_state == "CLOSED" else {}
        _set_cycle_state(conn, cycle_id, new_state, extra or None)

    return {"trade_id": trade_id, "new_state": new_state}


def close_position(
    *,
    cycle_id: int,
    price_per_share: float,
    source: str,
    commission: float = 0.0,
    option_symbol: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict:
    """
    Close an open option early (buy to close).
    SHORT_PUT → CLOSED, SHORT_CALL → LONG_STOCK.
    Returns {"trade_id": int, "new_state": str}.
    """
    with get_conn() as conn:
        cycle = _get_cycle(conn, cycle_id)
        if cycle["state"] not in ("SHORT_PUT", "SHORT_CALL"):
            raise InvalidTransitionError(
                f"close_position requires SHORT_PUT or SHORT_CALL, got {cycle['state']!r}"
            )

        is_put     = cycle["state"] == "SHORT_PUT"
        close_type = "BUY_PUT" if is_put else "BUY_CALL"
        new_state  = "CLOSED"    if is_put else "LONG_STOCK"

        current_leg = conn.execute(
            "SELECT contracts, strike, expiration FROM trade "
            "WHERE cycle_id=? AND leg_role IN ('OPEN','ROLL_OPEN') "
            "ORDER BY filled_at DESC LIMIT 1",
            (cycle_id,)
        ).fetchone()
        contracts  = current_leg["contracts"] if current_leg else 1
        net_credit = -(contracts * 100 * price_per_share)

        broker_order_id = _try_submit(
            cycle["underlying_id"], "buy_to_close", option_symbol, contracts, price_per_share, source
        )

        trade_id = _write_trade(
            conn,
            cycle_id=cycle_id, underlying_id=cycle["underlying_id"],
            trade_type=close_type, leg_role="CLOSE",
            contracts=contracts, price_per_share=-price_per_share,
            net_credit=net_credit, filled_at=_now(), source=source,
            commission=commission,
            strike=current_leg["strike"] if current_leg else None,
            expiration=current_leg["expiration"] if current_leg else None,
            broker_order_id=broker_order_id,
            fill_status=_fill_status(source), notes=notes,
        )

        _update_total_premium(conn, cycle_id, net_credit)
        extra = {"closed_at": _now()} if new_state == "CLOSED" else {}
        _set_cycle_state(conn, cycle_id, new_state, extra or None)

    return {"trade_id": trade_id, "new_state": new_state}


def record_called_away(
    *,
    cycle_id: int,
    fill_price: float,
    source: str,
    commission: float = 0.0,
    notes: Optional[str] = None,
) -> dict:
    """
    Record stock called away. Computes realized_pnl.
    SHORT_CALL → CLOSED.
    Returns {"trade_id": int, "realized_pnl": float}.
    """
    with get_conn() as conn:
        cycle = _get_cycle(conn, cycle_id)
        _assert_state(cycle, "SHORT_CALL")

        shares     = cycle["shares_held"]
        cost_basis = conn.execute(
            "SELECT cost_basis FROM cycle WHERE cycle_id=?", (cycle_id,)
        ).fetchone()["cost_basis"]

        realized_pnl = (fill_price - cost_basis) * shares - commission

        trade_id = _write_trade(
            conn,
            cycle_id=cycle_id, underlying_id=cycle["underlying_id"],
            trade_type="SELL_STOCK", leg_role="CALLED_AWAY",
            contracts=shares // 100, price_per_share=fill_price,
            net_credit=fill_price * shares,
            filled_at=_now(), source=source, commission=commission,
            fill_status="CONFIRMED", notes=notes,
        )

        _set_cycle_state(conn, cycle_id, "CLOSED", {
            "closed_at":   _now(),
            "realized_pnl": realized_pnl,
            "shares_held":  0,
        })

    return {"trade_id": trade_id, "realized_pnl": realized_pnl}


def confirm_fill(
    *,
    trade_id: int,
    broker_order_id: str,
    actual_price_per_share: Optional[float] = None,
) -> None:
    """Called by async poller. Updates fill_status → CONFIRMED, corrects price if needed."""
    with get_conn() as conn:
        trade = conn.execute(
            "SELECT * FROM trade WHERE trade_id=?", (trade_id,)
        ).fetchone()
        if trade is None:
            raise CycleNotFoundError(f"trade_id {trade_id} not found")

        updates: dict = {"broker_order_id": broker_order_id, "fill_status": "CONFIRMED"}

        if actual_price_per_share is not None:
            old_net = trade["net_credit"]
            sign    = 1 if trade["price_per_share"] >= 0 else -1
            new_net = sign * trade["contracts"] * 100 * actual_price_per_share
            updates["price_per_share"] = sign * actual_price_per_share
            updates["net_credit"]      = new_net
            _update_total_premium(conn, trade["cycle_id"], new_net - old_net)

        set_clause = ", ".join(f"{k}=?" for k in updates)
        conn.execute(
            f"UPDATE trade SET {set_clause} WHERE trade_id=?",
            (*updates.values(), trade_id)
        )


def reject_fill(*, trade_id: int, notes: Optional[str] = None) -> None:
    """Called by async poller on rejection. Reverses optimistic premium update."""
    with get_conn() as conn:
        trade = conn.execute(
            "SELECT * FROM trade WHERE trade_id=?", (trade_id,)
        ).fetchone()
        if trade is None:
            raise CycleNotFoundError(f"trade_id {trade_id} not found")

        _update_total_premium(conn, trade["cycle_id"], -trade["net_credit"])
        conn.execute(
            "UPDATE trade SET fill_status='REJECTED', notes=? WHERE trade_id=?",
            (notes, trade_id)
        )
