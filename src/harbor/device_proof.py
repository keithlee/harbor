"""Which builders get ``[builder] device_proof``, and whether a PR changes ``.harbor/``.

``granted_platforms`` is the single decision the runner factory (for the
provider a wake actually runs on) and ``harbor doctor`` (for every provider a
builder of the project could run on) both use, so doctor never reports a grant
the factory does not apply, or misses one it does.

``builder_sandbox_change`` is a best-effort guard for Harbor's own merge
commands. It cannot stop a builder that holds a GitHub token or can write the
repository's git directory; GitHub branch protection with required Code Owner
review on ``/.harbor/`` is the only hard guarantee.
"""
from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import tempfile
import unicodedata
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from harbor.config.workflow import WORKFLOW_DIR, device_proof_settings

WORKFLOW_FILE = (WORKFLOW_DIR / "workflow.toml").as_posix()
CODEX_DEVICE_PROOF = (
    "[builder] device_proof applies to Claude Code builders only and does not change a Codex builder's "
    "sandbox. Harbor already gives Codex builders network access, which covers local ports (adb, dev "
    "servers); the Codex sandbox has no setting for simulator services, so a Codex builder cannot use "
    "xcrun simctl"
)


BUILDER_SANDBOX_CHANGED = (
    "human merge required: the PR changes .harbor/, where [builder] device_proof and [quota_fallback] can "
    "widen the builder sandbox; Harbor's readiness check and pr-merge do not merge it, whatever "
    "auto_merge_risks says"
)
BUILDER_SANDBOX_UNKNOWN = (
    "human merge required: Harbor could not read the PR diff against GitHub's base and head SHAs to rule out "
    "a .harbor/ change; Harbor's readiness check and pr-merge do not merge it"
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
FULL_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
CHECK_REF_PREFIX = "refs/harbor/check/"
# Config a builder could have planted in the repository it can write is never read:
# the check runs git in a private bare repository that borrows only the objects.
HARDENING = (
    "-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null", "-c", "core.sshCommand=",
    "-c", "protocol.ext.allow=never", "-c", "uploadpack.packObjectsHook=", "-c", "core.alternateRefsCommand=",
    # A commit-graph in the borrowed object directory could lie about parents and move the merge base.
    "-c", "core.commitGraph=false",
)


def main_git_dir(path: Any) -> Path | None:
    """The common git directory of the repository at ``path`` (or above it), read without running git.

    A worktree's ``.git`` file points at ``<common>/worktrees/<name>``, whose
    ``commondir`` names the common directory. Nothing in the repository's
    config is consulted.
    """

    if not path:
        return None
    start = Path(str(path)).expanduser()
    for directory in (start, *start.parents):
        dot = directory / ".git"
        if dot.is_dir():
            return dot.resolve()
        if dot.is_file():
            text = dot.read_text(encoding="utf-8", errors="replace").strip()
            if not text.startswith("gitdir:"):
                return None
            admin = (directory / text.split(":", 1)[1].strip()).resolve()
            common = admin / "commondir"
            if common.is_file():
                return (admin / common.read_text(encoding="utf-8").strip()).resolve()
            return admin
    return None


class _CheckRepo:
    """A throwaway bare repository whose only link to the real one is ``objects/info/alternates``."""

    def __init__(self, git_dir: Path):
        self.git_dir = git_dir
        self._tmp = tempfile.TemporaryDirectory(prefix="harbor-check-")
        self.path = Path(self._tmp.name) / "check.git"

    def __enter__(self) -> "_CheckRepo":
        objects = self.git_dir / "objects"
        if not objects.is_dir():
            raise OSError(f"{objects} is not a git object directory")
        init = self.run("init", "--bare", "--quiet", str(self.path), git_dir=False)
        if init.returncode != 0:
            raise OSError(init.stderr.strip())
        (self.path / "objects" / "info" / "alternates").write_text(str(objects) + "\n", encoding="utf-8")
        return self

    def __exit__(self, *exc: Any) -> None:
        self._tmp.cleanup()

    def run(self, *args: str, timeout: float = 30, git_dir: bool = True) -> subprocess.CompletedProcess:
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0", GIT_NO_REPLACE_OBJECTS="1",
                   GIT_CEILING_DIRECTORIES=str(Path(self._tmp.name)))
        location = ("--git-dir", str(self.path)) if git_dir else ()
        return subprocess.run(["git", "--no-replace-objects", *HARDENING, *location, *args],
                              capture_output=True, text=True, timeout=timeout, env=env, cwd=self._tmp.name)


def _tree_entry(repo: _CheckRepo, rev: str, path: str) -> tuple[str, str, str] | None:
    """``(mode, type, object)`` of exactly ``path`` in ``rev``; None when absent. Raises OSError if git fails."""

    listed = repo.run("ls-tree", "-z", "--end-of-options", rev, "--", path)
    if listed.returncode != 0:
        raise OSError(listed.stderr.strip())
    for record in listed.stdout.split("\0"):
        meta, _, name = record.partition("\t")
        if name == path and len(meta.split()) == 3:
            mode, kind, obj = meta.split()
            return mode, kind, obj
    return None


def _harbor_tracked(repo: _CheckRepo, rev: str) -> bool:
    """Whether ``rev`` tracks anything at a top-level name that case-folds to ``.harbor``."""

    listed = repo.run("ls-tree", "-z", "--name-only", "--end-of-options", rev)
    if listed.returncode != 0:
        raise OSError(listed.stderr.strip())
    return any(_is_harbor_path(name) for name in listed.stdout.split("\0") if name)


