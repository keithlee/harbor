-- Replace repository-local PR-number identity with the exact GitHub URL.
-- The Python migration guard runs this only for the legacy number-PK schema.

PRAGMA foreign_keys=OFF;
BEGIN IMMEDIATE;

ALTER TABLE review_threads RENAME TO review_threads_number_identity;
ALTER TABLE prs RENAME TO prs_number_identity;
DROP INDEX IF EXISTS review_threads_pr_idx;
DROP INDEX IF EXISTS prs_ticket_idx;

CREATE TABLE prs (
    url TEXT PRIMARY KEY,
    number INTEGER NOT NULL,
    ticket TEXT NOT NULL REFERENCES tickets(identifier) ON DELETE CASCADE,
    head_sha TEXT,
    ci_state TEXT,
    merged INTEGER NOT NULL DEFAULT 0,
    observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX prs_ticket_idx ON prs(ticket);

INSERT INTO prs(url, number, ticket, head_sha, ci_state, merged, observed_at)
SELECT url, number, ticket, head_sha, ci_state, merged, observed_at
FROM prs_number_identity
WHERE url IS NOT NULL AND TRIM(url) <> '';

CREATE TABLE review_threads (
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
CREATE INDEX review_threads_pr_idx ON review_threads(pr_url);

INSERT INTO review_threads(
    id, pr_url, severity, is_bot, outdated, resolved, path, line, body,
    seen_at_sha, observed_at
)
SELECT
    thread.id, pr.url, thread.severity, thread.is_bot, thread.outdated,
    thread.resolved, thread.path, thread.line, thread.body,
    thread.seen_at_sha, thread.observed_at
FROM review_threads_number_identity AS thread
JOIN prs_number_identity AS pr ON pr.number = thread.pr
WHERE pr.url IS NOT NULL AND TRIM(pr.url) <> '';

DROP TABLE review_threads_number_identity;
DROP TABLE prs_number_identity;

COMMIT;
PRAGMA foreign_keys=ON;
