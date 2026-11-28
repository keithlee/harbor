"""Git invocations made by Harbor itself, outside any builder sandbox.

Harbor runs git on the host, in the project repository and in builder
worktrees.  A builder can write its whole worktree (including the ``.git``
file that tells git where the repository is), its worktree's admin directory
under ``<common>/worktrees/<name>/``, and the shared object and ref stores.
Without care, any of those can make Harbor's git run a program the builder
chose: a hook, ``core.fsmonitor``, or anything in a config file the builder
planted by pointing ``.git`` at a git directory of its own.

Every call therefore:

* passes :data:`HARDENING` (no hooks, no fsmonitor, no ``ext::`` transport,
  no replace objects) and, for commands that never touch the network,
  :data:`LOCAL_HARDENING` as well;
* runs with every ``GIT_*`` variable removed and ``GIT_CONFIG_NOSYSTEM=1``;
* gets a minimal environment with none of Harbor's credentials, unless it is
  one of the two operator-repository commands that may need to authenticate
  (``fetch`` and ``worktree add``, see :func:`git_env`);
* and, inside a builder worktree, runs only after :func:`worktree_git_dirs`
  has proved, by reading files rather than running git, that the worktree's
  ``.git`` points at this repository's own ``worktrees/<name>`` directory.
  ``GIT_DIR``, ``GIT_COMMON_DIR`` and ``GIT_WORK_TREE`` are then pinned, so git
  does not rediscover the repository from files the builder can rewrite.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence

# ``-c`` overrides every configuration file, including anything they include.
# ``/dev/null`` is not a directory, so no hook can be found under it.
HARDENING: tuple[str, ...] = (
    "--no-replace-objects",
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=false",
    "-c", "protocol.ext.allow=never",
    # A submodule is a repository whose config, attributes and hooks are all
    # the builder's.  Harbor never recurses into one: not to check it out,
    # not to fetch it.
    "-c", "submodule.recurse=false",
    "-c", "fetch.recurseSubmodules=false",
)
# For commands that never connect anywhere.  Blanking transport programs
# costs nothing there; ``fetch`` keeps the operator's own ``core.sshCommand``,
# because an empty one would break ssh remotes.
#
# Submodules: to tell whether a submodule's work tree is dirty, ``status`` and
# ``diff`` run git inside it, where the builder's own config (filter drivers,
# fsmonitor) applies.  ``diff.ignoreSubmodules=dirty`` makes that the default,
# but a builder's ``.gitmodules`` can override it per submodule
# (``ignore = none``), so every ``status`` and ``diff`` Harbor runs in a
# worktree must also pass :data:`IGNORE_SUBMODULES` on its command line,
# which nothing in the repository overrides.  ``dirty`` rather than ``all``:
# a changed or added gitlink is still reported (so the protected-path check
# and the cleanup dirty check still see it), without git entering the
# submodule.
LOCAL_HARDENING: tuple[str, ...] = (
    "-c", "core.sshCommand=",
    "-c", "uploadpack.packObjectsHook=",
    "-c", "core.alternateRefsCommand=",
    "-c", "diff.ignoreSubmodules=dirty",
    "-c", "status.submoduleSummary=false",
)
IGNORE_SUBMODULES = "--ignore-submodules=dirty"

# What a local git command needs from the environment: where to find git and
# the operator's global config, and the locale.  Nothing else is passed, so no
# credential Harbor holds (GitHub, Linear, model keys, or a project's own
# secrets) can reach a program git runs.
_LOCAL_ENV_KEYS = frozenset({"PATH", "HOME", "USER", "LOGNAME", "LANG", "LANGUAGE", "TMPDIR", "TZ", "XDG_CONFIG_HOME"})
# Never needed by git, even to authenticate.
_NEVER_TO_GIT = re.compile(r"(^|_)(LINEAR|ANTHROPIC|OPENAI|CODEX|CLAUDE)(_|$)", re.IGNORECASE)


class UnsafeGitDirectory(ValueError):
    """A directory's git metadata does not point where Harbor created it."""


def git_argv(*args: str, network: bool = False) -> list[str]:
    """Return ``git <args>`` with repository hooks, fsmonitor and other repository programs disabled."""

    return ["git", *HARDENING, *(() if network else LOCAL_HARDENING), *args]


def safe_path(value: str, *, outside: Path | None = None) -> str:
    """``PATH`` without empty or relative entries, and without entries inside ``outside``.

    Git runs with a builder's worktree as its working directory, and Python
    looks ``git`` up on the child's ``PATH``.  An empty entry or ``.`` would
    find a ``git`` the builder planted there, and so would an absolute entry
    inside the worktree (``node_modules/.bin`` for example).
    """

    kept = []
    for entry in value.split(os.pathsep):
        if not entry or not os.path.isabs(entry):
            continue
        if outside is not None:
            resolved = Path(entry).resolve()
            if resolved == outside or outside in resolved.parents:
                continue
        kept.append(entry)
    return os.pathsep.join(kept) or os.defpath


