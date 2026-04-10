# State machine

All cycle state transitions are enforced in `src/state_machine.py`. No other module may UPDATE `cycle.state` directly.

## States

| State | Meaning |
|---|---|
| `SHORT_PUT` | CSP is open, monitoring |
| `LONG_STOCK` | Assigned; holding shares, no CC open |
| `SHORT_CALL` | CC is open against held shares |
| `CLOSED` | Cycle complete, no open positions |

`IDLE` is not a state — it is the absence of a cycle row.

## Valid transitions

| From | Event | To | Trades written |
|---|---|---|---|
| *(none)* | `open_short_put()` | `SHORT_PUT` | `SELL_PUT / OPEN` |
| `SHORT_PUT` | `roll_position()` | `SHORT_PUT` | `BUY_PUT / ROLL_CLOSE` + `SELL_PUT / ROLL_OPEN` |
| `SHORT_PUT` | `close_position()` | `CLOSED` | `BUY_PUT / CLOSE` |
| `SHORT_PUT` | `record_expiration()` | `CLOSED` | `BUY_PUT / EXPIRATION` (price=0) |
| `SHORT_PUT` | `record_assignment()` | `LONG_STOCK` | `BUY_STOCK / ASSIGNMENT` |
| `LONG_STOCK` | `open_short_call()` | `SHORT_CALL` | `SELL_CALL / OPEN` |
| `SHORT_CALL` | `roll_position()` | `SHORT_CALL` | `BUY_CALL / ROLL_CLOSE` + `SELL_CALL / ROLL_OPEN` |
| `SHORT_CALL` | `close_position()` | `LONG_STOCK` | `BUY_CALL / CLOSE` |
| `SHORT_CALL` | `record_expiration()` | `LONG_STOCK` | `BUY_CALL / EXPIRATION` (price=0) |
| `SHORT_CALL` | `record_called_away()` | `CLOSED` | `SELL_STOCK / CALLED_AWAY` |

Any transition not in this table is invalid. The state machine must raise `InvalidTransitionError`.

## Transition side effects

**`record_assignment(cycle_id, fill_price)`**
- Sets `cycle.assignment_price = fill_price`
- Sets `cycle.shares_held = contracts × 100`
- `cycle.cost_basis` recomputes automatically (VIRTUAL column)

**Any credit trade (SELL_PUT, SELL_CALL, ROLL_OPEN)**
- Adds `net_credit` to `cycle.total_premium`
- `cycle.cost_basis` recomputes automatically

**Any debit trade (BUY_PUT, BUY_CALL, ROLL_CLOSE)**
- Subtracts `abs(net_credit)` from `cycle.total_premium`
- This correctly reduces the total credit when closing at a debit

**`record_called_away(cycle_id, fill_price)`**
- Writes `SELL_STOCK / CALLED_AWAY` trade
- Computes `realized_pnl`: `(fill_price - cost_basis) × shares_held`
- Sets `cycle.state = CLOSED`, `cycle.closed_at = now()`
- Sets `cycle.shares_held = 0`

**`record_expiration()` (option expires worthless)**
- Writes synthetic close trade with `price_per_share = 0`, `net_credit = 0`
- Does NOT add to `total_premium` (zero credit)
- Transitions state per table above

## Roll mechanics

A roll is always two separate `trade` rows:

```python
roll_group_id = str(uuid.uuid4())

# Leg 1: close existing position (debit)
write_trade(trade_type='BUY_PUT', leg_role='ROLL_CLOSE',
            roll_group_id=roll_group_id, price_per_share=-close_price, ...)

# Leg 2: open new position (credit)
write_trade(trade_type='SELL_PUT', leg_role='ROLL_OPEN',
            roll_group_id=roll_group_id, price_per_share=open_price, ...)

# Write roll_event for UI display
write_roll_event(roll_group_id=roll_group_id, net_credit=open_credit - close_debit, ...)

# Update total_premium with net
update_total_premium(cycle_id, net_credit=(open_credit - close_debit))
```

`cycle.state` does not change on a roll.

## Error handling

- `InvalidTransitionError`: attempted transition not in the valid table
- `CycleNotFoundError`: cycle_id does not exist
- `CycleClosedError`: any mutation attempted on a CLOSED cycle
