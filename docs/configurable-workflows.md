# The work-pipeline workflow

Harbor polls Linear and GitHub, persists what it sees in SQLite, and hands each
ticket to **one** workflow: the project's copy of the work-pipeline. Harbor
supervises custody, observation, retries, cancellation and recovery. The
work-pipeline's delivery skill owns planning, implementation, review, GitHub
writes and terminal Linear changes.

There are no workflow packages, presets or override layers. To change the dev
workflow, edit your project's copy.

## Layout

`harbor init` copies `workflows/work-pipeline/` into the repository:

```
<repo>/.harbor/
  pipeline.md                 # scope, cadence, worktree, install command
  workflow/                   # your editable work-pipeline (commit it)
    workflow.toml             # policy: merge mode, recovery, decisions, evidence, hooks, checks
    config.toml               # models: controller, planner, reviewer, implementor, first_mate
    skills/delivery/SKILL.md  # the ticket-to-PR instructions the builder follows
    skills/{planning,review,risk,blocker-assessment}/SKILL.md
    scripts/readiness.py      # pre_handoff / pre_merge readiness hook
    scripts/decision_policy.py
```

Harbor always loads `<repo>/.harbor/workflow/workflow.toml`. A missing copy
fails `harbor doctor` / `harbor start` with an exact message. There is no
fallback to the bundled copy at runtime.

## New user

```sh
harbor init Demo --repo /absolute/path/to/demo \
  --registry /absolute/path/to/projects.md \
  --linear-project YOUR_SLUG --linear-team YOUR_TEAM \
  --github-repository YOUR_OWNER/YOUR_REPO --model YOUR_MODEL \
  --required-check tests \
  --install-command 'uv sync' --test-command '["uv","run","pytest"]'
export HARBOR_PROJECTS_FILE=/absolute/path/to/projects.md
harbor workflow inspect Demo
harbor doctor Demo
```

Omit values in an interactive terminal to be prompted. `init` refuses to
overwrite an existing `.harbor/workflow`, `pipeline.md` or legacy
`workflow.toml`, writes `config.toml` with `--model` as the controller, starts
the project with `hold: true`, and removes only what it created if validation
fails. Commit `.harbor/workflow/`. Release `hold` before `harbor start Demo`.
Run one Harbor supervisor per project.

## Models (`config.toml`)

```toml
[controller]                  # the builder Harbor launches (required)
model = "gpt-6.1-terra"
reasoning_effort = "medium"
fallback_models = ["gpt-5.6-sol"]   # optional, ordered, explicit

[planner]
model = "gpt-6.2-sol"
[reviewer]
model = "gpt-6.2-sol"
[implementor]                 # empty: inherits the controller

[first_mate]                  # model defaults to the controller's
model = "gpt-6-luna"
max_recoveries = 3
allow_fresh_session = true
recheck_minutes = 10
stall_minutes = 30
```

Harbor reads `controller` (to launch the builder) and `first_mate`. The delivery
skill reads the child roles: the effective `roles` are also embedded in its
prompt. A role without a model inherits the controller's. A rejected child model
makes the skill retry with the controller model instead of blocking.

Fallbacks are opt-in and switch only after a structured provider error rejects
the model. Authentication, quota, timeout, cancellation and tool failures never
switch models.

### Provider fallback on usage limits (`[quota_fallback]`)

Model fallbacks never cross providers. To keep working when a provider reports a
usage limit, add a second provider profile. It is opt-in; without the section
Harbor waits for the reset exactly as before.

```toml
[quota_fallback]
max_switches = 2            # builder switches per run; default 2. 0 stops in-flight
                            # builder switches only: First Mate and new tickets
                            # still fall back. Remove the section to turn fallback off.

[quota_fallback.controller] # builder; planner/reviewer/implementor inherit it
provider = "codex"          # must differ from builder_provider in pipeline.md
model = "gpt-6.1-terra"
reasoning_effort = "medium" # required; must be valid for this provider

[quota_fallback.reviewer]   # optional per-role override (model / reasoning_effort)
model = "gpt-6.2-sol"

[quota_fallback.first_mate] # must differ from firstmate_provider in pipeline.md
provider = "codex"
model = "gpt-6-luna"
```

- **Two profiles, symmetric.** The primary profile is your roles plus the
  providers in `pipeline.md`; the fallback is this section. A run on either one
  that hits a usage limit moves to the other, unless that provider is also known
  to be out of usage.
