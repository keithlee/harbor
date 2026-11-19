---
name: harbor-delivery
description: Deliver the supplied ticket end to end with the project's work-pipeline.
---
# Delivery workflow
Work only on the supplied ticket, repository and retained worktree. Linear and GitHub are authoritative.
On continuation reconcile partial edits, the existing PR and current comments; do not insist the ticket is still Todo.
Do not create a separate ticket or select another Todo on resume.
Read the project's AGENTS.md and Pipeline Profile. Never invent install, proof or project settings.

## Roles and models
Models come from `config.toml` in this workflow package (mirrored in the policy's `roles`).
Spawn `planner`, `implementor` and `reviewer` children with their configured model and reasoning effort.
A role without a model uses the controller's. Never block on a model setting: if the adapter rejects a
model, retry the child with the controller model and note it in the run result.

## Child agents (recover, never collide)
Use the native collaboration tools (spawn, list, wait, resume, close).
1. Before spawning any role, list the existing agents.
2. If the role's agent exists and is running or idle, resume or message it instead of spawning.
3. If it exists but is interrupted, errored or unknown, close it, then spawn a fresh child under a new
   name (for example `planner-2`). A name collision is never a blocker.
4. Before ending any turn, close children you no longer need; never leave an interrupted child behind.
Keep one planner, one implementor and one reviewer per ticket; the reviewer must not be the implementor.

## Flow
1. Plan: the planner writes numbered acceptance criteria and a test plan to the Linear ticket (`skills/planning`).
2. Implement: the implementor makes the smallest safe change and runs the repository's checks.
   Use `harbor workflow checks PROJECT --ticket IDENTIFIER --parallel` when `[checks]` are configured.
3. Review: a separate reviewer reviews the current head (`skills/review`) and posts its verdict on the PR.
   Return fixes to the same implementor; stop after three failed rounds and report a blocker.
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
model settings. Include concrete options and a recommendation. First Mate monitors blocked runs
and may resume you with an instruction or start a fresh session.

Return the run-result schema with the actual PR URL, status, waiting_on and a concrete next_action.
Stop child agents and commands before reporting completion. Harbor owns worktree cleanup; never force-remove it.
Do not print credentials. Disabled requirements are not passes.
