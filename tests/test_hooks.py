import asyncio
import sys
import sqlite3
import pytest
from harbor.config.workflow import load_workflow
from harbor.hooks import execute_hook
from harbor.workflow_store import bind, record
from test_workflow_config import config


@pytest.mark.parametrize("mode,status,blocking", [("required", "fail", True), ("advisory", "fail", False), ("off", "disabled", False)])
def test_hook_modes(tmp_path, mode, status, blocking):
    workflow = load_workflow(config(tmp_path))
    workflow.config["hooks"]["x"] = {"mode": mode, "event": "setup", "executor": "script", "command": [sys.executable, "-c", "raise SystemExit(2)"], "protocol": "exit_code"}
    result = asyncio.run(execute_hook(workflow, "x", {"project":"A", "ticket":"A-1", "run_id":"r"}, cwd=tmp_path))
    assert result["status"] == status and result["blocking"] == blocking


def test_timeout_and_snapshot_change(tmp_path):
    workflow = load_workflow(config(tmp_path))
    workflow.config["hooks"]["x"] = {"mode":"required", "event":"setup", "executor":"script", "command":[sys.executable,"-c","import time;time.sleep(10)"], "protocol":"json", "timeout_seconds":0.01}
    result = asyncio.run(execute_hook(workflow,"x", {"project":"A","ticket":"A-1","run_id":"r"},cwd=tmp_path))
    assert result["blocking"]
    db = sqlite3.connect(":memory:")
    bind(db,"A","A-1","r",workflow)
    record(db,result)
    # A policy edit is adopted and recorded, never a blocker.
    bind(db,"A","A-1","r",load_workflow(config(tmp_path,"reviewed")))
    assert [r[0] for r in db.execute("SELECT kind FROM workflow_receipts")][-1] == "policy_change"


def test_skill_hook_and_oversized_invalid_json(tmp_path,monkeypatch):
    import json
    workflow=load_workflow(config(tmp_path))
    context={'project':'A','ticket':'A-1','run_id':'r'}
    workflow.config['skills']['blocker_assessment']['model']='test-model'
    workflow.config['hooks']['assessment']={'mode':'required','event':'blocker','executor':'skill','skill':'blocker_assessment'}
    async def triage(self,prompt):
        assert self.model=='test-model'
        return json.dumps({'status':'pass','reason':'cited option','evidence':['project:policy']})
    monkeypatch.setattr('harbor.first_mate.FirstMate.triage',triage)
    assert asyncio.run(execute_hook(workflow,'assessment',context,cwd=tmp_path))['status']=='pass'
    for output in ('x'*66000,'{}','{"status":"pass","reason":"ok","evidence":"wrong"}'):
        workflow.config['hooks']['bad']={'mode':'required','event':'setup','executor':'script','protocol':'json','command':[sys.executable,'-c',f'print({output!r})']}
        assert asyncio.run(execute_hook(workflow,'bad',context,cwd=tmp_path))['blocking']


def test_hook_timeout_kills_descendant_after_parent_exit(tmp_path):
    import os
    import time
    workflow=load_workflow(config(tmp_path))
    program="import subprocess,sys,pathlib; p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']); pathlib.Path('child.pid').write_text(str(p.pid))"
    workflow.config['hooks']['fork']={'mode':'required','event':'setup','executor':'script','protocol':'json','command':[sys.executable,'-c',program],'timeout_seconds':.2}
    result=asyncio.run(execute_hook(workflow,'fork',{'project':'A','ticket':'A-1','run_id':'r'},cwd=tmp_path))
    assert result['blocking']
    pid=int((tmp_path/'child.pid').read_text())
    from harbor.runner.base import process_alive
    for _ in range(100):
        if not process_alive(pid):break
        time.sleep(.01)
    try:
        assert not process_alive(pid)
    finally:
        if process_alive(pid):os.kill(pid,9)
