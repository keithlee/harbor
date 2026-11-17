import pytest
from harbor.config.workflow import load_workflow


def config(tmp_path, preset="minimal", extra=""):
    path = tmp_path / "workflow.toml"
    path.write_text(f'version = 1\n[workflow]\npackage = "builtin:reference"\npreset = "{preset}"\n' + extra)
    return path


def test_presets_overrides_and_package_assets(tmp_path):
    minimal = load_workflow(config(tmp_path))
    assert not minimal.config["review"]["enabled"]
    assert "Managed execution" in minimal.prompt()
    reviewed = load_workflow(config(tmp_path, "reviewed"))
    assert reviewed.config["review"]["enabled"]
    auto = load_workflow(config(tmp_path, "autonomous", '[delivery]\nmerge_mode="human"\n'))
    assert auto.config["delivery"]["merge_mode"] == "human"
    assert auto.digest != reviewed.digest


@pytest.mark.parametrize("extra", ['[delivery]\nmerge_mode="enforced"', '[cleanup]\ndelay_seconds=-1', '[unknown]\na=1', '[skills.review]\nenabled=false'])
def test_fail_closed(tmp_path, extra):
    with pytest.raises(ValueError):
        load_workflow(config(tmp_path, "reviewed", extra))


@pytest.mark.parametrize('extra',[
    '[roles.reviewer]\nreuse="implementor"\n[roles.implementor]\nmodel="x"',
    '[skills.review]\nenabled="false"',
    '[skills.delivery]\nenabled=false',
    '[skills.delivery]\nreasoning_effort="magic"',
    '[recovery]\nmax_attempts=99',
    '[execution]\ndeadline_minutes=nan',
    '[checks.a]\ncommand=["true"]\nafter=["b"]\n[checks.b]\ncommand=["true"]\nafter=["a"]',
    '[checks.a]\ncommand="true"',
    '[checks.a]\ncommand=["true"]\nafter=["unknown"]',
    '[checks.a]\ncommand=["true"]\ntimeout_seconds=0',
    '[evidence]\nproof="skip"',
    '[states]\nready="Todo"',
    '[states]\nready=["Todo"]\npaused=["todo"]',
    '[hooks.bad]\nevent="pre_merge"\nmode="required"\nexecutor="skill"\nskill="missing"',
    '[hooks.bad]\nevent="pre_merge"\nmode="required"\nexecutor="script"\ncommand="true"',
    '[hooks.bad]\nevent="unknown"\nmode="required"',
])
def test_hostile_config_never_silently_selects_defaults(tmp_path,extra):
    with pytest.raises(ValueError):
        load_workflow(config(tmp_path,extra=extra))


def test_changed_package_and_escaping_assets_are_rejected(tmp_path):
    import shutil
    from harbor.hooks import execute_event
    import asyncio
    original=load_workflow(config(tmp_path))
    local=tmp_path/'local'
    shutil.copytree(original.package,local)
    path=config(tmp_path)
    path.write_text(path.read_text().replace('builtin:reference','local'))
    workflow=load_workflow(path)
    with pytest.raises(ValueError):workflow.asset('../workflow.toml')
    (local/'scripts/readiness.py').write_text('raise SystemExit(0)')
    receipts=asyncio.run(execute_event(workflow,'pre_merge',{'project':'A','ticket':'A-1','run_id':'r'},cwd=tmp_path))
    assert all(r['blocking'] for r in receipts)
    (local/'escape').symlink_to(tmp_path/'workflow.toml')
    with pytest.raises(ValueError,match='symlink'):load_workflow(path)


def test_workflow_profile_never_inherits_legacy_provider_models(tmp_path):
    from harbor.config.projects import ProjectConfig
    config(tmp_path)
    pipeline=tmp_path/'pipeline.md'
    pipeline.write_text('linear_project_slug: demo\nlinear_team: DEM\nworkflow_path: workflow.toml\ninstall_command: none\n')
    with pytest.raises(ValueError,match='explicit.*model'):
        ProjectConfig.from_pipeline('Demo',pipeline)
    pipeline.write_text(pipeline.read_text()+'builder_model: configured-model\n')
    project=ProjectConfig.from_pipeline('Demo',pipeline)
    assert project.builder_model == project.firstmate_model == 'configured-model'


@pytest.mark.parametrize("setting", ["builder_provider", "firstmate_provider"])
def test_blank_provider_fails_closed(tmp_path, setting):
    from harbor.config.projects import ProjectConfig

    pipeline = tmp_path / "pipeline.md"
    pipeline.write_text(
        "linear_project_slug: demo\nlinear_team: DEM\n"
        "builder_model: configured-model\ninstall_command: none\n"
        f"{setting}: \n"
    )
    with pytest.raises(ValueError, match=f"invalid {setting}"):
        ProjectConfig.from_pipeline("Demo", pipeline)


def test_retained_workflow_rejects_on_disk_policy_edit(tmp_path):
    import sqlite3
    from harbor.workflow_store import bind
    path = config(tmp_path)
    workflow = load_workflow(path)
    db = sqlite3.connect(":memory:")
    bind(db, "Demo", "DEM-1", "run", workflow)
    path.write_text(path.read_text() + '\n[cleanup]\nmode="off"\n')
    with pytest.raises(ValueError, match="configuration changed"):
        bind(db, "Demo", "DEM-1", "run", workflow)
    with pytest.raises(ValueError, match="configuration changed"):
        workflow.prompt()