- **Builder.** A run that ends on a usage limit, and is still waiting for its
  provider's reset, continues in a *fresh* session in the same worktree and
  branch. If the reset has already passed (for example Harbor was down across
  it), Harbor resumes the retained session in place instead, which keeps its
  memory. The old native session cannot move between
  providers, so the new session starts from a hand-off built from the ledger:
  ticket, PRs, open questions, operator answers not yet acted on, and the role
  models for the new provider. Conversation memory is not carried over. Each
  switch is recorded in `first_mate_recoveries` as `quota_fallback` (it does not
  spend First Mate's recovery budget), the operator is notified, and
  `/status --full` shows `switched <from> -> <to>`.
- **Stays until done.** A switched builder keeps its new provider until its run
  ends. At most `max_switches` switches happen per run, counted from the ledger
  so a restart does not reset it.
- **First Mate.** Its triage calls retry on the fallback profile after a quota
  error and use the primary again once the recorded reset passes.
- **New tickets** start on the fallback profile while the primary is known to be
  out of usage. Their launch prompt still lists the primary roles' models; a child
  agent whose model the provider rejects retries with the controller model, as the
  delivery skill already instructs.
- **Readiness.** A builder switch, and a new ticket starting on the fallback,
  happen only when that provider can run: the executable exists, and for Claude
  the CLI is logged in (`claude auth status`) and the project has no
  `playwright_browser_image`. Otherwise the builder keeps waiting and Harbor
  notifies once. First Mate's fallback call does not check readiness first; an
  unready fallback simply fails that call. `harbor doctor` prints each
  fallback's readiness.
- **Claude limits.** Claude Code builders allow only github.com, api.github.com,
  api.linear.app and registry.npmjs.org, and refuse `playwright_browser_image`.
- **Reload.** The fallback policy is read when Harbor starts; restart to apply
  edits. An invalid section stops startup instead of silently disabling it.

## Policy (`workflow.toml`)

| Section | Bundled default | Meaning |
| --- | --- | --- |
| `[delivery]` | `merge_mode = "human"` | `workflow_checked` allows merges for `auto_merge_risks` when `pre_merge` readiness passes |
| `[recovery]` | enabled, 30/120/300 s, 3 attempts | crash retries after an unexpected builder exit |
| `[decisions]` | `cite_only` | how First Mate answers blockers: `human`, `cite_only`, `bounded` |
| `[evidence]` | `ci = "required"`, `feedback = "advisory"` | what readiness gates on |
| `[cleanup]` | `after_merge` | worktree cleanup after an observed merge |
| `[hooks.*]` | readiness at `pre_handoff`/`pre_merge`, decision policy at `blocker` | script or skill hooks |
| `[checks.*]` | none | optional repository commands the skill runs with `harbor workflow checks` |
| `[github]` | `transport = "rest"` | how the builder reads and writes GitHub: `rest` (built-in helper) or opt-in `mcp` |

Readiness gates only on facts Harbor can verify itself: fresh evidence, hold
state, PR identity, mergeability and CI (plus feedback when set to `required`).
Review, risk and proof are the skill's job. Harbor does not gate on its own
receipts for them, and a human still merges by default.

`pipeline.md` keeps `unblock_attempts` and `decision_authors`. Decision mode
and crash-retry delays come from `workflow.toml`.

### Merge risk (`auto_merge_risks`)

With `merge_mode = "workflow_checked"`, `pre_merge` readiness also requires
the ticket's effective risk to be listed in `auto_merge_risks`. The effective
risk is the higher of two tiers:

- the **Linear floor**: a `risk:low|medium|high` label, or a `Risk: <tier>`
  (or `Risk tier: <tier>`) line in the ticket description; and
- the **PR analysis tier**: a line that is exactly `Risk tier: low`,
  `Risk tier: medium` or `Risk tier: high` inside the PR body's
  `## Risk Analysis` section, where that same section also carries
  `<!-- harbor:risk-sha:<current head SHA> -->`.

```markdown
## Risk Analysis
Risk tier: low
Rationale: documentation-only change; reverting is one commit.
<!-- harbor:risk-sha:0123456789abcdef0123456789abcdef01234567 -->
```

The analysis can only raise the Linear floor, never lower it, and Harbor keeps
the highest tier it has seen for the run, so a later edit cannot drop it. If
the analysis is missing, its marker names another commit, or it has no exact
`Risk tier:` line, the effective risk is unknown and the automatic merge fails
closed. Harbor does not interpret free text. The builder writes the analysis,
so list only the tiers you are willing to merge on the builder's own
assessment; the default `auto_merge_risks = []` and `merge_mode = "human"`
never auto-merge.

### GitHub transport (`[github]`)

The builder needs GitHub to open, update, comment on, check and (when the
workflow allows) merge its PR. Harbor does not rely on the GitHub CLI for this:
inside Claude Code's macOS sandbox `gh` cannot complete a TLS handshake and
hangs, while plain HTTPS to the API works. The builder is told to prefer the
configured transport; where `gh` does work (for example Codex, or Linux) it may
still use it, and with no token in the environment it is told to try `gh` before
reporting a blocker. Harbor tells the builder which transport it
has in the launch prompt.

```toml
[github]
transport = "rest"   # default; or "mcp" (Claude builders only)
# mcp_url = "https://api.githubcopilot.com/mcp/"   # only with transport = "mcp"
```

**`rest` (default, no extra setup).** The builder runs Harbor's helper,
`python -m harbor.github_rest`, which calls the GitHub REST API through `curl`.
Operations: `pr-view`, `pr-create`, `pr-edit`, `pr-comment`, `pr-checks`,
`pr-merge` (pinned to a head SHA) and `api METHOD /path` for anything else. It
uses the token you already export for Harbor (`GITHUB_TOKEN` or `GH_TOKEN`),
targets the project's `github_repository`, and derives the REST base from
`github_api_url` (`https://host/api/graphql` becomes `https://host/api/v3` on
GitHub Enterprise Server). The token is passed to `curl` on stdin, so it is never
on a command line, on disk or in output. Requests only go to that API host.

Set `github_api_url` (or its aliases `github_graphql_url` and `github_endpoint`)
in the project registry, as a column of the project's row, or in
`.harbor/pipeline.md`. A non-empty registry value wins over the pipeline file,
as `linear_api_url` does; an empty registry column is ignored. With neither,
Harbor uses `https://api.github.com/graphql`. A registry value goes through the
same checks as a pipeline value, so a malformed one stops `harbor start` and
fails `harbor doctor` for a Claude builder.

`github_repository`, `required_checks` and `bot_rework_cap` use the same
precedence: registry, then `.harbor/pipeline.md`, then the default, with an
empty registry column ignored (a bare registry repository name keeps the
pipeline's `github_owner`; a registry `bot_rework_cap` that is not a
non-negative integer is an error). The repository the REST helper targets
(`HARBOR_GITHUB_REPOSITORY`) is the one Harbor's GitHub poller reads, in
`harbor start`, `harbor poll`, `harbor doctor` and `harbor workflow check`.

**`mcp` (opt-in, Claude builders only).** Harbor passes the Claude builder a
GitHub MCP server and pre-approves only its tools; the Bash sandbox and its
network allowlist are unchanged. The server runs outside the Bash sandbox. The
`Authorization` header holds `${GITHUB_TOKEN}` (or whichever variable is set),
which Claude Code expands, so the token is never written into the command.
The default endpoint is GitHub's hosted server, which serves GitHub.com only and
is a dependency on that service; set `mcp_url` to a self-hosted
`github-mcp-server` to avoid it. On GitHub Enterprise Server `mcp_url` is
required, so an Enterprise token is never sent to the GitHub.com server. Codex builders
are refused at startup, because a non-interactive Codex run refuses MCP calls
that need approval. The REST helper stays available as a fallback.

Token scopes: the builder pushes branches and edits PRs, so a fine-grained token
needs repository **Contents** and **Pull requests** read/write, plus **Checks**
and **Commit statuses** read. `harbor doctor` prints the transport and which
variable the token comes from, and fails when GitHub is configured with no token.
Verified on macOS with Claude Code 2.1.330 and GitHub.com; Linux, Codex and
GitHub Enterprise Server use the same code path but were not run.

**Claude builder child agents.** The delivery skill spawns planner,
implementor and reviewer children. A Claude builder does this with Claude
Code's `Agent` tool, which Harbor always enables; there is nothing to
configure. Children run inside the builder's own Claude Code process, so they
get exactly the builder's tools (`Bash`, `Read`, `Glob`, `Grep`, `Skill`,
`Agent`), its `dontAsk` permission mode and the same Bash sandbox: the same
writable paths and the same network allowlist below. They cannot use a tool or
reach a host the builder cannot. Verified on macOS with Claude Code 2.1.332.

