# Provider fallback on usage limits — design

Status: draft for approval. Approach A of three considered (see Approaches).

## Intent and why

When a provider (Codex or Claude) reports a usage limit, Harbor today waits for
the reset. On 2026-11-19/20 that stalled HAN-87 for hours: the builder died on
the Codex limit and First Mate, running on the same quota, could only record
"wait" five times. Harbor already supports both providers, but a project picks
one statically. This change lets Harbor continue on the other provider when the
current one is out of usage, for the in-flight builder and for First Mate.

## Goals

- A builder that ends `usage_limit_exceeded` continues on the other provider in
  the same worktree and branch, without waiting for the reset.
- First Mate triage keeps working when its provider is out of usage.
- New tickets start on a provider that is not known to be exhausted.
- Every switch is recorded (ledger receipt, operator notice, status line).
- Opt-in. With no `[quota_fallback]` section Harbor behaves exactly as today.

## Non-goals and anti-goals

- No moving a native session between providers. A switch is a fresh session
  with a hand-off; conversation memory is not carried over.
- No automatic move of a running fallback builder back to the primary provider.
  It stays until its run ends. Only First Mate calls and new tickets return.
- No cost or budget controls, no chains of three or more providers.
- Does not change the existing model-rejection `fallback_models` behaviour.
- Anti-goal: ping-pong. A run must never bounce between two exhausted providers.
- Anti-goal: silently degrading delivery safeguards. Merge authority, review and
  proof rules are the workflow's, unchanged by which provider runs them.

## Constraints

- AGENTS.md: SQLite is operational custody only; no credentials stored or
  printed; configuration fails closed; same worktree across re-engagement
  unless First Mate recovery deliberately starts a fresh session (this is that).
- Protected paths are not touched: no migrations, schemas, `clients/`,
  `worktree.py`. New tables use the existing `CREATE TABLE IF NOT EXISTS`
  pattern from `recovery.py`.
- A retained builder stays with the provider that owns its session
  (`bind_runner_identity` raises on mismatch). The switch is the only path
  allowed to rebind, and only through a recorded fresh session.
- Claude headless builders refuse `playwright_browser_image` and allow only
  github.com, api.github.com, api.linear.app and registry.npmjs.org.
- Risk budget: at most 50 files and 1500 added lines; target well under both.

## Approaches considered

- **A. Quota-triggered fresh session on the same run (chosen).** Extend the
  existing `fresh_session` recovery; rebind runner identity inside it; add a
  First Mate provider fallback. Smallest change, reuses tested paths. Cost: one
  run id carries two providers, so the audit trail rests on the receipt.
- **B. New run id for the continuation.** Cleaner history, but run ids are tied
  into PR events, escalations, answers and worktree custody. Much wider and
  riskier.
- **C. Provider pool.** One runner picking a provider from quota state. Most
  general, but a rewrite of the runner layer for a two-provider problem.

## Design

### Config (opt-in, per project, in `workflow/config.toml`)

```toml
[quota_fallback]
max_switches = 2            # per run; default 2, 0 disables

[quota_fallback.controller] # builder; planner/reviewer/implementor inherit it
provider = "codex"
model = "gpt-6.1-terra"
reasoning_effort = "medium"

[quota_fallback.first_mate]
provider = "codex"
model = "gpt-6-luna"
```

Two provider profiles exist per project: the primary (today's roles plus
`builder_provider` / `firstmate_provider` in `pipeline.md`) and the fallback
above. A run on one profile that hits a usage limit moves to the other. The
fallback provider must differ from the primary; models must suit the provider
(no `gpt-` model for Claude); effort is validated against the fallback
provider, and the controller profile must set `reasoning_effort` (the primary's
effort is never reused on the other provider). Invalid config
fails at load, naming the key.

### Provider quota state

A small table `provider_quota(provider PK, exhausted_until, observed_at,
confirmed)` is written wherever `defer_quota` already runs and cleared where a
run of that provider completes successfully (the same place `quota_recovery`
rows are deleted). `provider_exhausted(provider, now)` answers "known out of
usage until when". This is provider-level; `quota_recovery` stays per-run.

### Builder switch (deterministic, no model call)

In the supervisor's existing quota branch (a failed run with terminal reason
`usage_limit_exceeded`), before the usual wait:

1. Fallback configured, switches used for this run < `max_switches`, and the
   target provider is not itself exhausted.
2. Target readiness check passes (executable present; for Claude, logged in and
   no `playwright_browser_image`). If not ready: keep waiting and notify the
   operator once with the reason.
3. Call `fresh_session(ticket, handoff, provider=target)`, which rebinds
   runner identity to the target provider, model and effort, clears the old
   native session id, and starts the new session in the retained worktree.
4. Record a `first_mate_recoveries` row (`action=fresh_session`,
   `reason="quota_fallback: <from> -> <to>"`), notify the operator, and bump
   the run's switch count.

