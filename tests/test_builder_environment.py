"""``harbor start`` settles the builder environment once; builders run plain ``harbor``.

Regression: every builder prompt carried a long per-ticket shell prefix
(``/usr/bin/env HARBOR_PROJECTS_FILE=... HARBOR_DB_PATH=... PYTHONPATH=...
<python> -P -m harbor.cli``) and told builders to substitute it for ``harbor``
and copy it into every child agent task. Now the registry, ledger, project,
GitHub token variable and a ``harbor`` shim first on PATH are resolved at
start and inherited by every builder process, and the prompt says to run
``harbor workflow check --ticket ID --event ...`` as written.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from harbor import cli
from harbor.config.projects import ProjectConfig
from harbor.db import HarborDB
from harbor.wake import Wake
from workflow_support import config

TOKEN = "sentinel-token-value-never-shown"


def _project(tmp_path: Path, *, provider: str = "codex", registry_name: str = "projects.md"):
    registry = tmp_path / "start" / registry_name
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text("# registry", encoding="utf-8")
    cfg = ProjectConfig(name="SampleProject", linear_project_slug="sample", repo_path=tmp_path,
                        workflow_path=str(config(tmp_path)), install_command="none",
                        registry_path=registry, builder_provider=provider)
    return cfg, registry


def _started(tmp_path: Path, monkeypatch, **kwargs):
    ledger = tmp_path / "start" / "ledger" / "harbor.db"
    monkeypatch.setenv("HARBOR_DB_PATH", str(ledger))
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    cfg, registry = _project(tmp_path, **kwargs)
    db = HarborDB(tmp_path / "state.db")
    dispatcher, _, factory = cli.build_production_dispatcher(cfg, db, worktree_manager=object())
    return cfg, registry, ledger, db, dispatcher, factory


def test_the_prompt_has_no_runtime_prefix_and_says_to_run_plain_harbor(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    cfg, registry = _project(tmp_path)
    prompt = cli._pipeline_text(cfg)
    assert "/usr/bin/env" not in prompt
    assert "HARBOR_PROJECTS_FILE=" not in prompt and "HARBOR_DB_PATH=" not in prompt and "PYTHONPATH=" not in prompt
    assert "Authoritative runtime invocation" not in prompt
    assert "Copy this entire runtime invocation" not in prompt
    assert str(registry) not in prompt
    assert "`harbor workflow check --ticket IDENTIFIER --event pre_handoff`" in prompt
    assert "Child agents inherit this environment" in prompt
    assert TOKEN not in prompt
    # The bundled delivery skill uses the same plain form.
    assert "harbor workflow check --ticket IDENTIFIER --event pre_handoff" in prompt
    assert "workflow check PROJECT" not in prompt


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_builders_get_the_environment_resolved_at_start(tmp_path: Path, monkeypatch, provider: str) -> None:
    token = TOKEN
    if provider == "claude":
        from test_github_transport import TOKEN as token, claude_production
        registry = tmp_path / "start" / "projects.md"
        registry.parent.mkdir()
        registry.write_text("# registry", encoding="utf-8")
        ledger = tmp_path / "start" / "ledger" / "harbor.db"
        monkeypatch.setenv("HARBOR_PROJECTS_FILE", str(registry))
        monkeypatch.setenv("HARBOR_DB_PATH", str(ledger))
        (_, _, factory), db = claude_production(tmp_path, monkeypatch)  # starts with GITHUB_TOKEN=token
    else:
        cfg, registry, ledger, db, dispatcher, factory = _started(tmp_path, monkeypatch, provider=provider)
    try:
        # Harbor's own shell environment changes after start; builders must not follow it.
        monkeypatch.setenv("HARBOR_PROJECTS_FILE", str(tmp_path / "other.md"))
        monkeypatch.setenv("HARBOR_DB_PATH", str(tmp_path / "other.db"))
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.setenv("GITHUB_TOKEN", "a-later-token")
        worktree = tmp_path / "wt"
        worktree.mkdir()
        runner = factory(worktree, Wake(ticket={"identifier": "SAMPLE-1"}, action="build"))
        env = runner.environment()
        assert env["HARBOR_PROJECTS_FILE"] == str(registry.resolve())
        assert env["HARBOR_DB_PATH"] == str(ledger.resolve())
        assert env["HARBOR_PROJECT"] == "SampleProject"
        assert env["GITHUB_TOKEN"] == token
        assert env["HARBOR_WORKTREE"] == str(worktree)
        first = Path(env["PATH"].split(os.pathsep)[0])
        shim = first / "harbor"
        assert shim.is_file() and os.access(shim, os.X_OK)
        assert first.is_relative_to(ledger.parent.resolve())
        text = shim.read_text(encoding="utf-8")
        assert token not in text and "a-later-token" not in text
        assert str(registry.resolve()) in text
    finally:
        db.close()


def test_the_shim_runs_this_harbor_from_any_cwd_and_shell(tmp_path: Path, monkeypatch) -> None:
    cfg, registry, ledger, db, dispatcher, factory = _started(tmp_path, monkeypatch)
    try:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        # A shadow ``harbor`` package in the builder's working directory must not load.
        (worktree / "harbor").mkdir()
        (worktree / "harbor" / "__init__.py").write_text("")
        (worktree / "harbor" / "cli.py").write_text("raise RuntimeError('shadow Harbor loaded')")
        env = factory(worktree, Wake(ticket={"identifier": "SAMPLE-1"}, action="build")).environment()
        env.pop("PYTHONPATH", None)
        for shell in ("sh", "bash", "zsh"):
            path = shutil.which(shell)
            if path is None:
                continue
            script = ("command -v harbor; harbor --help; "
                      "python3 -c 'import os; print(os.environ[\"HARBOR_PROJECTS_FILE\"])'")
            result = subprocess.run([path, "-c", script], cwd=worktree, env=env,
                                    capture_output=True, text=True, timeout=60)
            assert result.returncode == 0, (shell, result.stderr)
            assert result.stdout.splitlines()[0] == str(Path(env["PATH"].split(os.pathsep)[0]) / "harbor")
            assert "A calm project supervisor" in result.stdout
            assert "shadow Harbor loaded" not in result.stderr
            assert str(registry.resolve()) in result.stdout
    finally:
        db.close()


def test_a_tampered_shim_is_restored_before_the_next_builder(tmp_path: Path, monkeypatch) -> None:
    cfg, registry, ledger, db, dispatcher, factory = _started(tmp_path, monkeypatch)
    try:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        env = factory(worktree, Wake(ticket={"identifier": "SAMPLE-1"}, action="build")).environment()
        shim = Path(env["PATH"].split(os.pathsep)[0]) / "harbor"
        original = shim.read_text(encoding="utf-8")
        shim.write_text("#!/bin/sh\necho tampered\n", encoding="utf-8")
        factory(worktree, Wake(ticket={"identifier": "SAMPLE-2"}, action="build"))
        assert shim.read_text(encoding="utf-8") == original
    finally:
        db.close()


def test_start_fails_closed_without_a_registry(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("HARBOR_PROJECTS_FILE")
    cfg = ProjectConfig(name="SampleProject", linear_project_slug="sample", repo_path=tmp_path,
                        workflow_path=str(config(tmp_path)), install_command="none")
    db = HarborDB(tmp_path / "state.db")
    try:
        with pytest.raises(ValueError, match="authoritative project registry"):
            cli.build_production_dispatcher(cfg, db, worktree_manager=object())
    finally:
        db.close()


def test_the_builder_environment_repr_and_report_never_show_the_token(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    cfg, registry = _project(tmp_path)
    builder = cli._builder_environment(cfg, "GITHUB_TOKEN")
    assert TOKEN not in repr(builder)
    assert TOKEN not in "\n".join(builder.report())
    assert "GitHub token variable: GITHUB_TOKEN (value not shown)" in "\n".join(builder.report())
    assert TOKEN not in builder.shim_text


def test_workflow_commands_default_to_the_builders_project(tmp_path: Path, monkeypatch, capsys) -> None:
    from workflow_support import materialize

    repo = tmp_path / "repo"
    repo.mkdir()
    materialize(repo)
    pipeline = tmp_path / "pipeline.md"
    pipeline.write_text("# Pipeline\nlinear_project_slug: sample\nlinear_team: SAMPLE\n")
    projects = tmp_path / "projects.md"
    projects.write_text("| name | repo_path | pipeline_path |\n| --- | --- | --- |\n"
                        f"| SampleProject | {repo} | {pipeline} |\n")
    monkeypatch.setenv("HARBOR_PROJECTS_FILE", str(projects))
    monkeypatch.setenv("HARBOR_DB_PATH", str(tmp_path / "ledger" / "harbor.db"))
    monkeypatch.setenv("HARBOR_PROJECT", "SampleProject")
    assert cli.main(["workflow", "check", "--ticket", "SAMPLE-1", "--event", "pre_handoff"]) == 2
    # The empty ledger names the project it resolved from HARBOR_PROJECT.
    assert "harbor start SampleProject" in capsys.readouterr().err
    monkeypatch.delenv("HARBOR_PROJECT")
    assert cli.main(["workflow", "check", "--ticket", "SAMPLE-1", "--event", "pre_handoff"]) == 2
    assert "name the project" in capsys.readouterr().err


def test_doctor_reports_the_builder_environment(tmp_path: Path) -> None:
    from test_github_transport import run_doctor

    for provider in ("codex", "claude"):
        root = tmp_path / provider
        root.mkdir()
        result = run_doctor(root, provider, None)
        out = result.stdout
        assert (f"Builder environment: HARBOR_PROJECT=SampleProject · HARBOR_PROJECTS_FILE="
                f"{(root / 'projects.md').resolve()} · HARBOR_DB_PATH={(root / 'harbor.db').resolve()}") in out, out
        assert "GitHub token variable: GITHUB_TOKEN (value not shown)" in out, out
        shim_line = next(line for line in out.splitlines() if line.startswith("Builder harbor: "))
        assert "resolves in /bin/sh" in shim_line, out
        assert "dummy-token-value" not in out + result.stderr


def test_doctor_fails_closed_when_a_shell_resolves_another_harbor(tmp_path: Path, monkeypatch) -> None:
    from harbor.builder_env import check_resolution

    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    cfg, registry = _project(tmp_path)
    builder = cli._builder_environment(cfg)
    # A shell whose startup resets PATH hides the shim.
    shell = tmp_path / "resetting-shell"
    shell.write_text("#!/bin/sh\nPATH=/usr/bin:/bin; export PATH\nexec /bin/sh \"$@\"\n")
    shell.chmod(0o755)
    [(name, problem)] = check_resolution(builder, shells=(str(shell),))
    assert problem and "resolves harbor to nothing" in problem
    assert [problem for _, problem in check_resolution(builder, shells=("/bin/sh",))] == [None]