**Claude builder sandbox network.** A Claude builder's Bash commands (the REST
helper, `git push`, `curl`) can reach only an allowlist of hosts:
`github.com`, `api.github.com`, `api.linear.app` and `registry.npmjs.org`, plus
the hosts your project configures. Harbor adds the host of `github_api_url`
(on GitHub Enterprise Server, `https://ghe.example.com/api/graphql` adds
`ghe.example.com`, which serves both the API and git), the git host of an
`api.<host>` endpoint (`api.acme.ghe.com` also adds `acme.ghe.com`), and the
host of `linear_api_url`. Only the host of an `https` URL is used; a path, port,
wildcard, user name, IP address (including numeric forms such as
`0x7f.0x0.0x0.0x1`, or any host whose last label is a decimal or `0x` number) or
whitespace is never added. A malformed GitHub value stops `harbor start` and
fails `harbor doctor` instead of widening the sandbox. A `linear_api_url` the
allowlist cannot express (for example a local `http://127.0.0.1:8080/graphql`
stub) is left out with a warning from `harbor start` and a
`Linear sandbox access: not added (...)` line in `harbor doctor`; Harbor itself
still reaches it, but builder Bash commands cannot. A GitHub.com project gets exactly the default list. `harbor doctor`
prints the list as `Builder sandbox network:` for Claude builders; it shows
what the sandbox allows, not that the host answers. A git remote on a host
other than the API host (for example SSH through a separate hostname) is not
added. A Claude builder therefore cannot use a GitHub Enterprise Server that is
addressed by an IP address or a single-label name such as `https://github/`;
Harbor refuses it instead of guessing, so give the server a DNS name. Codex
builders are unaffected, since their sandbox does not filter by host, and these
rules are applied only to Claude builders, including a retained Claude session
on a project since switched to Codex. `harbor start` and `harbor doctor` do not
check such a session, so when it is resumed Harbor prints the same Linear
warning, and if the GitHub value stops it, a `Harbor: warning: the retained
Claude builder session for <ticket> cannot resume: ...` line (once per process);
the session stays unresumed until `github_api_url` is fixed and Harbor is
restarted. Harbor reads the GitHub URL (`github_api_url`, or its aliases
`github_graphql_url` and `github_endpoint`) and the Linear URL
(`linear_api_url`, or `api_url`) for the sandbox once, when `harbor start`
builds its dispatcher (only the workflow package is reloaded per poll). If you
change either one, restart Harbor: a running `harbor start` keeps the old value
and, having warned once, prints nothing more. The warnings from `harbor start`
and from a resumed session say so; `harbor doctor`, which reads the current
value each run, does not. A deliberate local Linear stub needs no change. A configured DNS name is trusted as-is:
whoever can set `github_api_url` (the registry, `.harbor/pipeline.md` or
`AGENTS.md` in the main checkout) already decides where the GitHub token is
sent, so allowing that host gives them nothing new. Verified with unit tests and the production runner factory on macOS
(generated `--settings` for GitHub.com and a GitHub Enterprise Server config);
not run against a real GitHub Enterprise Server, a custom Linear endpoint, or
on Linux.

