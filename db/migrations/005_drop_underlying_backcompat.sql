-- Migration 005: remove backward-compat columns from underlying.
-- Canonical strategy store is underlying_strategy (since migration 004).
-- Per-strategy notes live on underlying_strategy.quality_notes.
-- Any remaining ineligible-ticker notes are merged into underlying.notes
-- so the rationale is not lost.

UPDATE underlying
SET notes = CASE
    WHEN notes IS NULL OR notes = '' THEN quality_notes
    WHEN quality_notes IS NULL OR quality_notes = '' THEN notes
    ELSE notes || ' | ' || quality_notes
END
WHERE wheel_eligible = 0 AND quality_notes IS NOT NULL AND quality_notes <> '';

ALTER TABLE underlying DROP COLUMN eligible_strategy;
ALTER TABLE underlying DROP COLUMN quality_notes;
