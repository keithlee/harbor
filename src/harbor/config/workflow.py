"""Versioned, portable workflow configuration. No project-specific defaults."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import tomllib
from dataclasses import dataclass
from pathlib import Path

from harbor.resources import runtime_asset

EVENTS = {"setup", "blocker", "pre_handoff", "pre_merge", "pre_cleanup"}
SECTIONS = {
    "workflow": {"package", "preset"},
    "delivery": {"merge_mode", "auto_merge_risks"},
    "review": {"enabled", "require_independence", "max_failed_rounds"},
    "recovery": {"enabled", "delays_seconds", "max_attempts"},
    "decisions": {"mode", "require_citation", "require_rationale"},
    "cleanup": {"mode", "delay_seconds"},
    "execution": {"deadline_minutes"},
    "evidence": {"feedback", "risk", "proof", "ci"},
    "states": {"ready", "paused", "terminal"},
}
ENTRY_FIELDS = {
    "skills": {"ref", "enabled", "model", "reasoning_effort", "role", "fallback_models"},
    "roles": {"model", "reasoning_effort", "reuse", "fallback_models"},
    "hooks": {"event", "mode", "executor", "command", "protocol", "timeout_seconds", "skill"},
    "checks": {"command", "required", "timeout_seconds", "after"},
}


def merge(base, patch):
    result = copy.deepcopy(base)
    for key, value in patch.items():
        result[key] = merge(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) else copy.deepcopy(value)
    return result


def validate_keys(data):
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


@dataclass(frozen=True)
class Workflow:
    package: Path
    config: dict
    digest: str
    origins: dict
    package_digest: str = ""

    def verify_package(self):
        source = self.origins.get("overrides")
        if source and load_workflow(Path(source)).digest != self.digest:
            raise ValueError("workflow configuration changed; reconcile policy before continuing")
        if self.package_digest and package_hash(self.package) != self.package_digest:
            raise ValueError("workflow package changed; reconcile policy before continuing")

    def asset(self, relative):
        path = (self.package / relative).resolve()
        if not path.is_relative_to(self.package) or not path.is_file():
            raise ValueError(f"missing or escaping package asset: {relative}")
        return path

    def prompt(self):
        self.verify_package()
        skill = self.config["skills"]["delivery"]
        return (self.asset(skill["ref"]).read_text() + "\nEffective Harbor workflow policy (authoritative):\n"
                + json.dumps(self.config, sort_keys=True) + f"\nPolicy digest: {self.digest}\nTrusted workflow package: {self.package}\n"
                + "Managed execution: continue the supplied ticket and retained worktree even when it is no longer Todo. "
                "Do not select a different ticket. Run required hooks before advancing; disabled checks are not passes.")


def package_hash(package):
    digest = hashlib.sha256()
    for asset in sorted(package.rglob("*")):
        if asset.is_symlink():
            raise ValueError("workflow package must not contain symlinks")
        if asset.is_file() and "__pycache__" not in asset.parts:
            digest.update(str(asset.relative_to(package)).encode())
            digest.update(asset.read_bytes())
    return digest.hexdigest()


def load_workflow(path: Path) -> Workflow:
    path = path.resolve()
    overrides = tomllib.loads(path.read_text())
    validate_keys(overrides)
    if overrides.get("version") != 1:
        raise ValueError("workflow version=1 is required")
    selection = overrides.get("workflow", {})
    name = selection.get("package", "")
    if name.startswith("builtin:"):
        if name not in {"builtin:reference", "builtin:work-pipeline"}:
            raise ValueError("unknown bundled workflow")
        package = runtime_asset("workflows", name.split(":")[1] + "/workflow.toml").parent.resolve()
    elif name:
        package = (path.parent / name).resolve()
    else:
        raise ValueError("workflow.package is required")
    manifest = tomllib.loads((package / "workflow.toml").read_text())
    if manifest.get("version") != 1 or set(manifest) - {"version", "defaults", "presets"}:
        raise ValueError("invalid workflow manifest")
    preset = selection.get("preset")
    if not preset or preset not in manifest.get("presets", {}):
        raise ValueError("select an available workflow preset")
    defaults = manifest.get("defaults", {})
    validate_keys(defaults)
    validate_keys(manifest["presets"][preset])
    data = merge(merge(defaults, manifest["presets"][preset]), overrides)
    for group in ("recovery", "review"):
        if not isinstance(data.get(group, {}).get("enabled"), bool):
            raise ValueError(f"{group}.enabled must be explicit")
    if data["delivery"]["merge_mode"] not in {"human", "workflow_checked"}:
        raise ValueError("unsupported merge mode; enforced merging is not implemented")
    if data["cleanup"]["mode"] not in {"after_merge", "manual", "off"}:
        raise ValueError("invalid cleanup mode")
    if data["decisions"]["mode"] not in {"human", "cite_only", "bounded"}:
        raise ValueError("invalid decision mode")
    for value in [data["cleanup"]["delay_seconds"], *data["recovery"]["delays_seconds"]]:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError("delays must be finite nonnegative numbers")
    if data["review"]["enabled"] and not data.get("skills", {}).get("review", {}).get("enabled", False):
        raise ValueError("required review skill is disabled")
    if data["review"]["enabled"] and data["review"].get("require_independence") is not True:
        raise ValueError("bundled review requires independence; disable review or replace its validator")
    if not data["review"]["enabled"] and data["review"].get("require_independence"):
        raise ValueError("independence requires review")
    if not data.get("skills", {}).get("delivery", {}).get("ref"):
        raise ValueError("delivery skill is required")
    from harbor.model_policy import validate_fallbacks
    for entries in (data.get("roles",{}), data.get("skills",{})):
        for entry in entries.values():
            validate_fallbacks(entry.get("model"), entry.get("fallback_models", []))
    for name, role in data.get("roles",{}).items():
        if name not in {"controller","planner","reviewer","implementor"}:
            raise ValueError("unknown workflow role")
        if role.get("reuse") and (role["reuse"] not in data.get("roles",{}) or role["reuse"] == name or {role["reuse"],name} == {"reviewer","implementor"}):
            raise ValueError("invalid role reuse")
    for skill in data.get("skills",{}).values():
        if type(skill.get("enabled",True)) is not bool:
            raise ValueError("skill enabled must be boolean")
        if skill.get("reasoning_effort","high") not in {"none","minimal","low","medium","high","xhigh","max","ultra"}:
            raise ValueError("invalid skill reasoning effort")
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
    if type(data["review"]["max_failed_rounds"]) is not int or data["review"]["max_failed_rounds"] < 1:
        raise ValueError("review max_failed_rounds must be positive")
    for value in data.get("evidence", {}).values():
        if value not in {"required", "advisory", "off"}:
            raise ValueError("invalid evidence mode")
    for state in data.get("states", {}).values():
        if not isinstance(state,list) or not all(isinstance(x,str) and x for x in state):
            raise ValueError("states must be string arrays")
    state_groups = [{s.casefold() for s in data.get("states",{}).get(group,[])} for group in ("ready","paused","terminal")]
    if any(state_groups[i] & state_groups[j] for i in range(3) for j in range(i+1,3)):
        raise ValueError("ready, paused and terminal states must not overlap")
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
    result = Workflow(package, data, digest, {"package": str(package), "preset": preset, "overrides": str(path)}, assets)
    for skill in data.get("skills", {}).values():
        if skill.get("enabled", True) and "ref" in skill:
            result.asset(skill["ref"])
    return result
