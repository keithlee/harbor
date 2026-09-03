"""The conversational half of Harbor's First Mate.

Harbor has two deliberately separate paths:

* rendering a status digest is synchronous and model-free; and
* an ordinary operator question starts one short-lived, read-only Terra
  invocation.

Keeping the runner behind a small injection seam makes the cost and command
boundary testable without starting Codex in unit tests.  It also means Ctrl+C
can cancel the one active answer task without touching supervision or another
answer.
"""

from __future__ import annotations

import asyncio
import inspect
import os
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from harbor.digest import render_digest


DEFAULT_MODEL = "gpt-5.6-terra"
VALID_REASONING_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"})


class Runner(Protocol):
    """Protocol for an injectable ``codex exec`` runner."""

    def __call__(self, command: Sequence[str], prompt: str) -> Any:
        ...


class FirstMateError(RuntimeError):
    """Raised when the read-only First Mate invocation fails."""


def build_command(
    model: str = DEFAULT_MODEL,
    *,
    reasoning_effort: str | None = None,
    effort: str | None = None,
    json_output: bool = False,
) -> list[str]:
    """Build the only command shape allowed for an ordinary First Mate answer.

    The caller may select a model for local configuration, but the default is
    Terra.  ``read-only`` and ``--ephemeral`` are intentionally unconditional:
    a conversational answer never gets a worktree or persistent write access.
    """

    if not str(model).strip():
        raise ValueError("First Mate model must not be empty")
    command = ["codex", "exec", "-m", str(model), "-s", "read-only", "--ephemeral"]
    selected_effort = reasoning_effort if reasoning_effort is not None else effort
    if selected_effort is not None:
        selected_effort = str(selected_effort).strip().casefold()
        if selected_effort not in VALID_REASONING_EFFORTS:
            raise ValueError(f"unsupported reasoning effort: {selected_effort}")
        command.extend(["-c", f"model_reasoning_effort={selected_effort}"])
    if json_output:
        command.append("--json")
    return command


first_mate_command = build_command
codex_command = build_command
build_codex_command = build_command


def build_prompt(question: str, digest: str) -> str:
    """Compose a bounded prompt from one observed digest and one question.

    Delimiters make it clear that ticket titles, errors, and other observed
    text are data rather than instructions.  The question itself is similarly
    delimited so the runner can never accidentally append a second implicit
    task to the prompt.
    """

    question = str(question).strip()
    if not question:
        raise ValueError("a plain First Mate question is required")
    return (
        "You are Harbor's First Mate. Answer the operator's question using only "
        "the read-only observed status below. Do not edit files, run mutations, "
        "or claim work that is not present in the status.\n"
        "<harbor-status>\n"
        f"{digest}\n"
        "</harbor-status>\n"
        "<operator-question>\n"
        f"{question}\n"
        "</operator-question>"
    )


def _result_text(result: Any) -> str:
    """Extract human-readable text from common runner result shapes."""

    if result is None:
        return ""
    if isinstance(result, bytes):
        return result.decode("utf-8", errors="replace").strip()
    if isinstance(result, str):
        return result.strip()
    if isinstance(result, Mapping):
        for key in ("answer", "text", "output", "stdout", "content"):
            if key in result:
                return _result_text(result[key])
    for name in ("stdout", "output", "text", "answer"):
        value = getattr(result, name, None)
        if value is not None:
            return _result_text(value)
    return str(result).strip()


class CodexExecRunner:
    """Default async runner for one read-only ephemeral Codex process."""

    def __init__(self, *, env: Mapping[str, str] | None = None) -> None:
        self.env = dict(env) if env is not None else None

    async def __call__(self, command: Sequence[str], prompt: str) -> str:
        # ``create_subprocess_exec`` is intentionally used instead of a shell;
        # the operator's question is sent over stdin and cannot become argv.
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=dict(self.env) if self.env is not None else dict(os.environ),
        )
        try:
            stdout, stderr = await process.communicate(prompt.encode("utf-8"))
        except asyncio.CancelledError:
            # Cancellation must not leave a Codex child behind.  This process
            # belongs to this answer only; no supervisor task is cancelled.
            process.kill()
            await process.wait()
            raise
        output = stdout.decode("utf-8", errors="replace").strip()
        if process.returncode:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise FirstMateError(detail or f"codex exited with status {process.returncode}")
        return output