If the target is exhausted too, or the budget is spent, behaviour is today's:
wait for the reset on the run's own provider.

The hand-off is built from the ledger, not a model: ticket, branch, worktree,
PR URLs, open and answered escalations (including operator answers not yet
delivered), the last run summary, and the standard instruction to re-read
Linear and GitHub, reconcile partial work, and not create another worktree or
PR. The launch prompt (delivery skill, policy, roles) is rendered with the
target profile's roles so child agents use the target provider's models.

### First Mate fallback

`FirstMate._generate` tries its primary provider. On a quota error it records
the exhaustion, then runs the same prompt through the fallback profile's
read-only command and model. Later calls skip the primary until its recorded
reset passes, then try it first again. A `workflow_receipts` row
(`kind=model_fallback`, `reason=usage_limit`) records each switch.

### New tickets

The runner factory starts a new ticket on the primary profile unless
`provider_exhausted(primary)` and the fallback is ready, in which case it
starts on the fallback.

### Visibility

`harbor status` and the digest show "on <provider> (<primary> limit until
<time>)" for switched runs. `harbor doctor` reports fallback readiness (config
valid, executables found, Claude login) so problems surface before a limit.

## Files that change

- `src/harbor/config/workflow.py`: parse and validate `[quota_fallback]`.
- `src/harbor/recovery.py`: `provider_quota` helpers.
- `src/harbor/runner/base.py`: record and clear provider quota at the two
  existing quota sites.
- `src/harbor/runner/quota.py`: `rebind_runner_identity`.
- `src/harbor/supervisor.py`: switch in the quota branch; `fresh_session(...,
  provider=)`; hand-off builder; notices.
- `src/harbor/cli.py`: factory provider choice for new and switched runs;
  `doctor` checks.
- `src/harbor/first_mate.py`: fallback target in `_generate`.
- `src/harbor/first_mate_recovery.py`: hand-off text and history recording.
- `src/harbor/digest.py`: status line.
- `tests/`: new files below. `README.md`, `docs/configurable-workflows.md`:
  document the section and its limits.

## Tests

- Config: valid, absent (no behaviour change), same provider as primary, wrong
  model family, bad effort, unknown keys.
- Provider quota: record, expire, clear on success.
- Builder switch: failed-on-quota run switches once; blocked when target is
  exhausted; blocked when not ready (notify once); `max_switches` honoured; no
  ping-pong; run on the fallback that hits a limit moves back to the primary;
  identity rebound only through the switch; hand-off includes queued answers.
- First Mate: falls back on quota, returns after reset, receipt written.
- Factory: new ticket starts on fallback when primary is exhausted.
- Existing quota, recovery and First Mate suites stay green.

## Edge cases and risks

- Two providers both exhausted: wait on the earlier reset; never switch.
- Hand-off loses conversation memory: mitigated by ledger state, PR and Linear
  being the source of truth; the delivery workflow already re-reads them.
- Claude builder compatibility with the Codex-written role-tracking is
  unverified; first live switch to Claude needs watching. Not solved here.
- A switch right after a Harbor restart: switch count and provider quota live
  in SQLite, so they survive restarts.
- Race with the existing First Mate "wait" and the answer queue: the switch
  runs inside the per-ticket recovery guard (`_recovery_tickets`); the queued
  answer drains to the new session afterwards.
- Config reload: `workflow.reload()` runs on every runner build, so a config
  edit takes effect on the next runner; documented.

## Left to the builder

Exact table and function names, hand-off wording, digest formatting, and how
`workflow.for_provider(...)` renders roles into the launch prompt.

## My calls (not yours until you accept them)

- Fallback is symmetric (either direction), because the Bookgen primary is now
  Claude and the stalled HAN-87 run is Codex-bound.
- `max_switches` defaults to 2 per run.
- New tickets also start on the fallback when the primary is known exhausted.
- Switching is deterministic supervisor code, not a First Mate model decision,
  because First Mate's model may be the thing that is out of usage.
- No fallback values are baked in; the section is explicit or absent.

## Implementation notes (added while building)

- The ledger receipt uses `action='quota_fallback'`, not `fresh_session`, so a
  switch neither spends First Mate's recovery budget nor uses up its one
  legitimate fresh session. `Case.attempts` ignores it.
- The policy is built once at startup (restart to reload); an invalid section
  fails startup. Roles used in a switched session's hand-off come from the
  target profile. A new ticket started on the fallback still lists the primary
  roles in its launch prompt (documented limitation).
- Optional `[quota_fallback.<role>]` tables override child-role models.
- Found on the first real HAN-87 check: the switch only happens while the run is
  still waiting for its provider's reset (`quota_recovery.next_retry` in the
  future). After the reset has passed, the retained session resumes in place, so
  a restart after downtime does not discard a session whose provider is back.
