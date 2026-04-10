-- Migration 001: Switch from Alpaca to Tradier, expand IV fields.
--
-- SQLite cannot ALTER a CHECK constraint directly.
-- Strategy: rename trade → trade_old, recreate with new CHECK, copy data, drop old.
-- underlying gets new columns via ALTER (additive only, safe).
--
-- Run once against an existing wheel.db:
--   sqlite3 db/wheel.db < db/migrations/001_tradier.sql

BEGIN;

-- 1. Expand underlying with new IV columns (additive, safe)
ALTER TABLE underlying ADD COLUMN iv_pct_cached  REAL;
ALTER TABLE underlying ADD COLUMN iv_current     REAL;
ALTER TABLE underlying ADD COLUMN iv_52w_high    REAL;
ALTER TABLE underlying ADD COLUMN iv_52w_low     REAL;
ALTER TABLE underlying ADD COLUMN iv_updated     DATETIME;
-- iv_rank_updated kept for now; iv_updated is the new canonical column.
-- Remove iv_rank_updated manually after confirming iv_updated is populated.

-- 2. Recreate trade table with updated CHECK constraints
ALTER TABLE trade RENAME TO trade_old;

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
    roll_group_id   TEXT,
    expiration      DATE,
    strike          REAL,
    contracts       INTEGER NOT NULL DEFAULT 1,
    price_per_share REAL NOT NULL,
    net_credit      REAL NOT NULL,
    commission      REAL NOT NULL DEFAULT 0,
    filled_at       DATETIME NOT NULL,
    source          TEXT NOT NULL CHECK(source IN (
                        'TRADIER_SANDBOX','TRADIER_LIVE','MANUAL'
                    )),
    broker_order_id TEXT,
    fill_status     TEXT NOT NULL DEFAULT 'CONFIRMED'
                    CHECK(fill_status IN ('PENDING','CONFIRMED','REJECTED')),
    notes           TEXT
);

-- 3. Copy existing data; remap ALPACA_PAPER → TRADIER_SANDBOX
INSERT INTO trade SELECT
    trade_id, cycle_id, underlying_id, trade_type, leg_role,
    roll_group_id, expiration, strike, contracts,
    price_per_share, net_credit, commission, filled_at,
    CASE source
        WHEN 'ALPACA_PAPER' THEN 'TRADIER_SANDBOX'
        ELSE source
    END,
    broker_order_id,
    CASE fill_status
        WHEN 'PENDING' THEN 'PENDING'
        WHEN 'REJECTED' THEN 'REJECTED'
        ELSE 'CONFIRMED'
    END,
    notes
FROM trade_old;

DROP TABLE trade_old;

-- 4. Recreate indexes (dropped with old table)
CREATE INDEX IF NOT EXISTS idx_trade_cycle   ON trade(cycle_id);
CREATE INDEX IF NOT EXISTS idx_trade_filled  ON trade(filled_at);
CREATE INDEX IF NOT EXISTS idx_trade_pending ON trade(fill_status)
    WHERE fill_status = 'PENDING';

COMMIT;
