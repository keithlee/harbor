"""Codex ``exec`` runner for Harbor's persistent builder role."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Sequence

from harbor.runner.base import BaseRunner, RunResult, _db_connection, _table_exists
from harbor.resources import runtime_asset


DEFAULT_MODEL = "gpt-5.6-luna"
DEFAULT_REASONING_EFFORT = "xhigh"


class CodexRunner(BaseRunner):
    """Run the configured delivery skill in a worktree using Codex.

    A logical Harbor ``run_id`` remains stable across ``resume`` calls.  The
    Codex session id is captured from the JSONL stream when available and is
    used for subsequent ``codex exec resume`` invocations.
    """

    def __init__(
        self,
        worktree: str | Path,
        *,
        db: Any | None = None,
        ticket: str = "",
        model: str = DEFAULT_MODEL,
        reasoning_effort: str = DEFAULT_REASONING_EFFORT,
        sandbox: str = "workspace-write",
        schema_path: str | Path | None = None,
        executable: str = "codex",
        deadline_seconds: float = 90 * 60,
        run_id: str | None = None,
        clock: Callable[[], float] = time.monotonic,
        git_dir: str | Path | None = None,
        writable_roots: Sequence[str | Path] = (),
        network_access: bool = True,
    ) -> None:
        super().__init__(
            worktree,
            db=db,
            ticket=ticket,
            deadline_seconds=deadline_seconds,
            run_id=run_id,
            clock=clock,
        )
        if not model.strip():
            raise ValueError("Codex model is required")
        if reasoning_effort not in {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}:
            raise ValueError(f"unsupported reasoning effort: {reasoning_effort}")
        if sandbox != "workspace-write":
            raise ValueError("builder Codex runner requires workspace-write sandbox")
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.sandbox = sandbox
        self.executable = executable
        self.schema_path = (
            Path(schema_path).expanduser().resolve()
            if schema_path is not None
            else runtime_asset("schemas", "run-result.json")
        )
        # A linked worktree's ``.git`` is a FILE pointing at
        # ``<repo>/.git/worktrees/<name>``.  Commits write objects, refs, reflogs
        # and per-worktree metadata there -- all outside the worktree -- so a
        # plain workspace-write sandbox cannot commit at all.  The repository's
        # git directory is therefore a required writable root.  The operator's
        # working tree is NOT included, so the checkout stays untouched.
        roots: list[str] = [str(Path(item).expanduser().resolve()) for item in writable_roots]
        self.git_dir = Path(git_dir).expanduser().resolve() if git_dir is not None else self._discover_git_dir()
        if self.git_dir is not None and str(self.git_dir) not in roots:
            roots.append(str(self.git_dir))
        self.writable_roots = tuple(roots)
        # The builder pushes and opens the PR itself, so it needs the network.
        self.network_access = bool(network_access)

    def _discover_git_dir(self) -> Path | None:
        """Resolve the shared git directory backing this linked worktree."""

        pointer = self.worktree / ".git"
        try:
            if pointer.is_file():
                text = pointer.read_text(encoding="utf-8").strip()
                if text.startswith("gitdir:"):
                    target = Path(text.split(":", 1)[1].strip()).expanduser()
                    # <repo>/.git/worktrees/<name> -> <repo>/.git
                    for parent in (target, *target.parents):
                        if parent.name == ".git":
                            return parent.resolve()
                    return target.resolve()
            if pointer.is_dir():
                return pointer.resolve()
        except OSError:
            return None
        return None

    def _sandbox_config(self) -> list[str]:
        """Codex ``-c`` overrides that make a linked worktree usable."""

        flags: list[str] = []
        if self.writable_roots:
            flags += ["-c", f"sandbox_workspace_write.writable_roots={json.dumps(list(self.writable_roots))}"]
        if self.network_access:
            flags += ["-c", "sandbox_workspace_write.network_access=true"]
        return flags

    def command(self, instruction: str, *, run_id: str, resume: bool = False) -> list[str]:
        del instruction  # prompt is supplied over stdin to avoid argv leakage
        schema = str(self.schema_path)
        if resume:
            # ``resume`` retains the original worktree/sandbox/session context;
            # Codex's resume subcommand accepts model/schema/json overrides but
            # intentionally does not accept -C or -s.
            return [
                self.executable,
                "exec",
                "resume",
                run_id,
                "-m",
                self.model,
                "-c",
                f"model_reasoning_effort={self.reasoning_effort}",
                "--output-schema",
                schema,
                "--json",
                "-",
            ]
        return [
            self.executable,
            "exec",
            "-m",
            self.model,
            "-C",
            str(self.worktree),
            "-s",
            self.sandbox,
            "-c",
            f"model_reasoning_effort={self.reasoning_effort}",
            *self._sandbox_config(),
            "--output-schema",
            schema,
            "--json",
            "-",
        ]

    def _stored_session_id(self, run_id: str) -> str | None:
        context = self.contexts.get(run_id)
        if context and context.session_id:
            return context.session_id
        connection = _db_connection(self.db)
        if connection is None or not _table_exists(connection, "runs"):
            return None
        columns = {row[1] for row in connection.execute("PRAGMA table_info(runs)")}
        if "session_id" not in columns:
            return None
        row = connection.execute("SELECT session_id FROM runs WHERE id = ?", (run_id,)).fetchone()
        return str(row[0]) if row and row[0] else None

    async def resume_run(self, run_id: str, instruction: str) -> RunResult:
        context = self.contexts.get(run_id)
        if context is None:
            context = self._new_context(run_id)
        context.session_id = self._stored_session_id(run_id) or context.session_id or run_id
        return await self._execute(context, instruction, resume=True)

    async def resume(self, run_id: str, instruction: str) -> RunResult:
        return await self.resume_run(run_id, instruction)

    continue_run = resume_run


CodexExecRunner = CodexRunner


__all__ = ["CodexExecRunner", "CodexRunner", "DEFAULT_MODEL", "DEFAULT_REASONING_EFFORT"]
