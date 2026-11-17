-- Harbor escalation state: open, answered, superseded, or resolved.
--
-- Rows written before this migration keep a NULL status.  Harbor reads a NULL
-- rung-2 row as open and a NULL rung-0 or rung-1 row as resolved, so no
-- backfill is needed and replaying the migration is harmless.

ALTER TABLE escalations ADD COLUMN status TEXT;
ALTER TABLE escalations ADD COLUMN reason TEXT;
ALTER TABLE escalations ADD COLUMN searched_json TEXT;
ALTER TABLE escalations ADD COLUMN resolution_id INTEGER;
ALTER TABLE escalations ADD COLUMN answered_at TEXT;
CREATE INDEX IF NOT EXISTS escalations_ticket_status_idx
    ON escalations(ticket, status);
