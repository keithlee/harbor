"""The project's work-pipeline: one package copied into ``<repo>/.harbor/workflow``.

There is no package selection, preset or override layer. Users change the dev
workflow by editing their copy. Model settings live in ``config.toml`` beside
``workflow.toml`` and are read by both Harbor (controller, First Mate) and the
delivery skill (child roles).
"""
from __future__ import annotations

import hashlib
import json
import math
import tomllib
from dataclasses import dataclass
from pathlib import Path

EVENTS = {"setup", "blocker", "pre_handoff", "pre_merge", "pre_cleanup"}
WORKFLOW_DIR = Path(".harbor") / "workflow"
MIGRATION_HINT = ("see docs/configurable-workflows.md#migrating: copy workflows/work-pipeline into "
                  "<repo>/.harbor/workflow and move role models into .harbor/workflow/config.toml")
SECTIONS = {
    "delivery": {"merge_mode", "auto_merge_risks"},
    "recovery": {"enabled", "delays_seconds", "max_attempts"},
    "decisions": {"mode", "require_citation", "require_rationale", "auto_accept_first_option", "max_auto_accepts"},
    "cleanup": {"mode", "delay_seconds"},
    "execution": {"deadline_minutes"},
    "evidence": {"feedback", "ci"},
    "states": {"ready", "paused", "terminal"},
    "github": {"transport", "mcp_url"},
    "builder": {"device_proof"},
}
DEVICE_PROOF_PLATFORMS = ("android", "ios", "web")


