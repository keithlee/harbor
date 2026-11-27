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


BUILDER_SANDBOX_CHANGED = (
    "human merge required: the PR changes [builder] in .harbor/workflow/workflow.toml, which can widen the "
    "builder sandbox (device_proof); Harbor's readiness check and merge command refuse it whatever "
    "auto_merge_risks says"
)
BUILDER_SANDBOX_UNKNOWN = (
    "human merge required: Harbor could not read the PR diff to rule out a [builder] change in "
    ".harbor/workflow/workflow.toml (device_proof); Harbor's readiness check and merge command refuse it"
)


def builder_sandbox_refusal(change: bool | None) -> str | None:
    """Why Harbor must not merge, given ``builder_sandbox_change``; None only for an explicit False."""

    if change is False:
        return None
    return BUILDER_SANDBOX_CHANGED if change is True else BUILDER_SANDBOX_UNKNOWN


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


REGULAR_BLOB_MODES = {"100644", "100755"}


def _tree_entry(repo: Path, rev: str, path: str) -> tuple[str, str, str] | None:
    """``(mode, type, object)`` of exactly ``path`` in ``rev``, or None when it is not there or git fails."""

    listed = subprocess.run(["git", "-C", str(repo), "ls-tree", "-z", rev, "--", path],
                            capture_output=True, text=True, timeout=30)
    if listed.returncode != 0:
        return None
    for record in listed.stdout.split("\0"):
        meta, _, name = record.partition("\t")
        if name == path and len(meta.split()) == 3:
            mode, kind, obj = meta.split()
            return mode, kind, obj
    return None


def _workflow_blob(repo: Path, rev: str) -> str | None:
    """The workflow.toml blob in ``rev``, only when every component is a plain tree or a regular file.

    ``ls-tree`` lists nothing under a symlinked ``.harbor`` or
    ``.harbor/workflow``, although Harbor's runtime resolves the link and loads
    the target, so a missing entry, a symlink (120000), a submodule (160000) or
    anything but a regular blob (100644/100755) is unreadable, never "absent".
    workflow.toml is mandatory for a Harbor project, so absence is unreadable too.
    """

    parts = WORKFLOW_FILE.split("/")
    for depth in range(1, len(parts)):
        entry = _tree_entry(repo, rev, "/".join(parts[:depth]))
        if entry is None or entry[0] != "040000" or entry[1] != "tree":
            return None
    entry = _tree_entry(repo, rev, WORKFLOW_FILE)
    if entry is None or entry[0] not in REGULAR_BLOB_MODES or entry[1] != "blob":
        return None
    return entry[2]


def _builder_table(repo: Path, rev: str) -> dict | None:
    """``[builder]`` from ``rev``'s workflow.toml (``{}`` when the table is absent); None if it cannot be read safely."""

    blob = _workflow_blob(repo, rev)
    if blob is None:
        return None
    shown = subprocess.run(["git", "-C", str(repo), "cat-file", "blob", blob],
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
    cannot resolve, a workflow.toml that is missing, reached through a symlink
    or submodule, or not a regular file, or one it cannot parse. Callers treat
    None as True.
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
