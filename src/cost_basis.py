"""
cost_basis.py — cost basis validation and P&L reporting helpers.

This module READS from the DB. It does not write.
All cost basis arithmetic derives from cycle.total_premium and
cycle.assignment_price — never computed independently here.

See docs/cost-basis-rules.md for the authoritative formula and worked example.
"""

from dataclasses import dataclass
from typing import Optional

from src.db import get_conn


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class CycleSummary:
    cycle_id: int
    underlying_id: str
    lot_id: Optional[str]
    state: str
    assignment_price: Optional[float]
    total_premium: float          # cumulative net credit, dollars
    cost_basis: Optional[float]   # per share; None until assigned
    shares_held: int
    opened_at: str
    closed_at: Optional[str]
    realized_pnl: Optional[float]
    trade_count: int
    roll_count: int
    net_pnl_to_date: float        # sum of all net_credits minus commissions


@dataclass
class PositionPnL:
    cycle_id: int
    underlying_id: str
    cost_basis: float             # per share
    shares_held: int
    current_price: float          # caller supplies; not fetched here
    unrealized_pnl: float         # (current_price - cost_basis) * shares_held
    premium_collected: float      # total_premium dollars
    total_pnl: float              # unrealized + premium (pre-assignment premium already in cost_basis)


@dataclass
class CycleAudit:
    """
    Full audit of a cycle's premium accounting.
    Used to validate that total_premium matches sum of trade legs.
    """
    cycle_id: int
    db_total_premium: float       # what cycle.total_premium says
    computed_total_premium: float # sum of trade net_credits (credits only, debits reverse)
    match: bool
    delta: float                  # db - computed; nonzero = data integrity issue
    trades: list                  # raw trade rows for inspection


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def get_cycle_summary(cycle_id: int) -> CycleSummary:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM cycle_summary WHERE cycle_id = ?", (cycle_id,)
        ).fetchone()
    if row is None:
        raise ValueError(f"cycle_id {cycle_id} not found")
    return _row_to_summary(row)


def list_active_cycles() -> list[CycleSummary]:
    """All cycles not in CLOSED state, ordered by opened_at desc."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM cycle_summary WHERE state != 'CLOSED' "
            "ORDER BY opened_at DESC"
        ).fetchall()
    return [_row_to_summary(r) for r in rows]


def list_cycles_for_underlying(underlying_id: str) -> list[CycleSummary]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM cycle_summary WHERE underlying_id = ? "
            "ORDER BY opened_at DESC",
            (underlying_id,)
        ).fetchall()
    return [_row_to_summary(r) for r in rows]


def get_position_pnl(cycle_id: int, current_price: float) -> PositionPnL:
    """
    Compute unrealized P&L for an assigned position given a current market price.
    Caller is responsible for fetching current_price from market_data.py.
    Only valid when cycle is in LONG_STOCK or SHORT_CALL state.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT state, cost_basis, shares_held, total_premium "
            "FROM cycle WHERE cycle_id = ?",
            (cycle_id,)
        ).fetchone()

    if row is None:
        raise ValueError(f"cycle_id {cycle_id} not found")
    if row["state"] not in ("LONG_STOCK", "SHORT_CALL"):
        raise ValueError(
            f"get_position_pnl requires LONG_STOCK or SHORT_CALL, "
            f"got {row['state']!r}"
        )
    if row["cost_basis"] is None:
        raise ValueError(f"cycle {cycle_id} has no cost_basis (not yet assigned)")

    cost_basis = row["cost_basis"]
    shares = row["shares_held"]
    unrealized = (current_price - cost_basis) * shares

    return PositionPnL(
        cycle_id=cycle_id,
        underlying_id=_get_underlying(cycle_id),
        cost_basis=cost_basis,
        shares_held=shares,
        current_price=current_price,
        unrealized_pnl=unrealized,
        premium_collected=row["total_premium"],
        total_pnl=unrealized,  # premium already baked into cost_basis
    )


