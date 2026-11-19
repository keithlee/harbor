# Configurable delivery workflows

Harbor supervises custody, observations, retries and cancellation. A delivery skill
owns planning, implementation, review, GitHub writes and terminal Linear changes.
Workflow packages supply skills and deterministic hooks; Harbor does not interpret
a stage graph. Existing `skill:` pipelines continue to work.

## New user, without personal skills

Install Harbor in a Python environment with `uv tool install .` from this checkout.
Authenticate the configured Codex runner and provide Linear/GitHub credentials in
the environment or existing supported credential mechanism. Never put credentials
in project files. Choose an actual model available to your runner account.

```sh
harbor init Demo --repo /absolute/path/to/demo \
  --registry /absolute/path/to/projects.md \
  --preset reviewed --linear-project YOUR_SLUG --linear-team YOUR_TEAM \
  --github-repository YOUR_OWNER/YOUR_REPO --model YOUR_MODEL \
  --required-check tests \
  --install-command 'uv sync' --test-command '["uv","run","pytest"]'
export HARBOR_PROJECTS_FILE=/absolute/path/to/projects.md
harbor workflow inspect Demo
harbor doctor Demo
```

Omit setup values in an interactive terminal to receive prompts. `--registry`
is always explicit. Setup refuses to overwrite existing pipeline/workflow files.
It writes `.harbor/pipeline.md`, `.harbor/workflow.toml` and a registry entry.
Repeat `--required-check` for each exact GitHub CI check name. Local test commands
do not configure GitHub CI. Without check names, setup warns and required CI
remains blocked; configure names or explicitly disable that evidence policy.
New projects start with `hold: true`. Review the generated files and change hold to
false before `harbor start Demo`. Use `--install-command none` for an explicit no-op.
Add another project with its own checkout, Linear identity and registry entry.
Run one Harbor supervisor per project. New worktrees require at least 5 GB of free
disk space by default. Missing configuration never chooses another
project or implicitly adopts a personal skill. Existing table registries must be
updated manually; `init` appends to heading-format registries.

## Explicit model fallback

Model fallbacks are opt-in, ordered, and limited to eight distinct model IDs.
For this project, use GPT-6.1 Sol with GPT-5.6 Sol as the authorized fallback:

```toml
[roles.controller]
model = "gpt-6.1-sol"
fallback_models = ["gpt-5.6-sol"]

[skills.blocker_assessment]
model = "gpt-6.1-sol"
fallback_models = ["gpt-5.6-sol"]
```

Add these values to the existing tables; do not duplicate TOML table headers.
New installations can pass `--model gpt-6.1-sol --fallback-model gpt-5.6-sol`
to `harbor init`. Repeat `--fallback-model` for additional authorized choices.
Legacy pipeline documents accept explicit JSON arrays in
`builder_fallback_models` and `firstmate_fallback_models`.

Harbor-owned CLI calls switch only after a structured provider error rejects the
model. Authentication, quota, timeout, cancellation, tool failure and arbitrary
assistant text never authorize switching. No implicit fallback is provided.
Delivery fallback resumes the same native session in the same worktree, consumes
the retained execution budget, and records sanitized model-selection receipts.
If no native session was established, Harbor blocks rather than inventing one.
Restart restores the selected fallback when it is still authorized by the current
policy. Exhausting the fallback list leaves a non-retryable model blocker.
First Mate's read-only answer and triage calls use their explicit fallback list;
its selected fallback remains in use and restores from receipts after restart
when First Mate is attached to the project supervisor.

Changing a retained ticket's workflow still requires explicit policy
reconciliation. Configured child-agent models remain the delivery adapter's
responsibility; this does not add fallback handling to external spawn APIs.

## Presets and overrides

| Setting | Minimal | Reviewed | Autonomous |
| --- | --- | --- | --- |
| Merge | Human | Human | Workflow-checked, low risk |
| Planning, independent review, proof, risk, feedback | Optional/off | Required | Required |
| CI | Required | Required | Required |
| First Mate | Cited authority | Cited authority | Bounded decisions |
| Unexpected-exit retries | Off | Off | 30/120/300 seconds |
| Cleanup | After verified merge and terminal completion | Same | Same |

Precedence is package defaults → selected preset → project overrides. Inspect
prints the effective configuration, package/preset/override sources and a digest.
No provider model is baked into a package: setup asks for one; role and skill
bindings can override it. Unspecified child roles inherit the controller model;
First Mate inherits that explicit model unless separately configured. Legacy model
defaults apply only to legacy profiles.

