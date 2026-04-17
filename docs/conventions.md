# Conventions

## Module responsibilities

| Module | Owns | Does not own |
|---|---|---|
| `src/state_machine.py` | All cycle state transitions, trade writes | UI, market data, screening |
| `src/cost_basis.py` | Cost basis validation, P&L helpers | State transitions |
| `src/db.py` | DB connection, raw query helpers, migrations | Business logic |
| `src/tradier.py` | Tradier REST API calls, order formatting | State decisions |
| `src/massive.py` | Massive.com REST API calls, market data for screening | IV rank, trade execution, DB writes |
| `src/market_data.py` | IV rank fetch + computation, caching | Trade logic, screening |
| `src/eligibility.py` | wheel_eligible flag, strategy assignment, quality gate | IV rank, trade execution, cycle state |
| `src/screener.py` | Equity screening criteria, candidate ranking | Execution, trade submission |
| `src/scanner.py` | Strategy criteria evaluation, scan universe | DB writes, eligibility flag, trade execution |
| `src/poller.py` | Background fill confirmation thread | UI, order placement |
| `pages/` | Streamlit UI only | Any computation |

## Naming

- Functions that write to DB: `create_`, `record_`, `update_` prefix
- Functions that only read: `get_`, `fetch_`, `list_` prefix
- State machine public API: verb-noun, e.g. `open_short_put`, `record_assignment`, `roll_position`
- Enums match DB CHECK constraints exactly: use the string literals, not magic values
- Screener functions: `get_screening_candidates()`, `has_earnings_soon()`, `get_all_watchlist()`

## Adding a new trade type

1. Add the value to the `trade_type` CHECK in `db/schema.sql`
2. Add the corresponding transition to the table in `docs/state-machine.md`
3. Implement the transition function in `src/state_machine.py`
4. Update `db/schema.sql` and `docs/data-model.md` in the same commit

## Tradier MCP calls

- All Tradier calls go through `src/tradier.py` — no MCP calls in pages or state machine
- Always check `cycle.source` before submitting — only `TRADIER_SANDBOX` and `TRADIER_LIVE` cycles submit to Tradier
- Every Tradier action has a manual fallback path (user enters fill manually if paper order fails)

## Testing expectations

- `src/state_machine.py` must have unit tests for every valid transition and every invalid transition (should raise `InvalidTransitionError`)
- `src/cost_basis.py` must have a test that walks the full worked example from `docs/cost-basis-rules.md` step by step
- `src/screener.py` should have unit tests for candidate filtering logic
- No Streamlit-dependent code in `src/` — pages are not unit tested

## DB migrations

- Schema changes: update `db/schema.sql`, write a migration script in `db/migrations/`
- Migration files are numbered sequentially: `001_add_earnings_date.sql`, `002_...sql`, etc.
- Migrations are run idempotently on database initialization (handled by `src/db.py`)
- Never ALTER a VIRTUAL GENERATED column — drop and recreate the table
- All migrations must be idempotent (safe to run multiple times)
