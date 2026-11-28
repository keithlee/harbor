"""Safe, fresh-base git worktrees for Harbor builders.

The supervisor never gives a builder the user's checkout.  ``WorktreeManager``
serializes git metadata operations per repository, fetches the configured base,
and creates a ticket-specific worktree from that fetched ref.  It deliberately
uses argument lists (never a shell) and checks every user-controlled path before
performing a git operation.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

from harbor.safe_git import IGNORE_SUBMODULES, UnsafeGitDirectory, run_git, run_worktree_git


DEFAULT_DISK_FLOOR_GB = 5.0
# pnpm hardlinks from its global store, so this is cheap rather than a
# multi-gigabyte copy per worktree.  Projects override it in pipeline.md.
DEFAULT_INSTALL_COMMAND: tuple[str, ...] = ("pnpm", "install")
DEFAULT_INSTALL_TIMEOUT_SECONDS = 15 * 60.0
_SAFE_TICKET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class WorktreeError(RuntimeError):
    """A worktree could not be created, verified, or removed."""


class DiskFloor(WorktreeError):
    """The volume does not have enough free space for another builder."""

    def __init__(self, available_gb: float, required_gb: float = DEFAULT_DISK_FLOOR_GB) -> None:
        self.available_gb = float(available_gb)
        self.required_gb = float(required_gb)
        super().__init__(
            f"free disk floor is {self.required_gb:.2f} GB; "
            f"only {self.available_gb:.2f} GB is available"
        )


class WorktreeConfinementError(WorktreeError):
    """A path or command would escape the configured repository/worktree."""


class WorktreeInstallError(WorktreeError):
    """Dependency installation failed inside a freshly created worktree.

    This is deliberately its own type: rung 0 of the unblock ladder knows how
    to remediate a failed install and retry, which it cannot do for a git
    failure.
    """

    def __init__(self, command: Sequence[str], returncode: int, output: str) -> None:
        self.command = tuple(str(item) for item in command)
        self.returncode = int(returncode)
        self.output = output
        super().__init__(
            f"{' '.join(self.command)} failed with exit code {self.returncode} in the new worktree"
        )


def _install_command(value: Sequence[str] | str | None) -> tuple[str, ...]:
    """Normalize a configured install command; empty or false disables it."""

    if value is None:
        return DEFAULT_INSTALL_COMMAND
    if isinstance(value, str):
        text = value.strip()
        if not text or text.casefold() in {"none", "false", "off", "skip"}:
            return ()
        return tuple(shlex.split(text))
    return tuple(str(item) for item in value)


def free_disk_bytes(path: str | Path) -> int:
    """Return free bytes on the volume containing *path*.

    ``path`` need not exist yet (important for a first worktree); the nearest
    existing parent is used for ``disk_usage``.
    """

    candidate = Path(path).expanduser()
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return int(shutil.disk_usage(candidate).free)


def free_disk_gb(path: str | Path = ".") -> float:
    return free_disk_bytes(path) / (1024**3)


def _common_git_dir(repo: Path) -> Path:
    """The operator repository's common git directory (never call it on a builder worktree)."""

    result = run_git(repo, "rev-parse", "--git-common-dir", check=True)
    raw = Path(result.stdout.strip())
    return (repo / raw).resolve() if not raw.is_absolute() else raw.resolve()


@contextlib.contextmanager
def git_lock(repo: str | Path) -> Iterator[Path]:
    """Take the repository-wide Harbor lock for git metadata mutations."""

    repository = Path(repo).expanduser().resolve()
    common = _common_git_dir(repository)
    common.mkdir(parents=True, exist_ok=True)
    lock_path = common / "harbor.lock"
    was_present = lock_path.exists()
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield lock_path
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    # Avoid leaving a Harbor-only file in the user's checkout when this call
    # created it.  A pre-existing lock belongs to another process and is kept.
    if not was_present:
        with contextlib.suppress(OSError):
            lock_path.unlink()


