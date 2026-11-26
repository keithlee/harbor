import asyncio
import json
from types import SimpleNamespace
import pytest
from harbor.cli import build_parser
from harbor.db import HarborDB
from harbor.wake import apply_phase3_migration
from harbor.workflow_cli import execute, fresh_context
from workflow_support import config


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
        @classmethod
        def from_profile(cls,profile,**kwargs):return cls(**kwargs)
        async def close(self):pass
        async def fetch_pull_requests(self,*args,**kwargs):
            return {'pr_1':{'number':1,'headRefOid':'abc','body':'## Risk Analysis\nRisk tier: low\n<!-- harbor:risk-sha:abc -->','url':'https://github.com/example/repo/pull/1','merged':False,'mergeable':'MERGEABLE','comments':{'nodes':[]}}}
    monkeypatch.setattr('harbor.clients.linear.LinearClient',Linear)
    monkeypatch.setattr('harbor.clients.github.GitHubClient',GitHub)
    cfg=SimpleNamespace(name='A',linear_api_url='https://linear.invalid',linear_project_scope=('canonical',),github_repository='example/repo',required_checks=(),workflow_path=str(config(tmp_path)))
    ctx=asyncio.run(fresh_context(cfg,db,'A-1',{'id':'run'},'pre_merge'))
    assert ctx['risk']=='medium' and ctx['head_sha']=='abc' and not ctx['hold']
    raw['project']['slugId']='foreign'
    with pytest.raises(ValueError,match='configured'):
        asyncio.run(fresh_context(cfg,db,'A-1',{'id':'run'},'pre_merge'))
    db.close()


@pytest.mark.parametrize(
    ("state", "state_type", "ready"),
    [("In Progress", "started", True), ("Icebox", "backlog", False)],
    ids=["active-ticket", "custom-backlog-state"],
)
def test_pre_merge_checks_fresh_linear_state(tmp_path, monkeypatch, capsys, state, state_type, ready):
    pr_url = "https://github.com/example/repo/pull/1"

    class Linear:
        def __init__(self, **kwargs):
            pass

        async def fetch_blocker_context(self, ticket):
            return {
                "identifier": ticket,
                "project": {"slugId": "canonical"},
                "description": "Risk: low",
                "state": {"name": state, "type": state_type},
                "attachments": {"nodes": [{"sourceType": "github", "url": pr_url}]},
            }

        async def close(self):
            pass

    class GitHub:
        @classmethod
        def from_profile(cls, profile, **kwargs):
            return cls()

        async def fetch_pull_requests(self, *args, **kwargs):
            return {"pr_1": {
                "number": 1,
                "headRefOid": "abc",
                "body": "## Risk Analysis\nRisk tier: low\n<!-- harbor:risk-sha:abc -->\n",
                "url": pr_url,
                "merged": False,
                "mergeable": "MERGEABLE",
                "statusCheckRollup": {"contexts": {"nodes": [
                    {"name": "tests", "conclusion": "SUCCESS"},
                ]}},
            }}

        async def close(self):
            pass

    monkeypatch.setattr("harbor.clients.linear.LinearClient", Linear)
    monkeypatch.setattr("harbor.clients.github.GitHubClient", GitHub)
    cfg = SimpleNamespace(
        name="A",
        linear_api_url="https://linear.invalid",
        linear_project_scope=("canonical",),
        github_repository="example/repo",
        required_checks=("tests",),
        workflow_path=config(tmp_path, "autonomous"),
    )
    args = build_parser().parse_args(["workflow", "check", "A", "--ticket", "A-1", "--event", "pre_merge"])
    db = HarborDB(":memory:")
    try:
        apply_phase3_migration(db)
        db.replace_tickets("A", [{"identifier": "A-1", "state": "In Progress", "owned": True, "hold": False}])
        with db.connection:
            db.connection.execute(
                "INSERT INTO runs (id, ticket, worktree, status) VALUES (?, ?, ?, ?)",
                ("run", "A-1", str(tmp_path), "running"),
            )
        exit_code = asyncio.run(execute(args, cfg, db))
    finally:
        db.close()

    output = json.loads(capsys.readouterr().out)
    assert output["ready"] is ready, output
    assert exit_code == (0 if ready else 1)
    assert output["head_sha"] == "abc"
    assert output["results"][0]["reason"] == (
        "configured requirements satisfied" if ready else "ticket is held or hold state unknown"
    )
