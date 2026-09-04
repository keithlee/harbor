"""Project configuration and the small Markdown format used by Harbor.

The project file is deliberately data-only.  A project's delivery skill remains an
opaque string to the supervisor; Phase 1 only needs the scope and polling settings.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from .pipeline import (
    DEFAULT_BASE_BRANCH,
    DEFAULT_BOT_REWORK_CAP,
    DEFAULT_BUILDER_EFFORT,
    DEFAULT_BUILDER_MODEL,
    DEFAULT_DEADLINE_MINUTES,
    DEFAULT_FIRST_MATE_EFFORT,
    DEFAULT_FIRST_MATE_MODEL,
    DEFAULT_HEARTBEAT_SECONDS,
    DEFAULT_SKILL,
    DEFAULT_WORKTREE_ROOT,
    PipelineConfig,
    load_projects,
    parse_pipeline_markdown as parse_pipeline_settings,
)


POLL_STATES = ("Todo", "In Progress", "In Review", "Blocked", "Human Review")
DISPATCH_STATES = ("Todo",)


def _as_bool(value: str, default: bool) -> bool:
    value = value.strip().lower()
    if value in {"true", "yes", "on", "1"}:
        return True
    if value in {"false", "no", "off", "0"}:
        return False
    return default


def _split_list(text: str | Sequence[str] | None) -> tuple[str, ...]:
    """Split a bracketed list on commas that are not inside a quoted item.

    Required check names may contain commas, so a naive ``split(",")`` can
    silently turn one check into several that can never all be observed.
    """

    if text is None:
        return ()
    if not isinstance(text, str):
        # Already a sequence (a parsed config value or a dataclass default).
        return tuple(str(item).strip() for item in text if str(item).strip())
    text = text.strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    items: list[str] = []
    buffer: list[str] = []
    quote: str | None = None
    for char in text:
        if quote is not None:
            if char == quote:
                quote = None
            else:
                buffer.append(char)
            continue
        if char in ("'", '"'):
            quote = char
            continue
        if char == ",":
            item = "".join(buffer).strip()
            if item:
                items.append(item)
            buffer = []
            continue
        buffer.append(char)
    trailing = "".join(buffer).strip()
    if trailing:
        items.append(trailing)
    return tuple(items)


def _as_list(value: str, default: Sequence[str]) -> tuple[str, ...]:
    return _split_list(value) or tuple(default)


def parse_pipeline_markdown(text: str, *, defaults: Mapping[str, object] | None = None) -> dict[str, object]:
    """Parse scalar scope keys from ``pipeline.md``.

    Both the plan's ``key: value`` notation and a Markdown table row such as
    ``| dispatch_states | [Todo] |`` are accepted.  Unknown keys are ignored so
    teams can add skill-specific settings without making Harbor parse them.
    """

    result: dict[str, object] = dict(defaults or {})
    scalar: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("<!--"):
            continue
        table = re.match(r"^\|\s*([A-Za-z][\w-]*)\s*\|\s*(.*?)\s*\|?\s*$", line)
        if table:
            scalar[table.group(1).replace("-", "_")] = table.group(2).split(" #", 1)[0].strip()
            continue
        match = re.match(r"^(?:[-*]\s*)?([A-Za-z][\w-]*)\s*:\s*(.*?)\s*$", line)
        if match:
            scalar[match.group(1).replace("-", "_")] = match.group(2).split(" #", 1)[0].strip().strip('`')

    if "dispatch_states" in scalar:
        result["dispatch_states"] = _as_list(scalar["dispatch_states"], DISPATCH_STATES)
    if "poll_states" in scalar:
        result["poll_states"] = _as_list(scalar["poll_states"], POLL_STATES)
    if "hold_all" in scalar:
        result["hold_all"] = _as_bool(scalar["hold_all"], bool(result.get("hold_all", True)))
    if "poll_interval_seconds" in scalar:
        try:
            result["poll_interval_seconds"] = float(scalar["poll_interval_seconds"])
        except ValueError:
            pass
    if "poll_interval" in scalar:
        try:
            result["poll_interval_seconds"] = float(scalar["poll_interval"])
        except ValueError:
            pass
    if "linear_project_slug" in scalar:
        result["linear_project_slug"] = scalar["linear_project_slug"]
    if "project_slug" in scalar:
        result["linear_project_slug"] = scalar["project_slug"]
    if "slug" in scalar:
        result["linear_project_slug"] = scalar["slug"]
    if "linear_team" in scalar:
        result["linear_team"] = scalar["linear_team"]
    if "team" in scalar:
        result["linear_team"] = scalar["team"]
    if "api_url" in scalar:
        result["linear_api_url"] = scalar["api_url"]
    # Keep this historical parser's scope behavior while exposing the newer
    # model/heartbeat fields to callers that import it directly.
    parsed_settings = parse_pipeline_settings(text, defaults=result)
    for key in (
        "builder_model",
        "builder_effort",
        "firstmate_model",
        "firstmate_effort",
        "heartbeat_seconds",
        "poll_interval_seconds",
        "linear_project_slug",
        "linear_projects",
        "linear_team",
        "linear_api_url",
        "dispatch_states",
        "poll_states",
        "hold_all",
        "github_repository",
        "required_checks",
        "bot_rework_cap",
        "skill",
        "deadline_minutes",
        "base_branch",
        "worktree_root",
        "hold",
        "hold_all",
        "install_command",
    ):
        if key in parsed_settings:
            result[key] = parsed_settings[key]
    return result


@dataclass(frozen=True)
class ProjectConfig:
    name: str
    linear_project_slug: str
    # Additional Linear projects to include in the same poll.  Adding a
    # project is a config edit, never a code change; the query stays one
    # request regardless of how many are listed.
    linear_projects: tuple[str, ...] = ()
    linear_team: str = ""
    repo_path: Path = field(default_factory=lambda: Path.cwd())
    pipeline_path: Path | None = None
    linear_api_url: str = "https://api.linear.app/graphql"
    poll_states: tuple[str, ...] = POLL_STATES
    dispatch_states: tuple[str, ...] = DISPATCH_STATES
    poll_interval_seconds: float = 60.0
    hold_all: bool = True
    # Model and heartbeat values are read from the project pipeline document.
    # Defaults preserve the MVP contract while keeping direct test fixtures
    # and embedders source-compatible with Phase 1.
    builder_model: str = DEFAULT_BUILDER_MODEL
    builder_effort: str = DEFAULT_BUILDER_EFFORT
    firstmate_model: str = DEFAULT_FIRST_MATE_MODEL
    firstmate_effort: str = DEFAULT_FIRST_MATE_EFFORT
    heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS
    skill: str = DEFAULT_SKILL
    deadline_minutes: float = DEFAULT_DEADLINE_MINUTES
    base_branch: str = DEFAULT_BASE_BRANCH
    worktree_root: str = DEFAULT_WORKTREE_ROOT
    github_repository: str = ""
    required_checks: tuple[str, ...] = ()
    bot_rework_cap: int = DEFAULT_BOT_REWORK_CAP
    # Opaque to Harbor: it runs this in a new worktree, nothing more.
    install_command: str | None = None
    # ``hold_all`` predates the project pipeline contract.  Accept both
    # spellings at construction time so parsed mappings from older embedders
    # remain source-compatible, then keep them synchronized.
    hold: bool | None = None

    def __post_init__(self) -> None:
        configured = self.hold_all if self.hold is None else bool(self.hold)
        object.__setattr__(self, "hold_all", configured)
        object.__setattr__(self, "hold", configured)
        object.__setattr__(self, "linear_projects", _split_list(self.linear_projects))

    @property
    def linear_project_scope(self) -> tuple[str, ...]:
        """Every Linear project slug this Harbor instance polls.

        ``linear_project_slug`` is the primary project and stays required, so
        an existing single-project config keeps working untouched.  Listing
        more in ``linear_projects`` widens the same single GraphQL request.
        """

        scope: list[str] = []
        for slug in (self.linear_project_slug, *self.linear_projects):
            text = str(slug).strip()
            if text and text not in scope:
                scope.append(text)
        return tuple(scope)

    @property
    def project_slug(self) -> str:
        """Alias useful to callers that use Linear's terminology."""

        return self.linear_project_slug

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
    def heartbeat_interval_seconds(self) -> float:
        return self.heartbeat_seconds

    @property
    def deadline_seconds(self) -> float:
        return self.deadline_minutes * 60.0

    @property
    def github_repo(self) -> str:
        return self.github_repository

    @property
    def pipeline_config(self) -> PipelineConfig:
        return PipelineConfig(
            builder_model=self.builder_model,
            builder_effort=self.builder_effort,
            firstmate_model=self.firstmate_model,
            firstmate_effort=self.firstmate_effort,
            heartbeat_seconds=self.heartbeat_seconds,
            poll_interval_seconds=self.poll_interval_seconds,
            skill=self.skill,
            deadline_minutes=self.deadline_minutes,
            base_branch=self.base_branch,
            worktree_root=self.worktree_root,
            github_repository=self.github_repository,
            required_checks=self.required_checks,
            bot_rework_cap=self.bot_rework_cap,
            hold=self.hold,
        )

    @classmethod
    def from_pipeline(cls, name: str, path: Path, **overrides: object) -> "ProjectConfig":
        explicit_slug = overrides.pop("linear_project_slug", None)
        explicit_team = overrides.pop("linear_team", None)
        values: dict[str, object] = {
            "linear_project_slug": "",
            "linear_team": "",
            "repo_path": path.parent.parent,
            "pipeline_path": path,
        }
        values.update(parse_pipeline_markdown(path.read_text(encoding="utf-8"), defaults=values))
        pipeline = PipelineConfig.from_file(path, defaults=values)
        values.update(
            {
                "builder_model": pipeline.builder_model,
                "builder_effort": pipeline.builder_effort,
                "firstmate_model": pipeline.firstmate_model,
                "firstmate_effort": pipeline.firstmate_effort,
                "heartbeat_seconds": pipeline.heartbeat_seconds,
                "poll_interval_seconds": pipeline.poll_interval_seconds,
                "skill": pipeline.skill,
                "deadline_minutes": pipeline.deadline_minutes,
                "base_branch": pipeline.base_branch,
                "worktree_root": pipeline.worktree_root,
                "github_repository": pipeline.github_repository,
                "required_checks": pipeline.required_checks,
                "bot_rework_cap": pipeline.bot_rework_cap,
                "hold_all": pipeline.hold,
                "hold": pipeline.hold,
            }
        )
        values.update(overrides)
        if explicit_slug is not None and str(explicit_slug).strip():
            values["linear_project_slug"] = explicit_slug
        if explicit_team is not None and str(explicit_team).strip():
            values["linear_team"] = explicit_team
        values["poll_states"] = tuple(values.get("poll_states", POLL_STATES))
        values["dispatch_states"] = tuple(values.get("dispatch_states", DISPATCH_STATES))
        values["required_checks"] = tuple(values.get("required_checks", ()))
        values["repo_path"] = Path(values["repo_path"])
        values["pipeline_path"] = Path(values["pipeline_path"])
        for field_name in ("linear_project_slug", "linear_team"):
            if not str(values.get(field_name, "")).strip():
                raise ValueError(f"Harbor project {name} is missing required {field_name}")
        return cls(name=name, **values)  # type: ignore[arg-type]

    @classmethod
    def from_file(cls, name: str, path: Path, **overrides: object) -> "ProjectConfig":
        return cls.from_pipeline(name, Path(path), **overrides)