**Linked PRs.** Harbor finds a ticket's PRs from its Linear GitHub attachments,
and counts a link only when it is a PR of the project's `github_repository` on
the web host of `github_api_url`: `github.com` for the default endpoint,
`<sub>.ghe.com` for `https://api.<sub>.ghe.com/graphql`, and `<host>` (with any
port) for GitHub Enterprise Server's `https://<host>/api/graphql`. A link to any
other host is ignored, so Harbor never queries or acts on it. Nothing new to
configure.

A builder can open, attach and merge its PR and move the ticket to `Done` in
one turn. The ticket then leaves the Linear poll before Harbor has seen the
attachment. For a completed `outcome=merged` run that Harbor has not yet
verified, normal supervision re-reads that ticket's attachments from Linear, so
it can still verify the merge on GitHub and remove the worktree. This read
happens at most 3 times per run in each supervisor process, at least 60 seconds
apart. The builder's `pr_url` counts only if Linear lists it as an attachment.

### Editing mid-run

Editing `.harbor/workflow/` while a ticket is in flight never blocks it. The
next builder launch reloads the copy and renders its prompt from it, so the new
models and policy (including `merge_mode`) reach the builder, and Harbor records a `policy_change` receipt. There is no
`reconcile` step.

## Hooks and checks

Hooks run at `setup`, `blocker`, `pre_handoff`, `pre_merge` and `pre_cleanup`.
Each hook is `required` (blocks), `advisory` (records failures) or `off`. Script
hooks receive versioned JSON on stdin and return:

