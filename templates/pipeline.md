# Harbor pipeline profile

Edit this file in the project repository. Values are data only. Harbor keeps
`skill` opaque: the configured runner owns how that skill is loaded and
executed. Unknown keys remain available to the delivery skill.

## Models

| role | model | reasoning_effort |
| --- | --- | --- |
| builder | gpt-5.6-luna | xhigh |
| firstmate | gpt-5.6-terra | xhigh |

skill: /work-pipeline
deadline_minutes: 90

## Scope

linear_project_slug: hanzireader-06c91e632f50
linear_team: HAN
poll_states: [Todo, In Progress, In Review, Blocked, Human Review]
dispatch_states: [Todo]
hold: true # shadow hold: retain the hold gate without external mutations
hold_all: true # legacy alias for poller integrations
poll_interval_seconds: 60
heartbeat_seconds: 60

## Git and GitHub facts (read-only)

base_branch: main
worktree_root: ~/.harbor/worktrees/Bookgen
github_repository: keithlee/Bookgen
required_checks: [CI]
bot_rework_cap: 1
