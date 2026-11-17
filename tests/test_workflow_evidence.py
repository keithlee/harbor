import asyncio
import sqlite3
from harbor.linear_feedback import pending
from harbor.risk_evidence import observed_floor,retain_floor
from harbor.config.workflow import load_workflow
from harbor.hooks import execute_event
from test_workflow_config import config


def test_linear_reply_is_bound_to_comment_version():
    source={'id':'1','body':'Fix wording','createdAt':'1','updatedAt':'1'}
    raw={'comments':{'nodes':[source]}}
    marker=pending(raw)[0]['marker']
    raw['comments']['nodes'].append({'id':'2','body':'Fixed '+marker,'createdAt':'2'})
    assert pending(raw)==[]
    source['body']='Fix different wording'
    assert len(pending(raw))==1


def test_medium_ticket_choice_cannot_lower_merge_risk():
    db=sqlite3.connect(':memory:')
    risk=observed_floor({'description':'Risk: Medium'},{'risk':'low'})
    assert observed_floor({'description':'Risk: HIGH'},{'risk':'low'})=='high'
    assert retain_floor(db,'A','A-1','run',risk)=='medium'
    assert retain_floor(db,'A','A-1','run','low')=='medium'
    assert retain_floor(db,'B','B-1','run','low')=='low'


def test_packaged_script_is_executed_and_fails_closed(tmp_path):
    workflow=load_workflow(config(tmp_path,'autonomous'))
    base={'project':'A','ticket':'A-1','run_id':'r','event':'pre_merge','fresh':True,'hold':False,'risk':'low','gaps':{'risk':'valid'},'review_verified':True,'proof_verified':True,'facts':{'url':'https://example.test/pr/1','head_sha':'a','mergeable':'MERGEABLE','ci':'green'}}
    async def check(ctx):
        return await execute_event(workflow,'pre_merge',ctx,cwd=tmp_path)
    assert not any(r['blocking'] for r in asyncio.run(check(base)))
    for change in ({'risk':'medium'},{'hold':True},{'review_verified':False},{'linear_feedback':[{'id':'new'}]},{'gaps':{'risk':'stale'}},{'fresh':False}):
        assert any(r['blocking'] for r in asyncio.run(check({**base,**change})))
