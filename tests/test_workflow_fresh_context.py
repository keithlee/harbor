import asyncio
from types import SimpleNamespace
import pytest
from harbor.db import HarborDB
from harbor.workflow_cli import fresh_context


def test_fresh_context_scopes_actual_client_observations(tmp_path,monkeypatch):
    db=HarborDB(':memory:')
    db.replace_tickets('A',[{'identifier':'A-1','state':'In Progress','owned':True,'hold':False}])
    raw={'identifier':'A-1','project':{'slugId':'canonical'},'title':'Mock ticket','description':'Risk: medium','state':{'name':'In Progress','type':'started'},'comments':{'nodes':[]},'attachments':{'nodes':[{'sourceType':'github','url':'https://github.com/example/repo/pull/1'}]},'inverseRelations':{'nodes':[]}}
    class Linear:
        def __init__(self,**kwargs):pass
        async def fetch_blocker_context(self,ticket):return raw
        async def close(self):pass
    class GitHub:
        def __init__(self,**kwargs):pass
        async def close(self):pass
        async def fetch_pull_requests(self,*args,**kwargs):
            return {'pr_1':{'number':1,'headRefOid':'abc','url':'https://github.com/example/repo/pull/1','merged':False,'mergeable':'MERGEABLE','comments':{'nodes':[]}}}
    monkeypatch.setattr('harbor.clients.linear.LinearClient',Linear)
    monkeypatch.setattr('harbor.clients.github.GitHubClient',GitHub)
    from test_workflow_config import config
    cfg=SimpleNamespace(name='A',linear_api_url='https://linear.invalid',linear_project_scope=('canonical',),github_repository='example/repo',required_checks=(),workflow_path=str(config(tmp_path)))
    ctx=asyncio.run(fresh_context(cfg,db,'A-1',{'id':'run'},'pre_merge'))
    assert ctx['risk']=='medium' and ctx['head_sha']=='abc' and not ctx['hold']
    raw['project']['slugId']='foreign'
    with pytest.raises(ValueError,match='configured'):
        asyncio.run(fresh_context(cfg,db,'A-1',{'id':'run'},'pre_merge'))
    db.close()
