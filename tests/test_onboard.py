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
    args=build_parser().parse_args(['init','Demo','--repo',str(repo),'--registry',str(registry),'--fallback-model','fallback-model','--preset','reviewed','--linear-project','demo123','--linear-team','DEM','--github-repository','example/demo','--model','test-model','--install-command','none','--test-command','["python3","-c","print(123)"]'] + required)
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
    assert 'builtin:reference' in output
    assert ('Required CI names are missing' in output) is (not bool(required))
    assert asyncio.run(_async_main(args)) == 2
