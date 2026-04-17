# Architecture

## Component map

```
Streamlit pages (UI layer)
    ↓
eligibility.py (wheel_eligible gate — upstream of screening)
screener.py (IV rank + timing filters)
state_machine.py (cycle transitions)
cost_basis.py (P&L calculations)
    ↓
SQLite wheel.db (schema in db/schema.sql)
    ↓
Tradier API (via tradier.py)
Alpha Vantage API (via market_data.py)
```

## Data flow: entering a trade

1. User selects ticker in Streamlit screener
2. `screener.py` filters candidates (IV rank ≥ 50%, no active positions, earnings check)
3. User confirms CSP parameters (strike, expiration, contracts)
4. UI calls `state_machine.open_short_put()`
5. `state_machine.py` validates inputs, creates `cycle` + `trade` rows
6. Optional: calls `tradier.py` to submit paper order to TRADIER_SANDBOX
7. Paper fill confirmed → `trade.broker_order_id` populated

## Data flow: roll

1. User triggers roll from position dashboard
2. UI calls `state_machine.roll_position(cycle_id, close_params, open_params)`
3. State machine writes two `trade` rows (`ROLL_CLOSE`, `ROLL_OPEN`) with shared `roll_group_id`
4. State machine writes one `roll_event` row for UI display
5. `cycle.total_premium` updated with net credit
6. `cycle.state` unchanged (still `SHORT_PUT` or `SHORT_CALL`)

## Data flow: assignment

1. Tradier MCP notifies assignment OR user enters manually
2. UI calls `state_machine.record_assignment(cycle_id, fill_price)`
3. State machine writes `BUY_STOCK / ASSIGNMENT` trade row
4. `cycle.assignment_price` set to fill price
5. `cycle.state` → `LONG_STOCK`
6. `cycle.cost_basis` auto-recomputes (VIRTUAL column)

## Key design decisions

- **SQLite over Postgres**: single-user, local, no infra to manage. Migrate later if needed.
- **VIRTUAL GENERATED cost_basis**: eliminates entire class of update bugs. Cannot be wrong.
- **State machine as single module**: any code that bypasses it is a bug by definition.
- **Paper-only Tradier integration**: `TRADIER_LIVE` source enum value reserved but not wired. Requires explicit flag to enable.
- **Manual entry path**: every Tradier action has a manual equivalent so real trades are always recordable.
- **Screening as pure filtering**: screener is stateless, returns candidates based on current market data + earnings.
- **Two-signal screening**: `wheel_eligible` (fundamental/technical quality, set manually) is a hard gate evaluated before IV rank. IV rank is a timing signal only, not a quality signal.
