from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from harbor.config.pipeline import PipelineConfig
from harbor.recovery import BlockerPolicy, claim_retry, recoverable
from harbor.unblock import AuthoritySource, _validated_triage, triage


def test_retry_budget_survives_reopen(tmp_path):
    path = tmp_path / "ledger.sqlite"
    row = dict(id="run-1", status="failed", terminal_reason="unexpected_exit", session_id="native-1", worktree=str(tmp_path))
    policy = BlockerPolicy()
    connection = sqlite3.connect(path)
    assert not claim_retry(connection, row, policy, 0)
    assert claim_retry(connection, row, policy, 30)
    assert not claim_retry(connection, row, policy, 30)
    connection.close()
    connection = sqlite3.connect(path)
    assert not claim_retry(connection, row, policy, 149)
    assert claim_retry(connection, row, policy, 150)
    assert claim_retry(connection, row, policy, 450)
    assert not claim_retry(connection, row, policy, 10000)


@pytest.mark.parametrize("reason", ["operator kill", "terminated", "deadline exceeded", "pause", "cancelled", None])
def test_explicit_or_unknown_stop_is_not_retried(reason):
    assert not recoverable(dict(status="failed", terminal_reason=reason, session_id="s", worktree="w"))


def test_invalid_policy_fails_closed():
    for value in ["recovery_delays: nan", "recovery_delays: -1", "unblock_attempts: 99", "firstmate_decisions: anything"]:
        with pytest.raises(ValueError):
            PipelineConfig.from_markdown(value)


def test_forged_partial_citation_is_rejected():
    result = _validated_triage({"disposition": "resolve", "answer": "Use blue", "citation": "AGENTS"},
                               [AuthoritySource("AGENTS.md", "Use blue", 10, "policy")])
    assert not result.resolved


def test_ticket_policy_can_answer_product_question(tmp_path):
    async def mate(*args, **kwargs):
        return {"disposition": "resolve", "answer": "Use blue", "citation": "ticket:MOCK-1:1", "reason": "The acceptance criteria specify blue."}
    result = asyncio.run(triage({"identifier": "MOCK-1", "description": "The button color must be blue."},
                                {"kind": "product_decision", "question": "What button color?"},
                                first_mate=mate, repo_path=tmp_path))
    assert result.resolved