def _workflow_path_is_plain(repo: _CheckRepo, rev: str) -> bool:
    """False when ``.harbor``, ``.harbor/workflow`` or workflow.toml is tracked as anything but a plain
    directory / regular file (a symlink 120000, a submodule 160000, ...).

    ``ls-tree`` lists nothing under a symlinked directory although Harbor's
    runtime follows the link, so such an entry must never read as "absent".
    A component that is not tracked at all is fine: the project keeps it
    untracked in the main checkout, where a PR cannot change it.
    """

    parts = WORKFLOW_FILE.split("/")
    for depth in range(1, len(parts) + 1):
        entry = _tree_entry(repo, rev, "/".join(parts[:depth]))
        if entry is None:
            return True
        last = depth == len(parts)
        if last and (entry[1] != "blob" or entry[0] not in REGULAR_BLOB_MODES):
            return False
        if not last and (entry[1] != "tree" or entry[0] != "040000"):
            return False
    return True


def _has_commit(repo: _CheckRepo, sha: str) -> bool:
    found = repo.run("rev-parse", "--verify", "--quiet", "--end-of-options", f"{sha}^{{commit}}")
    return found.returncode == 0 and found.stdout.strip() == sha


def _ensure_commit(repo: _CheckRepo, sha: str, fetch_url: str | None) -> bool:
    """True when commit ``sha`` is readable, fetching it from Harbor's configured URL if needed."""

    if _has_commit(repo, sha):
        return True
    if not fetch_url:
        return False
    repo.run("fetch", "--no-tags", "--no-write-fetch-head", "--quiet", "--upload-pack=git-upload-pack",
             "--end-of-options", fetch_url, f"+{sha}:{CHECK_REF_PREFIX}{sha}", timeout=120)
    return _has_commit(repo, sha)


def _is_harbor_path(path: str) -> bool:
    """Whether ``path`` is under ``.harbor/`` on a case-insensitive, normalising filesystem (macOS)."""

    first = unicodedata.normalize("NFC", path.split("/", 1)[0])
    return first.casefold() == WORKFLOW_DIR.parts[0].casefold()


def git_fetch_url(api_url: str | None, repository: str | None) -> str | None:
    """Harbor's own https git URL for ``owner/name`` on the configured GitHub host, or None."""

    if not repository or repository.count("/") != 1:
        return None
    from harbor.github_rest import rest_base_url
    host = urlsplit(rest_base_url(api_url)).hostname or ""
    if host == "api.github.com" or host.startswith("api."):
        host = host[len("api."):]
    return f"https://{host}/{repository}.git" if host else None


def builder_sandbox_change(git_dir: Any, head_sha: Any, base_sha: Any, *,
                           fetch_url: str | None = None) -> bool | None:
    """Whether the PR head changes anything under ``.harbor/`` against its merge base with the PR base.

    ``git_dir`` is the repository's common git directory, resolved by the
    caller from Harbor's project config (``main_git_dir``). git runs in a
    private bare repository that only borrows its objects, with hooks,
    fsmonitor, ssh and ext transports, upload-pack hooks, replacement objects,
    grafts and shallow files out of reach, so config a builder planted in the
    repository is never read or run. ``head_sha`` and ``base_sha`` must be
    full SHAs from GitHub, never local refs a builder could forge. A missing
    commit is fetched from ``fetch_url`` (Harbor's configured remote) only.

    Any change under a ``.harbor`` directory, compared after Unicode NFC
    normalisation and case folding, counts: ``[builder]`` and
    ``[quota_fallback]`` can both widen a grant. A project that keeps
    ``.harbor/`` untracked (Harbor reads it from the main checkout) gets False
    when the PR still tracks nothing there, and True when it adds some.

    Returns True on a change, False when there is none, None when Harbor
    cannot tell (bad SHAs, a commit it cannot read, or a tracked ``.harbor``,
    ``.harbor/workflow`` or workflow.toml that is a symlink, a submodule or
    not a regular file). Callers treat None as True.
    """

    if not isinstance(head_sha, str) or not isinstance(base_sha, str):
        return None
    if not FULL_SHA.fullmatch(head_sha) or not FULL_SHA.fullmatch(base_sha):
        return None
    if not git_dir or not Path(str(git_dir)).is_dir():
        return None
    try:
        with _CheckRepo(Path(str(git_dir)).resolve()) as repo:
            if not _ensure_commit(repo, head_sha, fetch_url) or not _ensure_commit(repo, base_sha, fetch_url):
                return None
            merged = repo.run("merge-base", "--end-of-options", base_sha, head_sha)
            merge_base = merged.stdout.strip()
            if merged.returncode != 0 or not FULL_SHA.fullmatch(merge_base):
                return None
            if not _harbor_tracked(repo, merge_base) and not _harbor_tracked(repo, head_sha):
                return False
            if not _workflow_path_is_plain(repo, merge_base) or not _workflow_path_is_plain(repo, head_sha):
                return None
            changed = repo.run("diff-tree", "-r", "-z", "--no-renames", "--name-only", "--no-commit-id",
                               "--end-of-options", merge_base, head_sha)
    except (OSError, subprocess.SubprocessError):
        return None
    if changed.returncode != 0:
        return None
    return any(_is_harbor_path(path) for path in changed.stdout.split("\0") if path)
