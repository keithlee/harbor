"""The persistent first-mate console.

Phase 1 intentionally answers only deterministic commands.  Free-form text is
acknowledged without invoking a model; the conversational role arrives later.
``prompt_toolkit``'s stdout patch keeps supervisor events above a partially
typed prompt instead of splicing into the user's input.
"""

from __future__ import annotations

import asyncio
import inspect
import math
import re
import signal
import sys
from collections.abc import Callable, Mapping
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


_HEARTBEAT_ACTIVE_STATES = frozenset({"running", "waiting", "blocked", "parked"})
_HEARTBEAT_STATE_ORDER = {
    status: index
    for index, status in enumerate(("running", "waiting", "blocked", "parked"))
}


def _heartbeat_text(value: Any, default: str = "") -> str:
    """Keep externally supplied heartbeat facts on one bounded safe line."""

    if value is None:
        return default
    try:
        raw = str(value)
    except Exception:
        return default
    clean = "".join(
        character if ord(character) >= 32 and ord(character) != 127 else " "
        for character in raw
    )
    text = " ".join(clean.split())
    return text[:120] if text else default


def _heartbeat_items(value: Any) -> tuple[Any, ...]:
    if value is None or isinstance(value, (str, bytes)):
        return ()
    if isinstance(value, Mapping):
        return (value,)
    try:
        return tuple(value)
    except TypeError:
        return ()


def _heartbeat_field(value: Any, *names: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
        return default
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    return default


def _heartbeat_activity(event: Any) -> str:
    """Render active run facts, or idle, without consulting live services."""

    runs = _heartbeat_items(
        getattr(event, "active_runs", getattr(event, "runs", None)),
    )
    active: list[tuple[str, str, str]] = []
    for run in runs:
        status = _heartbeat_text(_heartbeat_field(run, "status", "state", default="")).casefold()
        outcome = _heartbeat_text(_heartbeat_field(run, "outcome", default="")).casefold()
        if status not in _HEARTBEAT_ACTIVE_STATES:
            if outcome != "parked":
                continue
            status = "parked"
        ticket_value = _heartbeat_field(
            run,
            "ticket",
            "identifier",
            "ticket_id",
            "ticketId",
            default="?",
        )
        waiting_value = _heartbeat_field(
            run,
            "waiting_on",
            "waitingOn",
            default="none",
        )
        ticket = _heartbeat_text(ticket_value, "?")
        waiting_on = _heartbeat_text(waiting_value, "none")
        active.append((ticket, status, waiting_on))
    active.sort(
        key=lambda run: (
            _HEARTBEAT_STATE_ORDER.get(run[1], len(_HEARTBEAT_STATE_ORDER)),
            run[0].casefold(),
            run[2].casefold(),
        )
    )
    if active:
        return "; ".join(
            f"{ticket} {status} waiting_on={waiting_on}"
            for ticket, status, waiting_on in active
        )
    # Older event producers may expose the already-rendered alias.  It is
    # accepted only as a scalar; no status/provider call is made as a fallback.
    rendered = _heartbeat_text(
        getattr(event, "fleet_status", getattr(event, "fleet", "")),
    )
    return rendered if rendered and rendered.casefold() != "idle" else "idle"


def _format_heartbeat_duration(value: Any) -> str | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    whole = int(seconds)
    if whole < 60:
        return f"{whole}s"
    minutes, remainder = divmod(whole, 60)
    if minutes < 60:
        return f"{minutes}m" if remainder == 0 else f"{minutes}m{remainder:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h" if minutes == 0 else f"{hours}h{minutes:02d}m"


def _heartbeat_healthy(value: Any) -> bool:
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"0", "false", "no", "off", "unavailable", "red"}:
            return False
        if normalized in {"1", "true", "yes", "on", "healthy", "green"}:
            return True
    return bool(value)


