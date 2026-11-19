import asyncio
from datetime import datetime,timezone
from types import SimpleNamespace
from harbor.db import HarborDB
from harbor.config.workflow import load_workflow
from harbor.workflow_lifecycle import cleanup_allowed
from test_workflow_config import config
from test_worktree_phase4 import disposable_repo,git


def test_cleanup_requires_fresh_terminal_merge_and_exact_head(tmp_path,monkeypatch):
    repo,_=disposable_repo(tmp_path)
    head=git(repo,'rev-parse','HEAD')
    db=HarborDB(':memory:')
    workflow=load_workflow(config(tmp_path))
    run={'id':'r','status':'completed','finished_at':datetime.now(timezone.utc).isoformat()}
    supervisor=SimpleNamespace(project='A',db=db,dispatcher=SimpleNamespace(workflow=workflow),_ticket_key=lambda t:'A-1',_latest_run=lambda t:run)
    context={'project':'A','ticket':'A-1','run_id':'r','facts':{'merged':True},'terminal':True,'head_sha':head}
    async def handoff(*args):return context
    monkeypatch.setattr('harbor.workflow_lifecycle.handoff',handoff)
    def allowed():return asyncio.run(cleanup_allowed(supervisor,SimpleNamespace(ticket={}),{},repo,'r'))
    assert allowed()
    for key,value in [('head_sha','newer'),('terminal',False)]:
        old=context[key];context[key]=value
        assert not allowed()
        context[key]=old
    workflow.config['cleanup']['mode']='manual'
    assert not allowed()
    workflow.config['cleanup']['mode']='after_merge'
    workflow.config['cleanup']['delay_seconds']=100
    assert not allowed()
    workflow.config['cleanup']['delay_seconds']=0
    db.close()