def get_project(name: str, *, config_root: Path | None = None) -> ProjectConfig:
    """Resolve a named project, optionally from a local config root."""

    if not name or not name.strip():
        raise ValueError("project name is required")
    normalized = name.strip().lower()

    # The registry is the source of truth for named projects.  It may be
    # selected explicitly for tests/embeddings, or via HARBOR_PROJECTS_FILE.
    # The repository-level projects.md is the normal CLI default.
    base = Path(config_root).expanduser() if config_root else Path.cwd()
    configured = os.environ.get("HARBOR_PROJECTS_FILE")
    candidates: list[Path] = []
    if configured:
        selected = Path(configured).expanduser().resolve()
        if not selected.is_file():
            raise ValueError(f"Harbor project registry does not exist: {selected}")
        candidates.append(selected)
    else:
        if config_root:
            candidates.append(base / "projects.md")
        candidates.extend(
            [
                base / "projects.md",
                Path(__file__).resolve().parents[3] / "projects.md",
            ]
        )
    seen: set[Path] = set()
    registry_loaded = False
    for registry_path in candidates:
        registry_path = registry_path.resolve()
        if registry_path in seen or not registry_path.is_file():
            continue
        seen.add(registry_path)
        try:
            definitions = load_projects(registry_path)
        except (OSError, UnicodeError):
            continue
        registry_loaded = True
        for definition in definitions:
            if definition.name.casefold() != normalized:
                continue
            pipeline = definition.pipeline_path
            if pipeline is None:
                candidate = definition.repo_path / ".harbor" / "pipeline.md"
                pipeline = candidate
            overrides: dict[str, object] = dict(definition.values)
            # Registry values are intentionally explicit and win over a
            # repository's optional pipeline file for identity/scope fields.
            for key in ("linear_project_slug", "linear_team", "linear_api_url", "poll_interval_seconds", "heartbeat_seconds"):
                if key in overrides:
                    continue
                aliases = {
                    "linear_project_slug": ("project_slug", "slug"),
                    "linear_team": ("team",),
                    "linear_api_url": ("api_url",),
                    "poll_interval_seconds": ("poll_interval",),
                    "heartbeat_seconds": ("heartbeat_interval",),
                }[key]
                for alias in aliases:
                    if alias in overrides:
                        overrides[key] = overrides[alias]
                        break
            if "hold" in overrides and "hold_all" not in overrides:
                overrides["hold_all"] = _as_bool(str(overrides["hold"]), True)
            overrides["repo_path"] = definition.repo_path
            overrides = {
                key: value
                for key, value in overrides.items()
                if key
                in {
                    "linear_project_slug",
                    "linear_projects",
                    "linear_team",
                    "linear_api_url",
                    "poll_interval_seconds",
                    "heartbeat_seconds",
                    "poll_states",
                    "dispatch_states",
                    "hold_all",
                    "builder_model",
                    "builder_effort",
                    "firstmate_model",
                    "firstmate_effort",
                    "skill",
                    "deadline_minutes",
                    "base_branch",
                    "worktree_root",
                    "github_repository",
                    "required_checks",
                    "bot_rework_cap",
                    "hold",
                    "repo_path",
                    "install_command",
                }
            }
            if pipeline is None or not pipeline.is_file():
                expected = pipeline or (definition.repo_path / ".harbor" / "pipeline.md")
                raise ValueError(
                    f"Harbor project {definition.name} is missing its live pipeline: expected {expected}; "
                    "refusing to fall back to AGENTS.md"
                )
            return ProjectConfig.from_pipeline(definition.name, pipeline, **overrides)

    if registry_loaded:
        raise ValueError(f"unknown Harbor project: {name}")

    if not registry_loaded:
        expected = Path(configured).expanduser() if configured else base / "projects.md"
        raise ValueError(
            f"no Harbor project registry found; create {expected} or set HARBOR_PROJECTS_FILE"
        )


__all__ = [
    "DISPATCH_STATES",
    "POLL_STATES",
    "ProjectConfig",
    "get_project",
    "load_project",
    "parse_pipeline_markdown",
]

load_project = get_project
