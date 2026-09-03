# Harbor

Harbor is a calm, deterministic supervisor for one configured project. Linear
and GitHub are observed by the pollers; the project's delivery skill owns
implementation and external mutations.

## Project configuration

The registry in [`projects.md`](projects.md) maps a project name to its local
checkout, live pipeline document, Linear project slug, GitHub repository, and
base branch. The Bookgen entry is intentionally exact:

- Linear project: `hanzireader-06c91e632f50`
- GitHub repository: `keithlee/Bookgen`
- Base branch: `main`
- Pipeline: `~/Projects/Bookgen/.harbor/pipeline.md`

Copy [`templates/pipeline.md`](templates/pipeline.md) into the target
repository as `.harbor/pipeline.md`. The template uses the builder
`gpt-5.6-luna` at `xhigh`, First Mate `gpt-5.6-terra` at `xhigh`, the opaque
`/work-pipeline` skill, a 60-second heartbeat, and a shadow hold. `skill` is
passed through as data; Harbor never imports or rewrites it.

Harbor requires the live pipeline path. If it is missing, project resolution
returns an actionable error naming the expected file and explicitly refuses to
fall back to `AGENTS.md`.

## Same-terminal console

`harbor start <project>` opens a persistent `first-mate>` console. `/status`,
`/pause`, `/hold`, `/resume`, `/answer`, `/kill`, `/exit`, and `/help` are
behavioral commands and do not invoke a model. Plain text is the only path that
asks First Mate a question; it receives the same deterministic digest rendered
by `/status`.

`/hold` currently uses the supervisor pause hook when no dedicated host hook is
provided. `/kill` looks for a supervisor, dispatcher, or runner termination
hook; if none is configured it reports that integration seam instead of
pretending a builder was killed.

## Integration seams

The CLI assembly still needs to wire the pipeline fields into the live worker:
construct the worktree manager with `base_branch` and `worktree_root`, pass
`deadline_minutes * 60` to the builder runner, and invoke the opaque `skill`
with the configured builder model. The CLI should also pass
`github_repository`, `required_checks`, and `bot_rework_cap` to the GitHub
profile/decider. A host that wants `/kill` to terminate a builder should expose
`kill`/`terminate` on the supervisor, dispatcher, or runner; `/hold` may expose
a dedicated `hold` hook. These are intentionally outside the config/console
layer.
