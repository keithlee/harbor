import asyncio
from pathlib import Path
from types import SimpleNamespace
import pytest
from harbor.cli import build_production_dispatcher,build_parser
from harbor.config.projects import ProjectConfig
from harbor.db import HarborDB
from harbor.runner.base import apply_phase4_migration
from harbor.wake import apply_phase3_migration,Wake
from harbor.worktree import WorktreeManager
from harbor.workflow_cli import execute
from harbor.workflow_lifecycle import handoff
from test_workflow_config import config
from test_worktree_phase4 import disposable_repo,git
from test_runner_phase4 import fake_codex,slow_codex


def test_native_adapter_dispatch_checks_handoff_and_policy_edits(tmp_path,monkeypatch,capsys):
    async def scenario():
        repo,_=disposable_repo(tmp_path)
        db=HarborDB(tmp_path/'state.db')
        apply_phase3_migration(db);apply_phase4_migration(db)
        db.replace_tickets('Demo',[{'identifier':'DEM-1','owned':True,'hold':False,'state':'In Progress'}])
        workflow_path=config(tmp_path,extra='[checks.tests]\ncommand=["python3","-c","pass"]\n')
        registry=tmp_path/'projects.md'
        registry.write_text('# registry')  # launches render the prompt, which requires the authoritative registry
        cfg=ProjectConfig(name='Demo',linear_project_slug='demo',repo_path=repo,workflow_path=str(workflow_path),install_command='none',worktree_root=str(tmp_path/'wt'),registry_path=registry)
        executable=fake_codex(tmp_path)
        content=executable.read_text().replace("if 'resume' not in args:","print(json.dumps({'item':{'type':'collab_agent_tool_call','status':'completed','tool':'spawn_agent','receiver_thread_ids':['reviewer']}}),flush=True)\nif 'resume' not in args:")
        executable.write_text(content)
        monkeypatch.setenv('HARBOR_CODEX_EXECUTABLE',str(executable))
        manager=WorktreeManager(repo,root=tmp_path/'wt',repo_name='Demo',disk_usage=lambda _:30,install_command='none',custody=db.connection)
        dispatcher,_,_=build_production_dispatcher(cfg,db,worktree_manager=manager)
        result=await dispatcher.dispatch(Wake(ticket={'identifier':'DEM-1'},action='build',instruction='Implement mock ticket'))
        run=dict(db.connection.execute('SELECT * FROM runs').fetchone())
        assert result.session_id=='session-123'
        async def command(*parts):
            return await execute(build_parser().parse_args(['workflow',*parts,'Demo','--ticket','DEM-1']),cfg,db)
        assert await command('checks','--parallel')==0
        head=git(repo,'rev-parse','HEAD')
        context={'project':'Demo','ticket':'DEM-1','run_id':run['id'],'event':'pre_handoff','fresh':True,'hold':False,'head_sha':head,'facts':{'url':'https://example.test/pr/1','head_sha':head,'ci':'green','mergeable':'MERGEABLE'}}
        async def fresh(*args):return context
        monkeypatch.setattr('harbor.workflow_cli.fresh_context',fresh)
        assert await command('check','--event','pre_handoff')==0
        assert await command('check','--event','pre_merge')==1
        supervisor=SimpleNamespace(dispatcher=dispatcher,db=db,project='Demo',poller=SimpleNamespace(config=cfg),_ticket_key=lambda _: 'DEM-1',_latest_run=lambda _:run,_run_id_for_result=lambda *args:run['id'])
        assert await handoff(supervisor,SimpleNamespace(ticket={}),{'pr_url':'https://example.test/pr/1'})==context
        context['hold']=True
        with pytest.raises(ValueError,match='held'):
            await handoff(supervisor,SimpleNamespace(ticket={}),{})
        # Editing the project's workflow mid-run never blocks it: the next
        # command adopts the edit and records a policy_change receipt.
        context['hold']=False
        workflow_path.write_text(workflow_path.read_text().replace('mode = "after_merge"','mode = "off"'))
        assert await command('check','--event','pre_handoff')==0
        assert db.connection.execute("SELECT COUNT(*) FROM workflow_receipts WHERE kind='policy_change'").fetchone()[0]==1
        db.close()
    asyncio.run(scenario())


