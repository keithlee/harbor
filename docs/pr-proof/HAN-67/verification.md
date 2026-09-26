# HAN-67 verification evidence

Run date: 2026-09-26
Worktree: `/private/tmp/harbor-live-20260926/worktrees/HAN-67`
Logical run: `harbor-HarborLive20260926-HAN-67-builder`
Native builder session: `01a0dd0a-e90a-7c60-988b-fdb34e7fcdae`

The live Linear authority citation for the controlled unblock was `ticket:HAN-67:1`.

## Exact-content proof

Command:

```text
cmp -s docs/harbor-live-tests/low-risk.md <(printf '%s\n' '# Harbor low-risk delivery fixture' '' 'The delivery workflow may merge low-risk work only after all review, proof and CI gates pass.' '' 'This is a deliberately trivial fixture, not a change to Harbor policy. Proof files may be under docs/pr-proof/<this-ticket>/; no other product files may change.')
```

Output:

```text
exact-content: PASS
product-file: docs/harbor-live-tests/low-risk.md
bytes:      292
final-newline-byte:            0a
```

## Registry and fail-closed configuration proof

Command:

```text
UV_CACHE_DIR=/private/tmp/harbor-live-20260926/uv-cache uv run pytest -o addopts='' tests/test_project_config_portability.py tests/test_project_scope.py
```

Result: `16 passed in 0.09s`.

## Isolated custody proof

Command:

```text
UV_CACHE_DIR=/private/tmp/harbor-live-20260926/uv-cache uv run pytest -o addopts='' tests/test_integration_console_supervisor.py::test_answer_command_records_citation_and_resumes_same_session tests/test_integration_console_supervisor.py::test_worktree_resume_routes_a_second_blocker_with_persisted_worktree_to_unblock_ladder tests/test_restart_hydration.py::test_resume_after_restart_rebuilds_the_same_session tests/test_restart_hydration.py::test_explicit_retry_resumes_terminal_run_in_same_session_and_worktree tests/test_restart_hydration.py::test_an_operator_answer_in_a_new_process_resumes_the_same_session tests/test_unblock_phase7.py::test_unblock_uses_retained_worktree_authority_not_stale_base_checkout
```

Result: `8 passed in 0.12s`.

## Repository gates

The locked install completed with the task-scoped writable cache:

```text
Resolved 16 packages in 27ms
Checked 15 packages in 6ms
```

Required gates:

```text
uv run python -m compileall -q src tests        PASS
uv build                                        Successfully built dist/harbor-0.1.0.tar.gz
                                                Successfully built dist/harbor-0.1.0-py3-none-any.whl
uv run pytest                                   399 passed in 12.27s
```

## Revalidation after review fix

Sol identified and rejected a doubled backslash in the documented exact-content command. The command now uses `printf '%s\n'` and was replayed successfully:

```text
exact-content: PASS
```

The affected focused checks and all repository gates were rerun at the corrected proof head:

```text
registry/fail-closed tests: 16 passed in 0.35s
custody/session tests: 8 passed in 0.39s
uv run python -m compileall -q src tests: PASS
uv build: sdist and wheel PASS
uv run pytest: 399 passed in 20.14s
```
