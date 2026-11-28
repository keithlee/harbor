"""Codex ``exec`` runner for Harbor's persistent builder role."""

from __future__ import annotations

import inspect
import json
import math
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Callable, Sequence

from harbor.runner.base import BaseRunner, RunResult, _db_connection, _table_exists, _update_run_sql, iso_now
from harbor.runner.playwright import ENDPOINT_ENV, REQUIRED_ENV, browser_endpoint, validate_image
from harbor.resources import runtime_asset


DEFAULT_MODEL = "gpt-5.6-luna"
DEFAULT_REASONING_EFFORT = "xhigh"


class CodexRunner(BaseRunner):
    """Run the configured delivery skill in a worktree using Codex.

    A logical Harbor ``run_id`` remains stable across ``resume`` calls.  The
    Codex session id is captured from the JSONL stream when available and is
    used for subsequent ``codex exec resume`` invocations.
    """

    # Reinstalls the worktree's dependencies outside the sandbox before each
    # turn (harbor.dependencies.pre_turn_installer); None when the project has
    # no install_command.
    dependency_preparer: Callable[[Path], Any] | None = None

    def __init__(
        self,
        worktree: str | Path,
        *,
        db: Any | None = None,
        ticket: str = "",
        model: str = DEFAULT_MODEL,
        reasoning_effort: str = DEFAULT_REASONING_EFFORT,
        fallback_models: Sequence[str] = (),
        sandbox: str = "workspace-write",
        schema_path: str | Path | None = None,
        executable: str = "codex",
        deadline_seconds: float = 90 * 60,
        run_id: str | None = None,
        clock: Callable[[], float] = time.monotonic,
        git_dir: str | Path | None = None,
        writable_roots: Sequence[str | Path] = (),
        network_access: bool = True,
        playwright_browser_image: str | None = None,
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
        from harbor.model_policy import validate_fallbacks
        self.primary_model = model
        self.fallback_models = validate_fallbacks(model, fallback_models)
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
        validate_image(playwright_browser_image)
        if playwright_browser_image and not self.network_access:
            raise ValueError("the Playwright browser runtime requires builder network access")
        self.playwright_browser_image = playwright_browser_image
        self._playwright_endpoint = ""

    def _worktree_git_dir(self) -> Path | None:
        """Resolve this worktree's Git admin directory or pointer."""

        pointer = self.worktree / ".git"
        try:
            if pointer.is_file():
                text = pointer.read_text(encoding="utf-8").strip()
                if text.startswith("gitdir:"):
                    target = Path(text.split(":", 1)[1].strip()).expanduser()
                    return (self.worktree / target).resolve()
            if pointer.is_dir():
                return pointer.resolve()
        except OSError:
            return None
        return None

    def _discover_git_dir(self) -> Path | None:
        """Resolve the shared Git directory backing this linked worktree."""
        target = self._worktree_git_dir()
        if target is None:
            return None
        return next((parent for parent in (target, *target.parents) if parent.name == ".git"), target)

    def _sandbox_config(self) -> list[str]:
        """Codex ``-c`` overrides that make a linked worktree usable."""

        if self.playwright_browser_image:
            profile = "permissions.harbor-playwright"
            flags = [
                "-c", 'default_permissions="harbor-playwright"',
                "-c", f'{profile}.extends=":workspace"',
                "-c", f"{profile}.network.enabled=true",
            ]
            writable_roots = list(self.writable_roots)
            admin = self._worktree_git_dir()
            # The workspace profile protects the linked index independently
            # of the common Git root. Allow the linked admin directory while
            # retaining the protection on the worktree's .git pointer.
            if self.git_dir and admin and admin.parent == self.git_dir / "worktrees":
                writable_roots.append(str(admin))
            if writable_roots:
                writable = ", ".join(f'{json.dumps(root)}="write"' for root in dict.fromkeys(writable_roots))
                flags += ["-c", f"{profile}.filesystem={{{writable}}}"]
            return flags

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
            # Codex's resume subcommand accepts config overrides but
            # intentionally does not accept -C or -s. Reapply the sandbox
            # config because a new resume process does not inherit it.
            return [
                self.executable,
                "exec",
                "resume",
                run_id,
                "-m",
                self.model,
                "-c",
                f"model_reasoning_effort={self.reasoning_effort}",
                *self._sandbox_config(),
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
            *([] if self.playwright_browser_image else ["-s", self.sandbox]),
            "-c",
            f"model_reasoning_effort={self.reasoning_effort}",
            *self._sandbox_config(),
            "--output-schema",
            schema,
            "--json",
            "-",
        ]

    def _fallback_available(self, context):
        from harbor.model_policy import rejected_events
        chain = (self.primary_model, *self.fallback_models)
        return (not context.terminated and not context.deadline_expired and not context.session_mismatch
                and bool(context.session_id) and rejected_events(context.events)
                and chain.index(self.model) + 1 < len(chain))

    def _queue_transition(self, context, status, *, error=None):
        if status == "failed" and self._fallback_available(context):
            return  # Keep the queue owned while this invocation tries its fallback.
        return super()._queue_transition(context, status, error=error)

    def environment(self) -> dict[str, str]:
        env = super().environment()
        env.pop(ENDPOINT_ENV, None)
        env.pop(REQUIRED_ENV, None)
        if self.playwright_browser_image:
            env[REQUIRED_ENV] = "1"
        if self._playwright_endpoint:
            env[ENDPOINT_ENV] = self._playwright_endpoint
        return env

    async def _prepare_dependencies(self, context) -> tuple[str, RunResult | None]:
        """Run the pre-turn install: the prompt note, or the result of a turn Harbor refused to start."""

        if self.dependency_preparer is None:
            return "", None
        from harbor import dependencies
        # The preparer runs the install itself in a worker thread, keeping the loop free.
        check = self.dependency_preparer(context.worktree)
        if inspect.isawaitable(check):
            check = await check
        dependencies.log(self.ticket or context.run_id, check)
        if not check.failed:
            return dependencies.builder_note(check), None
        summary = f"Harbor did not start this builder turn: before it, Harbor {check.line()}"
        result = RunResult(
            status="blocked", summary=summary, waiting_on="human",
            blocker=dependencies.blocker(check, context.worktree, self.ticket),
            run_id=context.run_id, session_id=context.session_id, error=summary,
        )
        # Nothing was launched: the retained session stays as it was; record why the turn stopped.
        _update_run_sql(self.db, context.run_id, {
            "status": "blocked", "waiting_on": "human", "final_json": result.final_json,
            "terminal_reason": "dependency_install_failed", "finished_at": iso_now(),
        })
        # The wake was received and handled (the operator now owns the blocker), so
        # its receipt closes like any blocked turn's and is not replayed every poll.
        self._queue_transition(context, "completed", error=summary)
        return "", result

    async def _execute(self, context, instruction, *, resume, before_spawn: Callable[[], None] | None = None):
        if self.dependency_preparer is not None and before_spawn is not None:
            # Authorize the launch first (a stopped ticket or a superseded recovery
            # raises here), so a refused launch never triggers an unsandboxed install.
            # The launch path then calls before_spawn once per spawn, as before;
            # only the first call, made here, is not repeated.
            before_spawn()
            authorize = before_spawn
            pending = True

            def before_spawn() -> None:
                nonlocal pending
                if pending:
                    pending = False
                    return
                authorize()
        note, refused = await self._prepare_dependencies(context)
        if refused is not None:
            return refused
        if note:
            instruction = f"{instruction}\n\n{note}" if instruction else note
        runtime = nullcontext(("", ""))
        if self.playwright_browser_image:
            runtime = browser_endpoint(
                self.playwright_browser_image,
                lifetime_seconds=math.ceil(self.deadline_seconds) + 60,
            )
        async with runtime as (endpoint, reason):
            self._playwright_endpoint = endpoint
            try:
                if endpoint:
                    instruction += (
                        "\n\nA Playwright browser is available through "
                        f"`{ENDPOINT_ENV}`. For web proof, use the repository's "
                        "Playwright client to connect to it. Read the endpoint from "
                        "the environment without printing it."
                    )
                elif reason:
                    instruction += (
                        f"\n\nHarbor's Playwright browser is unavailable: {reason}. "
                        "Continue work that does not require browser proof and report "
                        "this blocker if browser proof is required."
                    )
                if before_spawn is not None:
                    return await self._execute_with_model_fallback(
                        context, instruction, resume=resume, before_spawn=before_spawn,
                    )
                return await self._execute_with_model_fallback(context, instruction, resume=resume)
            finally:
                self._playwright_endpoint = ""

    async def _execute_with_model_fallback(
        self, context, instruction, *, resume, before_spawn: Callable[[], None] | None = None,
    ):
        from harbor.model_policy import selected_model, record_fallback
        connection = _db_connection(self.db)
        project = getattr(self, "workflow_project", "")
        if connection is not None:
            self.model = selected_model(connection, project, self.ticket, context.run_id,
                                        self.primary_model, self.fallback_models)
        else:
            self.model = self.primary_model
        chain = (self.primary_model, *self.fallback_models)
        next_model = None

        def prepare_spawn() -> None:
            nonlocal next_model
            if before_spawn is not None:
                before_spawn()
            if next_model is None:
                return
            rejected = self.model
            self.model = next_model
            next_model = None
            if connection is not None:
                record_fallback(connection, project, self.ticket, context.run_id,
                                self.primary_model, rejected, self.model)

        while True:
            if before_spawn is None:
                prepare_spawn()
                result = await super()._execute(context, instruction, resume=resume)
            else:
                result = await super()._execute(context, instruction, resume=resume, before_spawn=prepare_spawn)
            if result.status != "failed" or not self._fallback_available(context):
                return result
            next_model = chain[chain.index(self.model) + 1]
            resume = True  # Retain the native session, logical run, worktree and budget.

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

    async def resume_run(self, run_id: str, instruction: str, *, before_spawn: Callable[[], None] | None = None) -> RunResult:
        context = self.contexts.get(run_id)
        if context is None:
            context = self._new_context(run_id)
        context.session_id = self._stored_session_id(run_id) or context.session_id or run_id
        if before_spawn is not None:
            return await self._execute(context, instruction, resume=True, before_spawn=before_spawn)
        return await self._execute(context, instruction, resume=True)

    async def resume(self, run_id: str, instruction: str, *, before_spawn: Callable[[], None] | None = None) -> RunResult:
        if before_spawn is not None:
            return await self.resume_run(run_id, instruction, before_spawn=before_spawn)
        return await self.resume_run(run_id, instruction)

    continue_run = resume_run


CodexExecRunner = CodexRunner


__all__ = ["CodexExecRunner", "CodexRunner", "DEFAULT_MODEL", "DEFAULT_REASONING_EFFORT"]
