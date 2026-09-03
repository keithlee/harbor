-- Harbor Phase 9: durable dispatch lifecycle and final-artifact metadata.
--
-- These fields are local operational receipts. Linear/GitHub remain the
-- authorities for ticket and PR state; the runner records what it observed.
-- The migration runner guards each ALTER for old SQLite databases.

ALTER TABLE work_queue ADD COLUMN error TEXT;
ALTER TABLE work_queue ADD COLUMN started_at TEXT;
ALTER TABLE work_queue ADD COLUMN finished_at TEXT;

ALTER TABLE runs ADD COLUMN commit_sha TEXT;
ALTER TABLE runs ADD COLUMN commit_url TEXT;
ALTER TABLE runs ADD COLUMN artifact_json TEXT;
ALTER TABLE runs ADD COLUMN merge_verified INTEGER NOT NULL DEFAULT 0;
ALTER TABLE runs ADD COLUMN cleanup_at TEXT;

CREATE INDEX IF NOT EXISTS work_queue_ticket_updated_idx
    ON work_queue(ticket, updated_at, id);
