-- Harbor Phase 8: read-only GitHub PR/review event receipts.
--
-- These tables are local idempotency/audit state. Harbor never writes to
-- GitHub; the retained builder and configured skill own all external replies,
-- fixes, PR body updates, and risk analysis.

CREATE TABLE IF NOT EXISTS github_pr_states (
    pr_key TEXT PRIMARY KEY,
    project TEXT NOT NULL DEFAULT '',
    ticket TEXT NOT NULL DEFAULT '',
    pr_number INTEGER NOT NULL,
    pr_url TEXT NOT NULL,
    head_sha TEXT,
    created_at TEXT,
    risk_status TEXT NOT NULL DEFAULT 'unknown',
    risk_head_sha TEXT,
    observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS github_pr_states_ticket_idx
    ON github_pr_states(project, ticket, pr_url);

CREATE TABLE IF NOT EXISTS github_pr_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT NOT NULL UNIQUE,
    project TEXT NOT NULL DEFAULT '',
    ticket TEXT NOT NULL DEFAULT '',
    pr_number INTEGER NOT NULL,
    pr_url TEXT NOT NULL,
    event_type TEXT NOT NULL,
    head_sha TEXT,
    payload_json TEXT NOT NULL,
    offered INTEGER NOT NULL DEFAULT 0,
    replied INTEGER NOT NULL DEFAULT 0,
    observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS github_pr_events_ticket_type_idx
    ON github_pr_events(project, ticket, pr_url, event_type, id);