async def _invoke(runner: Any, command: Sequence[str], prompt: str) -> Any:
    """Invoke a callable or runner object without making fallback calls.

    Two arguments are the canonical seam (``command, prompt``), while the
    one-argument ``prompt`` form is useful for tiny test doubles and embedded
    runners.  Signature inspection chooses one form before invocation, so a
    failed call is never retried and cannot turn one question into two model
    calls.
    """

    target: Any = runner
    if not callable(target):
        for name in ("run", "execute", "invoke"):
            candidate = getattr(runner, name, None)
            if callable(candidate):
                target = candidate
                break
    if not callable(target):
        raise TypeError("First Mate runner must be callable or expose run(command, prompt)")
    try:
        signature = inspect.signature(target)
    except (TypeError, ValueError):
        signature = None
    if signature is None:
        result = target(command, prompt)
    else:
        parameters = list(signature.parameters.values())
        positional = [
            parameter
            for parameter in parameters
            if parameter.kind
            in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        keyword_only = {parameter.name for parameter in parameters if parameter.kind == inspect.Parameter.KEYWORD_ONLY}
        if {"command", "prompt"}.issubset(keyword_only):
            result = target(command=command, prompt=prompt)
        elif len(positional) <= 1 and not any(parameter.kind == inspect.Parameter.VAR_POSITIONAL for parameter in parameters):
            parameter_name = positional[0].name.casefold() if positional else "prompt"
            result = target(command if parameter_name in {"command", "cmd", "argv", "args"} else prompt)
        else:
            result = target(command, prompt)
    if inspect.isawaitable(result):
        return await result
    return result


class FirstMate:
    """Answer plain operator questions from a deterministic status source."""

    def __init__(
        self,
        status_source: Any | None = None,
        *,
        runner: Any | None = None,
        model: str = DEFAULT_MODEL,
        reasoning_effort: str | None = None,
        effort: str | None = None,
        notifier: Any | None = None,
        status_provider: Any | None = None,
        digest_provider: Any | None = None,
    ) -> None:
        if status_source is None:
            status_source = status_provider if status_provider is not None else digest_provider
        self.status_source = status_source
        self.model = model
        self.reasoning_effort = reasoning_effort if reasoning_effort is not None else effort
        self.runner = runner if runner is not None else CodexExecRunner()
        self.notifier = notifier
        self._active_answer: asyncio.Task[Any] | None = None

    def digest(self, status: Any | None = None) -> str:
        """Return the same synchronous digest used by ``/status``."""

        source = self.status_source if status is None else status
        return render_digest(source if source is not None else {})

    status_text = digest
    status = digest

    def make_prompt(self, question: str, status: Any | None = None, *, digest: str | None = None) -> str:
        return build_prompt(question, digest if digest is not None else self.digest(status))

    async def answer(
        self,
        question: str,
        status: Any | None = None,
        *,
        digest: str | None = None,
    ) -> str:
        """Make exactly one Terra call for one plain question.

        There are no retries or hidden status polls here.  If the runner
        fails, its error is propagated to the console and the active handle is
        cleared.  A second concurrent answer is rejected so cancellation can
        only ever target the visible active generation.
        """

        if self._active_answer is not None and not self._active_answer.done():
            raise RuntimeError("a First Mate answer is already active")
        prompt = self.make_prompt(question, status, digest=digest)
        command = build_command(self.model, reasoning_effort=self.reasoning_effort)
        task = asyncio.current_task()
        self._active_answer = task
        try:
            result = await _invoke(self.runner, command, prompt)
            return _result_text(result)
        finally:
            if self._active_answer is task:
                self._active_answer = None

    ask = answer
    respond = answer
    answer_question = answer

    @property
    def active_answer(self) -> asyncio.Task[Any] | None:
        """The task currently generating an answer, if any."""

        return self._active_answer

    def cancel_active_answer(self) -> bool:
        """Cancel only the currently generating answer, if one exists."""

        task = self._active_answer
        if task is None or task.done():
            return False
        task.cancel()
        return True

    cancel = cancel_active_answer
    cancel_answer = cancel_active_answer

    async def notify_park(self, reason: str) -> Any:
        """Notify the operator about a park, preserving the literal reason."""

        if self.notifier is None:
            return None
        target = getattr(self.notifier, "notify", self.notifier)
        if not callable(target):
            raise TypeError("First Mate notifier must be callable or expose notify(reason)")
        result = target(str(reason))
        if inspect.isawaitable(result):
            return await result
        return result


FirstMateRunner = CodexExecRunner
CodexRunner = CodexExecRunner


__all__ = [
    "DEFAULT_MODEL",
    "VALID_REASONING_EFFORTS",
    "CodexExecRunner",
    "CodexRunner",
    "FirstMate",
    "FirstMateError",
    "FirstMateRunner",
    "Runner",
    "build_command",
    "build_codex_command",
    "build_prompt",
    "codex_command",
    "first_mate_command",
]