def test_cancel_native_subprocess_persists_operator_stop(tmp_path):
    from harbor.runner.codex import CodexRunner
    from harbor.runner.base import process_alive
    async def scenario():
        db=HarborDB(':memory:');apply_phase3_migration(db);apply_phase4_migration(db)
        db.replace_tickets('Demo',[{'identifier':'DEM-2','owned':True,'state':'In Progress'}])
        runner=CodexRunner(tmp_path,db=db,ticket='DEM-2',executable=str(slow_codex(tmp_path)),run_id='retained')
        task=asyncio.create_task(runner.start('continue'))
        for _ in range(100):
            if runner.active:break
            await asyncio.sleep(.001)
        pid=runner.active.pid
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        row=db.connection.execute("SELECT status,terminal_reason FROM runs WHERE id='retained'").fetchone()
        assert tuple(row)==('terminated','operator exit')
        assert not process_alive(pid) and runner.active is None
        db.close()
    asyncio.run(scenario())


def test_cached_runner_resume_adopts_changed_policy(tmp_path):
    from harbor.supervisor import WorktreeDispatcher
    from harbor.workflow_store import bind
    db=HarborDB(tmp_path/'state.db')
    apply_phase3_migration(db);apply_phase4_migration(db)
    path=config(tmp_path)
    from harbor.config.workflow import load_workflow
    workflow=load_workflow(path)
    bind(db.connection,'Demo','DEM-1','retained',workflow)
    calls=[]
    class Runner:
        default_run_id='retained'
        async def resume(self,*args,**kwargs):
            calls.append(args)
            return {'status':'completed','outcome':'no_pr','summary':'ok'}
    runner=Runner()
    dispatcher=WorktreeDispatcher(SimpleNamespace(),lambda *a:runner,db=db,project='Demo')
    dispatcher.workflow=workflow
    dispatcher.runners['DEM-1']=runner
    path.write_text(path.read_text().replace('mode = "after_merge"','mode = "off"'))
    try:
        asyncio.run(dispatcher.resume(Wake(ticket={'identifier':'DEM-1'},action='resume',instruction='continue')))
    except Exception:
        pass  # the fake runner is minimal; only the policy adoption matters here
    assert dispatcher.workflow.config['cleanup']['mode']=='off'
    assert db.connection.execute("SELECT COUNT(*) FROM workflow_receipts WHERE kind='policy_change'").fetchone()[0]==1
    db.close()


def test_production_start_supplies_worktree_custody(tmp_path,monkeypatch):
    from harbor import cli
    registry=tmp_path/'projects.md'
    registry.write_text('# registry')
    cfg=ProjectConfig(name='Demo',linear_project_slug='demo',repo_path=tmp_path,install_command='none',worktree_root=str(tmp_path/'wt'),workflow_path=str(config(tmp_path)),registry_path=registry)
    monkeypatch.setattr(cli,'get_project',lambda _:cfg)
    monkeypatch.setattr(cli,'_db_path',lambda:tmp_path/'state.db')
    class Captured(Exception):pass
    def supervisor(*args,**kwargs):
        assert kwargs['worktree_manager'].custody is kwargs['db'].connection
        kwargs['db'].close()
        raise Captured()
    monkeypatch.setattr(cli,'Supervisor',supervisor)
    with pytest.raises(Captured):
        asyncio.run(cli._start('Demo',linear_client=object()))


