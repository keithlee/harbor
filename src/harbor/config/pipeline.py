"""Human-editable Harbor pipeline and project configuration.

Harbor deliberately uses Markdown for the small amount of configuration it
needs.  The parser below is intentionally conservative (it is not YAML): it
understands ``key: value`` fields, Markdown tables, and the common nested
``models: / builder:`` spelling.  Unknown fields remain available in
``extras`` but never influence supervision.

Keeping this module independent from :mod:`harbor.config.projects` avoids a
configuration import cycle and makes it useful to callers that only want to
validate a pipeline document.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from harbor.recovery import BlockerPolicy
from harbor.worktree import DEFAULT_DISK_FLOOR_GB


DEFAULT_BUILDER_MODEL = "gpt-5.6-luna"
DEFAULT_BUILDER_EFFORT = "xhigh"
DEFAULT_FIRST_MATE_MODEL = "gpt-6-luna"
DEFAULT_FIRST_MATE_EFFORT = "medium"
DEFAULT_HEARTBEAT_SECONDS = 60.0
DEFAULT_POLL_INTERVAL_SECONDS = 60.0
DEFAULT_DEADLINE_MINUTES = 90.0
DEFAULT_BASE_BRANCH = "main"
DEFAULT_WORKTREE_ROOT = ""
DEFAULT_SKILL = ""
DEFAULT_HOLD = True
DEFAULT_BOT_REWORK_CAP = 1

VALID_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"})


def _normal_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")


def _scalar(value: str) -> str:
    # Markdown comments and code ticks are presentation, not configuration.
    value = value.split(" #", 1)[0].strip().strip("`").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1]
    return value.strip()


def _number(value: Any, default: float) -> float:
    try:
        parsed = float(_scalar(str(value)))
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _disk_floor(value: Any) -> float:
    """Parse ``disk_floor_gb`` strictly: a safety floor never falls back silently."""

    text = _scalar(str(value))
    try:
        parsed = float(text)
    except ValueError:
        raise ValueError(f"disk_floor_gb must be a positive number of GB, got {text!r}") from None
    if not parsed > 0 or parsed != parsed or parsed == float("inf"):
        raise ValueError(f"disk_floor_gb must be a positive number of GB, got {text!r}")
    return parsed


def _effort(value: Any, default: str) -> str:
    parsed = _scalar(str(value)).casefold()
    return parsed if parsed in VALID_EFFORTS else default


def _list(value: Any) -> tuple[str, ...]:
    from harbor.config.projects import _split_list

    return _split_list(_scalar(str(value)))


def _bool(value: Any, default: bool) -> bool:
    parsed = _scalar(str(value)).casefold()
    if parsed in {"true", "yes", "on", "1"}:
        return True
    if parsed in {"false", "no", "off", "0"}:
        return False
    return default


def _int(value: Any, default: int, *, minimum: int = 0) -> int:
    try:
        return max(minimum, int(_scalar(str(value))))
    except (TypeError, ValueError):
        return default


def _fields(text: str) -> dict[str, str]:
    """Flatten the supported scalar forms in *text*.

    Indentation is retained only to infer nested model fields.  This accepts
    both of these forms (and their table equivalents)::

        builder_model: gpt-5.6-luna
        models:
          builder:
            model: gpt-5.6-luna

    A role/model table is handled by :func:`_table_fields` below.
    """

    result: dict[str, str] = {}
    parents: list[tuple[int, str]] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        raw = lines[index]
        line = raw.strip()
        index += 1
        if not line or line.startswith("<!--") or line.startswith("```"):
            continue
        table = re.match(r"^\|\s*([^|]+?)\s*\|\s*(.*?)\s*\|?\s*$", line)
        if table:
            # Table rows with two cells are scalar settings.  Role tables are
            # parsed in a second pass where the header is available.
            key, value = table.groups()
            if _normal_key(key) in {"disk_floor_gb", "disk_floor"}:
                result[_normal_key(key)] = _scalar(value)
                continue
            if key.casefold() not in {"key", "setting", "field", "role"} and not set(value.replace("-", "").strip()):
                continue
            if key.casefold() not in {"role", "model", "effort", "reasoning", "reasoning_effort"}:
                result[_normal_key(key)] = _scalar(value)
            continue
        match = re.match(r"^(?P<indent>\s*)(?:[-*]\s*)?(?P<key>[A-Za-z][\w .-]*)\s*:\s*(?P<value>.*?)\s*$", raw)
        if not match:
            continue
        indent = len(match.group("indent"))
        key = _normal_key(match.group("key"))
        value = _scalar(match.group("value"))
        while parents and parents[-1][0] >= indent:
            parents.pop()
        if value:
            result[key] = value
            if parents:
                result["_".join(parent for _, parent in parents) + "_" + key] = value
        else:
            if key in {"disk_floor_gb", "disk_floor", "builder_provider", "firstmate_provider"}:
                result[key] = ""
                continue
            parents.append((indent, key))
            # A following Markdown/YAML list becomes this key's value.
            values: list[str] = []
            look = index
            while look < len(lines):
                child = lines[look].strip()
                if not child:
                    look += 1
                    continue
                item = re.match(r"^[-*]\s+(.+?)\s*$", child)
                if not item:
                    break
                values.append(_scalar(item.group(1)))
                look += 1
            if values:
                result[key] = ",".join(values)
                index = look

    # Explicit fields such as ``models.builder.model`` vary in punctuation.
    # Normalise them once more to the underscore spelling used below.
    return {
        _normal_key(key): value
        for key, value in result.items()
        if value != "" or _normal_key(key) in {
            "disk_floor_gb", "disk_floor", "builder_provider", "firstmate_provider"
        }
    }


def _table_fields(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    rows = [line.strip() for line in text.splitlines() if line.strip().startswith("|")]
    for offset, header in enumerate(rows[:-1]):
        cells = [cell.strip() for cell in header.strip("|").split("|")]
        if not cells or not any(_normal_key(cell) in {"role", "model", "effort", "reasoning_effort", "setting", "key"} for cell in cells):
            continue
        if all(set(cell.replace("-", "").strip()) <= {""} for cell in cells):
            continue
        # Find the contiguous header/data block.  A second table can safely
        # start later because its header is inspected independently.
        for raw in rows[offset + 1 :]:
            values = [cell.strip() for cell in raw.strip("|").split("|")]
            if all(set(cell.replace("-", "").strip()) <= {""} for cell in values):
                continue
            if len(values) != len(cells):
                break
            row = {_normal_key(cells[i]): _scalar(values[i]) for i in range(len(cells))}
            role = _normal_key(row.get("role", ""))
            if role in {"builder", "implementor", "implementation", "worker"}:
                if row.get("model"):
                    result["builder_model"] = row["model"]
                if row.get("effort") or row.get("reasoning_effort"):
                    result["builder_effort"] = row.get("effort") or row.get("reasoning_effort", "")
            elif role in {"first_mate", "firstmate", "planner", "planning", "coordinator"}:
                if row.get("model"):
                    result["firstmate_model"] = row["model"]
                if row.get("effort") or row.get("reasoning_effort"):
                    result["firstmate_effort"] = row.get("effort") or row.get("reasoning_effort", "")
            elif row.get("key") or row.get("setting"):
                key = _normal_key(row.get("key") or row.get("setting", ""))
                value = row.get("value") or row.get("default") or row.get("setting_value", "")
                if key in {"builder_fallback_models", "firstmate_fallback_models"} and not value:
                    raise ValueError("fallback models require an explicit JSON array; use [] to disable")
                if key and value:
                    result[key] = value
    return result


RETIRED_KEYS = (
    "workflow_path", "skill", "delivery_skill", "pipeline_skill",
    "builder_model", "implementor_model", "implementation_model", "worker_model", "models_builder_model",
    "firstmate_model", "first_mate_model", "planner_model", "planning_model",
    "models_firstmate_model", "models_first_mate_model",
    "builder_effort", "builder_reasoning_effort", "implementor_effort", "implementation_effort",
    "firstmate_effort", "first_mate_effort", "firstmate_reasoning_effort", "planner_effort", "planning_effort",
    "builder_fallback_models", "firstmate_fallback_models",
    # Owned by workflow.toml [recovery] and [decisions].
    "recovery_delays", "firstmate_decisions",
)


def retired_keys(text: str) -> tuple[str, ...]:
    """pipeline.md keys that moved to ``.harbor/workflow`` (workflow + models)."""
    fields = _fields(text)
    fields.update(_table_fields(text))
    return tuple(key for key in RETIRED_KEYS if _normal_key(key) in fields)


def parse_pipeline_markdown(text: str, *, defaults: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return normalized pipeline settings parsed from Markdown."""

    if re.search(r"(?m)^[ \t]*(?:[-*][ \t]*)?(?:builder_fallback_models|firstmate_fallback_models)[ \t]*:[ \t]*$", text):
        raise ValueError("fallback models require an explicit JSON array; use [] to disable")
    values: dict[str, Any] = dict(defaults or {})
    fields = _fields(text)
    fields.update(_table_fields(text))

    def first(*names: str) -> str | None:
        for name in names:
            value = fields.get(_normal_key(name))
            if value or (name in {"disk_floor_gb", "disk_floor", "builder_provider", "firstmate_provider"} and name in fields):
                return value
        return None

    builder_model = first("builder_model", "implementor_model", "implementation_model", "worker_model", "models_builder_model")
    builder_effort = first("builder_effort", "builder_reasoning_effort", "implementor_effort", "implementation_effort", "models_builder_effort")
    firstmate_model = first("firstmate_model", "first_mate_model", "planner_model", "planning_model", "models_firstmate_model", "models_first_mate_model")
    firstmate_effort = first("firstmate_effort", "first_mate_effort", "firstmate_reasoning_effort", "planner_effort", "planning_effort")
    heartbeat = first("heartbeat_seconds", "heartbeat_interval_seconds", "heartbeat_interval")
    poll = first("poll_interval_seconds", "poll_interval")
    for key, aliases in {
        "linear_project_slug": ("linear_project_slug", "project_slug", "slug"),
        "linear_projects": ("linear_projects", "projects", "project_slugs", "included_projects"),
        "linear_team": ("linear_team", "team"),
        "linear_api_url": ("linear_api_url", "api_url"),
        # These values are deliberately data-only.  In particular, ``skill``
        # is an opaque name/path owned by the delivery runner; Harbor must not
        # import it, reinterpret it, or silently substitute another skill.
        "skill": ("skill", "delivery_skill", "pipeline_skill"),
        "deadline_minutes": ("deadline_minutes", "deadline", "timeout_minutes"),
        "disk_floor_gb": ("disk_floor_gb", "disk_floor"),
        "base_branch": ("base_branch", "base_ref", "branch"),
        "worktree_root": ("worktree_root", "worktrees_root", "worktree_dir"),
        "dispatch_states": ("dispatch_states",),
        "poll_states": ("poll_states",),
        "hold": ("hold", "hold_all", "shadow_hold"),
        "github_repository": ("github_repository", "github_repo", "repository"),
        "required_checks": ("required_checks", "ci_required_checks"),
        "bot_rework_cap": ("bot_rework_cap",),
        # Opaque to Harbor: it runs it in the new worktree, nothing more.
        "install_command": ("install_command", "install", "setup_command"),
        "workflow_path": ("workflow_path",),
        "playwright_browser_image": ("playwright_browser_image",),
        "builder_provider": ("builder_provider",),
        "firstmate_provider": ("firstmate_provider",),
        "builder_fallback_models": ("builder_fallback_models",),
        "firstmate_fallback_models": ("firstmate_fallback_models",),
    }.items():
        value = first(*aliases)
        if key.endswith("_fallback_models") and _normal_key(aliases[0]) in fields:
            value = fields[_normal_key(aliases[0])]
        if value is None:
            continue
        if key.endswith("_fallback_models"):
            import json
            from harbor.model_policy import validate_fallbacks
            try:
                candidates = json.loads(value)
            except ValueError as exc:
                raise ValueError("fallback models require a JSON array") from exc
            values[key] = validate_fallbacks(builder_model if key.startswith("builder") else firstmate_model, candidates)
        elif key in {"dispatch_states", "poll_states", "required_checks", "linear_projects"}:
            values[key] = _list(value)
        elif key == "hold":
            parsed_hold = _bool(value, bool(values.get("hold", values.get("hold_all", DEFAULT_HOLD))))
            values["hold"] = parsed_hold
            # ``hold_all`` is the historical poller spelling.  Keep it in the
            # normalized mapping so older callers and newer callers observe
            # the same policy.
            values["hold_all"] = parsed_hold
        elif key == "deadline_minutes":
            values[key] = _number(value, DEFAULT_DEADLINE_MINUTES)
        elif key == "disk_floor_gb":
            values[key] = _disk_floor(value)
        elif key == "bot_rework_cap":
            values[key] = _int(value, DEFAULT_BOT_REWORK_CAP)
        elif key == "bot_rework_cap":
            try:
                values[key] = max(0, int(value))
            except (TypeError, ValueError):
                pass
        else:
            values[key] = value
    if builder_model:
        values["builder_model"] = builder_model
    if builder_effort:
        values["builder_effort"] = _effort(builder_effort, DEFAULT_BUILDER_EFFORT)
    if firstmate_model:
        values["firstmate_model"] = firstmate_model
    if firstmate_effort:
        values["firstmate_effort"] = _effort(firstmate_effort, DEFAULT_FIRST_MATE_EFFORT)
    if heartbeat:
        values["heartbeat_seconds"] = _number(heartbeat, DEFAULT_HEARTBEAT_SECONDS)
    if poll:
        values["poll_interval_seconds"] = _number(poll, DEFAULT_POLL_INTERVAL_SECONDS)
    values.setdefault("builder_model", DEFAULT_BUILDER_MODEL)
    values.setdefault("builder_effort", DEFAULT_BUILDER_EFFORT)
    values.setdefault("firstmate_model", DEFAULT_FIRST_MATE_MODEL)
    values.setdefault("firstmate_effort", DEFAULT_FIRST_MATE_EFFORT)
    values.setdefault("heartbeat_seconds", DEFAULT_HEARTBEAT_SECONDS)
    values.setdefault("poll_interval_seconds", DEFAULT_POLL_INTERVAL_SECONDS)
    values.setdefault("skill", DEFAULT_SKILL)
    values.setdefault("deadline_minutes", DEFAULT_DEADLINE_MINUTES)
    values.setdefault("disk_floor_gb", DEFAULT_DISK_FLOOR_GB)
    values.setdefault("base_branch", DEFAULT_BASE_BRANCH)
    values.setdefault("worktree_root", DEFAULT_WORKTREE_ROOT)
    values.setdefault("github_repository", "")
    values.setdefault("required_checks", ())
    values.setdefault("bot_rework_cap", DEFAULT_BOT_REWORK_CAP)
    values.setdefault("install_command", None)
    if "hold" not in values:
        values["hold"] = values.get("hold_all", DEFAULT_HOLD)
    values.setdefault("hold_all", values["hold"])
    values["blocker_policy"] = BlockerPolicy.parse({**(defaults or {}), **fields})
    return values


