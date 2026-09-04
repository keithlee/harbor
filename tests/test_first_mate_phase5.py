from __future__ import annotations

import asyncio
import os
import signal
from typing import Any

import pytest

from harbor.digest import render_digest
from harbor.console import Console
from harbor.first_mate import CodexExecRunner, DEFAULT_MODEL, FirstMate, build_command
from harbor.notify import Notifier, build_notification_command
from harbor.poll.linear_poller import PollResult
from harbor.config.projects import ProjectConfig
from harbor.supervisor import Lifecycle, Supervisor


def run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


def status() -> dict[str, Any]:
    return {
        "project": "Bookgen",
        "lifecycle": "RUNNING",
        "linear_healthy": True,
        "tickets": [
            {"identifier": "HAN-17", "state": "In Review", "owned": False, "title": "Reader"},
            {"identifier": "HAN-42", "state": "Todo", "owned": True, "title": "Build"},
        ],
    }


def test_digest_is_deterministic_and_ordered_without_a_model_call() -> None:
    first = render_digest(status())
    second = render_digest({**status(), "tickets": list(reversed(status()["tickets"]))})
    assert first == second
    assert first.index("HAN-17") < first.index("HAN-42")
    assert "HAN-17 (observed)" in first


def test_plain_question_makes_one_terra_call_and_uses_same_digest() -> None:
    calls: list[tuple[list[str], str]] = []

    async def runner(command: list[str], prompt: str) -> str:
        calls.append((command, prompt))
        return "It is waiting on review."

    mate = FirstMate(status(), runner=runner)
    rendered = mate.digest()
    answer = run(mate.answer("Why is it waiting?"))
    assert answer == "It is waiting on review."
    assert len(calls) == 1
    command, prompt = calls[0]
    assert command == ["codex", "exec", "-m", DEFAULT_MODEL, "-s", "read-only", "--ephemeral"]
    assert rendered in prompt
    assert "Why is it waiting?" in prompt


def test_prompt_only_runner_is_supported_without_a_retry() -> None:
    calls: list[str] = []

    async def runner(prompt: str) -> str:
        calls.append(prompt)
        return "one answer"

    assert run(FirstMate(status(), runner=runner).answer("question")) == "one answer"
    assert len(calls) == 1


def test_status_digest_never_invokes_runner() -> None:
    calls: list[object] = []

    def runner(*args: object) -> str:
        calls.append(args)
        return "unexpected"

    mate = FirstMate(status(), runner=runner)
    assert "Harbor: RUNNING" in mate.status_text()
    assert calls == []


def test_cancellation_targets_only_active_answer() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def runner(command: list[str], prompt: str) -> str:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return "done"

    async def exercise() -> None:
        mate = FirstMate(status(), runner=runner)
        answer = asyncio.create_task(mate.answer("wait"))
        await started.wait()
        assert mate.cancel_active_answer() is True
        with pytest.raises(asyncio.CancelledError):
            await answer
        # The unrelated event loop remains usable and no second hidden call
        # is made by cancellation.
        release.set()
        assert calls == 1
        assert mate.cancel_active_answer() is False

    run(exercise())


