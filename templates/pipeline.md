# Harbor pipeline profile

Copy this file for SampleProject and replace every placeholder before running
`harbor doctor`. Values are data only. The delivery workflow and its models
live in the repository's `.harbor/workflow/` (a copy of `workflows/work-pipeline`,
with models in `config.toml`); do not put `skill`, `workflow_path` or model keys
here. Unknown keys remain available to the delivery skill.

deadline_minutes: 90
disk_floor_gb: 5 # free GB required before a builder worktree is created

## Scope

linear_project_slug: sample-project-<slug>
linear_team: SAMPLE
poll_states: [Todo, In Progress, In Review, Blocked, Human Review]
dispatch_states: [Todo]
hold: true # shadow hold: retain the hold gate without external mutations
hold_all: true # legacy alias for poller integrations
poll_interval_seconds: 60
heartbeat_seconds: 60

## Git and GitHub facts (read-only)

base_branch: main
worktree_root: ~/.harbor/worktrees/SampleProject
github_repository: example/SampleProject # optional; remove for Linear-only supervision
required_checks: [CI]
bot_rework_cap: 1

## Blocker decisions and recovery

unblock_attempts: 2 # maximum attempts per blocker kind and rung
decision_authors: [] # optional Linear user IDs allowed to supply decision authority
# Decision mode and crash-retry delays live in .harbor/workflow/workflow.toml
# ([decisions] and [recovery]); First Mate recovery budgets in config.toml.

Bounded decisions choose a supplied, reversible implementation option within
the ticket's acceptance criteria and record the rationale. Protected paths,
conflicting instructions and unsupported decisions require a human.
Empty decision_authors accepts ticket participants; set explicit user IDs to
restrict comment-based authority. Operator console answers remain explicit
authority. Recovery preserves the native session, worktree and remaining
execution budget. Holds, pauses, cancellation and deliberate stops block
automatic recovery.