@dataclass(frozen=True)
class PipelineConfig:
    builder_provider: str = "codex"
    firstmate_provider: str = "codex"
    builder_model: str = DEFAULT_BUILDER_MODEL
    builder_fallback_models: tuple[str, ...] = ()
    firstmate_fallback_models: tuple[str, ...] = ()
    builder_effort: str = DEFAULT_BUILDER_EFFORT
    firstmate_model: str = DEFAULT_FIRST_MATE_MODEL
    firstmate_effort: str = DEFAULT_FIRST_MATE_EFFORT
    heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS
    # Runtime contract fields are retained here even though Harbor does not
    # interpret the delivery skill.  Keeping them typed makes the exact
    # project contract available to CLI/runner assembly without duplicating
    # Markdown parsing in several modules.
    skill: str = DEFAULT_SKILL
    deadline_minutes: float = DEFAULT_DEADLINE_MINUTES
    disk_floor_gb: float = DEFAULT_DISK_FLOOR_GB
    base_branch: str = DEFAULT_BASE_BRANCH
    worktree_root: str = DEFAULT_WORKTREE_ROOT
    github_repository: str = ""
    required_checks: tuple[str, ...] = ()
    bot_rework_cap: int = DEFAULT_BOT_REWORK_CAP
    install_command: str | None = None
    hold: bool = DEFAULT_HOLD
    blocker_policy: BlockerPolicy = field(default_factory=BlockerPolicy)
    extras: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        from harbor.model_policy import validate_fallbacks
        for prefix in ("builder", "firstmate"):
            key = prefix + "_fallback_models"
            object.__setattr__(self, key, validate_fallbacks(getattr(self, prefix + "_model"), getattr(self, key)))

    @property
    def first_mate_model(self) -> str:
        return self.firstmate_model

    @property
    def first_mate_effort(self) -> str:
        return self.firstmate_effort

    @property
    def planner_model(self) -> str:
        return self.firstmate_model

    @property
    def planner_effort(self) -> str:
        return self.firstmate_effort

    @property
    def implementation_model(self) -> str:
        return self.builder_model

    @property
    def implementation_effort(self) -> str:
        return self.builder_effort

    @property
    def heartbeat_interval_seconds(self) -> float:
        return self.heartbeat_seconds

    @property
    def hold_all(self) -> bool:
        """Historical alias consumed by the Linear poller."""

        return self.hold

    @property
    def deadline_seconds(self) -> float:
        return self.deadline_minutes * 60.0

    @property
    def github_repo(self) -> str:
        return self.github_repository

    @classmethod
    def from_markdown(cls, text: str, *, defaults: Mapping[str, Any] | None = None) -> "PipelineConfig":
        values = parse_pipeline_markdown(text, defaults=defaults)
        known = {
            "builder_provider",
            "firstmate_provider",
            "builder_model",
            "builder_fallback_models",
            "firstmate_fallback_models",
            "builder_effort",
            "firstmate_model",
            "firstmate_effort",
            "heartbeat_seconds",
            "poll_interval_seconds",
            "skill",
            "deadline_minutes",
            "disk_floor_gb",
            "base_branch",
            "worktree_root",
            "github_repository",
            "required_checks",
            "bot_rework_cap",
            "hold",
            "hold_all",
            "blocker_policy",
        }
        return cls(
            builder_provider=str(values.get("builder_provider", "codex")),
            firstmate_provider=str(values.get("firstmate_provider", "codex")),
            builder_model=str(values.get("builder_model", DEFAULT_BUILDER_MODEL)),
            builder_fallback_models=values.get("builder_fallback_models", ()),
            firstmate_fallback_models=values.get("firstmate_fallback_models", ()),
            builder_effort=_effort(values.get("builder_effort", DEFAULT_BUILDER_EFFORT), DEFAULT_BUILDER_EFFORT),
            firstmate_model=str(values.get("firstmate_model", DEFAULT_FIRST_MATE_MODEL)),
            firstmate_effort=_effort(values.get("firstmate_effort", DEFAULT_FIRST_MATE_EFFORT), DEFAULT_FIRST_MATE_EFFORT),
            heartbeat_seconds=_number(values.get("heartbeat_seconds"), DEFAULT_HEARTBEAT_SECONDS),
            poll_interval_seconds=_number(values.get("poll_interval_seconds"), DEFAULT_POLL_INTERVAL_SECONDS),
            skill=str(values.get("skill", DEFAULT_SKILL)),
            deadline_minutes=_number(values.get("deadline_minutes"), DEFAULT_DEADLINE_MINUTES),
            disk_floor_gb=_disk_floor(values.get("disk_floor_gb", DEFAULT_DISK_FLOOR_GB)),
            base_branch=str(values.get("base_branch", DEFAULT_BASE_BRANCH)),
            worktree_root=str(values.get("worktree_root", DEFAULT_WORKTREE_ROOT)),
            github_repository=str(values.get("github_repository", "")),
            required_checks=tuple(str(value) for value in values.get("required_checks", ())),
            bot_rework_cap=_int(values.get("bot_rework_cap"), DEFAULT_BOT_REWORK_CAP),
            hold=_bool(values.get("hold", values.get("hold_all", DEFAULT_HOLD)), DEFAULT_HOLD),
            blocker_policy=values["blocker_policy"],
            extras={key: value for key, value in values.items() if key not in known},
        )

    @classmethod
    def from_file(cls, path: str | Path, *, defaults: Mapping[str, Any] | None = None) -> "PipelineConfig":
        path = Path(path).expanduser()
        return cls.from_markdown(path.read_text(encoding="utf-8"), defaults=defaults)


