# Harbor projects

This registry is intentionally human-editable. Add one row per project; paths
may use `~` or environment variables. Every runnable project must point at its
live `.harbor/pipeline.md`; Harbor fails closed with the expected path instead
of silently reading `AGENTS.md`.

| name | repo_path | pipeline_path | linear_project_slug | github_repository | base_branch |
| --- | --- | --- | --- | --- | --- |
| Bookgen | ~/Projects/Bookgen | ~/Projects/Bookgen/.harbor/pipeline.md | hanzireader-06c91e632f50 | keithlee/Bookgen | main |
