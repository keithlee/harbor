# Harbor

Harbor is a calm, deterministic supervisor for one configured project. It
polls Linear and GitHub without making model calls, dispatches a configured
delivery skill in an isolated worktree, keeps the same builder session across
re-engagement, and gives the operator a First Mate console in the same
terminal.

The boundary is deliberate:

> The skill decides. Harbor watches, wakes, unblocks, and reports.

The skill is a Markdown-controlled prompt. A team can replace
`/work-pipeline` with its own ticket-to-PR instructions without changing
Harbor or adding a Harbor-specific implementation role.

## Install, verify, and start

From the Harbor checkout:

```bash
cd ~/Projects/harbor
uv sync
uv run harbor doctor Bookgen
uv run harbor start Bookgen
```

`projects.md` resolves `Bookgen` to its repository, Linear project, GitHub
repository, base branch, and live pipeline document. `doctor` verifies the
Linear connection and that the configured project scope exists. `start` then
opens the `first-mate>` prompt, starts model-free polling, and schedules any
eligible builder dispatch in the background so the prompt remains usable
while an agent works.

Linear and GitHub API credentials are external-service credentials, not model
calls. Linear uses `LINEAR_API_KEY` or `HARBOR_LINEAR_API_KEY`; the GitHub
poller uses `GITHUB_TOKEN` or `GH_TOKEN` when GitHub facts are configured.

For a read-only one-cycle check:

```bash
uv run harbor start Bookgen --once
uv run harbor start Bookgen --shadow
uv run harbor facts Bookgen --json
uv run harbor status Bookgen --json
```

`--shadow` computes wake decisions and prints `WOULD WAKE` lines without
dispatching, queuing, or writing Linear/GitHub. `facts` performs the read-only
PR-facts query; `status` reads the local runtime ledger.

## Same-terminal First Mate workflow

There is no `harbor ask` or project-name prefix for questions. Once
`harbor start Bookgen` is running, type a plain question directly. Slash
commands are deterministic and do not invoke a model; plain text sends one
question to the configured First Mate model with the same fresh digest shown
by `/status`.

```text
first-mate> /status
Harbor: RUNNING · Bookgen · Linear healthy
HAN-33  Done  ...  PR #226
Run harbor-Bookgen-HAN-33-builder  builder  completed  waiting on none  outcome=merged  pr_url=https://github.com/keithlee/Bookgen/pull/226

first-mate> What is the fleet waiting on?
<one Terra answer based on the refreshed fleet digest>

first-mate> /hold on HAN-42
Harbor: HOLD ON · HAN-42

first-mate> /hold off HAN-42
Harbor: HOLD OFF · HAN-42

first-mate> /pause Stopping for the evening
Harbor: PAUSED · Stopping for the evening

first-mate> /resume
Harbor: RUNNING

first-mate> /exit
Harbor: STOPPED
```

The commands are:

| Command | Effect |
| --- | --- |
| `/status` | Render the deterministic fleet digest: Linear tickets, PR/CI/review facts, builder runs, outcomes, and escalations. Zero model calls. |
| `/hold on/off <ticket>` | Change the durable merge hold for exactly one observed ticket. This does not pause polling or other agents. |
| `/pause [reason]` | Pause the whole supervisor: no new dispatches or resumes; a running builder is allowed to finish and its deadline is suspended. |
| `/resume` | Re-enable dispatch/resume, restart deadline accounting, and perform a fresh poll. |
| `/answer <escalation-id> <answer>` | Record an operator decision and attempt to resume the retained builder session. |
| `/kill [run-id]` | Request termination through an installed process-owner hook; reports when no hook is available. |
| `/exit` | Stop polling, heartbeat, and active dispatches, then close the foreground console. It does not create a replacement agent. |
| `/help` | Print the command list. |

`/hold` is intentionally not an alias for `/pause`: a hold is per-ticket
merge policy stored in Harbor's ledger, while pause is a whole-supervisor
lifecycle control. `hold_all`/`hold` in `pipeline.md` supplies the default
shadow policy for newly observed work; the command changes one ticket's local
operational flag.

## Technical flow

