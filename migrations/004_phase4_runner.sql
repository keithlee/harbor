-- Harbor Phase 4: process identity, liveness, and pause-aware deadlines.
-- The migration runner skips an ALTER when the column already exists so this
-- remains safe to apply after a supervisor restart.

ALTER TABLE runs ADD COLUMN session_id TEXT;
ALTER TABLE runs ADD COLUMN deadline_seconds REAL;
ALTER TABLE runs ADD COLUMN paused_seconds REAL NOT NULL DEFAULT 0;
ALTER TABLE runs ADD COLUMN paused_at TEXT;
ALTER TABLE runs ADD COLUMN last_activity_at TEXT;
ALTER TABLE runs ADD COLUMN terminal_reason TEXT;

CREATE INDEX IF NOT EXISTS runs_pid_idx ON runs(pid, pgid);
