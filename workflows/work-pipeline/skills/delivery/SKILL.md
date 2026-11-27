---
name: harbor-delivery
description: Deliver the supplied ticket end to end with the project's work-pipeline.
---
# Delivery workflow
Work only on the supplied ticket, repository and retained worktree. Linear and GitHub are authoritative.
On continuation reconcile partial edits, the existing PR and current comments; do not insist the ticket is still Todo.
Do not create a separate ticket or select another Todo on resume.
Read the project's AGENTS.md and Pipeline Profile. Never invent install, proof or project settings.
For GitHub reads and writes use the transport named in your launch prompt ("GitHub transport: ...");
prefer it over the `gh` CLI, which cannot reach GitHub from inside Claude Code's sandbox on macOS.

## Roles and models
Models come from `config.toml` in this workflow package (mirrored in the policy's `roles`).
Spawn `planner`, `implementor` and `reviewer` children with their configured model and reasoning effort.
A role without a model uses the controller's. Never block on a model setting: if the adapter rejects a
model, retry the child with the controller model and note it in the run result.

## Child agents (recover, never collide)
Use your own runtime's child-agent tools; never shell out to another agent CLI for children.

Codex: use the native collaboration tools (spawn, list, wait, resume, close).
1. Before spawning any role, list the existing agents.
2. If the role's agent exists and is running or idle, resume or message it instead of spawning.
3. If it exists but is interrupted, errored or unknown, close it, then spawn a fresh child under a new
   name (for example `planner-2`). A name collision is never a blocker.
4. Before ending any turn, close children you no longer need; never leave an interrupted child behind.

Claude Code: spawn each child with the `Agent` tool and always pass `run_in_background: false`. If you leave
it out, the child runs in the background and your turn can end before it returns, so you would hand off
without its result. A foreground call waits and returns the child's result, and a finished child needs no close. There is no list, resume, message or close: track the
children you started in this session yourself. Wherever this workflow says to resume, message or return
work to an existing child, spawn a fresh child for that role instead and give it the earlier child's
report plus the current ticket, PR and review state. In a new session with no earlier child report, give
it the PR and its review comments instead.

Keep one planner, one implementor and one reviewer per ticket at a time; a respawned child (Codex
`planner-2`, or a fresh Claude Code child) replaces the earlier one for its role. The reviewer must not be
the implementor.

## Flow
1. Plan: the planner writes numbered acceptance criteria and a test plan to the Linear ticket (`skills/planning`).
2. Implement: the implementor makes the smallest safe change and runs the repository's checks.
   Use `harbor workflow checks PROJECT --ticket IDENTIFIER --parallel` when `[checks]` are configured.
3. Review: a separate reviewer reviews the current head (`skills/review`) and posts its verdict on the PR.
   Return fixes to the same implementor (Claude Code: a fresh implementor given the reviewer's findings,
   the current PR head and the previous implementor's report); stop after three failed rounds and
   report a blocker. The count is the larger of the rounds you counted in this session and the
   rounds on the PR, so it survives a fresh session. On the PR, each reviewer JSON verdict comment with
   `"verdict": "reject"` is one failed round, even when it rejects the same head again. Count only
   comments whose `head_sha` is a commit on the PR, and ignore every other comment, such as a human "FAIL".
4. Risk: add the `## Risk Analysis` section to the PR body (`skills/risk`).
5. Hand off: run `harbor workflow check PROJECT --ticket IDENTIFIER --event pre_handoff`, reply to the
   `linear_feedback` it returns, then move the ticket to review.
6. Merge only when `delivery.merge_mode = "workflow_checked"` and
   `harbor workflow check PROJECT --ticket IDENTIFIER --event pre_merge` reports ready; use its head SHA.
   Human mode never merges. Only after an observed merge may you set terminal Linear completion.

## Merge authority (Harbor setting wins)
`delivery.merge_mode` in this workflow is the only merge authority. It overrides any merge or
auto-merge policy in the repository's AGENTS.md, CLAUDE.md, Pipeline Profile or standing
authorizations. With `merge_mode = "human"`: never run `gh pr merge`, never enable auto-merge,
never push to the base branch. Stop at the PR, move the ticket to review, and return
`outcome = "parked"` with `waiting_on = "human"`.

## Blockers
Report a blocker only for a real product/design question, ambiguous acceptance criteria, or an
environment failure you cannot work around. Never block on Harbor bookkeeping, agent names or
model settings. Include concrete options and a recommendation, and ALWAYS set
`recommended_option_index` to the zero-based index of the option you recommend (`0` is the first
option; `null` for none). Harbor decides from that index, and the prose can only veto it: when it
is `0`, Harbor may accept the first option automatically and resume you with it as the answer, but
only for a `product_decision` or `ambiguous_ac` blocker
whose first option and recommendation use only letters, spaces and `, . ' " ! ? ( )`, with no
identifier-shaped words; the exact rules are in Harbor's `docs/configurable-workflows.md`
("Auto-accepting the builder's first option"). Put a costly,
destructive or access-related choice anywhere but first, since Harbor's keyword checks for those
match words, not meaning. Keep `recommendation` short and plain, for example "Take option 1
because it matches the ticket criteria"; conditions, caveats or mentions of other options send the
question to a human. An auto-accepted answer does not authorize a merge, so `delivery.merge_mode`
still decides merges.
First Mate monitors blocked runs and may resume you with an instruction or start a fresh session.

Return the run-result schema with the actual PR URL, status, waiting_on and a concrete next_action.
Stop child agents and commands before reporting completion. Harbor owns worktree cleanup; never force-remove it.
Do not print credentials. Disabled requirements are not passes.