def git_env(*, network: bool = False, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment for one of Harbor's git calls.

    Local commands get only :data:`_LOCAL_ENV_KEYS` (and ``LC_*``).  A network
    command in the operator's repository keeps the rest of the environment, so
    the operator's credential helper (for example ``gh auth git-credential``,
    which reads ``GH_TOKEN``) and ssh agent still work, minus anything that is
    never a git credential.  Both drop every ``GIT_*`` variable first.
    """

    source = os.environ if environ is None else environ
    env: dict[str, str] = {}
    for key, value in source.items():
        if key.upper().startswith("GIT_"):
            continue
        if network:
            if not _NEVER_TO_GIT.search(key):
                env[key] = value
        elif key in _LOCAL_ENV_KEYS or key.startswith("LC_"):
            env[key] = value
    if "PATH" in env:
        env["PATH"] = safe_path(env["PATH"])
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0", GIT_NO_REPLACE_OBJECTS="1")
    if network:
        # The system config is root-owned and is where macOS sets
        # ``credential.helper=osxkeychain``; a fetch still needs it.
        env.pop("GIT_CONFIG_NOSYSTEM")
    return env


def main_git_dir(path: Any) -> Path | None:
    """The common git directory of the repository at ``path`` (or above it), read without running git.

    A worktree's ``.git`` file points at ``<common>/worktrees/<name>``, whose
    ``commondir`` names the common directory. Nothing in the repository's
    config is consulted.  Use this only on the operator's own checkout: inside
    a builder worktree every file it reads is the builder's (see
    :func:`worktree_git_dirs`).
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


def _read_pointer(path: Path, prefix: str = "") -> str:
    if path.is_symlink() or not path.is_file():
        raise UnsafeGitDirectory(f"{path} is missing or not a regular file")
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    if "\n" in text or not text.startswith(prefix):
        raise UnsafeGitDirectory(f"{path} is not a git pointer file")
    return text[len(prefix):].strip()


def worktree_git_dirs(worktree: str | Path, repo: str | Path) -> tuple[Path, Path, Path]:
    """Return ``(work_tree, git_dir, common_dir)`` for running git in ``worktree``, or fail closed.

    ``repo`` is the operator's checkout Harbor was configured with; its common
    git directory is the only one a builder worktree may use.  ``worktree``
    must be a linked worktree of it: its ``.git`` file must name
    ``<common>/worktrees/<name>``, that directory's ``commondir`` must lead
    back to ``<common>`` and its ``gitdir`` back to this worktree.  A ``.git``
    redirected to a git directory the builder made, or to another ticket's
    worktree, raises :class:`UnsafeGitDirectory` before git ever runs.
    ``repo`` itself is accepted as-is.
    """

    common = main_git_dir(repo)
    if common is None:
        raise UnsafeGitDirectory(f"{repo} is not a git checkout")
    work = Path(worktree).expanduser().resolve()
    dot = work / ".git"
    if work == Path(repo).expanduser().resolve() and dot.is_dir() and not dot.is_symlink():
        return work, dot.resolve(), common
    admin = (work / _read_pointer(dot, "gitdir:")).resolve()
    if admin.parent != common / "worktrees":
        raise UnsafeGitDirectory(f"{dot} points outside {common / 'worktrees'}: {admin}")
    if (admin / _read_pointer(admin / "commondir")).resolve() != common:
        raise UnsafeGitDirectory(f"{admin / 'commondir'} does not lead back to {common}")
    if (admin / _read_pointer(admin / "gitdir")).resolve() != dot.resolve():
        raise UnsafeGitDirectory(f"{admin / 'gitdir'} belongs to a different worktree")
    # Git reads ``config.worktree`` from the admin directory, which the builder
    # can write, whenever the repository enables ``extensions.worktreeConfig``.
    if os.path.lexists(admin / "config.worktree"):
        raise UnsafeGitDirectory(f"{admin / 'config.worktree'} could carry builder-written git config")
    return work, admin, common


def run_git(repo: str | Path, *args: str, timeout: float | None = None, check: bool = False,
            environ: Mapping[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run hardened git in the operator's repository ``repo``.

    Only ``fetch`` and ``worktree add`` keep the operator's credentials:
    ``fetch`` talks to the remote, and ``worktree add`` checks out files,
    which runs the operator's own smudge filters (Git LFS downloads).
    """

    network = args[:1] == ("fetch",) or args[:2] == ("worktree", "add")
    return subprocess.run(
        git_argv("-C", str(repo), *args, network=network),
        cwd=str(repo), env=git_env(network=network, environ=environ),
        capture_output=True, text=True, timeout=timeout, check=check,
    )


def run_worktree_git(worktree: str | Path, repo: str | Path, *args: str, timeout: float | None = None,
                     check: bool = False, environ: Mapping[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run hardened, credential-free git in a builder ``worktree`` of ``repo``, pinned to verified git dirs.

    Raises :class:`UnsafeGitDirectory` without running git when the worktree's
    metadata does not belong to ``repo``.
    """

    work, git_dir, common = worktree_git_dirs(worktree, repo)
    env = git_env(environ=environ)
    env["PATH"] = safe_path(env.get("PATH", os.defpath), outside=work)
    env.update(GIT_DIR=str(git_dir), GIT_COMMON_DIR=str(common), GIT_WORK_TREE=str(work))
    if args and args[0] in {"status", "diff"}:
        # Whatever the caller passed, never let git enter a submodule.
        args = (args[0], IGNORE_SUBMODULES, *(a for a in args[1:] if not a.startswith("--ignore-submodules")))
    return subprocess.run(
        git_argv(*args), cwd=str(work), env=env,
        capture_output=True, text=True, timeout=timeout, check=check,
    )


__all__ = [
    "HARDENING", "IGNORE_SUBMODULES", "LOCAL_HARDENING", "UnsafeGitDirectory", "git_argv", "git_env", "main_git_dir",
    "run_git", "run_worktree_git", "safe_path", "worktree_git_dirs",
]
