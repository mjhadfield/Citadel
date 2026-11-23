-- One link can back the Holodeck page's embedded iframe (Holodeck retro emulator, :8720).

ALTER TABLE links ADD COLUMN for_holodeck INTEGER NOT NULL DEFAULT 0;