def _heartbeat_idle(event: Any) -> str:
    activity = _heartbeat_text(
        getattr(event, "last_activity", getattr(event, "last_activity_at", "")),
    )
    duration = _format_heartbeat_duration(getattr(event, "idle_seconds", None))
    if activity and duration is not None:
        return f"idle={duration}"
    if duration is not None and not activity and duration != "0s":
        return f"idle={duration}"
    return f"last_activity={activity or 'unknown'}"


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
        healthy = _heartbeat_healthy(getattr(event, "healthy", False))
        health = "healthy" if healthy else "unavailable"
        project = _heartbeat_text(
            getattr(event, "project", getattr(self.supervisor, "project", "Harbor")),
            "Harbor",
        )
        lifecycle = _heartbeat_text(getattr(event, "lifecycle", "UNKNOWN"), "UNKNOWN")
        activity = _heartbeat_activity(event)
        idle = _heartbeat_idle(event)
        self._emit(
            f"Harbor: heartbeat · {project} · {lifecycle} · {health} · "
            f"fleet={activity} · {idle}"
        )

    def banner(self) -> str:
        status = self.supervisor.status()
        health = "✓" if status.linear_healthy else "✗"
        hold = "on" if self.supervisor.poller.config.hold_all else "off"
        if getattr(self.supervisor, "read_only", False):
            mode = "read-only"
        else:
            mode = "shadow" if getattr(self.supervisor, "shadow", False) else "live"
        return (
            f"Harbor: {status.lifecycle} · deterministic first mate · {status.project} · Linear {health}\n"
            f"Scope: {', '.join(self.supervisor.poller.config.dispatch_states)} → dispatch · "
            f"Backlog ignored · hold_all: {hold} ({mode} mode)"
        )

    def status_text(self, *, detailed: bool = False) -> str:
        return render_digest(self.supervisor.status(), detailed=detailed)

    @staticmethod
    def _pr_label(url: str) -> str:
        bits = str(url).rstrip("/").split("/")
        if bits and bits[-1].isdigit():
            return f"PR #{bits[-1]}"
        return str(url)

    def help_text(self) -> str:
        return (
            "/status [--full]  /pause [reason]  /hold on|off <ticket>  /resume  "
            "/answer <escalation-id> <answer>  /retry <ticket>  "
            "/kill [run-id]  /exit  /help"
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

    async def _answer_question(self, question: str, digest: str | None = None) -> str:
        if self.first_mate is None:
            return (
                "Deterministic slash commands include /status, /pause, /resume, /retry, "
                "/exit, and /help; no model call was made."
            )
        # The terminal refreshes before answering. Embedders can supply the
        # same snapshot they are already showing.
        if digest is None:
            refresh = getattr(self.supervisor, "fresh_digest", None)
            if callable(refresh):
                digest = refresh()
                if asyncio.iscoroutine(digest):
                    digest = await digest
            else:
                digest = render_digest(self.supervisor.status())
        lookup = getattr(self.supervisor, "ticket_status_context", None)
        if callable(lookup):
            identifiers = tuple(dict.fromkeys(
                value.upper() for value in re.findall(r"\b[A-Z][A-Z0-9]{0,9}-[0-9]+\b", question, re.IGNORECASE)
            ))[:5]
            additions: list[str] = []
            for identifier in identifiers:
                value = lookup(identifier)
                if asyncio.iscoroutine(value):
                    value = await value
                if value:
                    additions.append(str(value))
            if additions:
                digest = f"{digest}\n\nTicket-specific read-only lookups:\n" + "\n".join(additions)
        return await self.first_mate.answer(question, digest=digest)

    def _install_answer_signal_handler(self) -> Callable[[], None] | None:
        """Route SIGINT to the active answer while preserving host handling.

        ``asyncio.run`` installs its own SIGINT callback which cancels the
        host task.  That is correct for an idle console, but while a question
        is generating it would also tear down the foreground task.  Install a
        short-lived loop callback for the answer only, then restore the exact
        callback that was present before it.

        The event-loop signal table is intentionally inspected only to restore
        asyncio's callback (there is no public getter).  Embeddings that do
        not own the main thread's signal handlers simply receive no temporary
        handler; their ordinary task cancellation remains distinguishable.
        """

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None

        sig = signal.SIGINT
        signal_handlers = getattr(loop, "_signal_handlers", None)
        previous_handle = signal_handlers.get(sig) if signal_handlers is not None else None
        previous_signal_handler = signal.getsignal(sig)
        callback = self._on_answer_sigint
        try:
            loop.add_signal_handler(sig, callback)
        except (NotImplementedError, OSError, RuntimeError, ValueError):
            return None

        restored = False

        def restore() -> None:
            nonlocal restored
            if restored:
                return
            restored = True
            try:
                loop.remove_signal_handler(sig)
                if previous_handle is not None:
                    loop.add_signal_handler(
                        sig,
                        previous_handle._callback,  # type: ignore[attr-defined]
                        *previous_handle._args,  # type: ignore[attr-defined]
                    )
                else:
                    signal.signal(sig, previous_signal_handler)
            except (NotImplementedError, OSError, RuntimeError, ValueError):
                # The host may be closing the loop as the answer is canceled;
                # there is no useful restoration work left in that case.
                return

        return restore

    def _on_answer_sigint(self) -> None:
        """Cancel only the active answer from the temporary SIGINT callback."""

        if self._answer_task is None or self._answer_task.done():
            return
        self.cancel_answer()

    def _cancel_answer_task(self, task: asyncio.Task[str]) -> None:
        if self.first_mate is not None:
            cancel = getattr(self.first_mate, "cancel_active_answer", None)
            if callable(cancel):
                cancel()
        if not task.done():
            task.cancel()

    async def ask(self, question: str, *, digest: str | None = None, interactive: bool = True) -> str:
        if self._answer_task is not None and not self._answer_task.done():
            return "Harbor: an answer is already in progress."
        task = asyncio.create_task(self._answer_question(question, digest), name="harbor-first-mate-answer")
        self._answer_task = task
        restore_signal_handler = self._install_answer_signal_handler() if interactive else None
        try:
            # Keep cancellation of the foreground console task separate from
            # the answer it owns.  asyncio.run's first SIGINT cancels the
            # task running ``Console.run``; without a shield that cancellation
            # propagates into this child before the handler can turn it into a
            # scoped answer cancellation, and the whole process exits 130.
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            intended = self._answer_cancel_requested
            # A cancellation is scoped to this one answer.  Explicitly cancel
            # and drain the child so a real Codex process gets killed/reaped,
            # while the supervisor's polling/heartbeat tasks are untouched.
            self._cancel_answer_task(task)
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                # Cancellation wins over a late runner failure, regardless of
                # whether this is the operator's SIGINT or host shutdown.
                pass
            if intended:
                return "Harbor: answer cancelled"
            raise
        except FirstMateError as exc:
            if not interactive:
                raise
            return f"Harbor: First Mate unavailable: {exc}"
        finally:
            if restore_signal_handler is not None:
                # Restore the host's SIGINT policy before making this answer
                # idle again.  Otherwise a later Ctrl+C would still hit the
                # answer-only callback and be ignored after the task is gone.
                restore_signal_handler()
            self._answer_cancel_requested = False
            if self._answer_task is task:
                self._answer_task = None

    def cancel_answer(self) -> bool:
        """Cancel only the visible plain-question answer, if one is running."""

        task = self._answer_task
        if task is None or task.done():
            return False
        self._answer_cancel_requested = True
        self._cancel_answer_task(task)
        return True

    async def handle_command(self, line: str) -> str | None:
        line = line.strip()
        if not line:
            return None
        if not line.startswith("/"):
            return await self.ask(line)
        parts = line.split(None, 1)
        command = parts[0].casefold()
        argument = parts[1] if len(parts) == 2 else ""
        if command in {"/help", "/h"}:
            return self.help_text()
        if getattr(self.supervisor, "read_only", False) and command in {
            "/pause",
            "/hold",
            "/resume",
            "/answer",
            "/kill",
        }:
            return f"Harbor: read-only mode · {command} ignored while another supervisor owns the project"
        if command == "/status":
            option = argument.strip()
            if option not in {"", "--full"}:
                return "Usage: /status [--full]"
            return self.status_text(detailed=option == "--full")
        if command == "/pause":
            await self.supervisor.pause(argument.strip() or None)
            reason = f" · {argument.strip()}" if argument.strip() else ""
            return f"Harbor: PAUSED{reason}"
        if command == "/hold":
            hold_parts = argument.strip().split()
            if len(hold_parts) != 2 or hold_parts[0].casefold() not in {"on", "off"}:
                return "Usage: /hold on|off <ticket>"
            enabled = hold_parts[0].casefold() == "on"
            identifier = hold_parts[1]
            method = getattr(self.supervisor, "set_ticket_hold", None)
            if not callable(method):
                method = getattr(self.supervisor, "set_hold", None)
            if not callable(method):
                return "Harbor: hold unavailable · no durable ticket ledger is configured"
            value = method(identifier, enabled)
            if inspect.isawaitable(value):
                value = await value
            if not value:
                return f"Harbor: unknown ticket · {identifier}"
            state = "ON" if enabled else "OFF"
            return f"Harbor: HOLD {state} · {identifier}"
        if command == "/resume":
            await self.supervisor.resume()
            return f"Harbor: {self.supervisor.status().lifecycle}"
        if command == "/answer":
            answer_parts = argument.strip().split(None, 1)
            if len(answer_parts) != 2 or not answer_parts[0] or not answer_parts[1].strip():
                return "Usage: /answer <escalation-id> <answer> (optional Citation: path:line; ids are listed by /status)"
            escalation_id, response = answer_parts
            try:
                result = await self.supervisor.answer_escalation(escalation_id, response, background=True)
            except (RuntimeError, ValueError) as exc:
                return f"Harbor: answer not recorded: {exc}"
            resumed = " · same builder resume queued" if result.get("queued") else " · same builder resumed" if result.get("resumed") else " · builder session not available"
            return f"Harbor: answer recorded for escalation {escalation_id}{resumed}"
        if command == "/retry":
            identifier = argument.strip()
            if not identifier:
                return "Usage: /retry <ticket>"
            retry = getattr(self.supervisor, "retry_builder", None)
            if not callable(retry):
                return "Harbor: retry unavailable · no retained builder recovery hook is configured"
            try:
                result = retry(identifier)
                result = await result if inspect.isawaitable(result) else result
            except (RuntimeError, ValueError) as exc:
                return f"Harbor: retry refused · {exc}"
            status = result.get("status") if isinstance(result, Mapping) else getattr(result, "status", None)
            return f"Harbor: same builder retried · {identifier} · {status or 'result recorded'}"
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
                    # Ctrl+C while a terminal answer is awaited is handled below and
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
