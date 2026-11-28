"""Which builders get ``[builder] device_proof``, and whether a PR changes ``.harbor/``.

``granted_platforms`` is the single decision the runner factory (for the
provider a wake actually runs on) and ``harbor doctor`` (for every provider a
builder of the project could run on) both use, so doctor never reports a grant
the factory does not apply, or misses one it does.

``builder_sandbox_change`` is a best-effort guard for Harbor's own merge
commands. It cannot stop a builder that holds a GitHub token or can write the
repository's git directory. For a tracked ``.harbor/``, GitHub branch
protection with required Code Owner review makes PR changes need a human; it
does not cover an untracked ``.harbor/``, code Harbor runs unsandboxed
(install_command, git hooks), or the ``ios``/``android`` escapes.
"""
from __future__ import annotations

import base64
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
    "human merge required: the PR changes .harbor/, where [builder] (device_proof, allowed_domains, "
    "allow_tmp) and [quota_fallback] can "
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

    def run(self, *args: str, timeout: float = 30, git_dir: bool = True,
            extra_env: Mapping[str, str] | None = None) -> subprocess.CompletedProcess:
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0", GIT_NO_REPLACE_OBJECTS="1",
                   GIT_CEILING_DIRECTORIES=str(Path(self._tmp.name)))
        # Added after the scrub, so only Harbor's own values reach git (e.g. the fetch credential).
        env.update(extra_env or {})
        location = ("--git-dir", str(self.path)) if git_dir else ()
        return subprocess.run(["git", "--no-replace-objects", *HARDENING, *location, *args],
                              capture_output=True, text=True, timeout=timeout, env=env, cwd=self._tmp.name)


def _harbor_tracked(repo: _CheckRepo, rev: str) -> bool:
    """Whether ``rev`` tracks anything at a top-level name that case-folds to ``.harbor``."""

    listed = repo.run("ls-tree", "-z", "--name-only", "--end-of-options", rev)
    if listed.returncode != 0:
        raise OSError(listed.stderr.strip())
    return any(_is_harbor_path(name) for name in listed.stdout.split("\0") if name)


def _harbor_entries_plain(repo: _CheckRepo, rev: str) -> bool:
    """False when any tracked entry whose path case-folds to ``.harbor`` or below it is anything but a
    plain tree (040000) or a regular blob (100644/100755): a symlink (120000), a submodule (160000), ...

    On a case-insensitive filesystem ``.HARBOR -> cfg`` or ``.harbor/WORKFLOW -> ../cfg`` makes
    Harbor's runtime read files elsewhere in the repository, which ``ls-tree`` on the literal
    lowercase path never shows. Every case variant is walked recursively, at every depth. A path
    that is not tracked at all is fine: the project keeps it untracked in the main checkout.
    """

    top = repo.run("ls-tree", "-z", "--end-of-options", rev)
    if top.returncode != 0:
        raise OSError(top.stderr.strip())
    for record in top.stdout.split("\0"):
        meta, _, name = record.partition("\t")
        if not name or not _is_harbor_path(name):
            continue
        if not _plain(meta):
            return False
        if meta.split()[1] == "tree":
            below = repo.run("ls-tree", "-r", "-t", "-z", "--end-of-options", rev, "--", name)
            if below.returncode != 0:
                raise OSError(below.stderr.strip())
            if not all(_plain(entry.partition("\t")[0]) for entry in below.stdout.split("\0") if entry):
                return False
    return True


def _plain(meta: str) -> bool:
    """Whether an ``ls-tree`` ``<mode> <type> <object>`` field is a plain tree or a regular file."""

    fields = meta.split()
    if len(fields) != 3:
        return False
    mode, kind, _ = fields
    return (kind == "tree" and mode == "040000") or (kind == "blob" and mode in REGULAR_BLOB_MODES)


def _has_commit(repo: _CheckRepo, sha: str) -> bool:
    found = repo.run("rev-parse", "--verify", "--quiet", "--end-of-options", f"{sha}^{{commit}}")
    return found.returncode == 0 and found.stdout.strip() == sha


def _auth_env(fetch_url: str, token: str | None) -> dict[str, str]:
    """git config, passed through the environment, that sends ``token`` to ``fetch_url``'s origin only.

    Never on the command line, where ``ps`` would show it. ``GIT_CONFIG_NOSYSTEM``
    and the scrubbed environment stay in force; this is the only credential git sees.
    """

    parts = urlsplit(fetch_url)
    if parts.scheme != "https" or not parts.hostname or not token:
        return {}
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"http.https://{parts.netloc}/.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
    }


