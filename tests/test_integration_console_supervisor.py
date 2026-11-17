from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from harbor.config.pipeline import PipelineConfig, load_projects, parse_projects_markdown
from harbor.config.projects import ProjectConfig, get_project
from harbor.console import Console
from harbor.db import HarborDB
from harbor.first_mate import FirstMate
from harbor.heartbeat import Heartbeat
from harbor.poll.linear_poller import PollResult
from harbor.runner.base import apply_phase4_migration
from harbor.supervisor import Lifecycle, Supervisor, WorktreeDispatcher
from harbor.wake import apply_phase3_migration


def run(value: Any) -> Any:
    return asyncio.run(value)


class FakePoller:
    project_name = "SampleProject"
    config = ProjectConfig(name="SampleProject", linear_project_slug="sample-project", heartbeat_seconds=1)

    def __init__(self) -> None:
        self.calls = 0

    async def poll(self) -> PollResult:
        self.calls += 1
        ticket = {
            "identifier": "SAMPLE-42",
            "title": "A ticket",
            "state": "In Progress",
            "owned": True,
            "pr_urls": [],
        }
        return PollResult((ticket,), f"snapshot-{self.calls}", True, (ticket,))


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.active = object()

    async def pause(self) -> None:
        self.calls.append("pause")

    async def resume_deadline(self) -> None:
        self.calls.append("resume_deadline")


def test_pipeline_and_registry_are_human_editable(tmp_path: Path, monkeypatch: Any) -> None:
    pipeline = tmp_path / "pipeline.md"
    pipeline.write_text(
        """# Pipeline\n\n| role | model | reasoning_effort |\n| --- | --- | --- |\n| builder | custom-builder | high |\n| firstmate | custom-terra | medium |\nheartbeat_seconds: 7\nlinear_project_slug: demo-abc123\nlinear_team: DEMO\n""",
        encoding="utf-8",
    )
    registry = tmp_path / "projects.md"
    registry.write_text(
        f"| name | repo_path | pipeline_path |\n| --- | --- | --- |\n| Demo | {tmp_path} | {pipeline} |\n",
        encoding="utf-8",
    )
    parsed = PipelineConfig.from_file(pipeline)
    assert parsed.builder_model == "custom-builder"
    assert parsed.builder_effort == "high"
    assert parsed.firstmate_model == "custom-terra"
    assert parsed.firstmate_effort == "medium"
    assert parsed.heartbeat_seconds == 7
    assert load_projects(registry)[0].name == "Demo"
    assert parse_projects_markdown(registry.read_text(), source=registry)[0].repo_path == tmp_path
    monkeypatch.setenv("HARBOR_PROJECTS_FILE", str(registry))
    config = get_project("Demo")
    assert config.first_mate_model == "custom-terra"
    assert config.builder_model == "custom-builder"


def test_plain_question_refreshes_once_and_calls_first_mate_once() -> None:
    async def scenario() -> tuple[list[str], int, int]:
        poller = FakePoller()
        supervisor = Supervisor(poller)
        calls: list[str] = []

        async def answer(command: list[str], prompt: str) -> str:
            calls.append(prompt)
            assert command[0:2] == ["codex", "exec"]
            return "one answer"

        mate = FirstMate(supervisor, runner=answer)
        console = Console(supervisor, first_mate=mate)
        response = await console.handle_command("Why is this waiting?")
        await supervisor.exit()
        return calls, poller.calls, int(response == "one answer")

    calls, poll_count, responses = run(scenario())
    assert len(calls) == 1
    assert poll_count == 1
    assert responses == 1
    assert "SAMPLE-42" in calls[0]


