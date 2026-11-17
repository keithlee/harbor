# Harbor pipeline profile

Copy this file for SampleProject and replace every placeholder before running
`harbor doctor`. Values are data only. Harbor keeps
`skill` opaque: the configured runner owns how that skill is loaded and
executed. Unknown keys remain available to the delivery skill.

## Models

| role | model | reasoning_effort |
| --- | --- | --- |
| builder | gpt-5.6-luna | xhigh |
| firstmate | gpt-6-luna | high |

skill: /work-pipeline
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

firstmate_decisions: bounded # bounded | cite_only | human
unblock_attempts: 2 # maximum attempts per blocker kind and rung
recovery_delays: [30, 120, 300] # seconds before up to three retries; [] disables retries
decision_authors: [] # optional Linear user IDs allowed to supply decision authority

Bounded decisions choose a supplied, reversible implementation option within
the ticket's acceptance criteria and record the rationale. Protected paths,
conflicting instructions and unsupported decisions require a human.
Empty decision_authors accepts ticket participants; set explicit user IDs to
restrict comment-based authority. Operator console answers remain explicit
authority. Recovery preserves the native session, worktree and remaining
execution budget. Holds, pauses, cancellation and deliberate stops block
automatic recovery.
