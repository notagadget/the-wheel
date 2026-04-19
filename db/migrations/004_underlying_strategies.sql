-- Migration 004: multi-strategy eligibility table
-- Each underlying can now qualify under multiple strategy frameworks.
-- underlying.eligible_strategy is kept for backward compat (remove in follow-up)

CREATE TABLE IF NOT EXISTS underlying_strategy (
    underlying_id  TEXT NOT NULL REFERENCES underlying(underlying_id),
    strategy       TEXT NOT NULL CHECK(strategy IN ('FUNDAMENTAL','TECHNICAL','ETF_COMPONENT','VOL_PREMIUM')),
    quality_notes  TEXT,
    added_date     DATE NOT NULL,
    PRIMARY KEY (underlying_id, strategy)
);

CREATE INDEX IF NOT EXISTS idx_underlying_strategy_id   ON underlying_strategy(underlying_id);
CREATE INDEX IF NOT EXISTS idx_underlying_strategy_strat ON underlying_strategy(strategy);

INSERT OR IGNORE INTO underlying_strategy (underlying_id, strategy, quality_notes, added_date)
SELECT underlying_id, eligible_strategy, quality_notes, COALESCE(last_reviewed, date('now'))
FROM underlying
WHERE wheel_eligible = 1 AND eligible_strategy IS NOT NULL