Example `.harbor/workflow.toml`:

```toml
version = 1
[workflow]
package = "builtin:work-pipeline" # or builtin:reference, or a local package path
preset = "reviewed"

[roles.controller]
model = "YOUR_CONTROLLER_MODEL"
reasoning_effort = "high"
[roles.planner]
model = "YOUR_PLANNER_MODEL"
[roles.reviewer]
model = "YOUR_REVIEWER_MODEL"
[roles.implementor]
model = "YOUR_IMPLEMENTOR_MODEL"

[skills.blocker_assessment]
model = "YOUR_DECISION_MODEL"
reasoning_effort = "high"

[decisions]
mode = "bounded" # human | cite_only | bounded

[recovery]
enabled = true
delays_seconds = [10, 60]
max_attempts = 2

[cleanup]
mode = "after_merge" # manual | off prevent automatic removal
delay_seconds = 3600

[execution]
deadline_minutes = 60

[states]
ready = ["Todo"]
paused = ["Paused", "Human Review"]
terminal = ["Done", "Canceled"]

[checks.unit]
command = ["uv", "run", "pytest"]
required = true
timeout_seconds = 600
[checks.build]
command = ["uv", "build"]
after = ["unit"]
required = true
```

Disable review with BOTH `[review] enabled=false, require_independence=false`
and `[skills.review] enabled=false`. Evidence policies are separate:
`[evidence] feedback/risk/proof/ci` each accepts `required`, `advisory`, or `off`.
Disabling a skill does not turn a required validator into a pass. An unsupported
setting or contradictory binding is rejected. The bundled policies keep
medium/high risk human-controlled; custom policies can change eligible tiers.

The work-pipeline package retains one controller, planner/reviewer and implementor.
It uses the same execution machinery as the reference package. It does not modify
`~/.codex/skills/work-pipeline`. Models and role choices are supplied to the
controller; an adapter unable to honor them must report a capability blocker.

## Replace skills and hooks

Copy a bundled package to a trusted local directory, change `workflow.package`
to that directory, and edit its `workflow.toml` or skill references. Skill refs
are confined to that package. Packages are trusted executable code, not a sandbox;
keep them outside the builder's mutable worktree. Package symlinks are rejected.
The pipeline still supplies the project's AGENTS.md/Pipeline Profile constitution.

Hooks run at `setup`, `blocker`, `pre_handoff`, `pre_merge`, and `pre_cleanup`.
Each hook supports `required` (blocks), `advisory` (records failures), or `off`
(records disabled). Script hooks receive versioned JSON on stdin and return:

```json
{"status":"pass","reason":"requirements satisfied","evidence":["artifact reference"]}
```

Use `executor="script"`, `command=["python3","scripts/check.py"]`,
`protocol="json"` (or `exit_code`) and `timeout_seconds`. Commands are argument
arrays, never implicit shell strings. Timeouts/cancellation terminate the process
group. JSON output is capped at 64 KiB. Ordinary check output is drained without
being stored; retain proof in explicit project artifacts.

Use `executor="skill"`, `skill="your_binding"` for a read-only assessment hook.
Declare `[skills.your_binding] ref=...`, `model=...`, and optionally reasoning
 effort. The result uses the same schema. Mutating implementation remains in the
delivery skill. The bundled First Mate assessment is injected into its authority
packet, then the deterministic decision validator checks the result.

```sh
harbor workflow checks Demo --ticket DEM-1 --parallel
harbor workflow role Demo --ticket DEM-1 --role reviewer --agent ACTUAL_CHILD_ID
harbor workflow roles Demo --ticket DEM-1
harbor workflow check Demo --ticket DEM-1 --event pre_handoff
harbor workflow check Demo --ticket DEM-1 --event pre_merge
```

Independent command groups may run concurrently; dependencies wait, failed
required checks fail the aggregate, and dependent commands are skipped. No
configured checks returns `disabled`, not proof of success.

Role registration accepts only child IDs observed in that run's Codex JSON stream.
Multi-agent V2 children are observed from `SubAgentActivity` (`sub_agent_activity`)
`started` items and are registered by their `agent_path` (for example `/root/planner`).
It rejects controller/self-review, cross-ticket identities and reviewer/implementor
reuse. The same bindings survive restart. The reviewer returns structured JSON
binding verdict, proof and risk to the PR head and current feedback digest. A
comment containing “APPROVE” never becomes a reviewer receipt. If native agent
provenance or resumption is unavailable, reviewed delivery remains blocked.
Harbor cannot intercept an arbitrary agent's native tool call before execution;
the registered role interface and readiness gate reject invalid provenance.

