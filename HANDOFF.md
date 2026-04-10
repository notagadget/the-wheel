# Handoff context — Wheel Trader

## Status
Repo is functional and test-passing (47/47). Ready for next development phase.

## What exists and works

### Core modules (src/)
- `db.py` — SQLite connection, schema init, WAL mode
- `state_machine.py` — all cycle transitions, optimistic Tradier fills
- `cost_basis.py` — IV/pnl queries, audit validation
- `tradier.py` — REST client: orders, options chain, IV history, quotes
- `market_data.py` — IVR + IV percentile computation and DB cache
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

1. **`src/screener.py`** — equity screening logic. Should query `underlying` table,
   filter by IVR threshold, flag earnings within DTE window, rank candidates.
   Key inputs: iv_rank_cached, iv_pct_cached, earnings date (not yet in schema).

2. **Earnings calendar** — `underlying` table has no earnings_date column yet.
   Add via migration. Source: Tradier GET /v1/markets/calendar or a manual field.

3. **`.streamlit/secrets.toml` template** — credentials setup for Tradier.
   Required keys: TRADIER_API_KEY, TRADIER_ACCOUNT_ID, TRADIER_ENV (sandbox|live).

4. **`pages/2_Screener.py` IV refresh button** — calls `market_data.refresh_iv_for_ticker()`
   or `refresh_all_watchlist()`. Currently watchlist shows cached values with no
   way to refresh from the UI.

5. **`src/poller.py` poller status** in Dashboard — `poller_status()` exists but
   `pages/1_Dashboard.py` doesn't call it yet. Wire it into the sidebar or dashboard.

6. **Bug fix in `pages/3_Position.py` line ~257**: CC cost basis preview has a
   division error. Replace:
   `new_basis = summary.cost_basis - (contracts * 100 * price / 100)`
   with:
   `new_basis = summary.cost_basis - price`
   (price is already per-share; the /100 double-divides)

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
