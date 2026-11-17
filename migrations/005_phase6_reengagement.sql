-- Harbor Phase 6: read-only re-engagement audit/idempotency.
--
-- GitHub and Linear remain authoritative.  This table records the local fact
-- that an observation was offered to a retained builder; it is not an
-- outbound API queue and contains no mutation capability.

CREATE TABLE IF NOT EXISTS reengagement_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket TEXT NOT NULL,
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    idem_key TEXT NOT NULL UNIQUE,
    head_sha TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS reengagement_events_ticket_idx
    ON reengagement_events(ticket, run_id, kind, observed_at);
