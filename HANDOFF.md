# Handoff context — Wheel Trader

## Status
Repo is functional and test-passing (28/28). Screener, earnings tracking, and IV refresh complete. Ready for real-trade integration.

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
- `1_Dashboard.py` — active positions, P&L metrics, pending fill alerts
- `2_Screener.py` — watchlist, add ticker, open CSP form
- `3_Position.py` — cycle drill-down, all action forms (assign/roll/close/expire/called-away)
- `4_Ledger.py` — unified trade log, filterable, CSV export

### DB
- `db/schema.sql` — canonical DDL, source of truth
- `db/migrations/001_tradier.sql` — run if migrating from an older DB with ALPACA_PAPER

## What is NOT built yet (priority order)

1. **`.streamlit/secrets.toml` template** — credentials setup for Tradier.
   Required keys: TRADIER_API_KEY, TRADIER_ACCOUNT_ID, TRADIER_ENV (sandbox|live).

2. **`src/poller.py` poller status** in Dashboard — `poller_status()` exists but
   `pages/1_Dashboard.py` doesn't call it yet. Wire it into the sidebar or dashboard.

3. **Real-trade integration** — currently all trades go to TRADIER_SANDBOX or MANUAL.
   Add TRADIER_LIVE source enum support with explicit opt-in guard rails.

## What was recently completed

✅ **`src/screener.py`** — equity screening with IV rank filter, earnings flags, ranking  
✅ **Earnings calendar** — earnings_date column added to underlying, with migration system  
✅ **IV refresh button** — "Refresh all IV" button wired in pages/2_Screener.py  
✅ **Bug fix** — CC cost basis preview calculation in pages/3_Position.py

## Architecture rules (enforce strictly)

- `cycle.cost_basis` is VIRTUAL GENERATED — never UPDATE it directly
- All cycle state changes go through `src/state_machine.py` only
- No business logic in `pages/` — pages call `src/` functions only
- Rolls = two `trade` rows (ROLL_CLOSE + ROLL_OPEN) linked by `roll_group_id`
- Source enum: TRADIER_SANDBOX | TRADIER_LIVE | MANUAL (no others)
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
streamlit run app.py
```

## Running tests
```bash
python -m pytest tests/ -v
```

## Tradier credentials (not committed)
Create `.streamlit/secrets.toml`:
```toml
TRADIER_API_KEY    = "your-key"
TRADIER_ACCOUNT_ID = "your-account-id"
TRADIER_ENV        = "sandbox"
```
