-- One link can back the Labels page's embedded iframe (ZebraLab, :8710).
-- At most one at a time, enforced in application code.
ALTER TABLE links ADD COLUMN for_labels INTEGER NOT NULL DEFAULT 0;
