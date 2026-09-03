from __future__ import annotations

import asyncio
from typing import Any

import pytest

from harbor.digest import render_digest
from harbor.first_mate import DEFAULT_MODEL, FirstMate, build_command
from harbor.notify import Notifier, build_notification_command


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
