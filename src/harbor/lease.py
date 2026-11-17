"""Process-lifetime ownership for one Harbor project supervisor."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO


class ProjectLeaseError(RuntimeError):
    """Another live process already owns a project's supervisor lease."""

    def __init__(self, project: str, owner: str) -> None:
        self.project = str(project)
        self.owner = str(owner)
        super().__init__(
            f"{self.project} already has a live Harbor supervisor ({self.owner})"
        )


def _lease_path(db_path: str | Path, project: str) -> Path:
    project_name = str(project)
    safe_project = re.sub(r"[^A-Za-z0-9_.-]+", "-", project_name).strip("-") or "project"
    identity = hashlib.sha256(project_name.encode("utf-8")).hexdigest()[:12]
    filename = f"{safe_project}-{identity}.lock"
    return Path(db_path).expanduser().resolve().parent / "locks" / filename


class ProjectLease:
    """Hold a non-blocking filesystem lease until the supervisor exits.

    The lock file is deliberately retained after release. Removing a flocked
    path can create two lockable inodes and let two supervisors both believe
    they own the project. The next owner overwrites the diagnostic metadata.
    """

    def __init__(self, db_path: str | Path, project: str) -> None:
        self.project = str(project)
        self.path = _lease_path(db_path, self.project)
        self._handle: TextIO | None = None

    def acquire(self) -> "ProjectLease":
        if self._handle is not None:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        handle = os.fdopen(descriptor, "r+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.seek(0)
            owner = handle.read().strip() or "owner metadata unavailable"
            handle.close()
            raise ProjectLeaseError(self.project, owner) from exc
        try:
            metadata = {
                "project": self.project,
                "pid": os.getpid(),
                "cwd": os.getcwd(),
                "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            handle.seek(0)
            handle.truncate()
            json.dump(metadata, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        except Exception:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
            raise
        self._handle = handle
        return self

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> "ProjectLease":
        return self.acquire()

    def __exit__(self, *_: object) -> None:
        self.release()


__all__ = ["ProjectLease", "ProjectLeaseError"]
