-- Migration 003: add wheel eligibility columns to underlying
-- Idempotent — each ALTER is guarded by _column_exists in db.py

ALTER TABLE underlying ADD COLUMN wheel_eligible    INTEGER NOT NULL DEFAULT 0;
ALTER TABLE underlying ADD COLUMN eligible_strategy TEXT
    CHECK(eligible_strategy IN (
        'FUNDAMENTAL', 'TECHNICAL', 'ETF_COMPONENT', 'VOL_PREMIUM'
    ));
ALTER TABLE underlying ADD COLUMN quality_notes     TEXT;
ALTER TABLE underlying ADD COLUMN last_reviewed     DATE;