def _run_git(repo: Path, args: Sequence[str], *, worktree_of: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run git in the operator's ``repo``, or, with ``worktree_of``, in builder worktree ``repo`` of it.

    A builder worktree whose git metadata does not lead back to
    ``worktree_of`` is refused before git runs (see ``safe_git``).
    """

    try:
        if worktree_of is not None:
            return run_worktree_git(repo, worktree_of, *args, check=True)
        return run_git(repo, *args, check=True)
    except UnsafeGitDirectory as exc:
        raise WorktreeConfinementError(f"worktree repository mismatch: {exc}") from exc
    except FileNotFoundError as exc:  # pragma: no cover - host configuration
        raise WorktreeError("git executable was not found") from exc
    except subprocess.CalledProcessError as exc:
        details = (exc.stderr or exc.stdout or "").strip()
        command = "git " + " ".join(args)
        raise WorktreeError(f"{command} failed: {details or 'unknown git error'}") from exc


def _safe_ticket(ticket: str) -> str:
    value = str(ticket).strip()
    if not _SAFE_TICKET.fullmatch(value):
        raise WorktreeConfinementError(
            "ticket identifier must contain only letters, digits, '.', '_' or '-'; "
            "path separators are not allowed"
        )
    return value


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class Worktree:
    repo: Path
    path: Path
    branch: str
    base_ref: str

    @property
    def root(self) -> Path:
        return self.path

    def contains(self, path: str | Path) -> bool:
        candidate = Path(path).expanduser().resolve()
        return _inside(candidate, self.path.resolve())


class WorktreeManager:
    """Create and remove one isolated worktree per ticket.

    ``root`` defaults to Harbor's per-project worktree directory.  Tests can
    pass a temporary root and a fake disk-floor function without touching a
    real project checkout.
    """

    def __init__(
        self,
        repo: str | Path,
        *,
        root: str | Path | None = None,
        repo_name: str | None = None,
        remote: str = "origin",
        base_branch: str = "main",
        disk_floor_gb: float = DEFAULT_DISK_FLOOR_GB,
        disk_usage: object | None = None,
        install_command: Sequence[str] | str | None = None,
        install_timeout_seconds: float = DEFAULT_INSTALL_TIMEOUT_SECONDS,
        installer: object | None = None,
        custody=None,
    ) -> None:
        self.repo = Path(repo).expanduser().resolve()
        if not self.repo.is_dir():
            raise WorktreeError(f"repository does not exist: {self.repo}")
        self.repo_name = repo_name or self.repo.name
        self.root = (
            Path(root).expanduser().resolve()
            if root is not None
            else (Path.home() / ".harbor" / "wt" / self.repo_name).resolve()
        )
        self.remote = remote
        self.base_branch = base_branch
        self.disk_floor_gb = float(disk_floor_gb)
        self._disk_usage = disk_usage or free_disk_gb
        self.install_command = _install_command(install_command)
        self.install_timeout_seconds = float(install_timeout_seconds)
        self._installer = installer
        self.custody = custody
        if custody is not None:
            custody.execute("CREATE TABLE IF NOT EXISTS worktree_setup (project TEXT, ticket TEXT, repo TEXT, path TEXT UNIQUE, branch TEXT, head TEXT, status TEXT, PRIMARY KEY(project,ticket))")
            custody.commit()

    @property
    def base_ref(self) -> str:
        return f"{self.remote}/{self.base_branch}"

    def _target(self, ticket: str) -> Path:
        safe = _safe_ticket(ticket)
        raw = self.root / safe
        if raw.is_symlink():
            raise WorktreeConfinementError("worktree must not be a symlink")
        target = raw.resolve()
        if not _inside(target, self.root.resolve()) or target == self.root.resolve():
            raise WorktreeConfinementError(f"worktree path escapes Harbor root: {target}")
        return target

    def _check_disk(self) -> None:
        # Check the existing parent before creating the root so the floor check
        # itself cannot fail merely because this is the first dispatch.
        available = float(self._disk_usage(self.root))
        if available < self.disk_floor_gb:
            raise DiskFloor(available, self.disk_floor_gb)

    def create(self, ticket: str, *, retry_setup: bool = False) -> Worktree:
        """Create *ticket* from a freshly fetched ``origin/main`` ref."""

        target = self._target(ticket)
        branch = f"harbor/{_safe_ticket(ticket)}"
        retryable_ready = False
        with git_lock(self.repo):
            self._check_disk()
            if target.exists():
                row = self.custody.execute("SELECT repo,path,branch,head,status FROM worktree_setup WHERE project=? AND ticket=?", (self.repo_name,ticket)).fetchone() if self.custody is not None else None
                retryable_ready = False
                if row and row[4] == "ready" and retry_setup:
                    has_runs = self.custody.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runs'").fetchone()
                    retryable_ready = not has_runs or not self.custody.execute("SELECT 1 FROM runs WHERE ticket=?",(ticket,)).fetchone()
                if not row or tuple(row[:3]) != (str(self.repo),str(target),branch) or (row[4] != "installing" and not retryable_ready):
                    raise WorktreeError(f"worktree path already exists without retryable custody: {target}")
                if self._git(target,["rev-parse","--show-toplevel"]).stdout.strip() != str(target):
                    raise WorktreeConfinementError("setup worktree repository mismatch")
                if self._git(target,["branch","--show-current"]).stdout.strip() != branch or self._git(target,["rev-parse","HEAD"]).stdout.strip() != row[3]:
                    raise WorktreeError("setup worktree branch or HEAD changed")
                worktree = Worktree(self.repo,target,branch,self.base_ref)
            else:
                _run_git(self.repo, ["worktree", "prune"])
                _run_git(self.repo, ["fetch", self.remote, self.base_branch])
                target.parent.mkdir(parents=True, exist_ok=True)
                _run_git(self.repo, ["worktree", "add", "-b", branch, str(target), self.base_ref])
                worktree = Worktree(self.repo, target, branch, self.base_ref)
                self.verify(worktree)
                if self.custody is not None:
                    with self.custody:
                        self.custody.execute("INSERT OR REPLACE INTO worktree_setup VALUES (?,?,?,?,?,?,?)", (self.repo_name,ticket,str(self.repo),str(target),branch,self._git(target,["rev-parse","HEAD"]).stdout.strip(),"installing"))
        if not retryable_ready:
            self.install(worktree)
            if self.custody is not None and self.install_command:
                # The first builder turn's pre-turn check (harbor.dependencies)
                # then sees these inputs as installed instead of installing twice.
                # They come from the base branch, so they are trusted: a later
                # reinstall of these same inputs may run install scripts.
                from harbor.dependencies import record
                record(self.custody, worktree.path, self.install_command, status="installed", trusted=True,
                       project=self.repo_name)
        if self.custody is not None:
            with self.custody:
                self.custody.execute("UPDATE worktree_setup SET status='ready' WHERE project=? AND ticket=?", (self.repo_name,ticket))
        return worktree

    ensure = create

    def attach(self, ticket: str) -> Worktree:
        """Attach only an exact retained path in this repository and branch."""
        target = self._target(ticket)
        if self.custody is None:
            raise WorktreeError("retained worktree requires durable custody")
        row = self.custody.execute("SELECT worktree FROM runs WHERE ticket=? ORDER BY COALESCE(finished_at,started_at,id) DESC,id DESC LIMIT 1",(ticket,)).fetchone()
        if not row or not row[0] or Path(row[0]).is_symlink() or Path(row[0]).resolve() != target:
            raise WorktreeConfinementError("retained worktree custody mismatch")
        branch = f"harbor/{_safe_ticket(ticket)}"
        if self._git(target,["rev-parse","--show-toplevel"]).stdout.strip() != str(target):
            raise WorktreeConfinementError("retained worktree repository mismatch")
        if self._git(target,["branch","--show-current"]).stdout.strip() != branch:
            raise WorktreeError("retained worktree branch mismatch")
        return Worktree(self.repo,target,branch,self.base_ref)

    def install(self, worktree: Worktree) -> None:
        """Install dependencies in *worktree* so the builder starts runnable.

        A fresh worktree has the repository's files but none of its installed
        packages, so without this the builder's first command fails on a
        missing module -- exactly the rung-0 blocker the ladder then has to
        remediate.

        It runs outside any builder sandbox and inherits Harbor's whole
        environment, credentials included, on a worktree made from the base
        branch. The reinstall before each builder turn
        (``harbor.dependencies``) runs the same command on files the builder
        can edit, so it removes Harbor's credentials first.
        """

        if not self.install_command:
            return
        target = Path(worktree.path if isinstance(worktree, Worktree) else worktree).expanduser().resolve()
        if not _inside(target, self.root.resolve()):
            raise WorktreeConfinementError(f"refusing to install outside Harbor root: {target}")
        if self._installer is not None:
            self._installer(target, self.install_command)
            return
        try:
            completed = subprocess.run(
                list(self.install_command),
                cwd=str(target),
                capture_output=True,
                text=True,
                timeout=self.install_timeout_seconds,
                check=False,
            )
        except FileNotFoundError as exc:
            raise WorktreeInstallError(self.install_command, 127, str(exc)) from exc
        except subprocess.TimeoutExpired as exc:
            raise WorktreeInstallError(
                self.install_command, 124, f"timed out after {self.install_timeout_seconds:.0f}s"
            ) from exc
        if completed.returncode != 0:
            raise WorktreeInstallError(
                self.install_command,
                completed.returncode,
                (completed.stderr or completed.stdout or "").strip()[-2000:],
            )

    def verify(self, worktree: Worktree) -> None:
        repo = Path(worktree.repo).expanduser().resolve()
        target = Path(worktree.path).expanduser().resolve()
        if repo != self.repo or not _inside(target, self.root.resolve()):
            raise WorktreeConfinementError("worktree does not belong to this manager")
        if not target.is_dir():
            raise WorktreeError(f"worktree is missing: {target}")
        branch = self._git(target, ["branch", "--show-current"]).stdout.strip()
        if branch != worktree.branch:
            raise WorktreeError(f"unexpected worktree branch {branch!r}; expected {worktree.branch!r}")
        target_head = self._git(target, ["rev-parse", "HEAD"]).stdout.strip()
        base_head = _run_git(self.repo, ["rev-parse", worktree.base_ref]).stdout.strip()
        if target_head != base_head:
            raise WorktreeError(
                f"worktree is not based on fresh {worktree.base_ref} ({target_head} != {base_head})"
            )
        if self._git(target, ["status", "--porcelain", IGNORE_SUBMODULES]).stdout:
            raise WorktreeError("new worktree is unexpectedly dirty")

    def remove(self, worktree: Worktree | str | Path) -> None:
        """Remove one verified worktree, retaining its branch for audit."""

        raw = Path(worktree.path if isinstance(worktree, Worktree) else worktree).expanduser()
        if raw.is_symlink():
            raise WorktreeConfinementError("refusing to remove symlink")
        target = raw.resolve()
        if not _inside(target, self.root.resolve()) or target == self.root.resolve():
            raise WorktreeConfinementError(f"refusing to remove path outside Harbor root: {target}")
        if not target.exists() and not target.is_symlink():
            return
        with git_lock(self.repo):
            # Harbor's git never enters a submodule or nested repository (its
            # config is the builder's), so it cannot tell whether one holds
            # uncommitted work: ``status`` reports a committed gitlink as clean
            # even when files inside it are modified.  Keep any worktree that
            # has one rather than delete work unseen.
            nested = self._nested_repositories(target)
            if nested:
                raise WorktreeError(
                    f"cleanup retained a worktree containing a submodule or nested git repository ({nested}); "
                    "Harbor does not look inside those, so check them for uncommitted work and remove the worktree by hand"
                )
            # Modified or untracked files are work someone may want: keep the
            # worktree for inspection. Ignored files are install/build output
            # (node_modules, .next, ...) that every worktree accumulates; they
            # never block cleanup, or no worktree could ever be removed.
            # ``_git`` first proves the worktree's metadata is this repository's.
            if self._git(target, ["status", "--porcelain", IGNORE_SUBMODULES, "--untracked-files=all"]).stdout:
                raise WorktreeError("cleanup retained modified or untracked files")
            for line in self._git(target, ["status", "--porcelain", IGNORE_SUBMODULES, "--ignored", "--untracked-files=all"]).stdout.splitlines():
                if not line.startswith("!! "):
                    continue
                ignored = (target / line[3:].rstrip("/")).resolve() if not (target / line[3:]).is_symlink() else target / line[3:]
                if not _inside(ignored, target):
                    raise WorktreeConfinementError(f"ignored path escapes the worktree: {line[3:]}")
            # --force is required because git also refuses ignored content;
            # the status check above already proved nothing else remains.
            _run_git(self.repo, ["worktree", "remove", "--force", str(target)])
            _run_git(self.repo, ["worktree", "prune"])

    def path_for(self, ticket: str) -> Path:
        return self._target(ticket)

    def _nested_repositories(self, target: Path) -> str | None:
        """The first gitlink in ``target``'s index, or nested ``.git`` under it, read without entering either."""

        for line in self._git(target, ["ls-files", "--stage"]).stdout.splitlines():
            if line.startswith("160000 "):
                return line.split("\t", 1)[-1]
        for directory, dirs, files in os.walk(target, followlinks=False):
            here = Path(directory)
            if here != target and (".git" in dirs or ".git" in files):
                return str(here.relative_to(target))
            dirs[:] = [name for name in dirs if name != ".git"]
        return None

    def _git(self, target: Path, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        """Git in a builder worktree, refused unless its metadata belongs to this repository."""

        return _run_git(target, args, worktree_of=self.repo)


def create_worktree(repo: str | Path, ticket: str, **kwargs: object) -> Worktree:
    return WorktreeManager(repo, **kwargs).create(ticket)


__all__ = [
    "DEFAULT_DISK_FLOOR_GB",
    "DEFAULT_INSTALL_COMMAND",
    "DiskFloor",
    "Worktree",
    "WorktreeConfinementError",
    "WorktreeError",
    "WorktreeInstallError",
    "WorktreeManager",
    "create_worktree",
    "free_disk_bytes",
    "free_disk_gb",
    "git_lock",
]
