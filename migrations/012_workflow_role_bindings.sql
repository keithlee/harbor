CREATE TABLE IF NOT EXISTS workflow_agents (
 project TEXT NOT NULL, ticket TEXT NOT NULL, run_id TEXT NOT NULL, agent TEXT NOT NULL,
 state TEXT NOT NULL, verdict TEXT, PRIMARY KEY(project,ticket,run_id,agent)
);
CREATE TABLE IF NOT EXISTS workflow_roles (
 project TEXT NOT NULL, ticket TEXT NOT NULL, run_id TEXT NOT NULL, role TEXT NOT NULL,
 agent TEXT NOT NULL, PRIMARY KEY(project,ticket,run_id,role)
);