def redact(text: str, token: str | None) -> str:
    """``text`` with the token, and the basic-auth form Harbor builds from it, removed."""

    if token:
        for secret in (token, base64.b64encode(f"x-access-token:{token}".encode()).decode()):
            text = text.replace(secret, "[redacted]")
    return text


def _ensure_commit(repo: _CheckRepo, sha: str, fetch_url: str | None, token: str | None) -> str | None:
    """None when commit ``sha`` is readable (fetched from Harbor's URL if needed), else why not."""

    if _has_commit(repo, sha):
        return None
    if not fetch_url:
        return f"commit {sha} is not in the local repository and Harbor has no fetch URL for it"
    if urlsplit(fetch_url).scheme == "https" and not token:
        return (f"commit {sha} is not in the local repository and no GitHub token is configured to fetch it "
                f"from {fetch_url} (set GITHUB_TOKEN, GH_TOKEN or the variable named by github_token_env)")
    fetched = repo.run("fetch", "--no-tags", "--no-write-fetch-head", "--quiet", "--upload-pack=git-upload-pack",
                       "--end-of-options", fetch_url, f"+{sha}:{CHECK_REF_PREFIX}{sha}", timeout=120,
                       extra_env=_auth_env(fetch_url, token))
    if _has_commit(repo, sha):
        return None
    detail = redact((fetched.stderr or fetched.stdout or f"exit {fetched.returncode}").strip(), token)
    last = detail.splitlines()[-1] if detail else "unknown error"
    return f"could not fetch commit {sha} from {fetch_url}: {last}"


def _is_harbor_path(path: str) -> bool:
    """Whether ``path`` is under ``.harbor/`` on a case-insensitive, normalising filesystem (macOS)."""

    first = unicodedata.normalize("NFC", path.split("/", 1)[0])
    return first.casefold() == WORKFLOW_DIR.parts[0].casefold()


def _commits_touching_harbor(repo: _CheckRepo, base_sha: str, head_sha: str) -> bool | None:
    """Whether any commit in ``base..head`` changes a path under case-folded ``.harbor/``; None if git fails.

    Non-merge commits are compared with their parent; a merge commit counts
    for paths it changes against every parent (``--cc``), the content the
    merge itself introduced.
    """

    listed = repo.run("log", "-z", "--format=", "--name-only", "--no-renames", "--cc",
                      "--end-of-options", f"^{base_sha}", head_sha, timeout=120)
    if listed.returncode != 0:
        return None
    return any(_is_harbor_path(path.strip("\n")) for path in listed.stdout.split("\0") if path.strip("\n"))


def git_fetch_url(api_url: str | None, repository: str | None) -> str | None:
    """Harbor's own https git URL for ``owner/name`` on the configured GitHub host, or None."""

    if not repository or repository.count("/") != 1:
        return None
    from harbor.github_rest import rest_base_url
    parts = urlsplit(rest_base_url(api_url))
    host = (parts.hostname or "").lower()
    if not host or parts.scheme != "https":
        return None
    if host == "api.github.com":
        host = "github.com"
    elif host.startswith("api.") and host.endswith(".ghe.com") and host.count(".") == 3:
        host = host[len("api."):]  # GHE.com: api.<sub>.ghe.com serves git at <sub>.ghe.com
    # Anything else is GitHub Enterprise Server, which serves git on its API host
    # (https://HOST/api/v3, https://HOST/api/graphql): use that host unchanged, never a guess.
    netloc = host if parts.port is None else f"{host}:{parts.port}"
    return f"https://{netloc}/{repository}.git"