```json
{"status":"pass","reason":"requirements satisfied","evidence":["artifact reference"]}
```

Commands are argument arrays, never shell strings. Timeouts terminate the
process group, and JSON output is capped at 64 KiB. Skill hooks use First
Mate's model.

```sh
harbor workflow checks Demo --ticket DEM-1 --parallel
harbor workflow check Demo --ticket DEM-1 --event pre_handoff
harbor workflow check Demo --ticket DEM-1 --event pre_merge
```

## Child agents

The delivery skill spawns planner, implementor and reviewer children with the
native collaboration tools. Before spawning a role it lists the existing
agents. It resumes a live one; an interrupted or errored one is closed and the
role is respawned under a fresh name. A name collision is never a blocker.
Harbor does not register or verify child identities.

## First Mate: monitor and recover

Each poll, First Mate looks at:

- blocked runs older than `recheck_minutes`;
- running builders with no activity for `stall_minutes`;
- failed or orphaned runs whose crash retries are spent;
- `watchdog.flag` events from `harbor watch`.

It returns one action, and Harbor validates and executes it:

| Action | What Harbor does |
| --- | --- |
| `resume` | Resumes the retained session with First Mate's concrete instruction |
| `retry` | Restarts a failed/orphaned builder in its retained session |
| `fresh_session` | Starts a new builder session in the same worktree with a hand-off summary (at most once per run) |
| `wait` | Checks again later |
| `escalate` | Notifies the operator (once) |

Guardrails:

- Product, design, ambiguous-AC and conflicting-instruction blockers, and
  anything touching security, auth, billing, privacy, deletion or migrations,
  always escalate.
- Each run gets `max_recoveries` attempts, re-checked every `recheck_minutes`.
  Only a model usage limit backs off exponentially (30, then 60 minutes).
- A spent budget escalates once and never loops.

- Watchdog flags get the same eligibility checks: a held or closed ticket is
  never restarted (cleaning up a merged ticket is the one exception).
- A supervisor started with `--ticket` only triages that ticket and leaves other
  tickets' flags for the supervisor that owns them.

Recoveries run as background tasks (`harbor-recovery-<ticket>`), one per
ticket, so polling and other tickets keep moving while a builder recovers. A
stalled live builder is stopped through the normal cancel path (process group
killed, run recorded as terminated) before its replacement starts. A fresh
session receives the full launch prompt (delivery skill, current policy
including merge authority, runtime invocation) followed by First Mate's
hand-off.

Every action posts a `[First Mate recovery]` Linear comment, notifies the
operator and is stored in `first_mate_recoveries`. The cite-or-escalate ladder
for ticket questions is unchanged. A verified merge resolves that run's open
escalations, not those of newer runs on the same ticket.

## Progress watchdog

```sh
harbor watch Demo --minutes 30 --interval 60
```

The watchdog is a separate process. It reads the supervisor heartbeat
(`~/.harbor/heartbeat-<project>.json`) and the ledger, and flags:

- `no_progress`: an active run with no new commit and no new receipt for N minutes;
- `repeated_answer`: the same question answered, or the same comment
  re-delivered, three or more times;
- `cleanup_stuck`: a merged run whose worktree is still present after N minutes;
- `supervisor_stale`: the heartbeat stopped.

Flags are `events` rows with `event_type = 'watchdog.flag'`. First Mate handles
them on the next poll. Stuck cleanups are retried deterministically, with no
model call. The watchdog never touches worktrees, Linear or GitHub.

## Recovery, handoff and cleanup

Console `/answer` persists an answer and schedules same-session work.
Standalone `harbor answer` queues delivery for the project supervisor. Operator
stops are never automatically retried. Holds, inactive states and exhausted
budgets block automatic recovery.

