"""Operator surfaces the plan's test blocks invoke directly.

Phase 2 runs ``harbor facts``, Phase 3 runs ``harbor start --shadow``, and
Phase 7 AC-6 runs ``harbor answer``.  These pin the argument surface and the
shadow contract: decide, report, mutate nothing.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

import harbor.cli as cli
from harbor.cli import build_parser
from harbor.config.projects import ProjectConfig
from harbor.poll.linear_poller import PollResult
from harbor.supervisor import Supervisor


class TodoPoller:
    """One Todo ticket, which a live supervisor would dispatch."""

    project_name = "SampleProject"
    config = ProjectConfig(name="SampleProject", linear_project_slug="sample-project", heartbeat_seconds=1)

    async def poll(self) -> PollResult:
        ticket = {"identifier": "SAMPLE-42", "title": "A ticket", "state": "Todo", "pr_urls": []}
        return PollResult((ticket,), "snapshot-1", True, (ticket,))


class RecordingDispatcher:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def dispatch(self, wake: Any) -> None:
        self.calls.append("dispatch")

    async def resume(self, wake: Any) -> None:
        self.calls.append("resume")


class ShadowLinearClient:
    def __init__(self, nodes: list[dict[str, Any]]) -> None:
        self.nodes = nodes
        self.fetches: list[tuple[tuple[str, ...], tuple[str, ...]]] = []

    async def fetch_issues(self, projects: tuple[str, ...], states: tuple[str, ...]) -> list[dict[str, Any]]:
        self.fetches.append((projects, states))
        return self.nodes

    async def close(self) -> None:
        return None


def test_parser_exposes_facts_shadow_and_answer() -> None:
    parser = build_parser()
    assert parser.parse_args(["facts", "SampleProject", "--json"]).json is True
    assert parser.parse_args(["start", "SampleProject", "--shadow"]).shadow is True
    answer = parser.parse_args(["answer", "7", "Keith", "owns", "it", "--project", "SampleProject"])
    assert answer.escalation == "7"
    assert " ".join(answer.text) == "Keith owns it"


def test_shadow_start_prints_observed_ticket_digest_without_dispatch(monkeypatch, tmp_path: Path, capsys) -> None:
    node = {
        "id": "linear-id-42",
        "identifier": "SAMPLE-42",
        "title": "A ticket",
        "state": {"name": "Todo", "type": "backlog"},
        "attachments": {"nodes": []},
        "comments": {"nodes": []},
        "inverseRelations": {"nodes": []},
    }
    config = ProjectConfig(name="SampleProject", linear_project_slug="sample-project", repo_path=tmp_path)
    client = ShadowLinearClient([node])
    dispatcher = RecordingDispatcher()
    monkeypatch.setattr(cli, "get_project", lambda _: config)
    monkeypatch.setattr(cli, "_db_path", lambda: tmp_path / "harbor.db")

    result = asyncio.run(
        cli._start(
            "SampleProject",
            shadow=True,
            linear_client=client,
            dispatcher=dispatcher,
            first_mate=object(),
            notifier=object(),
            unblock_ladder=object(),
        )
    )

    output = capsys.readouterr().out
    assert result == 0
    assert "(shadow mode)" in output
    assert "Harbor: RUNNING · SampleProject · Linear healthy\nSAMPLE-42 (observed)  Todo  A ticket" in output
    assert "WOULD WAKE build SAMPLE-42 · idem_key=SAMPLE-42@build" in output
    assert dispatcher.calls == []
    assert len(client.fetches) == 1


def test_shadow_decides_a_build_but_never_dispatches() -> None:
    async def scenario() -> None:
        dispatcher = RecordingDispatcher()
        supervisor = Supervisor(TodoPoller(), dispatcher=dispatcher, shadow=True)
        await supervisor.poll_once()
        assert dispatcher.calls == []
        assert [w.action for w in supervisor.shadow_wakes] == ["build"]
        assert supervisor.shadow_wakes[0].idem_key == "SAMPLE-42@build"

    asyncio.run(scenario())


def test_a_live_supervisor_dispatches_the_same_ticket() -> None:
    """The control: shadow is the only reason nothing happened above."""

    async def scenario() -> None:
        dispatcher = RecordingDispatcher()
        supervisor = Supervisor(TodoPoller(), dispatcher=dispatcher)
        await supervisor.poll_once()
        assert dispatcher.calls == ["dispatch"]
        assert supervisor.shadow_wakes == []

    asyncio.run(scenario())


def test_two_shadow_cycles_produce_identical_decisions() -> None:
    """Phase 3: the diff of two consecutive shadow runs must be empty."""

    async def scenario() -> None:
        supervisor = Supervisor(TodoPoller(), dispatcher=RecordingDispatcher(), shadow=True)
        await supervisor.poll_once()
        first = [(w.action, w.idem_key) for w in supervisor.shadow_wakes]
        supervisor.shadow_wakes.clear()
        await supervisor.poll_once()
        second = [(w.action, w.idem_key) for w in supervisor.shadow_wakes]
        # The decider's shadow ledger makes the second cycle a no-op.
        assert second == []
        assert first == [("build", "SAMPLE-42@build")]

    asyncio.run(scenario())


def test_shadow_does_not_deliver_pr_events() -> None:
    class Processor:
        def __init__(self) -> None:
            self.calls = 0

        async def process(self, *args: Any, **kwargs: Any) -> None:
            self.calls += 1

    async def scenario() -> None:
        processor = Processor()
        supervisor = Supervisor(TodoPoller(), shadow=True, pr_event_processor=processor)
        await supervisor._process_pr_events({"identifier": "SAMPLE-42"}, {"prs": [{"number": 1}]})
        assert processor.calls == 0

    asyncio.run(scenario())


def test_answer_requires_a_citation() -> None:
    """AC-4: a resolution with an empty citation is never accepted."""

    class Ledger:
        def set_runtime(self, *args: Any, **kwargs: Any) -> None:
            return None

        def escalations(self) -> list[dict[str, Any]]:
            return [{"id": "7", "kind": "product_decision", "question": "Who owns it?", "ticket": "SAMPLE-42"}]

        def record_resolution(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            raise AssertionError("must not record an uncited answer")

    async def scenario() -> None:
        supervisor = Supervisor(TodoPoller(), db=Ledger(), shadow=True)
        with pytest.raises(ValueError, match="citation"):
            await supervisor.answer_escalation("7", "Keith owns it")

    asyncio.run(scenario())


def test_answer_records_a_cited_resolution() -> None:
    """AC-6: harbor answer writes a resolutions row."""

    class Ledger:
        def __init__(self) -> None:
            self.recorded: list[tuple[Any, ...]] = []

        def set_runtime(self, *args: Any, **kwargs: Any) -> None:
            return None

        def escalations(self) -> list[dict[str, Any]]:
            return [{"id": "7", "kind": "product_decision", "question": "Who owns it?", "ticket": "SAMPLE-42"}]

        def record_resolution(self, kind: str, question: str, answer: str, citation: str, **kwargs: Any) -> int:
            self.recorded.append((kind, question, answer, citation))
            return 1

    async def scenario() -> None:
        ledger = Ledger()
        supervisor = Supervisor(TodoPoller(), db=ledger, shadow=True)
        result = await supervisor.answer_escalation("7", "Keith owns it\nCitation: AGENTS.md:12")
        assert result["citation"] == "AGENTS.md:12"
        assert result["answer"] == "Keith owns it"
        assert ledger.recorded == [("product_decision", "Who owns it?", "Keith owns it", "AGENTS.md:12")]

    asyncio.run(scenario())
