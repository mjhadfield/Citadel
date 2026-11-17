-- Which link (if any) the Casino page embeds in its iframe (Casino World, ~/Projects/CasinoWorld). Mirrors
-- for_house: at most one at a time, enforced in application code.
ALTER TABLE links ADD COLUMN for_casino INTEGER NOT NULL DEFAULT 0;
