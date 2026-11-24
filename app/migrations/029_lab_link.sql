-- One link can back the HomeLab page's embedded iframe (Home Assistant on Probe-1).
-- At most one at a time, enforced in application code.
ALTER TABLE links ADD COLUMN for_lab INTEGER NOT NULL DEFAULT 0;
