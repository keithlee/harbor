import asyncio
import json
import sqlite3
import sys

import pytest
from harbor.runner.codex import CodexRunner
from harbor.model_policy import model_rejection, validate_fallbacks
from harbor.first_mate import FirstMate, FirstMateError, ModelUnavailableError
from harbor.recovery import recoverable
from test_runner_phase4 import runs_db


def provider_error(model='primary'):
    return {'type':'error','status':400,'error':{'type':'invalid_request_error','message':f"The '{model}' model is not supported when using Codex with a ChatGPT account."}}


def executable(tmp_path, *, rejected=('primary',), error=None, session=True):
    path=tmp_path/'runner.py'
    path.write_text(f'''#!{sys.executable}
import json, sys, pathlib
args=sys.argv[1:]
model=args[args.index('-m')+1]
root=pathlib.Path.cwd()
with (root/'attempts.jsonl').open('a') as f: f.write(json.dumps(args)+'\\n')
if {session!r}: print(json.dumps({{'type':'thread.started','thread_id':'same-session'}}),flush=True)
if model in {rejected!r}:
    print(json.dumps({{'type':'error','message':json.dumps({error or provider_error()!r})}}),flush=True)
    raise SystemExit(1)
print(json.dumps({{'type':'result','result':{{'status':'waiting','outcome':'no_pr','waiting_on':'human','summary':'fallback worked','next_action':'wait','pr_url':None,'blocker':None}}}}),flush=True)
''')
    path.chmod(0o755)
    return str(path)


def attempts(tmp_path):
    return [json.loads(l) for l in (tmp_path/'attempts.jsonl').read_text().splitlines()]


def test_fallback_retains_session_worktree_queue_budget_and_restart(tmp_path):
    async def scenario():
        db=runs_db()
        db.execute("CREATE TABLE work_queue(id INTEGER PRIMARY KEY,ticket TEXT,status TEXT,run_id TEXT,updated_at TEXT,error TEXT)")
        db.execute("INSERT INTO work_queue VALUES(1,'DEM-1','queued','run',NULL,NULL)");db.commit()
        runner=CodexRunner(tmp_path,db=db,ticket='DEM-1',model='primary',fallback_models=['fallback'],executable=executable(tmp_path),deadline_seconds=20)
        result=await runner.start('work',run_id='run')
        assert result.status=='waiting' and result.session_id=='same-session'
        calls=attempts(tmp_path)
        assert len(calls)==2 and calls[0][calls[0].index('-m')+1]=='primary'
        assert calls[1][:3]==['exec','resume','same-session'] and calls[1][calls[1].index('-m')+1]=='fallback'
        row=dict(db.execute('SELECT * FROM runs').fetchone())
        assert row['worktree']==str(tmp_path) and row['terminal_reason'] is None and 0<row['deadline_seconds']<=20
        assert db.execute('SELECT status FROM work_queue').fetchone()[0]=='waiting'
        receipts=[json.loads(r[0]) for r in db.execute("SELECT payload FROM workflow_receipts WHERE kind='model_fallback'")]
        assert receipts[0]['selected']=='fallback' and receipts[0]['reason']=='model_unavailable'
        restarted=CodexRunner(tmp_path,db=db,ticket='DEM-1',model='primary',fallback_models=['fallback'],executable=str(tmp_path/'runner.py'))
        again=await restarted.resume('run','continue')
        assert again.session_id=='same-session' and len(attempts(tmp_path))==3
        assert attempts(tmp_path)[-1][attempts(tmp_path)[-1].index('-m')+1]=='fallback'
        db.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('fallbacks,rejected,session,expected_calls',[
    ([],('primary',),True,1),
    (['fallback'],('primary','fallback'),True,2),
    (['fallback','last'],('primary','fallback','last'),True,3),
    (['fallback'],('primary',),False,1),
])
def test_unavailable_chain_is_bounded_and_not_auto_recoverable(tmp_path,fallbacks,rejected,session,expected_calls):
    async def scenario():
        db=runs_db()
        runner=CodexRunner(tmp_path,db=db,ticket='DEM-1',model='primary',fallback_models=fallbacks,executable=executable(tmp_path,rejected=rejected,session=session))
        result=await runner.start('work',run_id='run')
        assert result.status=='failed' and len(attempts(tmp_path))==expected_calls
        assert not recoverable(dict(db.execute('SELECT * FROM runs').fetchone()))
        db.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('error',[
    {'type':'error','status':401,'error':{'type':'authentication_error','message':'bad token'}},
    {'type':'error','status':429,'error':{'code':'rate_limit_exceeded'}},
    {'type':'error','status':500,'error':{'type':'server_error'}},
    {'type':'error','status':400,'error':{'type':'invalid_request_error','message':'unsupported reasoning effort'}},
    {'type':'error','message':'tool failed'},
    {'type':'error','error':{'code':['model_not_found']}},
    {'type':'error','error':{'code':{'model':'unsupported_model'}}},
    {'type':'error','status':401,'error':{'type':'authentication_error','code':'model_not_found'}},
    {'type':'error','status':429,'error':{'type':'rate_limit_error','code':'unsupported_model'}},
    {'type':'error','status':400,'error':{'type':'authentication_error','code':'model_not_found'}},
    {'type':'error','error':{'type':['invalid_request_error'],'code':'model_not_found'}},
])
def test_other_errors_never_switch_model(tmp_path,error):
    async def scenario():
        runner=CodexRunner(tmp_path,model='primary',fallback_models=['fallback'],executable=executable(tmp_path,error=error))
        result=await runner.start('work')
        assert result.status=='failed' and len(attempts(tmp_path))==1
    asyncio.run(scenario())


