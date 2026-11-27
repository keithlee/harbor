"""Which builders get ``[builder] device_proof``, and whether a PR changes it.

``granted_platforms`` is the single decision the runner factory (for the
provider a wake actually runs on) and ``harbor doctor`` (for every provider a
builder of the project could run on) both use, so doctor never reports a grant
the factory does not apply, or misses one it does.
"""
from __future__ import annotations

import sqlite3
import subprocess
import tomllib
from pathlib import Path
from typing import Any, Mapping

from harbor.config.workflow import WORKFLOW_DIR, device_proof_settings

WORKFLOW_FILE = (WORKFLOW_DIR / "workflow.toml").as_posix()
CODEX_DEVICE_PROOF = (
    "[builder] device_proof applies to Claude Code builders only and does not change a Codex builder's "
    "sandbox. Harbor already gives Codex builders network access, which covers local ports (adb, dev "
    "servers); the Codex sandbox has no setting for simulator services, so a Codex builder cannot use "
    "xcrun simctl"
)


def granted_platforms(workflow_config: Mapping[str, Any], provider: str) -> tuple[str, ...]:
    """The device-proof platforms a builder on ``provider`` is granted: Claude only."""

    return device_proof_settings(workflow_config) if provider == "claude" else ()


def builder_providers(config: Any, policy: Any = None, connection: Any = None) -> dict[str, list[str]]:
    """Every provider a builder of this project could run on, with why.

    The configured ``builder_provider``; either side of a ``[quota_fallback]``
    builder pair (a usage limit moves a run, or starts a new ticket, on the
    other one); and the provider of any retained builder session in the
    ledger, which resumes on the provider it started on even after the project
    switches.
    """

    providers: dict[str, list[str]] = {}
    configured = getattr(config, "builder_provider", "codex")
    providers.setdefault(configured, []).append("builder_provider")
    pair = getattr(policy, "builder", None) if policy is not None else None
    for profile in pair or ():
        if profile.provider != configured:
            providers.setdefault(profile.provider, []).append("quota_fallback builder")
    for provider, count in _retained_providers(connection, getattr(config, "name", "")):
        if provider != configured:
            providers.setdefault(provider, []).append(f"{count} retained builder session(s)")
    return providers


def _retained_providers(connection: Any, project: str) -> list[tuple[str, int]]:
    if connection is None or not project:
        return []
    try:
        return [tuple(row) for row in connection.execute(
            "SELECT i.provider, COUNT(*) FROM runner_identity i JOIN runs r ON r.id = i.run_id "
            "JOIN tickets t ON t.identifier = r.ticket WHERE t.project = ? GROUP BY i.provider", (project,)
        ).fetchall()]
    except sqlite3.Error:
        return []  # A ledger without these tables has no retained sessions.


def open_ledger_read_only(path: Path) -> sqlite3.Connection | None:
    """The ledger for reading, or None when it does not exist yet. Never creates it."""

    if not path.is_file():
        return None
    try:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None


def _builder_table(repo: Path, rev: str) -> dict | None:
    """``[builder]`` from ``rev``'s workflow.toml; ``{}`` when the file or table is absent; None if unreadable."""

    listed = subprocess.run(["git", "-C", str(repo), "ls-tree", "--name-only", rev, "--", WORKFLOW_FILE],
                            capture_output=True, text=True, timeout=30)
    if listed.returncode != 0:
        return None
    if not listed.stdout.strip():
        return {}
    shown = subprocess.run(["git", "-C", str(repo), "show", f"{rev}:{WORKFLOW_FILE}"],
                           capture_output=True, text=True, timeout=30)
    if shown.returncode != 0:
        return None
    try:
        return dict(tomllib.loads(shown.stdout).get("builder") or {})
    except (tomllib.TOMLDecodeError, TypeError, ValueError):
        return None


def builder_sandbox_change(worktree: Any, head_sha: Any, base_ref: Any) -> bool | None:
    """Whether the PR head changes ``[builder]`` in the workflow against its merge base.

    True when the table differs (added, changed or removed), False when it is
    the same, None when Harbor cannot tell: no worktree, a head or base it
    cannot resolve, or a workflow it cannot parse. Callers treat None as True.
    """

    if not worktree or not head_sha or not base_ref:
        return None
    repo = Path(str(worktree))
    if not repo.is_dir():
        return None
    try:
        base = subprocess.run(["git", "-C", str(repo), "merge-base", str(base_ref), str(head_sha)],
                              capture_output=True, text=True, timeout=30)
        if base.returncode != 0 or not base.stdout.strip():
            return None
        before = _builder_table(repo, base.stdout.strip())
        after = _builder_table(repo, str(head_sha))
    except (OSError, subprocess.SubprocessError):
        return None
    if before is None or after is None:
        return None
    return before != after