@pytest.mark.parametrize("checkpoint", ["edit", "commit", "push", "pr", "reply"])
def test_real_sigkill_restart_recovery_with_mock_linear(tmp_path, checkpoint):
    """Real subprocess, git worktree, SQLite and supervisor; only Linear/model mocked."""
    import os
    import subprocess
    import sys
    import httpx
    from harbor.clients.linear import LinearClient
    from harbor.config.projects import ProjectConfig
    from harbor.db import HarborDB
    from harbor.poll.linear_poller import LinearPoller
    from harbor.runner.codex import CodexRunner
    from harbor.supervisor import Supervisor, WorktreeDispatcher, Lifecycle
    from harbor.wake import apply_phase3_migration

    repo = tmp_path / "repo"
    repo.mkdir()
    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)
    git("init", "-b", "main")
    git("-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "--allow-empty", "-m", "initial")
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
    git("remote", "add", "origin", str(origin))
    wt = tmp_path / "retained"
    git("worktree", "add", "-b", "ticket/mock-1", str(wt))
    script = tmp_path / "builder"
    script.write_text(f'''#!{sys.executable}
import json, os, pathlib, signal, subprocess, sys, time
root = pathlib.Path.cwd()
checkpoint = {checkpoint!r}
args = sys.argv[1:]
prompt = sys.stdin.read()
print(json.dumps({{"type":"session.started", "session_id":"native-retained-1"}}), flush=True)
if "resume" not in args:
    (root / "partial.txt").write_text("keep this work")
    if checkpoint != "edit":
        subprocess.run(["git", "add", "partial.txt"], check=True, capture_output=True)
        subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-m", "partial implementation"], check=True, capture_output=True)
    if checkpoint in {{"push", "pr", "reply"}}:
        subprocess.run(["git", "push", "origin", "HEAD:ticket/mock-1"], check=True, capture_output=True)
    # These files are mock external service receipts, retained across death.
    if checkpoint in {{"pr", "reply"}}:
        (root / "mock-pr.json").write_text(json.dumps({{"number":1,"create_calls":1}}))
    if checkpoint == "reply":
        (root / "mock-reply.json").write_text(json.dumps({{"id":"reply-1","publish_calls":1}}))
    time.sleep(0.05)
    os.kill(os.getpid(), signal.SIGKILL)
else:
    assert args[args.index("resume")+1] == "native-retained-1"
    assert (root / "partial.txt").read_text() == "keep this work"
    (root / "resumed.json").write_text(json.dumps({{"cwd":str(root), "args":args, "prompt":prompt}}))
    print(json.dumps({{"type":"result", "result":{{"status":"waiting", "waiting_on":"review", "outcome":"pr_open", "pr_url":"https://github.com/example/project/pull/1", "summary":"Recovered partial work; ready for review"}}}}), flush=True)
''')
    script.chmod(0o755)
    requests = []
    def linear(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert "mutation" not in payload["query"]
        return httpx.Response(200, json={"data":{"issues":{"nodes":[{
            "id":"uuid-1", "identifier":"MOCK-1", "title":"Recover a partial edit",
            "description":"Preserve partial.txt and finish the change.", "state":{"name":"In Progress", "type":"started"},
            "comments":{"nodes":[]}, "inverseRelations":{"nodes":[]}, "attachments":{"nodes":[]}
        }]}}})

    async def scenario():
        db = HarborDB(tmp_path / "ledger.sqlite")
        apply_phase3_migration(db)
        db.replace_tickets("mock-project", [{"identifier":"MOCK-1", "title":"Mock ticket", "state":"In Progress", "owned":True, "hold":False}], emit_events=False)
        first = CodexRunner(wt, db=db, ticket="MOCK-1", executable=str(script), deadline_seconds=30)
        stopped = await first.start("Implement the ticket", run_id="logical-1")
        assert stopped.status == "failed"
        row = dict(db.connection.execute("SELECT * FROM runs WHERE id='logical-1'").fetchone())
        assert row["terminal_reason"] == "unexpected_exit"
        remaining = row["deadline_seconds"]
        head_before = subprocess.check_output(["git", "-C", str(wt), "rev-parse", "HEAD"], text=True).strip()
        assert 0 < remaining < 30
        db.close()
        # Entirely new supervisor, dispatcher and runner: no in-memory custody.
        db = HarborDB(tmp_path / "ledger.sqlite")
        class Manager:
            def attach(self, identifier):
                assert identifier == "MOCK-1"
                return wt
            def create(self, identifier):
                raise AssertionError("replacement worktree forbidden")
        def factory(worktree, wake):
            return CodexRunner(worktree, db=db, ticket="MOCK-1", executable=str(script), deadline_seconds=999)
        client = LinearClient(token="mock-token", transport=httpx.MockTransport(linear))
        config = ProjectConfig("mock-project", "mock-slug", hold_all=False, blocker_policy=BlockerPolicy((0, 0, 0)))
        poller = LinearPoller(client, config, db=db)
        dispatcher = WorktreeDispatcher(Manager(), factory, db=db, project="mock-project")
        supervisor = Supervisor(poller, db=db, dispatcher=dispatcher)
        supervisor.lifecycle = Lifecycle.RUNNING
        await supervisor.poll_once()
        tasks = list(supervisor._dispatch_tasks.values())
        assert len(tasks) == 1
        await supervisor.poll_once()  # duplicate poll cannot launch another retry
        assert len(supervisor._dispatch_tasks) == 1
        await asyncio.gather(*tasks)
        row = dict(db.connection.execute("SELECT * FROM runs WHERE id='logical-1'").fetchone())
        assert row["status"] == "waiting", supervisor.last_error
        assert row["session_id"] == "native-retained-1"
        assert row["worktree"] == str(wt)
        assert 0 < row["deadline_seconds"] <= remaining
        assert db.connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
        assert db.connection.execute("SELECT attempts FROM recovery_receipts").fetchone()[0] == 1
        proof = json.loads((wt / "resumed.json").read_text())
        assert "Next step:" in proof["prompt"]
        assert "Do not create another session" in proof["prompt"]
        assert proof["cwd"] == str(wt)
        assert subprocess.check_output(["git", "-C", str(wt), "rev-parse", "HEAD"], text=True).strip() == head_before
        if checkpoint in {"push", "pr", "reply"}:
            assert subprocess.check_output(["git", "--git-dir", str(origin), "rev-parse", "ticket/mock-1"], text=True).strip() == head_before
        if checkpoint in {"pr", "reply"}:
            assert json.loads((wt / "mock-pr.json").read_text())["create_calls"] == 1
        if checkpoint == "reply":
            assert json.loads((wt / "mock-reply.json").read_text())["publish_calls"] == 1
        await client.close()
        db.close()
    asyncio.run(scenario())
    assert len(requests) >= 2


def test_fresh_paginated_linear_ac_unblocks_without_human(tmp_path):
    import httpx
    from harbor.clients.linear import LinearClient
    from harbor.db import HarborDB
    from harbor.unblock import UnblockLadder
    seen = []
    def transport(request):
        data = json.loads(request.content)
        seen.append(data)
        if "mutation HarborComment" in data["query"]:
            return httpx.Response(200, json={"data":{"commentCreate":{"success":True,"comment":{"id":"receipt"}}}})
        after = data["variables"]["after"]
        return httpx.Response(200, json={"data":{"issue":{
            "id":"uuid", "identifier":"MOCK-2", "description":"The button color must be blue.",
            "state":{"name":"In Progress","type":"started"},
            "comments":{"nodes":[{"id":str(after), "body":"Use the approved blue button color.","user":{"id":"owner"}}],
                        "pageInfo":{"hasNextPage":after is None,"endCursor":"cursor-2"}}
        }}})
    async def scenario():
        db = HarborDB(tmp_path / "ledger.sqlite")
        client = LinearClient(token="mock", transport=httpx.MockTransport(transport))
        prompts, resumes = [], []
        async def mate(prompt, **kwargs):
            prompts.append(prompt)
            return {"disposition":"resolve","answer":"Use blue", "citation":"ticket:MOCK-2:1", "reason":"The current AC specifies blue."}
        async def resume(run_id, prompt):
            resumes.append((run_id,prompt))
            return True
        ladder = UnblockLadder(db, linear=client, first_mate=mate, resumer=resume, repo_path=tmp_path)
        result = await ladder.handle({"identifier":"MOCK-2","description":"stale ticket text"}, {"id":"retained-2"},
                                     {"next_action":"Implement the button style and test it", "blocker":{"kind":"product_decision","question":"What button color?"}})
        assert result.resumed
        assert len(resumes) == 1 and resumes[0][0] == "retained-2"
        assert "Implement the button style" in resumes[0][1]
        assert "must be blue" in prompts[0] and "stale ticket text" not in prompts[0]
        assert db.escalations()[0]["status"] == "resume_pending"
        await client.close()
        db.close()
    asyncio.run(scenario())
    assert len([x for x in seen if "HarborBlocker" in x["query"]]) == 2
    assert len([x for x in seen if "HarborComment" in x["query"]]) == 1


def test_bounded_decision_requires_option_and_rationale(tmp_path):
    ticket = {"identifier":"MOCK-3", "description":"Show an empty state when the list has no entries."}
    blocker = {"kind":"product_decision", "question":"Which empty-state wording?", "options":["No entries yet", "Nothing here"]}
    async def decide(*args, **kwargs):
        return {"disposition":"resolve", "answer":"No entries yet", "citation":"project:bounded-decision", "reason":"Reversible copy choice within the empty-state AC; no public contract changes."}
    result = asyncio.run(triage(ticket, blocker, first_mate=decide, repo_path=tmp_path, policy=BlockerPolicy()))
    assert result.resolved and result.reason
    async def unsupported(*args, **kwargs):
        return {"disposition":"resolve", "answer":"Charge users $5", "citation":"project:bounded-decision"}
    assert not asyncio.run(triage(ticket, blocker, first_mate=unsupported, repo_path=tmp_path, policy=BlockerPolicy())).resolved
    blocker["question"] = "Which billing policy?"
    assert not asyncio.run(triage(ticket, blocker, first_mate=decide, repo_path=tmp_path, policy=BlockerPolicy())).resolved


def test_decisions_are_project_scoped(tmp_path):
    from harbor.db import HarborDB
    db = HarborDB(tmp_path / "ledger.sqlite")
    for project, ticket, answer in [("project-a", "A-1", "Use blue"), ("project-b", "B-1", "Use green")]:
        db.replace_tickets(project, [{"identifier":ticket,"state":"Blocked","title":"Color"}], emit_events=False)
        db.record_resolution("product_decision", "Which color?", answer, f"linear:{ticket}#answer", source_ticket=ticket)
    assert db.find_resolution("product_decision", "Which color?", project="project-a")["answer"] == "Use blue"
    assert db.find_resolution("product_decision", "Which color?", project="project-b")["answer"] == "Use green"
    assert db.find_resolution("product_decision", "Which color?", project="unknown") is None
    assert db.find_resolution("product_decision", "Which color?") is None
    db.close()


def test_conflict_does_not_call_model(tmp_path):
    async def forbidden(*args, **kwargs):
        raise AssertionError("conflicting instructions need scope reconciliation")
    result = asyncio.run(triage({"identifier":"MOCK-4", "description":"Preserve the API"},
                                {"kind":"conflicting_instruction", "question":"Change the API?"},
                                first_mate=forbidden, repo_path=tmp_path, policy=BlockerPolicy()))
    assert not result.resolved and "reconciliation" in result.reason


def test_committed_protected_path_parks_with_full_context(tmp_path):
    import subprocess
    from harbor.db import HarborDB
    from harbor.unblock import UnblockLadder
    repo = tmp_path / "repo"
    repo.mkdir()
    def git(*args):
        return subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.test", *args], capture_output=True, text=True, check=True)
    git("init", "-b", "main")
    git("commit", "--allow-empty", "-m", "initial")
    git("checkout", "-b", "ticket")
    (repo / "secret-policy.py").write_text("# protected auth code\n")
    git("add", "secret-policy.py")
    git("commit", "-m", "partial change")
    comments = []
    class Linear:
        async def create_comment(self, issue_id, body):
            comments.append(body)
            return {"id":"comment-1"}
        async def update_state(self, *args, **kwargs):
            return True
    async def forbidden(*args, **kwargs):
        raise AssertionError("protected path must not invoke model or resume")
    db = HarborDB(tmp_path / "ledger.sqlite")
    ladder = UnblockLadder(db, linear=Linear(), first_mate=forbidden, resumer=forbidden, repo_path=repo,
                           protected_paths=("secret-policy.py",), policy=BlockerPolicy())
    result = asyncio.run(ladder.handle({"identifier":"MOCK-5", "id":"uuid-5"}, {"id":"retained-5", "worktree":str(repo)},
                                      {"blocker":{"kind":"product_decision", "question":"Which option?", "options":["A","B"], "recommendation":"A", "evidence":"Partial auth code committed"}}))
    assert not result.resumed and result.reason == "protected path"
    assert comments and all(word in comments[0] for word in ["Options:","Recommendation:","Evidence:","Prior attempts:"])
    db.close()


@pytest.mark.parametrize("gate", ["hold", "shadow", "paused", "foreign", "cancelled", "dependency", "unhealthy", "missing_session", "live_process", "deadline", "ticket_paused", "review_lower", "backlog_lower", "terminal_name"])
def test_supervisor_recovery_gates(tmp_path, gate):
    import os
    from types import SimpleNamespace
    from harbor.db import HarborDB
    from harbor.runner.base import apply_phase4_migration, apply_phase9_migration
    from harbor.supervisor import Supervisor, Lifecycle
    db = HarborDB(tmp_path / "ledger.sqlite")
    from harbor.wake import apply_phase3_migration
    apply_phase3_migration(db)
    apply_phase4_migration(db)
    apply_phase9_migration(db)
    ticket = {"identifier":"MOCK-6", "title":"Ticket", "state":"In Progress", "owned":True, "hold":gate == "hold"}
    if gate in {"ticket_paused", "review_lower", "backlog_lower", "terminal_name"}:
        ticket["state"] = {"ticket_paused": " Paused ", "review_lower": "human review", "backlog_lower": "backlog", "terminal_name": "CANCELLED"}[gate]
    if gate == "cancelled": ticket["state_type"] = "canceled"
    if gate == "dependency": ticket["blocked_by"] = ["MOCK-7"]
    db.replace_tickets("other" if gate == "foreign" else "mock", [ticket], emit_events=False)
    db.connection.execute("INSERT INTO runs(id,ticket,status,terminal_reason,session_id,worktree,pid,deadline_seconds) VALUES (?,?,?,?,?,?,?,?)",
                          ("run-6","MOCK-6","failed","unexpected_exit", None if gate == "missing_session" else "session-6", str(tmp_path), os.getpid() if gate == "live_process" else None, 0 if gate == "deadline" else 60))
    db.connection.commit()
    class Dispatcher:
        async def retry(self, *args):
            raise AssertionError("gate must prevent retry")
    poller = SimpleNamespace(project_name="mock", config=SimpleNamespace(blocker_policy=BlockerPolicy((0,))))
    supervisor = Supervisor(poller, db=db, dispatcher=Dispatcher(), shadow=gate == "shadow")
    supervisor.lifecycle = Lifecycle.PAUSED if gate == "paused" else Lifecycle.RUNNING
    supervisor.linear_healthy = gate != "unhealthy"
    supervisor._fleet_tickets = (ticket,)
    async def scenario():
        await supervisor._retry_stopped_builders()
        # Inspect before loop shutdown can cancel and discard a forbidden task.
        assert not supervisor._dispatch_tasks
    asyncio.run(scenario())
    db.close()


def test_killed_first_mate_retries_before_resuming_builder(tmp_path):
    import sys
    from types import SimpleNamespace
    from harbor.db import HarborDB
    from harbor.first_mate import FirstMate, CodexExecRunner
    from harbor.runner.base import apply_phase4_migration, apply_phase9_migration
    from harbor.supervisor import Supervisor, Lifecycle
    from harbor.unblock import UnblockLadder
    from harbor.wake import Wake, apply_phase3_migration
    marker = tmp_path / "model-attempt"
    script = tmp_path / "firstmate.py"
    script.write_text('''import json, os, pathlib, signal, sys
marker = pathlib.Path(sys.argv[1])
prompt = sys.stdin.read()
assert "CITE OR ESCALATE" in prompt
assert "What button color?" in prompt
if not marker.exists():
    marker.touch()
    os.kill(os.getpid(), signal.SIGKILL)
print(json.dumps({"disposition":"resolve","answer":"Use blue","citation":"ticket:MOCK-8:1","reason":"Current AC specifies blue."}))
''')
    db = HarborDB(tmp_path / "ledger.sqlite")
    apply_phase3_migration(db)
    apply_phase4_migration(db)
    apply_phase9_migration(db)
    ticket = {"identifier":"MOCK-8", "id":"uuid-8", "title":"Button", "description":"The button color must be blue.", "state":"In Progress", "owned":True, "hold":False}
    final = {"status":"blocked", "summary":"Need a color", "run_id":"run-8", "blocker":{"kind":"product_decision","question":"What button color?"}}
    db.replace_tickets("mock", [ticket], emit_events=False)
    db.connection.execute("INSERT INTO runs(id,ticket,status,worktree,session_id,deadline_seconds,final_json) VALUES (?,?,?,?,?,?,?)", ("run-8","MOCK-8","blocked",str(tmp_path),"session-8",60,json.dumps(final)))
    db.connection.commit()
    calls = []
    class Dispatcher:
        async def retry(self, *args):
            raise AssertionError("builder must wait until First Mate resolves")
    async def model_runner(command, prompt):
        assert "--ephemeral" in command and "read-only" in command
        return await CodexExecRunner()([sys.executable,str(script),str(marker)],prompt)
    poller = SimpleNamespace(project_name="mock", config=SimpleNamespace(blocker_policy=BlockerPolicy((0,0))))
    supervisor = Supervisor(poller, db=db, dispatcher=Dispatcher())
    async def resume(run_id, prompt):
        calls.append((run_id,prompt))
        return True
    ladder = UnblockLadder(db, first_mate=FirstMate(runner=model_runner), resumer=resume, repo_path=tmp_path)
    supervisor.unblock_ladder = ladder
    supervisor.lifecycle = Lifecycle.RUNNING
    supervisor.linear_healthy = True
    supervisor._fleet_tickets = (ticket,)
    wake = Wake(ticket=ticket, action="reengage", resume="run-8")
    async def scenario():
        await supervisor._handle_runner_result(wake, final)
        assert not calls and "-9" in supervisor.last_error
        assert db.connection.execute("SELECT attempts FROM recovery_receipts WHERE run_id='triage:run-8'").fetchone()[0] == 0
        await supervisor._retry_stopped_builders()
        await asyncio.gather(*list(supervisor._dispatch_tasks.values()))
        assert len(calls) == 1 and calls[0][0] == "run-8"
        assert db.escalations()[0]["status"] == "resume_pending"
        # Only a later observed delivery result confirms progress.
        await supervisor._handle_runner_result(wake, {"status":"waiting", "summary":"Applied blue style", "run_id":"run-8", "waiting_on":"review"})
        assert db.escalations()[0]["status"] == "resolved"
    asyncio.run(scenario())
    db.close()


def test_related_ticket_is_not_a_blocking_dependency():
    from harbor.poll.linear_poller import normalize_issue
    issue = {"identifier":"MOCK-9", "state":{"name":"Todo"}, "inverseRelations":{"nodes":[
        {"type":"related", "issue":{"identifier":"MOCK-10","state":{"type":"started"}}},
        {"type":"blocks", "issue":{"identifier":"MOCK-11","state":{"type":"started"}}},
    ]}}
    assert normalize_issue(issue)["blocked_by"] == ["MOCK-11"]


def test_authorized_comment_answers_without_description(tmp_path):
    ticket = {"identifier":"MOCK-12", "linear_comments":[{"id":"c1", "body":"Use blue for the button color.", "author":{"id":"owner"}}]}
    async def mate(*args, **kwargs):
        return {"disposition":"resolve", "answer":"Use blue", "citation":"linear:MOCK-12#comment-c1"}
    policy = BlockerPolicy(authorized_authors=("owner",))
    blocker = {"kind":"product_decision","question":"What button color?"}
    assert asyncio.run(triage(ticket, blocker, first_mate=mate, repo_path=tmp_path, policy=policy)).resolved
    ticket["linear_comments"][0]["author"]["id"] = "outsider"
    assert not asyncio.run(triage(ticket, blocker, first_mate=mate, repo_path=tmp_path, policy=policy)).resolved


@pytest.mark.parametrize("mode", ["missing_final", "different_session"])
def test_resume_without_new_final_does_not_reuse_old_success(tmp_path, mode):
    import sys
    from harbor.db import HarborDB
    from harbor.runner.codex import CodexRunner
    from harbor.wake import apply_phase3_migration
    script = tmp_path / "builder"
    script.write_text(f'''#!{sys.executable}
import json, sys
sys.stdin.read()
if "resume" not in sys.argv:
    print(json.dumps({{"type":"session.started","session_id":"retained-stale"}}))
    print(json.dumps({{"type":"result","result":{{"status":"waiting","waiting_on":"review","summary":"Old result"}}}}))
elif {mode!r} == "different_session":
    print(json.dumps({{"type":"session.started","session_id":"replacement"}}))
''')
    script.chmod(0o755)
    db = HarborDB(tmp_path / "ledger.sqlite")
    apply_phase3_migration(db)
    db.replace_tickets("mock", [{"identifier":"MOCK-STALE", "state":"In Progress", "title":"Stale final"}], emit_events=False)
    async def scenario():
        runner = CodexRunner(tmp_path, db=db, ticket="MOCK-STALE", executable=str(script), deadline_seconds=30)
        first = await runner.start("start", run_id="stale-test")
        assert first.status == "waiting"
        second = await runner.resume("stale-test", "continue")
        assert second.status == "failed"
        expected = "unexpected_exit" if mode == "missing_final" else "session_mismatch"
        row = db.connection.execute("SELECT terminal_reason,session_id FROM runs WHERE id='stale-test'").fetchone()
        assert tuple(row) == (expected, "retained-stale")
    asyncio.run(scenario())
    db.close()


def test_pipeline_settings_reach_project_runtime(tmp_path):
    from harbor.config.projects import ProjectConfig
    pipeline = tmp_path / "pipeline.md"
    pipeline.write_text("linear_project_slug: mock\nlinear_team: MOCK\nfirstmate_decisions: cite_only\nrecovery_delays: [1, 5]\nunblock_attempts: 1\ndecision_authors: [owner, backup]\n")
    config = ProjectConfig.from_pipeline("mock", pipeline)
    assert config.blocker_policy == BlockerPolicy((1,5), "cite_only", 1, ("owner","backup"))


def test_ticket_moved_out_of_project_is_not_resumed(tmp_path):
    from harbor.unblock import UnblockLadder
    class Linear:
        async def fetch_blocker_context(self, identifier):
            return {"identifier":identifier,"state":{"name":"In Progress"},"project":{"slugId":"foreign"}}
    ladder = UnblockLadder(linear=Linear(), repo_path=tmp_path, linear_projects=("expected",))
    with pytest.raises(ValueError, match="left the configured"):
        asyncio.run(ladder.handle({"identifier":"MOCK-13"}, {"id":"retained"}, {}))


def test_reconfigured_worktree_root_cannot_replace_retained_worktree(tmp_path):
    from harbor.db import HarborDB
    from harbor.runner.base import apply_phase4_migration
    from harbor.supervisor import WorktreeDispatcher
    from harbor.wake import apply_phase3_migration
    retained, other = tmp_path / "retained", tmp_path / "other"
    retained.mkdir()
    other.mkdir()
    db = HarborDB(tmp_path / "ledger.sqlite")
    apply_phase3_migration(db)
    apply_phase4_migration(db)
    db.replace_tickets("mock", [{"identifier":"MOCK-14","state":"Blocked","title":"Custody"}], emit_events=False)
    db.connection.execute("INSERT INTO runs(id,ticket,status,session_id,worktree) VALUES (?,?,?,?,?)", ("run-14","MOCK-14","failed","native-14",str(retained)))
    db.connection.commit()
    class Manager:
        def attach(self, identifier):
            return other
    def factory(*args):
        raise AssertionError("must not construct a runner in another worktree")
    dispatcher = WorktreeDispatcher(Manager(), factory, db=db)
    assert asyncio.run(dispatcher.hydrate("MOCK-14", recover_terminal=True)) is None
    assert db.connection.execute("SELECT worktree FROM runs").fetchone()[0] == str(retained)
    db.close()


def test_legacy_resolution_requires_project_provenance(tmp_path):
    from harbor.db import HarborDB
    db = HarborDB(tmp_path / "ledger.sqlite")
    db.record_resolution("product_decision","Which color?","Use blue","operator:old",source_ticket="MOCK-15")
    assert db.find_resolution("product_decision","Which color?",project="mock") is None
    db.replace_tickets("mock", [{"identifier":"MOCK-15","state":"Blocked","title":"Color"}], emit_events=False)
    assert db.find_resolution("product_decision","Which color?",project="mock")["answer"] == "Use blue"
    assert db.find_resolution("product_decision","Which color?",project="foreign") is None
    db.close()
