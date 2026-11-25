# Harbor projects example

This checked-in file is an empty format example, never a source of real user
configuration. Create a private registry and select it with
`HARBOR_PROJECTS_FILE`. Paths may use `~` or environment variables. Every
runnable project must point at its live pipeline; Harbor fails closed instead
of silently reading `AGENTS.md` or inheriting another product's identity.

| name | repo_path | pipeline_path | linear_project_slug | linear_team | github_repository | base_branch |
| --- | --- | --- | --- | --- | --- | --- |

Optional endpoint columns are `linear_api_url` (alias `api_url`) and, for
GitHub Enterprise Server, `github_api_url` (aliases `github_graphql_url` and
`github_endpoint`; for example `https://ghe.example.com/api/graphql`). For
these endpoint columns, a non-empty cell wins over the same setting in the
project's pipeline, and an empty cell is ignored: Harbor uses the pipeline's
value, then the default (`https://api.linear.app/graphql` or
`https://api.github.com/graphql`).

The GitHub columns `github_repository` (alias `github_repo`), `required_checks`
(alias `ci_required_checks`, a list such as `[CI, "Lint, typecheck"]`) and
`bot_rework_cap` (alias `bot_rework_rounds`) follow the same rule: a non-empty
cell wins over the pipeline, an empty cell is ignored, and then the pipeline's
value or the default applies. A `github_repository` cell without an owner (a
bare name) keeps the owner the pipeline sets with `github_owner`. A
`bot_rework_cap` cell that is not a non-negative integer stops Harbor with an
error instead of being replaced by the default.

Harbor resolves the repository and required checks once per project (the
pipeline profile). The builder's `HARBOR_GITHUB_REPOSITORY`, the GitHub poller,
PR-event processing, `harbor workflow check` and `harbor doctor` all read that
resolved value. The supervisor's bot-rework decision reads `bot_rework_cap`
from the project configuration, which applies the same precedence and accepts
the same spellings.

## How other registry values are parsed

Every registry cell is text. Harbor parses the remaining columns the way it
parses the same setting in a pipeline file, with one difference: a value it
cannot read stops Harbor with an error naming the project, the column and the
value, instead of falling back to a default.

- **Empty cells are ignored** in every column. An empty cell never sets a
  value, so the pipeline's value, then the default, applies. A column added for
  one project can stay blank for the others.
- **`hold` and `hold_all`** take `true`/`false`, `yes`/`no`, `on`/`off` or
  `1`/`0`, in any case. A non-empty cell wins over the pipeline's `hold`. If
  both columns are filled, `hold` wins, as in the pipeline. Any other value
  (for example `maybe`) is an error. An empty, missing or unreadable cell never
  releases hold.
- **`poll_states`, `dispatch_states` and `linear_projects`** are lists, such
  as `[Todo, In Progress]`. Quote an item that contains a comma.
- **`poll_interval_seconds`** (alias `poll_interval`), **`heartbeat_seconds`**
  (alias `heartbeat_interval`) and **`deadline_minutes`** must be positive
  numbers, such as `30` or `1.5`. Zero, a negative number or text is an error.
  The pipeline file silently uses the default instead.
- **`builder_effort` and `firstmate_effort`** must be one of `none`,
  `minimal`, `low`, `medium`, `high`, `xhigh`, `max` or `ultra`, in any case.
- Other text columns (`linear_project_slug`, `linear_team`, the providers,
  `base_branch`, `worktree_root`, `install_command`, `skill`,
  `playwright_browser_image`) are used as written, after surrounding spaces are
  removed. The project configuration still checks providers and the
  Playwright image.

A project's `.harbor/workflow` still has the last word on what it sets: its
role models, fallback models and reasoning efforts, `execution.deadline_minutes`,
and `states` (its ready states replace `dispatch_states`, and its ready and
paused states are added to `poll_states`).
