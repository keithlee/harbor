---
name: harbor-delivery
description: Deliver the supplied ticket using the effective Harbor project policy.
---
# Retained delivery workflow
Work only on the supplied ticket, repository and retained worktree. Linear and GitHub are authoritative.
On continuation reconcile partial edits, existing PR and current comments; do not insist the ticket is still Todo.
Read the project's AGENTS.md and Pipeline Profile. Never invent install, proof, project, or model settings.
Implement the acceptance criteria and run configured checks. Do not change the policy to approve your own work.
Use configured planning/review skills when enabled. Record actual spawn IDs; preserve the same reviewer and implementor.
Wait for all repository writers and required checks before review. Review is independent only with verified distinct identity and current-head evidence.
If the adapter cannot provide role provenance or resume the retained agent, return a capability blocker.
Before handing off or merging, invoke the configured Harbor workflow event for the current ticket. Required failures block.
Run: harbor workflow check PROJECT --ticket IDENTIFIER --event pre_handoff
Before any merge run: harbor workflow check PROJECT --ticket IDENTIFIER --event pre_merge
Human mode never merges. Workflow-checked mode may merge only when the fresh readiness result permits; use the expected head SHA.
Reconcile current Linear and PR feedback before delivery. Only after observed merge may the delivery workflow set terminal Linear completion.
Return the run-result schema with actual PR URL, status, waiting_on and concrete next_action.
Stop child agents and commands before reporting completion. Harbor owns local worktree cleanup; never force-remove it.
Do not print credentials. Disabled requirements are not passes. These gates are cooperative, not credential enforcement.

Keep one persistent planner/reviewer and one implementor, using configured roles/models. The planner writes the plan to the existing Linear ticket. Return all fixes to the same implementor and reviews to the same reviewer; stop after configured failed rounds. Do not create a separate ticket or select another Todo on resume.

Load enabled skill references relative to the trusted package path in your prompt.
For configured command groups use `harbor workflow checks PROJECT --ticket IDENTIFIER --parallel`
when the commands are independent; `after` dependencies are enforced by Harbor.
After spawning a child, register its real returned ID with
`harbor workflow role PROJECT --ticket IDENTIFIER --role implementor --agent ID`
or `--role reviewer`. Use `harbor workflow roles PROJECT --ticket IDENTIFIER`
to recover retained bindings before any resume or wait. Never wait on the controller ID.
If review is enabled, assign implementation to the registered implementor and the
review skill to the distinct registered reviewer. Pass configured role model and
reasoning effort to the adapter; if that adapter cannot honor them, report a capability blocker.
The reviewer must finish with the JSON verdict contract in the review skill.
For every Linear comment needing disposition, post your reply with the marker
returned in readiness's `linear_feedback` field. New or edited comments invalidate old markers.
Respect auto_merge_risks; bundled presets permit only low-risk automatic merging. Resolving a wording decision never lowers ticket risk.

Roles without a model override inherit the current controller model. Do not substitute a named model from personal defaults.
