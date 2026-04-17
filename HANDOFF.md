# Handoff context — Wheel Trader

## Status
Repo is functional and test-passing (83/83). All P0 blockers resolved. Ready for live-trade guard rails (P2) as the last remaining feature work.

## What exists and works

### Core modules (src/)
- `db.py` — SQLite connection, schema init, WAL mode, migration runner
- `state_machine.py` — all cycle transitions, optimistic Tradier fills
- `cost_basis.py` — IV/pnl queries, audit validation
- `tradier.py` — REST client: orders, options chain, IV history, quotes
- `market_data.py` — IVR + IV percentile computation and DB cache
- `screener.py` — equity screening (IV rank filter, earnings window, ranking)
- `poller.py` — background thread, confirms/rejects PENDING trades every 30s

### UI (pages/)
- `1_Dashboard.py` — active positions, P&L metrics, pending fill alerts, poller status
- `2_Screener.py` — watchlist, add ticker, open CSP form
- `3_Position.py` — cycle drill-down, all action forms (assign/roll/close/expire/called-away)
- `4_Ledger.py` — unified trade log, filterable, CSV export

### DB
- `db/schema.sql` — canonical DDL, source of truth
- `db/migrations/001_tradier.sql` — run if migrating from an older DB with ALPACA_PAPER

### Infrastructure
- `requirements.txt` — all Python dependencies
- `.streamlit/secrets.toml.example` — credential template (copy to `.streamlit/secrets.toml`)
- `.gitignore` — protects secrets.toml and wheel.db from commit

## What is NOT built yet

1. **Real-trade integration** — currently all trades go to TRADIER_SANDBOX or MANUAL.
   Add TRADIER_LIVE source enum support with explicit opt-in guard rails (confirmation dialog, warning banner).

2. **`src/test_state_machine.py` is misplaced** — should be `tests/test_state_machine.py`.
   Tests are comprehensive; just needs to move.

## What was recently completed

✅ **Source enum fixed** — all pages now use `TRADIER_SANDBOX` / `TRADIER_LIVE` / `MANUAL`
   (was `ALPACA_PAPER`, which violated the DB CHECK constraint)  
✅ **`requirements.txt`** created  
✅ **`.streamlit/secrets.toml.example`** created with Tradier credential keys  
✅ **`.gitignore`** created (secrets.toml and wheel.db protected)  
✅ **Tests for tradier.py** — 25 tests (auth, orders, options chain, expirations, HV, quotes)  
✅ **Tests for market_data.py** — 15 tests (IV metrics, ATM selection, DB cache, watchlist refresh)  
✅ **Tests for screener.py** — 17 tests (earnings window, IV filter, active cycle exclusion)  
✅ **`src/screener.py`** — equity screening with IV rank filter, earnings flags, ranking  
✅ **Poller status** — wired into `pages/1_Dashboard.py`  

## Architecture rules (enforce strictly)

- `cycle.cost_basis` is VIRTUAL GENERATED — never UPDATE it directly
- All cycle state changes go through `src/state_machine.py` only
- No business logic in `pages/` — pages call `src/` functions only
- Rolls = two `trade` rows (ROLL_CLOSE + ROLL_OPEN) linked by `roll_group_id`
- Source enum: `TRADIER_SANDBOX` | `TRADIER_LIVE` | `MANUAL` (schema-enforced CHECK, no others)
- Schema changes require updating both `db/schema.sql` and `docs/data-model.md`

## Cost basis formula
```
cost_basis (per share) = assignment_price - (total_premium / 100)
```
total_premium is cumulative net credit in dollars for the entire cycle.
See `docs/cost-basis-rules.md` for the full worked example with numbers.

## Running the app
```bash
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
# edit secrets.toml with your Tradier credentials
streamlit run app.py
```

## Running tests
```bash
python -m pytest tests/ -v
```