PipelineSettings = PipelineConfig


@dataclass(frozen=True)
class ProjectDefinition:
    """One entry parsed from the Harbor-level ``projects.md`` registry."""

    name: str
    repo_path: Path
    pipeline_path: Path | None = None
    values: Mapping[str, Any] = field(default_factory=dict)


def _project_rows(text: str) -> list[dict[str, str]]:
    lines = [line.strip() for line in text.splitlines()]
    rows: list[dict[str, str]] = []
    for index, line in enumerate(lines[:-1]):
        if not line.startswith("|"):
            continue
        headers = [_normal_key(cell) for cell in line.strip("|").split("|")]
        divider = lines[index + 1]
        if len(headers) < 2 or not divider.startswith("|") or not all(set(cell.replace("-", "").strip()) <= {""} for cell in divider.strip("|").split("|")):
            continue
        for raw in lines[index + 2 :]:
            if not raw.startswith("|"):
                break
            values = [_scalar(cell) for cell in raw.strip("|").split("|")]
            if len(values) != len(headers):
                break
            rows.append({headers[pos]: values[pos] for pos in range(len(headers))})
        if rows:
            return rows
    # Heading + key/value sections are easier for a human to maintain than a
    # wide table and are intentionally supported too.
    current: dict[str, str] | None = None
    for raw in lines:
        heading = re.match(r"^#{1,6}\s+(.+?)\s*$", raw)
        if heading:
            if current and current.get("name"):
                rows.append(current)
            title = _scalar(heading.group(1)).strip("# ")
            current = {"name": title} if title.casefold() not in {"projects", "harbor projects", "project registry"} else None
            continue
        if current is None:
            continue
        match = re.match(r"^(?:[-*]\s*)?([A-Za-z][\w .-]*)\s*:\s*(.*?)\s*$", raw)
        if match:
            current[_normal_key(match.group(1))] = _scalar(match.group(2))
    if current and current.get("name"):
        rows.append(current)
    if rows:
        return rows
    # A compact YAML-like list is also pleasant to edit in Markdown code
    # blocks. This is still not a YAML parser: only one-level list entries
    # with scalar key/value fields are recognized.
    current = None
    for raw in lines:
        item = re.match(r"^\s*-\s*(?:name|project)\s*:\s*(.*?)\s*$", raw)
        if item:
            if current and current.get("name"):
                rows.append(current)
            current = {"name": _scalar(item.group(1))}
            continue
        if current is None:
            continue
        match = re.match(r"^\s+(?:[-*]\s*)?([A-Za-z][\w .-]*)\s*:\s*(.*?)\s*$", raw)
        if match:
            current[_normal_key(match.group(1))] = _scalar(match.group(2))
    if current and current.get("name"):
        rows.append(current)
    return rows


