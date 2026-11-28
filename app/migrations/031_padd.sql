-- PADD: cross-device Dropzone items and persistent Markdown notes / checklists.

CREATE TABLE padd_drops (
    id              TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,          -- 'text' | 'link' | 'code' | 'image' | 'file'
    title           TEXT NOT NULL DEFAULT '',
    body            TEXT NOT NULL DEFAULT '',
    url             TEXT,
    url_host        TEXT NOT NULL DEFAULT '',
    url_description TEXT NOT NULL DEFAULT '',
    file_name       TEXT,
    stored_name     TEXT,                   -- safe filename inside data/padd/
    mime_type       TEXT NOT NULL DEFAULT '',
    size_bytes      INTEGER NOT NULL DEFAULT 0,
    sha256          TEXT NOT NULL DEFAULT '',
    device_id       TEXT,
    device_name     TEXT NOT NULL DEFAULT '',
    pinned          INTEGER NOT NULL DEFAULT 0,
    share_nonce     TEXT NOT NULL DEFAULT '',
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);

CREATE INDEX idx_padd_drops_order ON padd_drops (pinned DESC, created_at DESC);
CREATE INDEX idx_padd_drops_kind  ON padd_drops (kind, created_at DESC);

CREATE TABLE padd_notes (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT '',
    body        TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL DEFAULT 'note',  -- 'note' | 'checklist'
    tags        TEXT NOT NULL DEFAULT '[]',    -- JSON array of lowercase tags
    pinned      INTEGER NOT NULL DEFAULT 0,
    archived    INTEGER NOT NULL DEFAULT 0,
    device_name TEXT NOT NULL DEFAULT '',
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);

CREATE INDEX idx_padd_notes_order ON padd_notes (archived, pinned DESC, updated_at DESC);