```text
projects.md
   │ resolve project + live .harbor/pipeline.md
   ▼
CLI composition root
   │ build clients, pollers, ledger, worktree manager, Terra First Mate
   ▼
Supervisor (model-free asyncio loop)
   ├─ Linear GraphQL: configured project + poll states
   ├─ GitHub GraphQL: only PRs attached to those Linear tickets
   ├─ snapshot diff: unchanged cycle emits no event
   ├─ wake decision: Todo → one idempotent build dispatch
   ├─ heartbeat: configurable liveness/activity publication
   └─ digest: the exact status context used by /status and plain questions
          │
          ├─ build/re-engage → isolated worktree → Codex builder (Luna)
          │                    raw pipeline.md + ticket facts
          │                    same run/session on resume
          │
          ├─ PR created/commented/CI changed → read-only facts → re-engage
          │                    comment handling belongs to the skill
          │
          └─ blocked → deterministic remediation → Terra cite/escalate
                                    → operator answer → same builder resume
```

The supervisor is the deterministic half of First Mate: it polls, dispatches,
supervises, and re-engages at zero model cost. The conversational half is the
configured First Mate model (Terra by default), invoked only for a plain
question or rung-1 blocker triage. The builder is Luna by default and receives
the configured pipeline document as prompt data. Harbor does not import,
rewrite, or interpret the skill's internal workflow.

### Heartbeat

The heartbeat is an independent, model-free loop. `heartbeat_seconds` defaults
to 60 and is configurable in the pipeline document. Each event carries the
project, lifecycle, health, publication time, snapshot hash, last observed
activity, and idle seconds. The terminal prints a heartbeat line; `/status`
shows the detailed run and waiting state when you need to know what the agent
is doing. Heartbeats do not poll an LLM, alter Linear/GitHub, or make wake
decisions.

### External authority and local custody

Linear and GitHub remain authoritative for external state:

- Linear owns ticket state, ticket comments, project scope, and ticket-to-PR
  attachments. The builder skill owns normal progress and the terminal `Done`
  transition after a verified merge. Harbor's unblock ladder may write a
  cited progress comment and park an owned ticket in `Blocked` or `Human
  Review`; it never writes `Done`.
- GitHub owns PR state, head SHA, checks, reviews, review threads, comments,
  and merge state. Harbor's GitHub client is query-only. The builder skill is
  the only component that posts PR comments, addresses review comments,
  computes/writes risk proof, or merges.

SQLite at `~/.harbor/harbor.db` is runtime custody, not a competing source of
truth. It records ownership, idempotency keys, snapshots/events, run/session
identity, liveness, review-thread observations, blocker/escalation history,
resolutions, and merge/cleanup receipts. This is what lets Harbor resume the
same builder and avoid duplicate dispatches after a restart. A fresh poll
reconciles external state; local rows never override Linear or GitHub.

## Configuration

Copy the template into the target repository:

```bash
mkdir -p ~/Projects/Bookgen/.harbor
cp templates/pipeline.md ~/Projects/Bookgen/.harbor/pipeline.md
```

Edit the Markdown document as data. The important fields are:

```md
## Models
| role | model | reasoning_effort |
| --- | --- | --- |
| builder | gpt-5.6-luna | xhigh |
| firstmate | gpt-5.6-terra | xhigh |

skill: /work-pipeline
heartbeat_seconds: 60
poll_interval_seconds: 60
deadline_minutes: 90
hold: true

linear_project_slug: hanzireader-06c91e632f50
poll_states: [Todo, In Progress, In Review, Blocked, Human Review]
dispatch_states: [Todo]
base_branch: main
worktree_root: ~/.harbor/worktrees/Bookgen
github_repository: keithlee/Bookgen
required_checks: [CI]
bot_rework_cap: 1
```

Change `skill`, the builder/First Mate models, reasoning effort, heartbeat,
poll interval, scope, or worktree settings in this file. Unknown fields remain
available as skill prompt data. Harbor only parses the operational settings it
needs and keeps the delivery instructions opaque.

## Files and responsibilities

