# Data model

Source of truth for schema is `db/schema.sql`. This doc explains the *why* behind each decision.

## Tables

### `underlying`

Tickers being watched or actively wheeled. One row per ticker, ever.

```sql
CREATE TABLE underlying (
    underlying_id      TEXT PRIMARY KEY,       -- same as ticker, e.g. "RKLB"
    ticker             TEXT UNIQUE NOT NULL,
    notes              TEXT,                   -- general free-text notes
    iv_rank_cached     REAL,                   -- last fetched IVR (0-100)
    iv_pct_cached      REAL,                   -- % of days in past year IV was below current
    iv_current         REAL,                   -- raw current IV (30-day)
    iv_52w_high        REAL,
    iv_52w_low         REAL,
    iv_updated         DATETIME,
    earnings_date      DATE,                   -- next earnings announcement (optional)
    wheel_eligible     INTEGER NOT NULL DEFAULT 0,  -- 1 = cleared for wheel trading
    last_reviewed      DATE,                   -- date eligibility was last set via eligibility.py
    created_at         DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

`underlying_id = ticker` is intentional — avoids a join in every query and there is no ambiguity.

**`earnings_date`** is optional. Used by screener to flag candidates with earnings within a configurable DTE window (default 7 days). Source: manual entry or Tradier earnings calendar API.

**`wheel_eligible`** is a hard quality gate set manually via `src/eligibility.py`. A ticker must have `wheel_eligible = 1` to appear in screening results. This is a separate signal from IV rank: eligibility answers "should I ever wheel this?", IV rank answers "should I wheel this right now?".

Which strategy frameworks justify eligibility is recorded in `underlying_strategy` (one row per strategy). Per-strategy rationale lives in `underlying_strategy.quality_notes`. Generic ticker notes — including the rationale for marking a ticker ineligible — live in `underlying.notes`.

**`last_reviewed`** is set automatically by `update_eligibility()` in `src/eligibility.py` — do not update it directly.

---

### `underlying_strategy`

Maps an underlying to one or more strategy frameworks that justify its eligibility. A ticker with two rows here has conviction=2.

```sql
CREATE TABLE underlying_strategy (
    underlying_id  TEXT NOT NULL REFERENCES underlying(underlying_id),
    strategy       TEXT NOT NULL CHECK(strategy IN ('FUNDAMENTAL','TECHNICAL','ETF_COMPONENT','VOL_PREMIUM')),
    quality_notes  TEXT,        -- per-strategy rationale (optional)
    added_date     DATE NOT NULL,
    PRIMARY KEY (underlying_id, strategy)
);
```

**`conviction`** is derived as `COUNT(*)` from this table for a given `underlying_id`. Range 1–4. Higher conviction = more strategy frameworks agree the ticker is wheelable.

When `eligibility.update_eligibility(eligible=False)` is called, all rows for that `underlying_id` are deleted here and `underlying.wheel_eligible` is set to 0. `remove_strategy()` removes a single row and auto-clears `wheel_eligible` if no rows remain.

---

### `cycle`

One Wheel attempt on one ticker. Unit of P&L measurement. A cycle begins at first CSP sale and ends at close/expiration/called-away.

```sql
CREATE TABLE cycle (
    cycle_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    underlying_id    TEXT NOT NULL REFERENCES underlying(underlying_id),
    state            TEXT NOT NULL DEFAULT 'SHORT_PUT'
                     CHECK(state IN ('SHORT_PUT','LONG_STOCK','SHORT_CALL','CLOSED')),
    lot_id           TEXT,               -- user label for concurrent cycles, e.g. 'RKLB-2025-01'
    shares_held      INTEGER DEFAULT 0,
    assignment_price REAL,               -- per-share price at stock assignment
    assignment_strike REAL,              -- the put strike (reference only)
    total_premium    REAL NOT NULL DEFAULT 0,  -- running sum of all net credits this cycle
    cost_basis       REAL GENERATED ALWAYS AS
                     (assignment_price - (total_premium / 100.0)) VIRTUAL,
    opened_at        DATETIME DEFAULT CURRENT_TIMESTAMP,
    closed_at        DATETIME,
    realized_pnl     REAL,               -- populated when state → CLOSED
    notes            TEXT,
    UNIQUE(underlying_id, lot_id)
);
```

**`cost_basis` is VIRTUAL** — it is always derived, never set. If `assignment_price` or `total_premium` change, it updates automatically. Do not attempt to UPDATE this column; SQLite will reject it.

**`total_premium` is per-cycle cumulative net credit in dollars** (not per-share). Formula: `cost_basis = assignment_price - (total_premium / 100)`. See `docs/cost-basis-rules.md` for a worked example.

**`lot_id`** supports concurrent cycles on the same ticker. If running one cycle at a time, leave null or auto-assign.

**`IDLE` is not a state** — it is the absence of a cycle row. The screener shows tickers from `underlying` with no active cycle as candidates.

---

### `trade`

Every individual leg. The immutable audit trail. Never delete rows; mark them with notes if erroneous.

```sql
CREATE TABLE trade (
    trade_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id        INTEGER NOT NULL REFERENCES cycle(cycle_id),
    underlying_id   TEXT NOT NULL REFERENCES underlying(underlying_id),
    trade_type      TEXT NOT NULL CHECK(trade_type IN (
                        'SELL_PUT','BUY_PUT',
                        'SELL_CALL','BUY_CALL',
                        'BUY_STOCK','SELL_STOCK'
                    )),
    leg_role        TEXT NOT NULL CHECK(leg_role IN (
                        'OPEN','CLOSE','ROLL_CLOSE','ROLL_OPEN',
                        'ASSIGNMENT','EXPIRATION','CALLED_AWAY'
                    )),
    roll_group_id   TEXT,               -- links ROLL_CLOSE + ROLL_OPEN as one logical roll
    expiration      DATE,               -- options only
    strike          REAL,               -- options only
    contracts       INTEGER NOT NULL DEFAULT 1,
    price_per_share REAL NOT NULL,      -- premium per share; negative = debit paid
    net_credit      REAL NOT NULL,      -- contracts × 100 × price_per_share
    commission      REAL NOT NULL DEFAULT 0,
    filled_at       DATETIME NOT NULL,
    source          TEXT NOT NULL CHECK(source IN ('TRADIER_SANDBOX','TRADIER_LIVE','MANUAL')),
    broker_order_id TEXT,               -- Tradier order ID if applicable
    fill_status     TEXT NOT NULL DEFAULT 'CONFIRMED'
                    CHECK(fill_status IN ('PENDING','CONFIRMED','REJECTED')),
    notes           TEXT
);
```

**`net_credit` sign convention**: positive = credit received, negative = debit paid. A `SELL_PUT` has positive `net_credit`. A `BUY_PUT` (to close) has negative.

**Rolls are always two rows**: `ROLL_CLOSE` (negative net_credit, buying back) + `ROLL_OPEN` (positive net_credit, selling new). Link them with the same `roll_group_id` UUID. The sum of both is the net credit of the roll.

**`EXPIRATION` leg_role**: when an option expires worthless, record a synthetic close with `price_per_share = 0`, `net_credit = 0`. This keeps the trade log complete.

---

### `roll_event`

One row per roll, for display purposes. Derived from the two `trade` rows but stored for query convenience.

```sql
CREATE TABLE roll_event (
    roll_group_id   TEXT PRIMARY KEY,
    cycle_id        INTEGER NOT NULL REFERENCES cycle(cycle_id),
    old_expiration  DATE,
    new_expiration  DATE,
    old_strike      REAL,
    new_strike      REAL,
    net_credit      REAL,               -- net of ROLL_CLOSE + ROLL_OPEN
    rolled_at       DATETIME,
    notes           TEXT
);
```

---

### `cycle_summary` (view)

```sql
CREATE VIEW cycle_summary AS
SELECT
    c.cycle_id,
    c.underlying_id,
    c.lot_id,
    c.state,
    c.assignment_price,
    c.total_premium,
    c.cost_basis,
    c.shares_held,
    c.opened_at,
    c.closed_at,
    c.realized_pnl,
    COUNT(t.trade_id)                                        AS trade_count,
    SUM(CASE WHEN t.leg_role IN ('ROLL_CLOSE','ROLL_OPEN')
        THEN 1 ELSE 0 END) / 2                              AS roll_count,
    SUM(t.net_credit) - SUM(COALESCE(t.commission, 0))      AS net_pnl_to_date
FROM cycle c
LEFT JOIN trade t ON t.cycle_id = c.cycle_id
GROUP BY c.cycle_id;
```

## Indexes

```sql
CREATE INDEX idx_trade_cycle    ON trade(cycle_id);
CREATE INDEX idx_trade_filled   ON trade(filled_at);
CREATE INDEX idx_trade_fill_status ON trade(fill_status);
CREATE INDEX idx_cycle_ticker   ON cycle(underlying_id, state);
```

## Migrations

Schema changes are managed in `db/migrations/`. Each migration file is numbered and run idempotently on database initialization.

- `001_add_earnings_date.sql` — Adds `earnings_date` column to `underlying` for earnings tracking.
- `003_add_wheel_eligibility.sql` — Adds `wheel_eligible`, `eligible_strategy`, `quality_notes`, `last_reviewed` to `underlying`.
- `004_underlying_strategies.sql` — Creates `underlying_strategy` join table for multi-strategy eligibility; migrates existing single-strategy rows.
- `005_drop_underlying_backcompat.sql` — Drops `underlying.eligible_strategy` and `underlying.quality_notes`. Merges any remaining ineligible-ticker notes into `underlying.notes`. Canonical strategy store is `underlying_strategy`.
