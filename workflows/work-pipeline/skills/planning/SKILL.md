---
name: harbor-planning
description: Prepare acceptance criteria and a testable plan for the supplied Harbor ticket.
---
Read the existing ticket, project constitution and relevant code. Add numbered,
observable acceptance criteria and targeted tests to the same ticket. Include
adversarial cases, affected files and the smallest safe implementation. Identify
missing authority with options and evidence instead of inventing requirements.
Use the planner model from config.toml. On continuation resume the existing planner; if it is gone
or interrupted, close it and spawn a fresh one with the plan already on the ticket. Claude Code cannot
resume or close children, so it always spawns a fresh planner with the plan already on the ticket.
