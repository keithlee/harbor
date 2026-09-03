-- Harbor Phase 7: unblock-ladder audit and reusable operator resolutions.
--
-- Linear remains authoritative for ticket state.  These tables record the
-- ladder decision and the citation that made a rung safe.  They do not grant
-- Harbor permission to merge or mark a ticket Done.

CREATE TABLE IF NOT EXISTS escalations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket TEXT,
    run_id TEXT,
    kind TEXT NOT NULL,
    question TEXT NOT NULL,
    rung_resolved INTEGER NOT NULL,
    citation TEXT,
    answer TEXT,
    notified INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS escalations_ticket_kind_idx
    ON escalations(ticket, kind, created_at);

CREATE TABLE IF NOT EXISTS resolutions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    question_hash TEXT NOT NULL,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    citation TEXT NOT NULL,
    source_ticket TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(kind, question_hash)
);
CREATE INDEX IF NOT EXISTS resolutions_kind_hash_idx
    ON resolutions(kind, question_hash);

-- The migration runner skips this statement if it is already present.
ALTER TABLE tickets ADD COLUMN unblock_attempts INTEGER NOT NULL DEFAULT 0;