def builder_sandbox_change(git_dir: Any, head_sha: Any, base_sha: Any, *, fetch_url: str | None = None,
                           token: str | None = None, reasons: list[str] | None = None,
                           facts: dict[str, Any] | None = None) -> bool | None:
    """Whether merging the PR head would change anything under ``.harbor/`` on the PR base.

    The merge result comes from ``git merge-tree --write-tree`` (the same ort
    merge GitHub runs, over every merge base), so a criss-cross history cannot
    hide a change behind one convenient merge base; a conflict is unknown. On a
    git without ``--write-tree`` the head is compared with every merge base.
    Any single commit in ``base..head`` that touches ``.harbor/`` also counts,
    as a conservative extra. It does not model a rebase merge, whose rename
    detection can redirect an edit into ``.harbor/``: ``facts["harbor_tracked"]``
    (when ``facts`` is given) says whether anything under ``.harbor/`` is tracked
    at the base, the head or the merge result, and pr-merge refuses rebase then.

    ``git_dir`` is the repository's common git directory, resolved by the
    caller from Harbor's project config (``main_git_dir``). git runs in a
    private bare repository that only borrows its objects, with hooks,
    fsmonitor, ssh and ext transports, upload-pack hooks, replacement objects,
    grafts and shallow files out of reach, so config a builder planted in the
    repository is never read or run. ``head_sha`` and ``base_sha`` must be
    full SHAs from GitHub, never local refs a builder could forge. A missing
    commit is fetched from ``fetch_url`` (Harbor's configured remote) only,
    authenticated with ``token`` (Harbor's GitHub token) scoped to that URL's
    origin. When the answer is None, ``reasons`` (if given) gets why; the
    token never appears in it.

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

    def unknown(reason: str) -> None:
        if reasons is not None:
            reasons.append(redact(reason, token))
        return None

    if not isinstance(head_sha, str) or not isinstance(base_sha, str):
        return unknown("GitHub did not report the PR's head and base SHAs")
    if not FULL_SHA.fullmatch(head_sha) or not FULL_SHA.fullmatch(base_sha):
        return unknown("the PR's head and base must be full commit SHAs")
    if not git_dir or not Path(str(git_dir)).is_dir():
        return unknown(f"the project's git directory {git_dir or '(unknown)'} is not available")
    try:
        with _CheckRepo(Path(str(git_dir)).resolve()) as repo:
            for sha in (head_sha, base_sha):
                missing = _ensure_commit(repo, sha, fetch_url, token)
                if missing:
                    return unknown(missing)
            # What the merge would put on the base: GitHub merges against a virtual base built from
            # every merge base (ort), so a criss-cross history must not be reduced to one merge base.
            merged = repo.run("merge-tree", "--write-tree", "--no-messages", "--end-of-options", base_sha, head_sha)
            result = merged.stdout.split("\n", 1)[0].strip()
            if merged.returncode == 0 and FULL_SHA.fullmatch(result):
                compare = [(base_sha, result)]
                ends = (base_sha, head_sha, result)
            elif merged.returncode == 1:
                return unknown(f"merging {head_sha} into {base_sha} conflicts; Harbor cannot tell what would land")
            elif "--write-tree" in (merged.stderr or "") or "usage" in (merged.stderr or "").lower():
                # git older than 2.38: no merge-tree --write-tree. Any merge base whose .harbor/
                # differs from the head's counts, so a criss-cross cannot pick the convenient one.
                bases = repo.run("merge-base", "--all", "--end-of-options", base_sha, head_sha)
                merge_bases = [line.strip() for line in bases.stdout.splitlines() if line.strip()]
                if bases.returncode != 0 or not merge_bases or not all(FULL_SHA.fullmatch(m) for m in merge_bases):
                    return unknown(f"no merge base between {base_sha} and {head_sha}")
                compare = [(merge_base, head_sha) for merge_base in merge_bases]
                ends = (*merge_bases, head_sha)
            else:
                return unknown(f"git merge-tree failed: {(merged.stderr or '').strip() or merged.returncode}")
            tracked_anywhere = any(_harbor_tracked(repo, end) for end in ends)
            if facts is not None:
                facts["harbor_tracked"] = tracked_anywhere
            # Any PR commit that touches .harbor/ counts even when the ort merge result shows no
            # change. This does not model a rebase merge (rename detection can redirect an edit
            # into .harbor/); pr-merge refuses rebase outright when .harbor/ is tracked.
            touched = _commits_touching_harbor(repo, base_sha, head_sha)
            if touched is None:
                return unknown(f"could not list the commits in {base_sha}..{head_sha}")
            if touched:
                return True
            if not tracked_anywhere:
                return False
            if not all(_harbor_entries_plain(repo, end) for end in ends):
                return unknown("a tracked path under .harbor/ (in any letter case) is a symlink, a submodule "
                               "or not a regular file")
            for before, after in compare:
                changed = repo.run("diff-tree", "-r", "-z", "--no-renames", "--name-only", "--no-commit-id",
                                   "--end-of-options", before, after)
                if changed.returncode != 0:
                    return unknown(f"git diff-tree failed: {changed.stderr.strip()}")
                if any(_is_harbor_path(path) for path in changed.stdout.split("\0") if path):
                    return True
    except (OSError, subprocess.SubprocessError) as exc:
        return unknown(f"git failed: {exc}")
    return False
