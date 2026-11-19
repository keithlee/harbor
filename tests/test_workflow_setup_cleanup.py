import sqlite3
from pathlib import Path
import pytest
from harbor.worktree import WorktreeManager, WorktreeError, WorktreeInstallError
from test_worktree_phase4 import disposable_repo, git


def test_install_retry_retains_edits_and_rejects_unknown_custody(tmp_path):
    repo,_ = disposable_repo(tmp_path)
    ledger = sqlite3.connect(tmp_path/'custody.db')
    def broken(path,command):
        (path/'partial.txt').write_text('keep me')
        raise WorktreeInstallError(command,1,'failed')
    args = dict(root=tmp_path/'wt',disk_usage=lambda _:30,install_command='test-install',custody=ledger)
    with pytest.raises(WorktreeInstallError):
        WorktreeManager(repo,installer=broken,**args).create('DEMO-1')
    manager = WorktreeManager(repo,installer=lambda p,c:None,**args)
    wt = manager.create('DEMO-1')
    assert (wt.path/'partial.txt').read_text() == 'keep me'
    with pytest.raises(WorktreeError):
        manager.create('DEMO-1')
    with pytest.raises(WorktreeError):
        manager.remove(wt)
    (wt.path/'partial.txt').unlink()
    manager.remove(wt)
    assert git(repo,'show-ref','--verify','refs/heads/harbor/DEMO-1')


def test_setup_wrong_branch_and_symlink_are_preserved(tmp_path):
    repo,_ = disposable_repo(tmp_path)
    ledger=sqlite3.connect(':memory:')
    def broken(p,c):
        raise WorktreeInstallError(c,1,'failed')
    manager=WorktreeManager(repo,root=tmp_path/'wt',disk_usage=lambda _:30,install_command='test-install',custody=ledger,installer=broken)
    with pytest.raises(WorktreeInstallError):
        manager.create('DEMO-2')
    path=manager.path_for('DEMO-2')
    git(path,'checkout','-b','foreign')
    with pytest.raises(WorktreeError,match='branch or HEAD'):
        manager.create('DEMO-2')
    (tmp_path/'wt'/'DEMO-3').symlink_to(path)
    with pytest.raises(WorktreeError,match='symlink'):
        manager.create('DEMO-3')


def test_ignored_output_is_removed_but_untracked_and_modified_work_is_kept(tmp_path):
    repo,_=disposable_repo(tmp_path)
    manager=WorktreeManager(repo,root=tmp_path/'wt',disk_usage=lambda _:30,install_command='none')
    wt=manager.create('DEMO-4')
    exclude=Path(git(repo,'rev-parse','--git-common-dir'))/'info'/'exclude'
    if not exclude.is_absolute():
        exclude=repo/exclude
    with exclude.open('a') as f:
        f.write('\ncache\nnode_modules/\n')
    (wt.path/'cache').write_text('build output')
    (wt.path/'node_modules'/'pkg').mkdir(parents=True)
    (wt.path/'node_modules'/'pkg'/'index.js').write_text('x')
    (wt.path/'notes.txt').write_text('untracked work')
    with pytest.raises(WorktreeError,match='untracked'):
        manager.remove(wt)
    assert (wt.path/'notes.txt').exists() and (wt.path/'cache').exists()
    (wt.path/'notes.txt').unlink()
    tracked=next(p for p in wt.path.iterdir() if p.is_file() and p.name not in {'cache','.git'})
    original=tracked.read_text()
    tracked.write_text(original+'\nmodified')
    with pytest.raises(WorktreeError,match='modified'):
        manager.remove(wt)
    tracked.write_text(original)
    manager.remove(wt)  # only ignored output remains: removed
    assert not wt.path.exists()
    assert 'harbor/DEMO-4' in git(repo,'branch','--list','harbor/DEMO-4')  # branch kept for audit


def test_setup_hook_failure_reuses_prepared_worktree(tmp_path):
    import asyncio
    from harbor.db import HarborDB
    from harbor.config.workflow import load_workflow
    from harbor.supervisor import WorktreeDispatcher
    from harbor.wake import Wake
    from test_workflow_config import config
    repo,_=disposable_repo(tmp_path)
    db=HarborDB(':memory:')
    manager=WorktreeManager(repo,root=tmp_path/'wt',repo_name='Demo',disk_usage=lambda _:30,install_command='none',custody=db.connection)
    workflow=load_workflow(config(tmp_path,extra='[hooks.ready]\nevent="setup"\nmode="required"\nexecutor="script"\nprotocol="exit_code"\ncommand=["python3","-c","from pathlib import Path; assert Path(\'prerequisite\').exists()"]\n'))
    calls=[]
    class Runner:
        async def start(self,prompt):calls.append(prompt);return {'status':'waiting'}
    dispatcher=WorktreeDispatcher(manager,lambda *a:Runner(),db=db,project='Demo')
    dispatcher.workflow=workflow
    wake=Wake(ticket={'identifier':'DEM-3'},action='build',instruction='continue')
    with pytest.raises(ValueError,match='setup hook failed'):
        asyncio.run(dispatcher.dispatch(wake))
    retained=manager.path_for('DEM-3')
    (retained/'prerequisite').write_text('fixed')
    asyncio.run(dispatcher.dispatch(wake))
    assert calls and (retained/'prerequisite').read_text()=='fixed'
    db.close()
