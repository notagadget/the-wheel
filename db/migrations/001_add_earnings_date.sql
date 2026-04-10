-- Migration 001: Add earnings_date column to underlying table
-- Allows tracking next earnings announcement date for wheel candidates

ALTER TABLE underlying ADD COLUMN earnings_date DATE;
