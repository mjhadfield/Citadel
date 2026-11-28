-- A device with no remote desktop (Home Assistant OS on Probe-1, 2026-11-26): viewer 'none' keeps it off the Home
-- page's Remote Access tile while Monitoring and the Fleet page still watch it. SQLite can't change a CHECK in place, so
-- the table is rebuilt with the same rows and ids (the collector names machines rh<id>, so ids must not move).
CREATE TABLE remote_hosts_new (
    id        INTEGER PRIMARY KEY,
    name      TEXT NOT NULL,
    address   TEXT NOT NULL,
    sort      INTEGER NOT NULL DEFAULT 0,
    viewer    TEXT NOT NULL DEFAULT 'krdc' CHECK (viewer IN ('krdc', 'tigervnc', 'none')),
    ssh_user  TEXT NOT NULL DEFAULT '',
    always_on INTEGER NOT NULL DEFAULT 0,
    mac       TEXT NOT NULL DEFAULT ''
);
INSERT INTO remote_hosts_new (id, name, address, sort, viewer, ssh_user, always_on, mac)
    SELECT id, name, address, sort, viewer, ssh_user, always_on, mac FROM remote_hosts;
DROP TABLE remote_hosts;
ALTER TABLE remote_hosts_new RENAME TO remote_hosts;