def test_classifier_does_not_accept_assistant_prose_or_persist_raw_error(tmp_path):
    assert not model_rejection('The model is not supported')
    assert not model_rejection({'type':'agent_message','text':json.dumps(provider_error())})
    error=provider_error();error['error']['message']+=' SECRET_TEST_CANARY'
    async def scenario():
        db=runs_db()
        runner=CodexRunner(tmp_path,db=db,model='primary',fallback_models=['fallback'],executable=executable(tmp_path,error=error))
        await runner.start('work')
        dump='\n'.join(db.iterdump())
        assert 'SECRET_TEST_CANARY' not in dump
        db.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('models',['fallback',[None],[''],['x','x'],['primary'],['invalid space'],['x']*9])
def test_invalid_fallback_configuration_fails_closed(models):
    with pytest.raises(ValueError): validate_fallbacks('primary',models)


def test_firstmate_fallback_applies_to_answers_and_triage():
    async def scenario():
        calls=[]
        async def run(command,prompt):
            model=command[command.index('-m')+1];calls.append(model)
            if model=='primary':raise ModelUnavailableError('model unavailable')
            return 'answer'
        mate=FirstMate(runner=run,model='primary',fallback_models=['fallback'])
        assert await mate.answer('status?')=='answer'
        assert await mate.triage('facts')=='answer'
        assert calls==['primary','fallback','fallback']
        assert mate.active_answer is None and mate.model_fallback_receipts[0]['selected']=='fallback'
        async def fail(*args):raise FirstMateError('transport unavailable')
        mate=FirstMate(runner=fail,model='primary',fallback_models=['fallback'])
        with pytest.raises(FirstMateError):await mate.answer('status?')
        assert mate.model_fallback_receipts==[]
    asyncio.run(scenario())