Cleanup requires an independently observed merge, a completed delivery, a
terminal Linear state, the exact local/merged PR head, stopped processes, no
open escalation, no hold and passing `pre_cleanup` hooks. Modified or untracked
files keep the worktree for inspection; git-ignored install and build output
(`node_modules/`, `.next/`, ...) is removed with it. The branch is never deleted.
All ignored files go, including any unique ignored content (local notes,
generated `storage/` data, `.env` copies): keep anything you need outside the
worktree or commit it.

## Migrating

Older setups used `[workflow] package/preset` in `.harbor/workflow.toml`, plus
model and `workflow_path` keys in `pipeline.md`. Harbor now rejects those with a
link to this section.

1. Let in-flight tickets finish, or stop the supervisor.
2. Copy the bundled package: `cp -R <harbor>/workflows/work-pipeline <repo>/.harbor/workflow`.
3. Move each `[roles.<name>]` model and effort from the old `.harbor/workflow.toml`
   into `<repo>/.harbor/workflow/config.toml` as `[<name>]`. Move
   `firstmate_model` from `pipeline.md` into `[first_mate] model`.
4. Move `[checks.*]`, and any policy you had changed (`[delivery]`, `[recovery]`,
   `[decisions]`, `[evidence]`), into the copied `workflow.toml`. `[review]`
   and the evidence `risk`/`proof` keys no longer exist.
5. Delete `<repo>/.harbor/workflow.toml`. Remove `workflow_path`, `skill`,
   `builder_model`, `firstmate_model`, `*_effort`, `*_fallback_models`,
   `recovery_delays` and `firstmate_decisions` from `pipeline.md`.
6. Run `harbor doctor <project>` and `harbor workflow inspect <project>`, then
   commit `.harbor/workflow/`.

Retained runs continue: the first turn after migration records a
`policy_change` receipt instead of blocking.

## Delivery runtime binding

Managed delivery prompts include a shell-quoted invocation that pins the
resolved project registry, the operational ledger, the Python interpreter and
the Harbor import path. Workflow commands and child agents use that invocation
with the supplied project and ticket. The native sandbox includes the ledger
directory for operational receipts; the registry remains read-only.

**Claude builder permissions.** Claude builders run in Claude Code's
`dontAsk` permission mode, which refuses any tool call that is not
pre-approved. Sandboxed Bash is approved automatically, except a command that
sets `PYTHONPATH` or assigns a variable in front of an absolute executable path
(directly or through `/usr/bin/env`), which is exactly what the runtime
invocation does; Read, Glob and Grep only reach the ticket worktree,
and the workflow package lives in your clone outside it. So Harbor pre-approves
two things, and nothing else:

- Bash commands that start with the exact runtime invocation from the prompt
  (`Bash(<invocation>:*)`). They still run inside the OS sandbox, with the same
  writable directories and network allowlist.
- Reading the project's workflow package (`Read(//<repo>/.harbor/workflow/**)`),
  so the builder can open `skills/*/SKILL.md`, `scripts/` and `config.toml`.

No setup is needed. `harbor doctor` prints them as `Builder permissions:` for a
Claude project, and fails if a path contains a wildcard (`*`, `?`, `[`, `]`) or
a line break, which a rule cannot match literally; rename that directory.
Codex builders need neither: their
`workspace-write` sandbox reads anywhere and has no per-command approval.
Verified on macOS with Claude Code 2.1.332 by running the generated command
against a throwaway repository and worktree; not run on Linux.

## Verification map

| Area | Primary files | Scenario tests |
| --- | --- | --- |
| Project workflow and models | config/workflow.py, config/projects.py, onboard.py | test_workflow_config.py, test_onboard.py, test_runtime_resources.py |
| Readiness, hooks, checks | readiness.py, hooks.py, checks.py, workflow_cli.py | test_readiness.py, test_hooks.py, test_parallel_checks.py, test_workflow_fresh_context.py |
| First Mate recovery | first_mate_recovery.py, supervisor.py | test_first_mate_recovery.py |
| Watchdog | watchdog.py, db.py | test_watchdog.py |
| Bounded decisions | unblock.py, decision_policy.py, risk_evidence.py | test_decision_policy.py, test_blocker_recovery.py, test_workflow_evidence.py |
| Cleanup | workflow_lifecycle.py, worktree.py | test_workflow_cleanup_policy.py, test_merge_cleanup.py |

Run `uv run pytest`, `uv run python -m compileall -q src tests` and `uv build`.
The tests use mock external tickets and real local subprocesses and Git
worktrees. They are not evidence of a live Linear-to-merge run.
