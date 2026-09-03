"""The persistent first-mate console.

Phase 1 intentionally answers only deterministic commands.  Free-form text is
acknowledged without invoking a model; the conversational role arrives later.
``prompt_toolkit``'s stdout patch keeps supervisor events above a partially
typed prompt instead of splicing into the user's input.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
from collections.abc import Callable
from typing import Any, TextIO

try:  # prompt_toolkit is a runtime dependency, but a simple fallback helps embedders.
    from prompt_toolkit import PromptSession
    from prompt_toolkit.patch_stdout import patch_stdout
except ImportError:  # pragma: no cover - exercised only in minimal embeddings
    PromptSession = None  # type: ignore[assignment,misc]

    class _PatchStdout:
        def __enter__(self) -> "_PatchStdout":
            return self

        def __exit__(self, *_: object) -> None:
            return None

    def patch_stdout(*, raw: bool = False) -> _PatchStdout:  # type: ignore[misc]
        return _PatchStdout()

from harbor.supervisor import Supervisor
from harbor.digest import render_digest
from harbor.first_mate import FirstMate, FirstMateError


class Console:
    prompt = "first-mate> "

    def __init__(
        self,
        supervisor: Supervisor,
        *,
        output: Callable[[str], Any] | TextIO | None = None,
        first_mate: FirstMate | None = None,
    ) -> None:
        self.supervisor = supervisor
        self._output = output or sys.stdout
        # Console remains embeddable without a model.  The CLI supplies the
        # configured FirstMate; tests and minimal hosts can omit it and still
        # use all deterministic slash commands.
        self.first_mate = first_mate
        self.exiting = False
        self._answer_task: asyncio.Task[str] | None = None
        self._answer_cancel_requested = False
        self.supervisor.on_update = self._on_update
        self.supervisor.on_heartbeat = self._on_heartbeat

    def _emit(self, message: str = "") -> None:
        if callable(self._output):
            self._output(message)
        else:
            self._output.write(message + "\n")
            self._output.flush()

    async def _on_update(self, result: Any) -> None:
        if not result.changed:
            return
        # Under patch_stdout this is redrawn above the active prompt.
        self._emit(f"Harbor: observed {len(result.tickets)} Linear ticket(s) · snapshot changed")

    async def _on_heartbeat(self, event: Any) -> None:
        # This is a publication only.  It never invokes FirstMate and healthy
        # ticks do not pass through the supervisor wake path.
        if getattr(event, "healthy", False):
            self._emit(
                f"Harbor: heartbeat · {getattr(event, 'project', self.supervisor.project)} · healthy"
            )
        else:
            self._emit(
                f"Harbor: heartbeat · {getattr(event, 'project', self.supervisor.project)} · unavailable"
            )

    def banner(self) -> str:
        status = self.supervisor.status()
        health = "✓" if status.linear_healthy else "✗"
        hold = "on" if self.supervisor.poller.config.hold_all else "off"
        return (
            f"Harbor: {status.lifecycle} · deterministic first mate · {status.project} · Linear {health}\n"
            f"Scope: {', '.join(self.supervisor.poller.config.dispatch_states)} → dispatch · "
            f"Backlog ignored · hold_all: {hold} (shadow mode)"
        )

    def status_text(self) -> str:
        # Keep the operator view and the context supplied to First Mate on one
        # formatting boundary.  This is intentionally synchronous and has no
        # polling/model side effects.
        return render_digest(self.supervisor.status())

    @staticmethod
    def _pr_label(url: str) -> str:
        bits = str(url).rstrip("/").split("/")
        if bits and bits[-1].isdigit():
            return f"PR #{bits[-1]}"
        return str(url)

    def help_text(self) -> str:
        return (
            "/status  /pause [reason]  /hold [reason]  /resume  "
            "/answer <escalation-id> <answer>  /kill [run-id]  /exit  /help"
        )

    async def _invoke_control(self, names: tuple[str, ...], argument: str) -> bool:
        """Invoke one optional lifecycle hook without guessing or retrying.

        ``Supervisor`` currently exposes pause/resume but builder termination
        remains an integration seam.  Looking for the hook on the supervisor,
        dispatcher, and runner keeps commands behavioral and model-free while
        allowing the eventual process owner to supply the exact implementation.
        Signature inspection selects one call shape before invocation, so a
        TypeError from a real hook is not retried as a second operation.
        """

        owners = (
            self.supervisor,
            getattr(self.supervisor, "dispatcher", None),
            getattr(self.supervisor, "runner", None),
        )
        for owner in owners:
            if owner is None:
                continue
            method = next((getattr(owner, name, None) for name in names if callable(getattr(owner, name, None))), None)
            if method is None:
                continue
            try:
                signature = inspect.signature(method)
            except (TypeError, ValueError):
                signature = None
            if signature is None:
                value = method(argument) if argument else method()
            else:
                parameters = list(signature.parameters.values())
                positional = [
                    parameter
                    for parameter in parameters
                    if parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
                ]
                keyword_only = {parameter.name for parameter in parameters if parameter.kind == inspect.Parameter.KEYWORD_ONLY}
                if "reason" in keyword_only:
                    value = method(reason=argument)
                elif "run_id" in keyword_only or "runid" in keyword_only:
                    value = method(run_id=argument or None)
                elif positional:
                    parameter = positional[0]
                    if parameter.name.casefold() in {"run_id", "runid", "id"}:
                        value = method(argument or None)
                    elif argument or parameter.default is inspect.Parameter.empty:
                        value = method(argument)
                    else:
                        value = method()
                else:
                    value = method()
            if inspect.isawaitable(value):
                await value
            return True
        return False

    async def _answer_question(self, question: str) -> str:
        if self.first_mate is None:
            return "Phase 1 handles /status, /pause, /resume, /exit, and /help; no model call was made."
        # A question gets exactly one fresh observation and exactly one model
        # invocation.  Rendering the digest is synchronous and model-free.
        refresh = getattr(self.supervisor, "fresh_digest", None)
        if callable(refresh):
            digest = refresh()
            if asyncio.iscoroutine(digest):
                digest = await digest
        else:
            digest = render_digest(self.supervisor.status())
        return await self.first_mate.answer(question, digest=digest)

    async def _answer(self, question: str) -> str:
        if self._answer_task is not None and not self._answer_task.done():
            return "Harbor: an answer is already in progress."
        task = asyncio.create_task(self._answer_question(question), name="harbor-first-mate-answer")
        self._answer_task = task
        try:
            return await task
        except asyncio.CancelledError:
            # A cancellation is scoped to this one task.  The supervisor's
            # polling/heartbeat tasks are deliberately untouched.
            if self._answer_cancel_requested:
                return "Harbor: answer cancelled"
            raise
        except FirstMateError as exc:
            return f"Harbor: First Mate unavailable: {exc}"
        finally:
            self._answer_cancel_requested = False
            if self._answer_task is task:
                self._answer_task = None

    def cancel_answer(self) -> bool:
        """Cancel only the visible plain-question answer, if one is running."""

        task = self._answer_task
        if task is None or task.done():
            return False
        self._answer_cancel_requested = True
        if self.first_mate is not None:
            self.first_mate.cancel_active_answer()
        task.cancel()
        return True

    async def handle_command(self, line: str) -> str | None:
        line = line.strip()
        if not line:
            return None
        if not line.startswith("/"):
            return await self._answer(line)
        parts = line.split(None, 1)
        command = parts[0].casefold()
        argument = parts[1] if len(parts) == 2 else ""
        if command in {"/help", "/h"}:
            return self.help_text()
        if command == "/status":
            return self.status_text()
        if command == "/pause":
            await self.supervisor.pause(argument.strip() or None)
            reason = f" · {argument.strip()}" if argument.strip() else ""
            return f"Harbor: PAUSED{reason}"
        if command == "/hold":
            reason = argument.strip() or "operator hold"
            # Pause is the current supervisor's hold implementation.  A
            # dedicated hold hook, when supplied by a host, takes precedence.
            await self._invoke_control(("hold", "pause"), reason)
            return f"Harbor: HOLD · {reason}"
        if command == "/resume":
            await self.supervisor.resume()
            return f"Harbor: {self.supervisor.status().lifecycle}"
        if command == "/answer":
            answer_parts = argument.strip().split(None, 1)
            if len(answer_parts) != 2 or not answer_parts[0] or not answer_parts[1].strip():
                return "Usage: /answer <escalation-id> <answer> (include Citation: path:line)"
            escalation_id, response = answer_parts
            try:
                result = await self.supervisor.answer_escalation(escalation_id, response)
            except (RuntimeError, ValueError) as exc:
                return f"Harbor: answer not recorded: {exc}"
            resumed = " · same builder resumed" if result.get("resumed") else " · builder session not available"
            return f"Harbor: answer recorded for escalation {escalation_id}{resumed}"
        if command == "/kill":
            target = argument.strip()
            handled = await self._invoke_control(("kill", "terminate_builder", "terminate"), target)
            if handled:
                suffix = f" · {target}" if target else ""
                return f"Harbor: kill requested{suffix}"
            return "Harbor: kill requested · no builder termination hook is configured"
        if command in {"/exit", "/quit"}:
            self.exiting = True
            await self.supervisor.exit()
            return "Harbor: STOPPED"
        return f"Unknown command: {command}. Type /help."

    async def run(self) -> None:
        self._emit(self.banner())
        session = PromptSession() if PromptSession is not None else None
        with patch_stdout(raw=True):
            while not self.exiting:
                try:
                    if session is not None:
                        line = await session.prompt_async(self.prompt)
                    else:  # pragma: no cover - fallback only
                        line = await asyncio.to_thread(input, self.prompt)
                except (EOFError, KeyboardInterrupt):
                    # Ctrl+C at an empty prompt exits the foreground console;
                    # Ctrl+C while _answer is awaited is handled below and
                    # leaves supervision running.
                    self.exiting = True
                    await self.supervisor.exit()
                    break
                try:
                    response = await self.handle_command(line)
                except KeyboardInterrupt:
                    if self.cancel_answer():
                        self._emit("Harbor: answer cancelled")
                        continue
                    self.exiting = True
                    await self.supervisor.exit()
                    break
                except asyncio.CancelledError:
                    if self.cancel_answer():
                        self._emit("Harbor: answer cancelled")
                        continue
                    raise
                if response:
                    self._emit(response)

    async def run_lines(self, lines: list[str]) -> list[str]:
        """Drive command handling without a terminal, useful for acceptance tests."""

        output: list[str] = []
        for line in lines:
            try:
                response = await self.handle_command(line)
            except asyncio.CancelledError:
                response = "Harbor: answer cancelled"
            if response:
                output.append(response)
            if self.exiting:
                break
        return output


__all__ = ["Console"]
