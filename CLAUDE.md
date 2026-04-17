# Wheel Trader

A Python/Streamlit app for screening equities for the Wheel options strategy, tracking positions, and executing trades via Tradier (sandbox and live).

## Stack

- **UI**: Streamlit
- **DB**: SQLite (single file, `db/wheel.db`)
- **State/logic**: Pure Python modules in `src/`
- **Broker integration**: Tradier REST API (`src/tradier.py`) — sandbox by default, live gated
- **Market data**: Tradier options chain + IV history
- **Schema source of truth**: `db/schema.sql`

## Before writing any code, read:

| Task | Read first |
|---|---|
| Any DB change | `docs/data-model.md` |
| Any trade/position logic | `docs/state-machine.md` + `docs/cost-basis-rules.md` |
| New feature / module | `docs/architecture.md` + `docs/conventions.md` |
| Cost basis calculation | `docs/cost-basis-rules.md` — do not improvise this |

## Hard constraints

1. **Never write directly to `cycle.cost_basis`** — it is a SQLite VIRTUAL GENERATED column derived from `assignment_price` and `total_premium`. Update `total_premium` instead.
2. **All state transitions go through `src/state_machine.py`** — no raw SQL UPDATE on `cycle.state` anywhere else.
3. **Rolls are stored as two `trade` rows** (`ROLL_CLOSE` + `ROLL_OPEN`) linked by `roll_group_id` — never as one row.
4. **Source enum is strict**: `TRADIER_SANDBOX`, `TRADIER_LIVE`, or `MANUAL` only — enforced by DB CHECK constraint. Never use `ALPACA_PAPER` (stale value).
5. **No business logic in Streamlit pages** — pages call `src/` functions, they do not compute state or P&L directly.
6. **Schema changes require updating both `db/schema.sql` and `docs/data-model.md`** in the same commit.

## Repo layout

```
db/             schema.sql + SQLite file (not committed)
docs/           architecture, data model, state machine, cost basis, conventions
src/
  db.py         DB connection + helpers
  state_machine.py  sole authority on cycle state transitions
  cost_basis.py     cost basis calculation and validation
  tradier.py    Tradier REST client (sandbox + live)
  market_data.py    IV computation and refresh logic (via Tradier)
  screener.py   equity screening logic
pages/          Streamlit pages (UI only, no business logic)
tests/          pytest unit tests for src/ modules
```
