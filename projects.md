# Harbor projects example

This checked-in file is an empty format example, never a source of real user
configuration. Create a private registry and select it with
`HARBOR_PROJECTS_FILE`. Paths may use `~` or environment variables. Every
runnable project must point at its live pipeline; Harbor fails closed instead
of silently reading `AGENTS.md` or inheriting another product's identity.

| name | repo_path | pipeline_path | linear_project_slug | linear_team | github_repository | base_branch |
| --- | --- | --- | --- | --- | --- | --- |

Optional columns include `linear_api_url` and, for GitHub Enterprise Server,
`github_api_url` (for example `https://ghe.example.com/api/graphql`). A value
here wins over the same key in the project's pipeline; an empty cell is ignored.
