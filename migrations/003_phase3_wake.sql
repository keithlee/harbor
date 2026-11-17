-- Harbor Phase 3: deterministic wake state.
--
-- Linear and GitHub remain authoritative.  These tables are only the local
-- ledger needed to make a wake idempotent and to remember which review facts
-- were handed to the builder.  This migration is intentionally additive.

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    ticket TEXT NOT NULL REFERENCES tickets(identifier) ON DELETE CASCADE,
    role TEXT NOT NULL DEFAULT 'builder',
    worktree TEXT,
    pid INTEGER,
    pgid INTEGER,
    status TEXT NOT NULL,
    waiting_on TEXT,
    deadline_at TEXT,
    cost_usd REAL,
    final_json TEXT,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS runs_ticket_status_idx ON runs(ticket, status);

CREATE TABLE IF NOT EXISTS prs (
    url TEXT PRIMARY KEY,
    number INTEGER NOT NULL,
    ticket TEXT NOT NULL REFERENCES tickets(identifier) ON DELETE CASCADE,
    head_sha TEXT,
    ci_state TEXT,
    merged INTEGER NOT NULL DEFAULT 0,
    observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS prs_ticket_idx ON prs(ticket);

CREATE TABLE IF NOT EXISTS review_threads (
    id TEXT PRIMARY KEY,
    pr_url TEXT NOT NULL REFERENCES prs(url) ON DELETE CASCADE,
    severity TEXT,
    is_bot INTEGER NOT NULL DEFAULT 0,
    outdated INTEGER NOT NULL DEFAULT 0,
    resolved INTEGER NOT NULL DEFAULT 0,
    path TEXT,
    line INTEGER,
    body TEXT,
    seen_at_sha TEXT,
    observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS review_threads_pr_idx ON review_threads(pr_url);

CREATE TABLE IF NOT EXISTS work_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket TEXT NOT NULL REFERENCES tickets(identifier) ON DELETE CASCADE,
    action TEXT NOT NULL,
    idem_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    run_id TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- A completed/failed queue row is retained for audit, but must not prevent a
-- later legitimate action.  Live rows are the only rows participating in the
-- idempotency gate.
CREATE UNIQUE INDEX IF NOT EXISTS work_queue_live_idem_idx
    ON work_queue(idem_key)
    WHERE status IN ('queued', 'dispatched', 'running', 'waiting');
CREATE INDEX IF NOT EXISTS work_queue_ticket_status_idx ON work_queue(ticket, status);

CREATE TABLE IF NOT EXISTS comment_receipts (
    issue_id TEXT NOT NULL,
    body_hash TEXT NOT NULL,
    comment_id TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (issue_id, body_hash)
);
CREATE UNIQUE INDEX IF NOT EXISTS comment_receipts_issue_comment_idx
    ON comment_receipts(issue_id, comment_id)
    WHERE comment_id IS NOT NULL;

-- SQLite has no portable ADD COLUMN IF NOT EXISTS.  The migration runner in
-- harbor.wake checks for this column before applying this statement.
ALTER TABLE tickets ADD COLUMN bot_rework_rounds INTEGER NOT NULL DEFAULT 0;