## Recovery, handoff and cleanup

Console `/answer` persists an answer and schedules tracked same-session work.
Standalone `harbor answer` queues delivery for the project supervisor and returns
immediately. `/status` and `/exit` remain usable during delivery. Duplicate answers
cannot start duplicate tasks. Interrupted claimed answers are not blindly replayed;
bounded run recovery or explicit `/retry` reconciles the retained session.
Operator stops are never automatically retried. Holds, inactive states, exhausted
budgets and unverified custody block automatic recovery.

Readiness fetches current Linear/GitHub state. Receipts bind project, ticket, run,
head, feedback version and package/policy digest. New heads or comments invalidate
review approval. Linear reply markers are version-bound and observable on a
separate reply. The delivery workflow must call readiness immediately before
mutation and use the returned expected head.

These merge gates are **cooperative**. This version does not install repository
rules or remove agents' merge credentials, so it does not promise bypass-proof
merging. Human mode always refuses an automatic merge check.

Policy edits during a retained run fail closed. After stopping active work, an
operator can explicitly adopt the new policy:

```sh
harbor workflow reconcile Demo --ticket DEM-1 --expected-digest OLD_DIGEST
```

This records the change and invalidates reviewer verdicts. History remains.

Cleanup requires an independently observed merge, completed delivery and terminal
Linear state, exact local/merged PR head, stopped processes/agents, no open
escalation, no hold, and passing configured hooks. Dirty/untracked/**ignored** files
are retained for manual inspection. No force removal or branch deletion occurs.
`manual`/`off` prevent automatic cleanup; a delay defers eligibility. SQLite keeps
the run and cleanup receipts after files are removed.

## Verification map

| Phase | Primary files | Scenario tests |
| --- | --- | --- |
| Responsive answers/shutdown | answer_queue.py, console.py, runner/base.py | test_answer_dispatch.py, test_runner_phase4.py, test_blocker_recovery.py |
| Config/readiness/hooks | config/workflow.py, hooks.py, readiness.py, workflow_cli.py | test_workflow_config.py, test_hooks.py, test_workflow_evidence.py, test_workflow_fresh_context.py |
| Roles/parallel workflow | role_bindings.py, checks.py, workflows/ | test_workflow_roles.py, test_parallel_checks.py |
| Bounded decisions | unblock.py, decision_policy.py, risk_evidence.py | test_decision_policy.py, test_blocker_recovery.py, test_workflow_evidence.py |
| Identity/setup recovery | project_identity.py, worktree.py | test_project_scope.py, test_workflow_setup_cleanup.py, test_restart_hydration.py |
| Cleanup | workflow_lifecycle.py, worktree.py | test_workflow_cleanup_policy.py, test_merge_cleanup.py |
| New-user portability | onboard.py, pyproject.toml | test_onboard.py, test_runtime_resources.py |

Run `uv run pytest`, `uv run python -m compileall -q src tests`, and `uv build`.
Tests use mock external tickets and real local subprocesses/Git worktrees. They
are not evidence of a fresh live Linear-to-merge run or native child-agent
capability on every provider/version. Validate those on an isolated test project
before enabling autonomous delivery for an existing project.

## Verification of this implementation

Code commit `4694c1b` passed 509 tests, compileall, and wheel/source builds.
Changed executable-line coverage was 885/1018 (86.9%), above the profile's 80%
minimum. Isolated CLI execution proved setup and inspection succeed and unknown
projects/invalid configuration exit with code 2. All ten bundled skills passed
format validation.

Independent reviews reproduced and regression-tested setup-hook retry, custom
paused-state handling, orphan hook children, capitalized risk values, missing
explicit models, and missing decision rationale. The last rationale correction
was reverified by the existing independent reviewer after the tool's fresh-agent
limit was reached. A new clean-context pre-PR gate has not been claimed for that
last correction.

This is a broad, protected-path change exceeding the profile's file/line risk
budget; it requires human merge review. No fresh live Linear-to-merge acceptance
or actual provider-native child-agent resumption has been claimed. No existing
project was migrated or enabled for automatic merging by this implementation.

### Delivery runtime binding

Managed delivery prompts include a shell-quoted invocation that pins the resolved project registry, operational ledger, Python interpreter and Harbor import path. Workflow commands and child agents use that invocation with the supplied project and ticket. The native sandbox includes the ledger directory for operational receipts; the registry remains read-only. Missing registry context blocks workflow delivery.
