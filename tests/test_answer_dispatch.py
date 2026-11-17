import asyncio
import sys

from harbor.console import Console
from harbor.db import HarborDB
from harbor.supervisor import Supervisor
from test_integration_console_supervisor import FakePoller


def test_answer_keeps_console_responsive_and_exit_reaps_process(tmp_path):
    async def scenario():
        db = HarborDB(tmp_path / "answer.db")
        db.replace_tickets("SampleProject", [{"identifier": "SAMPLE-42", "state": "Blocked"}])
        db.record_escalation("SAMPLE-42", "run-42", "ambiguous_ac", "Which?", 2)
        started = asyncio.Event()

        class Runner:
            process = None
            calls = 0

            async def resume(self, run_id, prompt):
                self.calls += 1
                assert run_id == "run-42"
                self.process = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(60)")
                started.set()
                await self.process.wait()
                return True

            async def terminate(self, **kwargs):
                if self.process and self.process.returncode is None:
                    self.process.terminate()
                    await self.process.wait()

        runner = Runner()
        supervisor = Supervisor(FakePoller(), db=db, runner=runner)
        console = Console(supervisor)
        result = await asyncio.wait_for(console.handle_command("/answer 1 Option A"), 1)
        assert "queued" in result
        await asyncio.wait_for(started.wait(), 2)
        assert await asyncio.wait_for(console.handle_command("/status"), 1)
        assert "active work" in await console.handle_command("/answer 1 Option A")
        await asyncio.wait_for(console.handle_command("/exit"), 3)
        assert runner.calls == 1 and runner.process.returncode is not None
        assert not supervisor._dispatch_tasks
        db.close()

    asyncio.run(scenario())


def test_queued_answer_survives_restart_and_does_not_replay_stop(tmp_path):
    from harbor.answer_queue import enqueue,drain,deliver
    from types import SimpleNamespace
    async def scenario():
        db=HarborDB(tmp_path/'queue.db')
        enqueue(db.connection,'one','A','A-1','run','Choose A')
        db.close()
        db=HarborDB(tmp_path/'queue.db')
        calls=[]
        async def resume(run,instruction):calls.append((run,instruction));return True
        run={'id':'run','terminal_reason':None}
        supervisor=SimpleNamespace(db=db,project='A',read_only=False,_dispatch_suspended=False,_fleet_tickets=[{'identifier':'A-1','hold':False,'state':'In Progress'}],_latest_run=lambda _:run,_in_scope=lambda _:True,_dispatch_tasks={},_dispatch_wakes={},resume_session=resume,_dispatch_finished=lambda *args:None)
        supervisor.dispatcher=SimpleNamespace(workflow=SimpleNamespace(config={'states':{'paused':['Awaiting Approval']}}))
        supervisor._fleet_tickets[0]['state']='Awaiting Approval'
        drain(supervisor)
        assert not supervisor._dispatch_tasks
        supervisor._fleet_tickets[0]['state']='In Progress'
        drain(supervisor)
        await asyncio.gather(*supervisor._dispatch_tasks.values())
        assert calls==[('run','Choose A')]
        assert not await deliver(supervisor,'one','run','Choose A')
        enqueue(db.connection,'two','A','A-1','run','Choose B')
        run['terminal_reason']='operator exit'
        drain(supervisor)
        assert db.connection.execute("SELECT status FROM answer_queue WHERE escalation='two'").fetchone()[0]=='stopped'
        assert len(calls)==1
        db.close()
    asyncio.run(scenario())