def test_delivery_prompt_pins_registry_ledger_and_runtime(tmp_path, monkeypatch):
    from harbor.cli import _pipeline_text
    import shlex
    registry = tmp_path / 'registry with spaces.md'
    registry.write_text('# registry')
    ledger = tmp_path / 'state' / 'ledger.db'
    monkeypatch.setenv('HARBOR_PROJECTS_FILE', str(registry))
    monkeypatch.setenv('HARBOR_DB_PATH', str(ledger))
    cfg = ProjectConfig(name='Demo Project', linear_project_slug='demo', workflow_path=str(config(tmp_path)))
    prompt = _pipeline_text(cfg)
    line = next(line for line in prompt.splitlines() if line.startswith('Authoritative runtime invocation: '))
    command = shlex.split(line.split(': ', 1)[1])
    assert command[0] == '/usr/bin/env'
    assert f'HARBOR_PROJECTS_FILE={registry.resolve()}' in command
    assert f'HARBOR_DB_PATH={ledger.resolve()}' in command
    assert command[-3:] == ['-P', '-m', 'harbor.cli']
    shadow = tmp_path / 'harbor'
    shadow.mkdir()
    (shadow / '__init__.py').write_text('')
    (shadow / 'cli.py').write_text("raise RuntimeError('shadow Harbor loaded')")
    import subprocess
    result = subprocess.run([*command, '--help'], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0
    assert 'shadow Harbor loaded' not in result.stderr
    assert 'A calm project supervisor' in result.stdout
    assert "Replace PROJECT with 'Demo Project'" in prompt
    assert "Do not shell out to codex exec for children" in prompt
    assert "overrides any merge or auto-merge policy" in prompt
    assert "Harbor setting wins" in prompt
    assert "roles" in prompt
    monkeypatch.delenv('HARBOR_PROJECTS_FILE')
    with pytest.raises(ValueError, match='authoritative project registry'):
        _pipeline_text(cfg)


@pytest.mark.parametrize("shell_name", ["sh", "bash", "zsh"])
def test_runtime_prefers_resolved_registry_and_survives_login_shell(tmp_path, monkeypatch, shell_name):
    import shlex, shutil, subprocess, sys
    shell = shutil.which(shell_name)
    if shell is None:
        pytest.skip(f"optional login shell {shell_name} is unavailable")
    from harbor import cli
    registry = tmp_path / 'actual registry.md'
    registry.write_text('# selected registry')
    monkeypatch.setenv('HARBOR_PROJECTS_FILE', str(tmp_path / 'wrong.md'))
    cfg = ProjectConfig(name='Demo', linear_project_slug='demo', registry_path=registry, workflow_path=str(config(tmp_path)))
    prompt = cli._pipeline_text(cfg)
    invocation = next(line.split(': ', 1)[1] for line in prompt.splitlines() if line.startswith('Authoritative runtime invocation: '))
    command = shlex.split(invocation)
    assert f'HARBOR_PROJECTS_FILE={registry.resolve()}' in command
    assert f'PYTHONPATH={Path(cli.__file__).resolve().parent.parent}' in command
    assert command[-4] == sys.executable
    result = subprocess.run([shell, '-lc', invocation + ' --help'], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0 and 'A calm project supervisor' in result.stdout


def test_factory_grants_operational_ledger_root(tmp_path, monkeypatch):
    ledger = tmp_path / 'custody' / 'state.db'
    ledger.parent.mkdir()
    monkeypatch.setenv('HARBOR_DB_PATH', str(ledger))
    db = HarborDB(ledger)
    apply_phase3_migration(db); apply_phase4_migration(db)
    cfg = ProjectConfig(name='Demo', linear_project_slug='demo', repo_path=tmp_path, workflow_path=str(config(tmp_path)), install_command='none')
    _, _, factory = build_production_dispatcher(cfg, db, worktree_manager=object())
    wake = SimpleNamespace(ticket={'identifier':'DEM-1'}, run_id='test')
    workflow_runner = factory(tmp_path, wake)
    assert str(ledger.parent.resolve()) in workflow_runner.writable_roots
    db.close()