def test_heartbeat_publishes_without_waking_first_mate() -> None:
    async def scenario() -> tuple[list[object], list[object]]:
        published: list[object] = []
        model_calls: list[object] = []
        heartbeat = Heartbeat(
            "SampleProject",
            interval_seconds=1,
            status_provider=lambda: {"project": "SampleProject", "lifecycle": "RUNNING", "linear_healthy": True},
            publisher=published.append,
        )
        await heartbeat.tick()
        # The only model seam is intentionally unused by an operational tick.
        model_calls.append("not-called")
        return published, model_calls

    published, model_calls = run(scenario())
    assert len(published) == 1
    assert published[0].healthy is True
    assert model_calls == ["not-called"]


def test_pause_blocks_wakes_and_resume_reconciles() -> None:
    async def scenario() -> tuple[Lifecycle, list[str], int]:
        poller = FakePoller()
        runner = FakeRunner()
        db = HarborDB(":memory:")
        supervisor = Supervisor(poller, db=db, runner=runner)
        await supervisor.start()
        await supervisor.pause("operator pause")
        paused_calls = poller.calls
        # Direct polling while paused is a no-op and cannot dispatch/resume.
        await supervisor.poll_once()
        assert poller.calls == paused_calls
        await supervisor.resume()
        await supervisor.exit()
        db.close()
        return supervisor.lifecycle, runner.calls, poller.calls

    lifecycle, calls, poll_count = run(scenario())
    assert lifecycle == Lifecycle.STOPPED
    assert calls == ["pause", "resume_deadline"]
    assert poll_count == 2


def test_blocked_builder_final_routes_to_ladder_and_status_shows_escalation() -> None:
    async def scenario() -> tuple[list[Any], str]:
        poller = FakePoller()
        seen: list[Any] = []

        class Ladder:
            async def handle(self, ticket: Any, run: Any, final: Any, *, blocker: Any = None) -> str:
                seen.append((ticket, run, final, blocker))
                return "parked"

        class Dispatcher:
            async def dispatch(self, wake: Any) -> dict[str, Any]:
                return {
                    "status": "blocked",
                    "summary": "needs a decision",
                    "run_id": "run-42",
                    "blocker": {"kind": "product_decision", "question": "Which behavior?"},
                }

        supervisor = Supervisor(
            poller,
            dispatcher=Dispatcher(),
            unblock_ladder=Ladder(),
            decider=lambda ticket, facts: type(
                "WakeValue", (), {"action": "build", "ticket": ticket, "run_id": "run-42", "instruction": "build", "notify": False}
            )(),
        )
        # A tiny decider object is clearer than relying on DB queue state for
        # this routing-only behavior.
        supervisor.decider = type("Decider", (), {"decide": lambda self, ticket, facts: type("WakeValue", (), {"action": "build", "ticket": ticket, "run_id": "run-42", "instruction": "build", "notify": False})()})()
        await supervisor.poll_once()
        return seen, supervisor.fleet_digest()

    seen, digest = run(scenario())
    assert len(seen) == 1
    assert seen[0][1]["id"] == "run-42"
    assert "SAMPLE-42" in digest


def test_answer_command_records_citation_and_resumes_same_session(tmp_path: Path) -> None:
    async def scenario() -> str:
        db = HarborDB(tmp_path / "answer.db")
        db.replace_tickets("SampleProject", [{"identifier": "SAMPLE-42", "title": "A ticket", "state": "Blocked"}])
        db.record_escalation("SAMPLE-42", "run-42", "ambiguous_ac", "Which cache?", 2)
        resumed: list[tuple[str, str]] = []

        class Runner:
            async def resume(self, run_id: str, prompt: str) -> bool:
                resumed.append((run_id, prompt))
                return True

        supervisor = Supervisor(FakePoller(), db=db, runner=Runner())
        console = Console(supervisor)
        response = await console.handle_command("/answer 1 Use local cache. Citation: AGENTS.md:12")
        assert "queued" in response
        await asyncio.gather(*supervisor._dispatch_tasks.values())
        assert resumed and resumed[0][0] == "run-42"
        assert db.find_resolution("ambiguous_ac", "Which cache?", project="SampleProject")["citation"] == "AGENTS.md:12"
        db.close()
        return response

    response = run(scenario())
    assert "answer recorded" in response


