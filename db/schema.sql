-- Wheel Trader schema
-- Source of truth. Changes here must be reflected in docs/data-model.md.

CREATE TABLE IF NOT EXISTS underlying (
    underlying_id      TEXT PRIMARY KEY,
    ticker             TEXT UNIQUE NOT NULL,
    notes              TEXT,
    iv_rank_cached     REAL,       -- (current - 52w_low) / (52w_high - 52w_low) * 100
    iv_pct_cached      REAL,       -- % of days in past year IV was below current
    iv_current         REAL,       -- raw current IV (30-day)
    iv_52w_high        REAL,
    iv_52w_low         REAL,
    iv_updated         DATETIME,
    earnings_date      DATE,       -- next earnings announcement (optional)
    wheel_eligible     INTEGER NOT NULL DEFAULT 0,  -- hard gate: 1 = eligible to wheel
    eligible_strategy  TEXT CHECK(eligible_strategy IN (
                           'FUNDAMENTAL', 'TECHNICAL', 'ETF_COMPONENT', 'VOL_PREMIUM'
                       )),
    quality_notes      TEXT,       -- reason for eligibility decision
    last_reviewed      DATE,       -- date eligibility was last set
    created_at         DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS underlying_strategy (
    underlying_id  TEXT NOT NULL REFERENCES underlying(underlying_id),
    strategy       TEXT NOT NULL CHECK(strategy IN ('FUNDAMENTAL','TECHNICAL','ETF_COMPONENT','VOL_PREMIUM')),
    quality_notes  TEXT,
    added_date     DATE NOT NULL,
    PRIMARY KEY (underlying_id, strategy)
);

CREATE INDEX IF NOT EXISTS idx_underlying_strategy_id    ON underlying_strategy(underlying_id);
CREATE INDEX IF NOT EXISTS idx_underlying_strategy_strat ON underlying_strategy(strategy);

CREATE TABLE IF NOT EXISTS cycle (
    cycle_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    underlying_id    TEXT NOT NULL REFERENCES underlying(underlying_id),
    state            TEXT NOT NULL DEFAULT 'SHORT_PUT'
                     CHECK(state IN ('SHORT_PUT','LONG_STOCK','SHORT_CALL','CLOSED')),
    lot_id           TEXT,
    shares_held      INTEGER DEFAULT 0,
    assignment_price REAL,
    assignment_strike REAL,
    total_premium    REAL NOT NULL DEFAULT 0,
    cost_basis       REAL GENERATED ALWAYS AS
                     (assignment_price - (total_premium / 100.0)) VIRTUAL,
    opened_at        DATETIME DEFAULT CURRENT_TIMESTAMP,
    closed_at        DATETIME,
    realized_pnl     REAL,
    notes            TEXT,
    UNIQUE(underlying_id, lot_id)
);

CREATE TABLE IF NOT EXISTS trade (
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

CREATE TABLE IF NOT EXISTS roll_event (
    roll_group_id   TEXT PRIMARY KEY,
    cycle_id        INTEGER NOT NULL REFERENCES cycle(cycle_id),
    old_expiration  DATE,
    new_expiration  DATE,
    old_strike      REAL,
    new_strike      REAL,
    net_credit      REAL,
    rolled_at       DATETIME,
    notes           TEXT
);

CREATE INDEX IF NOT EXISTS idx_trade_cycle    ON trade(cycle_id);
CREATE INDEX IF NOT EXISTS idx_trade_filled   ON trade(filled_at);
CREATE INDEX IF NOT EXISTS idx_cycle_ticker   ON cycle(underlying_id, state);

CREATE VIEW IF NOT EXISTS cycle_summary AS
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
