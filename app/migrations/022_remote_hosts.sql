-- The Home page's Remote Access tile: devices on the estate this PC opens a VNC viewer to (TigerVNC's `vncviewer`),
-- edited in Settings → Remote access. `address` is what vncviewer takes: host, host:display or host::port.
CREATE TABLE remote_hosts (
    id      INTEGER PRIMARY KEY,
    name    TEXT NOT NULL,
    address TEXT NOT NULL,
    sort    INTEGER NOT NULL DEFAULT 0
);