| File/module | Responsibility |
| --- | --- |
| `projects.md` | Human-editable project registry: checkout, pipeline, Linear scope, GitHub repository, and base branch. |
| `templates/pipeline.md` | Starting point for a project's swappable skill/model/heartbeat configuration. |
| `src/harbor/cli.py` | Production composition root and `doctor`, `start`, `status`, `facts`, and `answer` commands. |
| `src/harbor/console.py` | Same-terminal prompt, slash commands, prompt-safe background output, and plain-question routing. |
| `src/harbor/supervisor.py` | Async lifecycle, polling loop, wake scheduling, background dispatch, pause/resume, merge reconciliation, and status assembly. |
| `src/harbor/db.py` | SQLite schema, migrations, WAL, idempotency, runtime custody, and durable receipts. |
| `src/harbor/digest.py` | Pure deterministic formatter shared by `/status` and First Mate prompts. |
| `src/harbor/heartbeat.py` | Configurable model-free liveness/activity events. |
| `src/harbor/first_mate.py` | Read-only ephemeral Terra question/triage invocation. |
| `src/harbor/notify.py` | Operator notifications for escalations and capped rework. |
| `src/harbor/config/projects.py` | Resolve named projects and require the live pipeline document. |
| `src/harbor/config/pipeline.py` | Parse Markdown pipeline settings while retaining skill data as opaque configuration. |
| `src/harbor/config/profile.py` | Parse read-only GitHub/project profile facts used by pollers and event handling. |
| `src/harbor/clients/linear.py` | Async Linear GraphQL reads plus narrow cited blocker/progress writes; rejects terminal `Done` writes. |
| `src/harbor/clients/github.py` | Async query-only GitHub GraphQL client; builds aliased PR queries. |
| `src/harbor/poll/linear_poller.py` | Poll configured Linear projects/states and normalize ticket/attachment/comment facts. |
| `src/harbor/poll/github_poller.py` | Poll only PRs attached to active tickets. |
| `src/harbor/poll/snapshot.py` | Canonical snapshot hashing and changed/unchanged detection. |
| `src/harbor/facts/ci.py` | Required-check and rollup interpretation for display/wake facts. |
| `src/harbor/facts/threads.py` | Review-thread severity, bot, outdated, path, line, and resolution facts. |
| `src/harbor/facts/remediation.py` | Deterministic environment-remediation facts/playbooks for rung 0. |
| `src/harbor/wake.py` | Model-free build/re-engagement decisions, ownership, caps, and idempotency. |
| `src/harbor/reengagement.py` | Turn changed PR/CI facts into a bounded same-session resume instruction. |
| `src/harbor/pr_events.py` | PR-created and external-comment event receipts, risk/comment handoff, and mandatory reply contract. |
| `src/harbor/unblock.py` | Rung 0 remediation, Terra cite-or-escalate, operator parking, and same-session resume. |
| `src/harbor/worktree.py` | Fresh `origin/main` worktrees, disk/lock safety, install, and verified-merge cleanup. |
| `src/harbor/runner/base.py` | Runner lifecycle, deadlines, result normalization, liveness, and orphan handling. |
| `src/harbor/runner/codex.py` | Persistent Luna `codex exec`/`resume` in the isolated worktree. |
| `src/harbor/runner/stream.py` | JSONL event parsing and final run-result extraction. |
| `schemas/run-result.json` | Stable builder final-result contract for status, outcome, blocker, and PR identity. |
| `migrations/` | Additive schema migrations for wake, runner, re-engagement, unblock, PR feedback, and merge receipts. |

## Safety boundaries

- Only `Todo` dispatches by default; `Backlog` is excluded from the Linear
  query.
- Every builder runs in `~/.harbor/wt/<project>/<ticket>` (or configured
  `worktree_root`), never in the operator's checkout.
- A logical run ID and Codex session ID are retained for re-engagement; Harbor
  never silently creates a replacement session for a blocker.
- Harbor observes GitHub and does not mutate it. Risk analysis, PR replies,
  review resolution, and merge are performed by the configured skill.
- A claimed merge is not enough to delete a worktree. Harbor first observes
  the exact PR as merged, persists a merge receipt, and only then removes the
  local worktree. Unobserved claims remain fail-closed.
- Pause stops new dispatch/resume and suspends deadlines; it does not erase
  the ledger or pretend the builder completed.

## Current implementation status

This repository is an incremental MVP implementation. Passing a unit test or a
single real ticket does not mark every phase acceptance criterion complete.
The verified checkpoint currently includes:

- HAN-33 completed the low-risk comment-only path in Bookgen. PR #226 merged,
  then Linear HAN-33 moved to `Done`; the exact final receipt is now reconciled
  through a read-only PR observation, with the worktree cleanup gated on that
  receipt.
- The Harbor suite reports 230 passing tests, including coverage for the
  asynchronous console/heartbeat startup, ticket-scoped hold semantics, and
  HAN-33 merge receipt/restart reconciliation.

Still required for end-to-end proof:

- a live seeded GitHub PR comment and evidence that the builder replies to
  every comment, fixes valid findings, and explicitly handles/ignores an
  invalid one;
- a fresh live run authorized by the operator that keeps the same terminal
  prompt usable during a real background dispatch and shows the one-minute
  heartbeat/activity output;
- completion of the remaining multi-ticket unblock-ladder and risk-gated
  human-review scenarios.

These gaps are proof work, not permission to weaken the fail-closed merge or
same-session recovery boundaries.
