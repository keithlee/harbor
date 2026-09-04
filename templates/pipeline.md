# Harbor pipeline profile

Copy this file for SampleProject and replace every placeholder before running
`harbor doctor`. Values are data only. Harbor keeps
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