def test_console_ctrl_c_cancels_answer_but_keeps_supervisor_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A SIGINT delivered while an answer is active belongs to that answer."""

    import harbor.console as console_module

    class Poller:
        project_name = "Bookgen"
        config = ProjectConfig(name="Bookgen", linear_project_slug="hanzireader")

        async def poll(self) -> PollResult:
            return PollResult((), "empty", False, ())

    class Session:
        prompts = ("why is this waiting?", "/status", "/exit")
        calls = 0

        def __init__(self) -> None:
            self.index = 0

        async def prompt_async(self, prompt: str) -> str:
            del prompt
            type(self).calls += 1
            value = self.prompts[self.index]
            self.index += 1
            return value

    async def exercise() -> tuple[list[str], Lifecycle, bool, bool, int]:
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def runner(command: list[str], prompt: str) -> str:
            del command, prompt
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return "unreachable"

        supervisor = Supervisor(Poller())
        mate = FirstMate(supervisor, runner=runner)
        output: list[str] = []
        console = Console(supervisor, first_mate=mate, output=output.append)
        monkeypatch.setattr(console_module, "PromptSession", Session)
        await supervisor.start()
        try:
            foreground = asyncio.create_task(console.run())
            await started.wait()
            # The temporary answer handler must turn the terminal SIGINT into
            # answer-only cancellation instead of letting asyncio.run cancel
            # the foreground task and exit the process with status 130.
            os.kill(os.getpid(), signal.SIGINT)
            await foreground
            await asyncio.wait_for(cancelled.wait(), timeout=1)
            return output, supervisor.lifecycle, mate.active_answer is None, console._answer_task is None, Session.calls
        finally:
            await supervisor.exit()

    output, lifecycle, mate_idle, console_idle, prompts = run(exercise())
    # ``/status`` ran after cancellation and reported RUNNING before the test's
    # final ``/exit`` prompt stopped the fixture supervisor.
    assert any("Harbor: RUNNING" in line for line in output)
    assert "Harbor: answer cancelled" in output
    assert prompts == 3
    assert lifecycle == Lifecycle.STOPPED
    assert mate_idle is True
    assert console_idle is True


def test_console_host_cancellation_propagates_after_answer_cleanup() -> None:
    class Poller:
        project_name = "Bookgen"
        config = ProjectConfig(name="Bookgen", linear_project_slug="hanzireader")

        async def poll(self) -> PollResult:
            return PollResult((), "empty", False, ())

    async def exercise() -> tuple[Lifecycle, bool, bool]:
        started = asyncio.Event()

        async def runner(command: list[str], prompt: str) -> str:
            del command, prompt
            started.set()
            await asyncio.Event().wait()
            return "unreachable"

        supervisor = Supervisor(Poller())
        mate = FirstMate(supervisor, runner=runner)
        console = Console(supervisor, first_mate=mate)
        await supervisor.start()
        try:
            host_task = asyncio.create_task(console.handle_command("host timeout"))
            await started.wait()
            host_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await host_task
            return supervisor.lifecycle, mate.active_answer is None, console._answer_task is None
        finally:
            await supervisor.exit()

    lifecycle, mate_idle, console_idle = run(exercise())
    assert lifecycle == Lifecycle.RUNNING
    assert mate_idle is True
    assert console_idle is True


def test_answer_restores_prior_sigint_handler_when_idle() -> None:
    class Poller:
        project_name = "Bookgen"
        config = ProjectConfig(name="Bookgen", linear_project_slug="hanzireader")

        async def poll(self) -> PollResult:
            return PollResult((), "empty", False, ())

    async def exercise() -> tuple[str | None, Lifecycle, bool]:
        loop = asyncio.get_running_loop()
        previous_seen = asyncio.Event()

        def previous_handler() -> None:
            previous_seen.set()

        # Install a real loop handler so the test detects whether the
        # answer-only handler was restored after the first SIGINT.
        loop.add_signal_handler(signal.SIGINT, previous_handler)
        started = asyncio.Event()

        async def runner(command: list[str], prompt: str) -> str:
            del command, prompt
            started.set()
            await asyncio.Event().wait()
            return "unreachable"

        supervisor = Supervisor(Poller())
        mate = FirstMate(supervisor, runner=runner)
        console = Console(supervisor, first_mate=mate)
        await supervisor.start()
        try:
            answer = asyncio.create_task(console.handle_command("why?"))
            await started.wait()
            os.kill(os.getpid(), signal.SIGINT)
            response = await answer
            assert response == "Harbor: answer cancelled"

            # The answer is now idle.  A second real SIGINT must reach the
            # prior handler instead of the stale answer callback.
            os.kill(os.getpid(), signal.SIGINT)
            await asyncio.wait_for(previous_seen.wait(), timeout=1)
            return response, supervisor.lifecycle, mate.active_answer is None
        finally:
            loop.remove_signal_handler(signal.SIGINT)
            await supervisor.exit()

    response, lifecycle, mate_idle = run(exercise())
    assert response == "Harbor: answer cancelled"
    assert lifecycle == Lifecycle.RUNNING
    assert mate_idle is True


def test_codex_runner_kills_and_reaps_only_cancelled_child(monkeypatch: pytest.MonkeyPatch) -> None:
    class Process:
        returncode: int | None = None

        def __init__(self) -> None:
            self.killed = 0
            self.waited = 0

        async def communicate(self, prompt: bytes) -> tuple[bytes, bytes]:
            del prompt
            await asyncio.Event().wait()
            return b"", b""

        def kill(self) -> None:
            self.killed += 1
            self.returncode = -9

        async def wait(self) -> int:
            self.waited += 1
            return self.returncode or -9

    process = Process()

    async def create_process(*command: str, **kwargs: object) -> Process:
        assert command[:2] == ("codex", "exec")
        assert kwargs["stdin"] is asyncio.subprocess.PIPE
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    async def exercise() -> None:
        task = asyncio.create_task(CodexExecRunner()(build_command(), "question"))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(exercise())
    assert process.killed == 1
    assert process.waited == 1


def test_notification_command_and_executor_are_injectable() -> None:
    commands: list[list[str]] = []

    def executor(command: list[str]) -> None:
        commands.append(command)

    notifier = Notifier(executor)
    reason = "parked: literal reason"
    run(notifier.notify(reason))
    assert commands == [build_notification_command(reason)]
    assert commands[0][0] == "osascript"
    assert reason in commands[0][-1]


def test_park_notification_preserves_literal_reason() -> None:
    reasons: list[str] = []

    async def notifier(reason: str) -> None:
        reasons.append(reason)

    run(FirstMate(status(), runner=lambda prompt: "answer", notifier=notifier).notify_park("parked: CI is red"))
    assert reasons == ["parked: CI is red"]


def test_command_options_are_read_only_and_ephemeral() -> None:
    command = build_command()
    assert command[:2] == ["codex", "exec"]
    assert "-s" in command and command[command.index("-s") + 1] == "read-only"
    assert "--ephemeral" in command
    assert "workspace-write" not in command