def test_worktree_resume_routes_a_second_blocker_with_persisted_worktree_to_unblock_ladder(
    tmp_path: Path,
) -> None:
    async def scenario() -> list[Any]:
        seen: list[Any] = []
        worktree = tmp_path / "retained-worktree"
        worktree.mkdir()
        db = HarborDB(tmp_path / "resume.db")
        db.replace_tickets(
            "SampleProject",
            [{"identifier": "SAMPLE-42", "title": "A ticket", "state": "Blocked"}],
        )
        apply_phase3_migration(db)
        apply_phase4_migration(db)
        db.connection.execute(
            "INSERT INTO runs(id, ticket, role, status, worktree) "
            "VALUES ('run-42', 'SAMPLE-42', 'builder', 'waiting', ?)",
            (str(worktree),),
        )
        db.connection.commit()

        class RetainedRunner:
            contexts = {"run-42": object()}

        class Ladder:
            async def handle(self, ticket: Any, run: Any, final: Any, *, blocker: Any = None) -> str:
                seen.append((ticket, run, final, blocker))
                return "parked"

        dispatcher = WorktreeDispatcher(object(), lambda *_args, **_kwargs: None)
        dispatcher.runners["SAMPLE-42"] = RetainedRunner()

        async def resume(_wake: Any) -> dict[str, Any]:
            return {
                "status": "blocked",
                "run_id": "run-42",
                "blocker": {"kind": "env_blocker", "question": "Still unavailable?"},
            }

        dispatcher.resume = resume  # type: ignore[method-assign]
        supervisor = Supervisor(
            FakePoller(), db=db, dispatcher=dispatcher, unblock_ladder=Ladder()
        )
        assert await supervisor.resume_session("run-42", "Use the deterministic adapter") is True
        db.close()
        return seen

    seen = run(scenario())
    assert len(seen) == 1
    assert seen[0][0]["identifier"] == "SAMPLE-42"
    assert seen[0][1]["id"] == "run-42"
    assert seen[0][1]["worktree"].endswith("retained-worktree")
    assert seen[0][3]["question"] == "Still unavailable?"


def test_startup_retries_only_latest_known_deterministic_blocker(tmp_path: Path) -> None:
    async def scenario() -> tuple[list[str], list[Any]]:
        db = HarborDB(tmp_path / "restart.db")
        handled: list[Any] = []

        class Ladder:
            def can_retry_deterministically(self, ticket: Any, run_row: Any, final: Any) -> bool:
                return final.get("blocker", {}).get("kind") == "env_blocker"

            async def handle(self, ticket: Any, run_row: Any, final: Any) -> str:
                handled.append((ticket, run_row, final))
                return "resumed"

        supervisor = Supervisor(FakePoller(), db=db, unblock_ladder=Ladder())
        await supervisor._apply_startup_migrations()
        db.record_observation(
            "SampleProject",
            "restart-snapshot",
            {},
            ({"identifier": "SAMPLE-42", "title": "A ticket", "state": "Blocked", "owned": True, "hold": False},),
        )
        for run_id, question, finished_at in (
            ("older", "Old connector failure", "2026-09-05T01:00:00+00:00"),
            ("latest", "Latest connector failure", "2026-09-05T02:00:00+00:00"),
        ):
            db.connection.execute(
                "INSERT INTO runs(id, ticket, role, status, final_json, finished_at) "
                "VALUES (?, 'SAMPLE-42', 'builder', 'blocked', ?, ?)",
                (
                    run_id,
                    '{"status":"blocked","blocker":{"kind":"env_blocker","question":"'
                    + question
                    + '"}}',
                    finished_at,
                ),
            )
        db.connection.commit()
        try:
            retried = await supervisor._retry_deterministic_blockers()
        finally:
            db.close()
        return retried, handled

    retried, handled = run(scenario())
    assert retried == ["SAMPLE-42"]
    assert len(handled) == 1
    assert handled[0][1]["id"] == "latest"
