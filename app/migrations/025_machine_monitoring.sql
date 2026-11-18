-- Monitoring for the other machines on the estate (app/machines.py). A Remote Access device is watched when it has an
-- SSH user: Citadel logs in with its own restricted key (data/machines_ed25519), which can only run the stats script.
-- `always_on` = it should never be off (NZXT, Nasferatu, the Home Assistant Pi), so being off for a while is a reminder;
-- machines woken when needed (Phoenix, the laptop) leave it unticked.
ALTER TABLE remote_hosts ADD COLUMN ssh_user TEXT NOT NULL DEFAULT '';
ALTER TABLE remote_hosts ADD COLUMN always_on INTEGER NOT NULL DEFAULT 0;