def device_proof_settings(data) -> tuple[str, ...]:
    """The builder's opt-in device/browser proof platforms; ``()`` when absent.

    Fails closed: anything but a list of known, distinct platform names raises,
    so a typo never launches a builder with a sandbox nobody asked for.
    """
    section = dict((data or {}).get("builder") or {})
    value = section.get("device_proof", [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("builder.device_proof must be a list of platform names")
    unknown = [item for item in value if item not in DEVICE_PROOF_PLATFORMS]
    if unknown:
        raise ValueError(
            f"unknown builder.device_proof platform(s) {', '.join(map(repr, unknown))}; "
            f"use any of {', '.join(DEVICE_PROOF_PLATFORMS)}"
        )
    if len(set(value)) != len(value):
        raise ValueError("builder.device_proof lists a platform twice")
    return tuple(value)
GITHUB_TRANSPORTS = {"rest", "mcp"}
DEFAULT_GITHUB_MCP_URL = "https://api.githubcopilot.com/mcp/"


def github_settings(data) -> dict:
    """The builder's GitHub transport. ``rest`` (default) or opt-in ``mcp``."""
    section = dict((data or {}).get("github") or {})
    transport = section.get("transport", "rest")
    if transport not in GITHUB_TRANSPORTS:
        raise ValueError("github.transport must be \"rest\" or \"mcp\"")
    url = section.get("mcp_url", DEFAULT_GITHUB_MCP_URL)
    if not isinstance(url, str) or not url.startswith("https://") or any(c.isspace() for c in url):
        raise ValueError("github.mcp_url must be an https URL")
    if "mcp_url" in section and transport != "mcp":
        raise ValueError("github.mcp_url is only used with transport = \"mcp\"")
    return {"transport": transport, "mcp_url": url}
ENTRY_FIELDS = {
    "skills": {"ref", "enabled"},
    "hooks": {"event", "mode", "executor", "command", "protocol", "timeout_seconds", "skill"},
    "checks": {"command", "required", "timeout_seconds", "after"},
}
ROLES = ("controller", "planner", "reviewer", "implementor")
ROLE_FIELDS = {"model", "reasoning_effort", "fallback_models"}
FIRST_MATE_FIELDS = ROLE_FIELDS | {"max_recoveries", "allow_fresh_session", "recheck_minutes", "stall_minutes"}
FIRST_MATE_DEFAULTS = {"max_recoveries": 3, "allow_fresh_session": True, "recheck_minutes": 10, "stall_minutes": 30}
EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
LEGACY_SECTIONS = {"workflow", "defaults", "presets", "review", "roles"}


def project_workflow_path(repo_path) -> Path:
    """The only workflow location Harbor reads for a project."""
    return Path(repo_path).expanduser() / WORKFLOW_DIR / "workflow.toml"


def validate_keys(data):
    legacy = sorted(set(data) & LEGACY_SECTIONS)
    if legacy:
        raise ValueError(f"unsupported workflow section(s) {', '.join(legacy)}; {MIGRATION_HINT}")
    for section, value in data.items():
        if section == "version":
            if value != 1:
                raise ValueError("unsupported workflow version")
        elif section in SECTIONS:
            if not isinstance(value, dict) or set(value) - SECTIONS[section]:
                raise ValueError(f"unknown {section} setting")
        elif section in ENTRY_FIELDS:
            if not isinstance(value, dict):
                raise ValueError(f"invalid {section}")
            for name, entry in value.items():
                if not isinstance(entry, dict) or set(entry) - ENTRY_FIELDS[section]:
                    raise ValueError(f"unknown {section}.{name} setting")
        else:
            raise ValueError(f"unknown workflow section: {section}")


def load_models(path: Path) -> dict:
    """Parse ``config.toml``: role models plus First Mate recovery settings."""
    if not path.is_file():
        raise ValueError(f"missing model config {path}; create it with [controller] model = \"...\"")
    data = tomllib.loads(path.read_text())
    unknown = set(data) - {*ROLES, "first_mate", "quota_fallback"}
    if unknown:
        raise ValueError(f"unknown config.toml section(s): {', '.join(sorted(unknown))}")
    from harbor.model_policy import validate_fallbacks
    roles = {}
    for name in ROLES:
        entry = data.get(name, {})
        if not isinstance(entry, dict) or set(entry) - ROLE_FIELDS:
            raise ValueError(f"unknown config.toml {name} setting")
        roles[name] = _role(name, entry, validate_fallbacks)
    if not roles["controller"].get("model"):
        raise ValueError(f"{path}: [controller] model is required")
    for name in ROLES[1:]:
        # A missing child-role model is never a blocker: inherit the controller.
        for key in ("model", "reasoning_effort"):
            roles[name].setdefault(key, roles["controller"].get(key))
        roles[name] = {k: v for k, v in roles[name].items() if v is not None}
    mate = data.get("first_mate", {})
    if not isinstance(mate, dict) or set(mate) - FIRST_MATE_FIELDS:
        raise ValueError("unknown config.toml first_mate setting")
    first_mate = {**FIRST_MATE_DEFAULTS, **_role("first_mate", mate, validate_fallbacks)}
    if not mate.get("model"):
        for key in ("model", "reasoning_effort", "fallback_models"):
            if key in roles["controller"]:
                first_mate[key] = roles["controller"][key]
    for key in ("max_recoveries", "recheck_minutes", "stall_minutes"):
        value = first_mate[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError(f"first_mate.{key} must be a nonnegative number")
    if type(first_mate["allow_fresh_session"]) is not bool:
        raise ValueError("first_mate.allow_fresh_session must be boolean")
    result = {"roles": roles, "first_mate": first_mate}
    if "quota_fallback" in data:
        # Only present when configured, so existing workflows keep their policy digest.
        result["quota_fallback"] = _quota_fallback(data["quota_fallback"])
    return result


FALLBACK_PROVIDERS = ("codex", "claude")
CLAUDE_EFFORTS = {"low", "medium", "high", "xhigh", "max"}
FALLBACK_KEYS = {"max_switches", "controller", "first_mate", *ROLES[1:]}


def _fallback_profile(name, entry, *, provider=None):
    """One validated ``[quota_fallback.<name>]`` table; child roles pass the controller's provider."""
    allowed = {"model", "reasoning_effort"} | ({"provider"} if provider is None else set())
    if not isinstance(entry, dict) or set(entry) - allowed:
        raise ValueError(f"unknown quota_fallback.{name} setting")
    provider = provider if provider is not None else entry.get("provider")
    if provider not in FALLBACK_PROVIDERS:
        raise ValueError(f"quota_fallback.{name}.provider must be one of {', '.join(FALLBACK_PROVIDERS)}")
    model, effort = entry.get("model"), entry.get("reasoning_effort")
    if not isinstance(model, str) or not model.strip():
        raise ValueError(f"quota_fallback.{name}.model must be a non-empty string")
    if provider == "claude" and model.startswith("gpt-"):
        raise ValueError(f"quota_fallback.{name}.model {model!r} is not a Claude model")
    if provider == "codex" and model.startswith("claude"):
        raise ValueError(f"quota_fallback.{name}.model {model!r} is not a Codex model")
    if effort is None and name == "controller":
        # A builder runner needs an effort valid for ITS provider; never borrow the primary's.
        raise ValueError("quota_fallback.controller.reasoning_effort is required")
    if effort is not None and effort not in (CLAUDE_EFFORTS if provider == "claude" else EFFORTS):
        raise ValueError(f"invalid quota_fallback.{name} reasoning effort")
    return {"provider": provider, "model": model, "reasoning_effort": effort}


def _quota_fallback(section):
    """Resolve ``[quota_fallback]``: a second provider profile used when the primary hits a usage limit."""
    if not isinstance(section, dict) or set(section) - FALLBACK_KEYS:
        raise ValueError("unknown quota_fallback setting")
    switches = section.get("max_switches", 2)
    if type(switches) is not int or switches < 0:
        raise ValueError("quota_fallback.max_switches must be a nonnegative integer")
    controller = section.get("controller")
    mate = section.get("first_mate")
    if controller is None and mate is None:
        raise ValueError("quota_fallback needs a controller or first_mate profile")
    roles = {}
    if controller is not None:
        controller = _fallback_profile("controller", controller)
        roles["controller"] = {k: controller[k] for k in ("model", "reasoning_effort")}
        for name in ROLES[1:]:
            child = _fallback_profile(name, section.get(name, {}) or {"model": controller["model"]}, provider=controller["provider"])
            child.pop("provider")
            if "reasoning_effort" not in section.get(name, {}):
                child["reasoning_effort"] = controller["reasoning_effort"]
            roles[name] = child
    elif any(name in section for name in ROLES[1:]):
        raise ValueError("quota_fallback child roles need a controller profile")
    return {
        "max_switches": switches,
        "controller": controller,
        "roles": roles,
        "first_mate": _fallback_profile("first_mate", mate) if mate is not None else None,
    }


def _role(name, entry, validate_fallbacks):
    result = dict(entry)
    if "model" in result and (not isinstance(result["model"], str) or not result["model"].strip()):
        raise ValueError(f"{name}.model must be a non-empty string")
    if result.get("reasoning_effort", "high") not in EFFORTS:
        raise ValueError(f"invalid {name} reasoning effort")
    if "fallback_models" in result:
        result["fallback_models"] = list(validate_fallbacks(result.get("model"), result["fallback_models"]))
    return result


@dataclass(frozen=True)
class Workflow:
    package: Path
    config: dict
    digest: str
    package_digest: str = ""

    @property
    def models_path(self) -> Path:
        return self.package / "config.toml"

    def changed(self) -> bool:
        """True when the project's copy was edited after this snapshot was loaded."""
        try:
            return package_hash(self.package) != self.package_digest
        except (OSError, ValueError):
            return True

    def reload(self) -> "Workflow":
        """Pick up edits on the next turn instead of failing the run."""
        return load_workflow(self.package / "workflow.toml") if self.changed() else self

    def asset(self, relative):
        path = (self.package / relative).resolve()
        if not path.is_relative_to(self.package) or not path.is_file():
            raise ValueError(f"missing or escaping package asset: {relative}")
        return path

    def prompt(self):
        skill = self.config["skills"]["delivery"]
        return (self.asset(skill["ref"]).read_text() + "\nEffective Harbor workflow policy (authoritative):\n"
                + json.dumps(self.config, sort_keys=True) + f"\nPolicy digest: {self.digest}\nWorkflow package: {self.package}\n"
                + f"Role models: read {self.models_path} (the `roles` above mirror it).\n"
                + "Managed execution: continue the supplied ticket and retained worktree even when it is no longer Todo. "
                "Do not select a different ticket. Run required hooks before advancing; disabled checks are not passes.")


def package_hash(package):
    digest = hashlib.sha256()
    for asset in sorted(package.rglob("*")):
        if asset.is_symlink():
            raise ValueError("workflow package must not contain symlinks")
        if asset.is_file() and "__pycache__" not in asset.parts and asset.name != ".DS_Store":
            digest.update(str(asset.relative_to(package)).encode())
            digest.update(asset.read_bytes())
    return digest.hexdigest()


def load_workflow(path: Path) -> Workflow:
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"missing workflow {path}; {MIGRATION_HINT}")
    package = path.parent
    data = tomllib.loads(path.read_text())
    validate_keys(data)
    if data.get("version") != 1:
        raise ValueError("workflow version=1 is required")
    data = {**data, **load_models(package / "config.toml")}
    for section in ("delivery", "recovery", "decisions", "cleanup", "skills"):
        if section not in data:
            raise ValueError(f"workflow.toml is missing [{section}]")
    if not isinstance(data["recovery"].get("enabled"), bool):
        raise ValueError("recovery.enabled must be explicit")
    if data["delivery"]["merge_mode"] not in {"human", "workflow_checked"}:
        raise ValueError("unsupported merge mode; enforced merging is not implemented")
    if data["cleanup"]["mode"] not in {"after_merge", "manual", "off"}:
        raise ValueError("invalid cleanup mode")
    if data["decisions"]["mode"] not in {"human", "cite_only", "bounded"}:
        raise ValueError("invalid decision mode")
    from harbor.auto_accept import validate_settings as validate_auto_accept
    validate_auto_accept(data["decisions"])
    for value in [data["cleanup"]["delay_seconds"], *data["recovery"]["delays_seconds"]]:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError("delays must be finite nonnegative numbers")
    if not data.get("skills", {}).get("delivery", {}).get("ref"):
        raise ValueError("delivery skill is required")
    for skill in data.get("skills",{}).values():
        if type(skill.get("enabled",True)) is not bool:
            raise ValueError("skill enabled must be boolean")
    if data["skills"]["delivery"].get("enabled",True) is not True:
        raise ValueError("delivery skill cannot be disabled")
    if not isinstance(data["delivery"].get("auto_merge_risks"),list) or any(r not in {"low","medium","high"} for r in data["delivery"]["auto_merge_risks"]):
        raise ValueError("invalid auto_merge_risks")
    for hook in data.get("hooks", {}).values():
        if hook.get("mode") not in {"required", "advisory", "off"} or hook.get("event") not in EVENTS:
            raise ValueError("invalid hook mode or event")
        if hook["mode"] == "off":
            continue
        if hook.get("executor") == "script":
            command = hook.get("command")
            if not isinstance(command, list) or not command or not all(isinstance(x, str) and x for x in command):
                raise ValueError("hook command must be an argument array")
            if hook.get("protocol") not in {"json", "exit_code"}:
                raise ValueError("invalid hook protocol")
        elif hook.get("executor") == "skill":
            if hook.get("skill") not in data.get("skills", {}) or not data["skills"][hook["skill"]].get("enabled",True):
                raise ValueError("missing hook skill")
        else:
            raise ValueError("invalid hook executor")
        timeout = hook.get("timeout_seconds", 60)
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("invalid hook timeout")
    for key in ("max_attempts",):
        value = data["recovery"][key]
        if type(value) is not int or not 0 <= value <= len(data["recovery"]["delays_seconds"]):
            raise ValueError("recovery max_attempts must fit configured delays")
    for value in data.get("evidence", {}).values():
        if value not in {"required", "advisory", "off"}:
            raise ValueError("invalid evidence mode")
    for state in data.get("states", {}).values():
        if not isinstance(state,list) or not all(isinstance(x,str) and x for x in state):
            raise ValueError("states must be string arrays")
    state_groups = [{s.casefold() for s in data.get("states",{}).get(group,[])} for group in ("ready","paused","terminal")]
    if any(state_groups[i] & state_groups[j] for i in range(3) for j in range(i+1,3)):
        raise ValueError("ready, paused and terminal states must not overlap")
    github_settings(data)
    device_proof_settings(data)
    deadline = data.get("execution",{}).get("deadline_minutes",90)
    if isinstance(deadline,bool) or not isinstance(deadline,(int,float)) or not math.isfinite(deadline) or deadline <= 0:
        raise ValueError("deadline must be positive and finite")
    checks = data.get("checks",{})
    for spec in checks.values():
        command = spec.get("command")
        if not isinstance(command,list) or not command or not all(isinstance(x,str) and x for x in command):
            raise ValueError("check command must be an argument array")
        if type(spec.get("required",True)) is not bool or not isinstance(spec.get("after",[]),list):
            raise ValueError("invalid check policy")
        if any(dep not in checks for dep in spec.get("after",[])):
            raise ValueError("unknown check dependency")
        timeout = spec.get("timeout_seconds",300)
        if isinstance(timeout,bool) or not isinstance(timeout,(int,float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("invalid check timeout")
    remaining = dict(checks)
    while remaining:
        ready = [n for n,v in remaining.items() if not any(d in remaining for d in v.get("after",[]))]
        if not ready:
            raise ValueError("check dependency cycle")
        for name in ready:
            del remaining[name]
    assets = package_hash(package)
    digest = hashlib.sha256((json.dumps(data, sort_keys=True) + assets).encode()).hexdigest()
    result = Workflow(package, data, digest, assets)
    for skill in data.get("skills", {}).values():
        if skill.get("enabled", True) and "ref" in skill:
            result.asset(skill["ref"])
    return result
