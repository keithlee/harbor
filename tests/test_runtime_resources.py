from __future__ import annotations

import sqlite3
import subprocess
import textwrap
import tomllib
import zipfile
from pathlib import Path

import pytest

import harbor.db as db_module
import harbor.pr_events as pr_events_module
import harbor.runner.base as runner_base_module
import harbor.runner.codex as codex_module
import harbor.unblock as unblock_module
import harbor.wake as wake_module
from harbor.db import SCHEMA

from harbor.resources import runtime_asset


ROOT = Path(__file__).resolve().parents[1]


def test_every_runtime_asset_resolves_from_a_source_checkout() -> None:
    expected = [
        *(runtime_asset("migrations", path.name) for path in sorted((ROOT / "migrations").glob("*.sql"))),
        runtime_asset("schemas", "run-result.json"),
    ]

    assert expected
    assert all(path.is_file() for path in expected)


def test_wheel_build_maps_runtime_assets_into_the_harbor_package() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    force_include = config["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]

    assert force_include == {
        "migrations": "harbor/migrations",
        "schemas": "harbor/schemas",
        "workflows": "harbor/workflows",
    }


def test_explicit_asset_paths_bypass_every_default_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> Path:
        raise AssertionError("default runtime asset resolver was called")

    for module in (
        db_module,
        pr_events_module,
        runner_base_module,
        codex_module,
        unblock_module,
        wake_module,
    ):
        monkeypatch.setattr(module, "runtime_asset", forbidden)

    connection = sqlite3.connect(":memory:")
    connection.executescript(SCHEMA)
    db_module.apply_phase7_migration(connection, ROOT / "migrations/006_phase7_unblock.sql")
    wake_module.apply_phase3_migration(connection, ROOT / "migrations/003_phase3_wake.sql")
    runner_base_module.apply_phase4_migration(connection, ROOT / "migrations/004_phase4_runner.sql")
    wake_module.apply_phase6_migration(connection, ROOT / "migrations/005_phase6_reengagement.sql")
    pr_events_module.apply_phase8_migration(connection, ROOT / "migrations/007_phase8_pr_feedback.sql")
    db_module.apply_phase9_migration(connection, ROOT / "migrations/008_phase9_correctness.sql")
    connection.close()

    schema = ROOT / "schemas/run-result.json"
    runner = codex_module.CodexRunner(tmp_path, schema_path=schema)
    command = unblock_module.build_resume_command("run-1", "continue", output_schema=str(schema))
    assert runner.schema_path == schema
    assert command.command[-1] == str(schema)


def test_built_wheel_installs_with_assets_and_runs_an_offline_shadow_cycle(
    tmp_path: Path,
) -> None:
    wheel_dir = tmp_path / "dist"
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(wheel_dir)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(wheel_dir.glob("harbor-*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    assert {f"harbor/migrations/{path.name}" for path in (ROOT / "migrations").glob("*.sql")} <= names
    assert "harbor/schemas/run-result.json" in names
    assert "harbor/workflows/reference/skills/delivery/SKILL.md" in names
    assert "harbor/workflows/work-pipeline/scripts/readiness.py" in names
    assert not any("projects.md" in name or ".harbor/" in name for name in names)

    venv = tmp_path / "venv"
    subprocess.run(["uv", "venv", str(venv)], check=True, capture_output=True, text=True)
    python = venv / "bin/python"
    subprocess.run(
        ["uv", "pip", "install", "--python", str(python), str(wheel)],
        check=True,
        capture_output=True,
        text=True,
    )
    script = textwrap.dedent(
        """
        import asyncio
        import os
        import tempfile
        from pathlib import Path

        from harbor.cli import _start
        from harbor.db import HarborDB

        class Linear:
            async def fetch_issues(self, *_args):
                return [{
                    "id": "linear-1",
                    "identifier": "SAMPLE-1",
                    "title": "Offline wheel proof",
                    "url": "https://example.invalid/SAMPLE-1",
                    "updatedAt": "2026-09-04T00:00:00Z",
                    "state": {"name": "Todo", "type": "unstarted"},
                    "attachments": {"nodes": []},
                    "comments": {"nodes": []},
                    "inverseRelations": {"nodes": []},
                }]
            async def close(self):
                return None

        class Delivery:
            async def dispatch(self, *_args):
                raise AssertionError("shadow mode dispatched")
            async def resume(self, *_args):
                raise AssertionError("shadow mode resumed")

        root = Path(tempfile.mkdtemp())
        repo = root / "repo"
        repo.mkdir()
        pipeline = root / "pipeline.md"
        pipeline.write_text(
            "linear_project_slug: sample-a1\\n"
            "linear_team: SAMPLE\\n"
            "poll_states: [Todo]\\n"
            "dispatch_states: [Todo]\\n"
            "hold: false\\n",
            encoding="utf-8",
        )
        registry = root / "projects.md"
        registry.write_text(
            "| name | repo_path | pipeline_path | linear_project_slug | linear_team |\\n"
            "| --- | --- | --- | --- | --- |\\n"
            f"| SampleProject | {repo} | {pipeline} | sample-a1 | SAMPLE |\\n",
            encoding="utf-8",
        )
        os.environ["HARBOR_PROJECTS_FILE"] = str(registry)
        os.environ["HARBOR_DB_PATH"] = str(root / "harbor.db")
        db = HarborDB(root / "construction.db")
        db.close()
        raise SystemExit(asyncio.run(_start(
            "SampleProject",
            once=True,
            shadow=True,
            ticket="SAMPLE-1",
            linear_client=Linear(),
            dispatcher=Delivery(),
        )))
        """
    )
    result = subprocess.run(
        [str(python), "-c", script],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "WOULD WAKE build SAMPLE-1" in result.stdout