def get_realized_pnl_summary(underlying_id: Optional[str] = None) -> dict:
    """
    Aggregate realized P&L across closed cycles.
    Optionally filter by underlying.
    Returns {"total_realized": float, "cycle_count": int, "avg_per_cycle": float}.
    """
    with get_conn() as conn:
        if underlying_id:
            row = conn.execute(
                "SELECT SUM(realized_pnl) as total, COUNT(*) as n "
                "FROM cycle WHERE state='CLOSED' AND underlying_id=?",
                (underlying_id,)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT SUM(realized_pnl) as total, COUNT(*) as n "
                "FROM cycle WHERE state='CLOSED'"
            ).fetchone()

    total = row["total"] or 0.0
    n = row["n"] or 0
    return {
        "total_realized": total,
        "cycle_count": n,
        "avg_per_cycle": total / n if n > 0 else 0.0,
    }


# ---------------------------------------------------------------------------
# Audit / validation
# ---------------------------------------------------------------------------

def audit_cycle(cycle_id: int) -> CycleAudit:
    """
    Verify that cycle.total_premium matches the sum of its trade legs.

    The formula: total_premium should equal the sum of net_credit across
    all CONFIRMED trades, where credits are positive and debits are negative.
    PENDING and REJECTED trades are excluded (they haven't settled).

    A nonzero delta indicates a data integrity issue — likely a direct
    UPDATE to total_premium that bypassed the state machine.
    """
    with get_conn() as conn:
        cycle_row = conn.execute(
            "SELECT total_premium FROM cycle WHERE cycle_id=?", (cycle_id,)
        ).fetchone()
        if cycle_row is None:
            raise ValueError(f"cycle_id {cycle_id} not found")

        trades = conn.execute(
            "SELECT trade_id, trade_type, leg_role, net_credit, fill_status "
            "FROM trade WHERE cycle_id=? ORDER BY filled_at",
            (cycle_id,)
        ).fetchall()

    db_total = cycle_row["total_premium"]

    # Only count settled trades; exclude stock legs (not premium)
    option_and_premium_roles = {
        "OPEN", "CLOSE", "ROLL_CLOSE", "ROLL_OPEN", "EXPIRATION"
    }
    computed = sum(
        t["net_credit"]
        for t in trades
        if t["fill_status"] == "CONFIRMED"
        and t["leg_role"] in option_and_premium_roles
    )

    delta = round(db_total - computed, 6)

    return CycleAudit(
        cycle_id=cycle_id,
        db_total_premium=db_total,
        computed_total_premium=computed,
        match=abs(delta) < 0.001,
        delta=delta,
        trades=[dict(t) for t in trades],
    )


def audit_all_active() -> list[CycleAudit]:
    """Run audit_cycle on every non-CLOSED cycle. Returns only those with mismatches."""
    with get_conn() as conn:
        ids = [
            r["cycle_id"] for r in conn.execute(
                "SELECT cycle_id FROM cycle WHERE state != 'CLOSED'"
            ).fetchall()
        ]
    return [a for cid in ids if not (a := audit_cycle(cid)).match]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _row_to_summary(row) -> CycleSummary:
    r = dict(row)
    return CycleSummary(
        cycle_id=r["cycle_id"],
        underlying_id=r["underlying_id"],
        lot_id=r.get("lot_id"),
        state=r["state"],
        assignment_price=r.get("assignment_price"),
        total_premium=r["total_premium"],
        cost_basis=r.get("cost_basis"),
        shares_held=r.get("shares_held", 0),
        opened_at=r["opened_at"],
        closed_at=r.get("closed_at"),
        realized_pnl=r.get("realized_pnl"),
        trade_count=r.get("trade_count", 0),
        roll_count=r.get("roll_count", 0),
        net_pnl_to_date=r.get("net_pnl_to_date") or 0.0,
    )


def _get_underlying(cycle_id: int) -> str:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT underlying_id FROM cycle WHERE cycle_id=?", (cycle_id,)
        ).fetchone()
    return row["underlying_id"] if row else ""
