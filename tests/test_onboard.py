import asyncio
import pytest
from harbor.cli import build_parser, _async_main
from harbor.config.projects import get_project
from test_worktree_phase4 import disposable_repo


@pytest.mark.parametrize("required", [[], ["--required-check", "tests"]])
def test_new_user_needs_no_personal_skills(tmp_path,monkeypatch,capsys,required):
    repo,_=disposable_repo(tmp_path)
    registry=tmp_path/'projects.md'
    monkeypatch.setenv('HARBOR_DB_PATH',str(tmp_path/'state.db'))
    args=build_parser().parse_args(['init','Demo','--repo',str(repo),'--registry',str(registry),'--fallback-model','fallback-model','--linear-project','demo123','--linear-team','DEM','--github-repository','example/demo','--model','test-model','--install-command','none','--test-command','["python3","-c","print(123)"]'] + required)
    assert asyncio.run(_async_main(args)) == 0
    monkeypatch.setenv('HARBOR_PROJECTS_FILE',str(registry))
    project=get_project('Demo')
    assert project.hold and project.builder_model == 'test-model'
    assert project.install_command == 'none'
    assert project.builder_fallback_models == project.firstmate_fallback_models == ('fallback-model',)
    assert project.required_checks == (('tests',) if required else ())
    from harbor.facts.ci import classify_ci
    assert classify_ci([{'name':'tests','status':'COMPLETED','conclusion':'SUCCESS'}], project.required_checks)['state'] == ('green' if required else 'absent')
    assert asyncio.run(_async_main(build_parser().parse_args(['workflow','inspect','Demo']))) == 0
    output = capsys.readouterr().out
    package = repo/'.harbor'/'workflow'
    assert str(package) in output
    assert (package/'workflow.toml').is_file() and (package/'skills/delivery/SKILL.md').is_file()
    models = (package/'config.toml').read_text()
    assert 'model = "test-model"' in models and 'fallback_models = ["fallback-model"]' in models
    assert '[checks.tests]' in (package/'workflow.toml').read_text()
    pipeline = (repo/'.harbor'/'pipeline.md').read_text()
    assert 'workflow_path' not in pipeline and 'builder_model' not in pipeline
    assert ('Required CI names are missing' in output) is (not bool(required))
    assert asyncio.run(_async_main(args)) == 2


def test_failed_init_removes_only_what_it_created(tmp_path,monkeypatch):
    from harbor.config import projects
    repo,_=disposable_repo(tmp_path)
    def broken(*args,**kwargs):
        raise ValueError("validation failed")
    monkeypatch.setattr(projects.ProjectConfig,'from_pipeline',classmethod(lambda cls,*a,**k: broken()))
    args=build_parser().parse_args(['init','Demo','--repo',str(repo),'--registry',str(tmp_path/'projects.md'),'--linear-project','demo123','--linear-team','DEM','--github-repository','example/demo','--model','test-model','--install-command','none'])
    assert asyncio.run(_async_main(args)) == 2
    assert not (repo/'.harbor'/'workflow').exists() and not (repo/'.harbor'/'pipeline.md').exists()
    assert not (tmp_path/'projects.md').exists()
