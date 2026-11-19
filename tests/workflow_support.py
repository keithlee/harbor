"""Materialize a project-local work-pipeline copy for tests.

The patches below are TEST FIXTURES that recreate the old preset behaviours
some tests still exercise. They are not product presets: Harbor ships exactly
one workflow, and projects change it by editing their copy.
"""
from __future__ import annotations

import copy
import json
import shutil
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUNDLED = ROOT / "workflows" / "work-pipeline"

TEST_PATCHES = {
    "minimal": {"evidence": {"feedback": "off"}, "recovery": {"enabled": False}},
    "reviewed": {"evidence": {"feedback": "required"}},
    "autonomous": {
        "delivery": {"merge_mode": "workflow_checked", "auto_merge_risks": ["low"]},
        "recovery": {"enabled": True},
        "decisions": {"mode": "bounded"},
        "evidence": {"feedback": "required"},
    },
}
DEFAULT_MODELS = {"controller": {"model": "test-model", "reasoning_effort": "medium"}}


def merge(base, patch):
    result = copy.deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, str, list)):
        return json.dumps(value)
    raise TypeError(f"unsupported TOML value {value!r}")


def dumps(data, prefix=""):
    """Minimal TOML writer for nested tables of scalars and arrays."""
    lines, tables = [], []
    for key, value in data.items():
        (tables if isinstance(value, dict) else lines).append((key, value))
    out = "".join(f"{key} = {_value(value)}\n" for key, value in lines)
    for key, value in tables:
        name = f"{prefix}.{key}" if prefix else key
        scalars = {k: v for k, v in value.items() if not isinstance(v, dict)}
        nested = {k: v for k, v in value.items() if isinstance(v, dict)}
        if scalars or not nested:
            out += f"\n[{name}]\n" + dumps(scalars)
        out += dumps(nested, name) if nested else ""
    return out


def config(tmp_path, preset="minimal", extra="", models=None):
    """Copy the bundled work-pipeline into ``tmp_path/.harbor/workflow``.

    ``extra`` is TOML merged into the copied ``workflow.toml``; any ``[roles.*]``
    tables in it are moved into ``config.toml``. Returns the workflow.toml path.
    """
    package = Path(tmp_path) / ".harbor" / "workflow"
    if package.exists():
        shutil.rmtree(package)
    shutil.copytree(BUNDLED, package, ignore=shutil.ignore_patterns("__pycache__", ".DS_Store"))
    patch = tomllib.loads(extra) if extra else {}
    role_models = merge(DEFAULT_MODELS, patch.pop("roles", {}))
    first_mate = patch.pop("first_mate", None)
    if first_mate is not None:
        role_models["first_mate"] = first_mate
    data = merge(merge(tomllib.loads((BUNDLED / "workflow.toml").read_text()), TEST_PATCHES[preset]), patch)
    (package / "workflow.toml").write_text(dumps(data))
    (package / "config.toml").write_text(dumps(merge(role_models, models or {})))
    return package / "workflow.toml"


def materialize(repo_path, **kwargs):
    """Give a test project the required ``.harbor/workflow`` copy."""
    return config(repo_path, **kwargs)