def test_workflow_and_legacy_profiles_parse_explicit_fallbacks(tmp_path):
    from harbor.config.projects import ProjectConfig
    from harbor.config.workflow import load_workflow
    from test_workflow_config import config
    workflow=config(tmp_path,extra='[roles.controller]\nmodel="primary"\nfallback_models=["fallback"]\n[skills.blocker_assessment]\nfallback_models=["fallback"]\n')
    pipeline=tmp_path/'pipeline.md'
    pipeline.write_text('linear_project_slug: demo\nlinear_team: DEM\nworkflow_path: workflow.toml\ninstall_command: none\n')
    profile=ProjectConfig.from_pipeline('Demo',pipeline)
    assert profile.builder_fallback_models==profile.firstmate_fallback_models==('fallback',)
    assert load_workflow(workflow).config['roles']['controller']['fallback_models']==['fallback']
    pipeline.write_text('linear_project_slug: demo\nlinear_team: DEM\nbuilder_model: primary\nfirstmate_model: primary\nbuilder_fallback_models: ["fallback"]\nfirstmate_fallback_models: ["fallback"]\n')
    profile=ProjectConfig.from_pipeline('Demo',pipeline)
    assert profile.builder_fallback_models==profile.firstmate_fallback_models==('fallback',)


def test_deadline_and_operator_stop_do_not_trigger_fallback(tmp_path):
    from test_runner_phase4 import paused_codex
    async def scenario():
        runner=CodexRunner(tmp_path,model='primary',fallback_models=['fallback'],executable=str(paused_codex(tmp_path)),deadline_seconds=.1)
        result=await runner.start('wait')
        assert result.status=='failed' and runner.model=='primary'
        runner=CodexRunner(tmp_path,model='primary',fallback_models=['fallback'],executable=str(paused_codex(tmp_path)),deadline_seconds=10)
        task=asyncio.create_task(runner.start('wait'))
        for _ in range(100):
            if runner.active:break
            await asyncio.sleep(.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        assert runner.model=='primary' and runner.active is None
    asyncio.run(scenario())


def test_production_dispatcher_receives_configured_fallback(tmp_path,monkeypatch):
    from harbor.cli import build_production_dispatcher
    from harbor.config.projects import ProjectConfig
    from harbor.db import HarborDB
    from harbor.wake import Wake
    db=HarborDB(tmp_path/'state.db')
    cfg=ProjectConfig(name='Demo',linear_project_slug='demo',repo_path=tmp_path,builder_model='primary',builder_fallback_models=('fallback',))
    dispatcher,manager,factory=build_production_dispatcher(cfg,db)
    runner=factory(tmp_path,Wake(ticket={'identifier':'DEM-1','run_id':'run'},action='build'))
    assert runner.primary_model=='primary' and runner.fallback_models==('fallback',)
    db.close()


@pytest.mark.parametrize('text',[
    'builder_fallback_models: ',
    '- builder_fallback_models: ',
    '* firstmate_fallback_models: ',
    '| key | value |\n| --- | --- |\n| builder_fallback_models | |',
    'builder_fallback_models: ["gpt-5.6-luna"]',
    'firstmate_fallback_models: ["gpt-6-luna"]',
    'builder_fallback_models: "fallback"',
])
def test_public_pipeline_parser_rejects_invalid_or_duplicate_effective_fallback(text):
    from harbor.config.pipeline import PipelineConfig
    with pytest.raises(ValueError):PipelineConfig.from_markdown(text)


def test_firstmate_restart_uses_authorized_receipt(tmp_path):
    from types import SimpleNamespace
    from harbor.db import HarborDB
    async def scenario():
        db=HarborDB(tmp_path/'state.db')
        source=SimpleNamespace(db=db,project='Demo')
        calls=[]
        async def run(command,prompt):
            model=command[command.index('-m')+1];calls.append(model)
            if model=='primary':raise ModelUnavailableError('model unavailable')
            return 'answer'
        mate=FirstMate(source,runner=run,model='primary',fallback_models=['fallback'])
        await mate.triage('facts')
        restarted=FirstMate(source,runner=run,model='primary',fallback_models=['fallback'])
        await restarted.triage('facts')
        assert calls==['primary','fallback','fallback']
        changed=FirstMate(source,runner=run,model='primary',fallback_models=[])
        assert changed.active_model=='primary'
        db.close()
    asyncio.run(scenario())
