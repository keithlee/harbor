CREATE TABLE IF NOT EXISTS workflow_runs (
 project TEXT NOT NULL, ticket TEXT NOT NULL, run_id TEXT NOT NULL,
 policy_digest TEXT NOT NULL, config_json TEXT NOT NULL,
 PRIMARY KEY(project,ticket,run_id)
);
CREATE TABLE IF NOT EXISTS workflow_receipts (
 id INTEGER PRIMARY KEY, project TEXT NOT NULL, ticket TEXT NOT NULL,
 run_id TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL
);