def parse_projects_markdown(text: str, *, source: str | Path | None = None) -> tuple[ProjectDefinition, ...]:
    """Parse a Harbor-level project registry without importing project code."""

    base = Path(source).expanduser().resolve().parent if source else Path.cwd()
    result: list[ProjectDefinition] = []
    for row in _project_rows(text):
        name = row.get("name") or row.get("project") or row.get("id")
        repo = row.get("repo_path") or row.get("repo") or row.get("repository") or row.get("path")
        if not name or not repo:
            continue
        repo_path = Path(os.path.expandvars(repo)).expanduser()
        if not repo_path.is_absolute():
            repo_path = (base / repo_path).resolve()
        pipeline_value = row.get("pipeline_path") or row.get("pipeline")
        pipeline_path: Path | None = None
        if pipeline_value:
            pipeline_path = Path(os.path.expandvars(pipeline_value)).expanduser()
            if not pipeline_path.is_absolute():
                pipeline_path = (base / pipeline_path).resolve()
        values = {key: value for key, value in row.items() if key not in {"name", "project", "id", "repo_path", "repo", "repository", "path", "pipeline", "pipeline_path"}}
        result.append(ProjectDefinition(str(name), repo_path, pipeline_path, values))
    return tuple(result)


def load_projects(path: str | Path) -> tuple[ProjectDefinition, ...]:
    path = Path(path).expanduser()
    return parse_projects_markdown(path.read_text(encoding="utf-8"), source=path)


parse_projects = parse_projects_markdown
load_project_registry = load_projects
load_projects_file = load_projects
parse_projects_file = parse_projects_markdown


__all__ = [
    "DEFAULT_BUILDER_EFFORT",
    "DEFAULT_BUILDER_MODEL",
    "DEFAULT_FIRST_MATE_EFFORT",
    "DEFAULT_FIRST_MATE_MODEL",
    "DEFAULT_BASE_BRANCH",
    "DEFAULT_BOT_REWORK_CAP",
    "DEFAULT_DEADLINE_MINUTES",
    "DEFAULT_DISK_FLOOR_GB",
    "DEFAULT_HEARTBEAT_SECONDS",
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "DEFAULT_SKILL",
    "DEFAULT_WORKTREE_ROOT",
    "PipelineConfig",
    "PipelineSettings",
    "ProjectDefinition",
    "VALID_EFFORTS",
    "load_projects",
    "load_project_registry",
    "load_projects_file",
    "parse_pipeline_markdown",
    "parse_projects",
    "parse_projects_markdown",
    "parse_projects_file",
]
