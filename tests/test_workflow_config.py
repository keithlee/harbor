import sqlite3

import pytest

from harbor.config.workflow import load_workflow, project_workflow_path
from workflow_support import config  # noqa: F401  (re-exported for other tests)


def test_bundled_policy_is_the_single_workflow(tmp_path):
    workflow = load_workflow(config(tmp_path, "reviewed"))
    policy = workflow.config
    assert policy["delivery"] == {"merge_mode": "human", "auto_merge_risks": []}
    assert policy["recovery"]["enabled"] is True
    assert policy["decisions"]["mode"] == "cite_only"
    assert policy["evidence"]["ci"] == "required"
    assert set(policy["skills"]) == {"delivery", "planning", "review", "risk", "blocker_assessment"}
    assert "review" not in policy and {"roles", "first_mate"} <= set(policy)
    assert "roles" not in tomllib_keys(workflow)  # models live in config.toml
    assert "Managed execution" in workflow.prompt()
    assert str(workflow.models_path) in workflow.prompt()


def tomllib_keys(workflow):
    import tomllib
    return set(tomllib.loads((workflow.package / "workflow.toml").read_text()))


def test_child_roles_and_first_mate_inherit_the_controller(tmp_path):
    workflow = load_workflow(config(tmp_path, extra='[roles.reviewer]\nmodel="review-model"\n'))
    roles = workflow.config["roles"]
    assert roles["planner"]["model"] == roles["implementor"]["model"] == "test-model"
    assert roles["reviewer"]["model"] == "review-model"
    assert workflow.config["first_mate"]["model"] == "test-model"
    assert workflow.config["first_mate"]["max_recoveries"] == 3


def test_project_workflow_path_is_fixed(tmp_path):
    assert project_workflow_path(tmp_path) == tmp_path / ".harbor" / "workflow" / "workflow.toml"


@pytest.mark.parametrize("text", [
    'version = 1\n[workflow]\npackage = "builtin:work-pipeline"\npreset = "reviewed"\n',
    'version = 1\n[presets.reviewed.review]\nenabled = true\n',
    'version = 1\n[defaults.delivery]\nmerge_mode = "human"\n',
])
def test_old_package_and_preset_files_fail_with_migration_hint(tmp_path, text):
    path = config(tmp_path)
    path.write_text(text)
    with pytest.raises(ValueError, match="migrating"):
        load_workflow(path)


def test_missing_workflow_or_controller_model_fails_closed(tmp_path):
    with pytest.raises(ValueError, match="missing workflow"):
        load_workflow(tmp_path / ".harbor" / "workflow" / "workflow.toml")
    path = config(tmp_path)
    (path.parent / "config.toml").write_text("[planner]\nmodel = \"x\"\n")
    with pytest.raises(ValueError, match="controller.*model is required"):
        load_workflow(path)
    (path.parent / "config.toml").unlink()
    with pytest.raises(ValueError, match="missing model config"):
        load_workflow(path)


@pytest.mark.parametrize('extra',[
    '[delivery]\nmerge_mode="enforced"',
    '[cleanup]\ndelay_seconds=-1',
    '[unknown]\na=1',
    '[skills.delivery]\nenabled=false',
    '[skills.review]\nenabled="false"',
    '[recovery]\nmax_attempts=99',
    '[execution]\ndeadline_minutes=nan',
    '[checks.a]\ncommand=["true"]\nafter=["b"]\n[checks.b]\ncommand=["true"]\nafter=["a"]',
    '[checks.a]\ncommand="true"',
    '[checks.a]\ncommand=["true"]\nafter=["unknown"]',
    '[checks.a]\ncommand=["true"]\ntimeout_seconds=0',
    '[evidence]\nci="skip"',
    '[states]\nready="Todo"',
    '[states]\nready=["Todo"]\npaused=["todo"]',
    '[hooks.bad]\nevent="pre_merge"\nmode="required"\nexecutor="skill"\nskill="missing"',
    '[hooks.bad]\nevent="pre_merge"\nmode="required"\nexecutor="script"\ncommand="true"',
    '[hooks.bad]\nevent="unknown"\nmode="required"',
    '[roles.controller]\nreasoning_effort="magic"',
    '[roles.unknown]\nmodel="x"',
    '[first_mate]\nmax_recoveries=-1',
])
def test_hostile_config_never_silently_selects_defaults(tmp_path, extra):
    with pytest.raises(ValueError):
        load_workflow(config(tmp_path, extra=extra))


def test_escaping_assets_and_symlinks_are_rejected(tmp_path):
    path = config(tmp_path)
    workflow = load_workflow(path)
    with pytest.raises(ValueError):
        workflow.asset('../pipeline.md')
    (path.parent / 'escape').symlink_to(path)
    with pytest.raises(ValueError, match='symlink'):
        load_workflow(path)


def test_edits_are_adopted_next_turn_instead_of_blocking(tmp_path):
    from harbor.workflow_store import bind
    path = config(tmp_path)
    workflow = load_workflow(path)
    db = sqlite3.connect(":memory:")
    bind(db, "Demo", "DEM-1", "run", workflow)
    assert not workflow.changed() and workflow.reload() is workflow
    (path.parent / "config.toml").write_text('[controller]\nmodel = "terra"\n')
    assert workflow.changed()
    current = workflow.reload()
    assert current.config["roles"]["controller"]["model"] == "terra"
    workflow.prompt()  # a stale snapshot never raises mid-run
    bind(db, "Demo", "DEM-1", "run", current)
    assert db.execute("SELECT policy_digest FROM workflow_runs").fetchone()[0] == current.digest
    kinds = [row[0] for row in db.execute("SELECT kind FROM workflow_receipts")]
    assert kinds == ["policy_change"]


def test_pipeline_md_derives_workflow_and_models(tmp_path):
    from harbor.config.projects import ProjectConfig
    config(tmp_path, extra='[first_mate]\nmodel="luna"\n')
    pipeline = tmp_path / ".harbor" / "pipeline.md"
    pipeline.write_text('linear_project_slug: demo\nlinear_team: DEM\ninstall_command: none\n')
    project = ProjectConfig.from_pipeline('Demo', pipeline)
    assert project.workflow_path == str(tmp_path / ".harbor" / "workflow" / "workflow.toml")
    assert project.builder_model == 'test-model' and project.builder_effort == 'medium'
    assert project.firstmate_model == 'luna'


@pytest.mark.parametrize("line", ["workflow_path: workflow.toml", "builder_model: x", "firstmate_model: x", "skill: /work-pipeline"])
def test_pipeline_md_rejects_retired_keys(tmp_path, line):
    from harbor.config.projects import ProjectConfig
    config(tmp_path)
    pipeline = tmp_path / ".harbor" / "pipeline.md"
    pipeline.write_text(f'linear_project_slug: demo\nlinear_team: DEM\ninstall_command: none\n{line}\n')
    with pytest.raises(ValueError, match="no longer belongs? in pipeline.md"):
        ProjectConfig.from_pipeline('Demo', pipeline)


@pytest.mark.parametrize("setting", ["builder_provider", "firstmate_provider"])
def test_blank_provider_fails_closed(tmp_path, setting):
    from harbor.config.projects import ProjectConfig
    config(tmp_path)
    pipeline = tmp_path / ".harbor" / "pipeline.md"
    pipeline.write_text(f"linear_project_slug: demo\nlinear_team: DEM\ninstall_command: none\n{setting}: \n")
    with pytest.raises(ValueError, match=f"invalid {setting}"):
        ProjectConfig.from_pipeline("Demo", pipeline)
