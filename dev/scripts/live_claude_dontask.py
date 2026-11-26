"""Live check: which ``harbor`` commands Claude Code allows a Harbor builder under ``dontAsk``.

Builds a throwaway project (registry, repo, ledger, worktree and shim state)
under ``.test-state/`` in this checkout, which is outside the system temporary
directories (Harbor refuses a shim directory there). Then it runs the real
``claude`` CLI with the exact ``--settings`` and environment Harbor gives a
builder, and asks it to run a fixed list of commands. It prints each command,
Claude Code's summary and the permission denials, then deletes the project
unless ``--keep`` is given.

Needs a logged-in ``claude`` on PATH and makes one small model call.

    uv run python dev/scripts/live_claude_dontask.py [--model claude-haiku-4-5] [--keep]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

COMMANDS = (
    "command -v harbor",
    "printenv HARBOR_BIN",
    "harbor workflow inspect",
    "SHIM workflow inspect",
    "harbor workflow check --ticket SAMPLE-1 --event pre_handoff",
    '"$HARBOR_BIN" workflow inspect',
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default="claude-haiku-4-5")
    parser.add_argument("--keep", action="store_true", help="keep the throwaway project")
    args = parser.parse_args()

    base = ROOT / ".test-state" / f"live-{uuid.uuid4().hex[:8]}"
    repo = base / "repo"
    try:
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        shutil.copytree(ROOT / "workflows" / "work-pipeline", repo / ".harbor" / "workflow")
        models = repo / ".harbor" / "workflow" / "config.toml"
        models.write_text(models.read_text().replace("REPLACE_WITH_MODEL", args.model))
        (base / "pipeline.md").write_text(
            "# Pipeline\nlinear_project_slug: sample\nlinear_team: SAMPLE\ninstall_command: none\n"
            f"builder_provider: claude\nworktree_root: {base}/wt\n")
        (base / "projects.md").write_text(
            "| name | repo_path | pipeline_path | linear_project_slug | linear_team | github_repository |\n"
            "| --- | --- | --- | --- | --- | --- |\n"
            f"| SampleProject | {repo} | {base}/pipeline.md | sample | SAMPLE | owner/repo |\n")
        os.environ.update(HARBOR_PROJECTS_FILE=str(base / "projects.md"),
                          HARBOR_DB_PATH=str(base / "state" / "harbor.db"),
                          XDG_STATE_HOME=str(base / "xdg-state"))

        from harbor import cli
        from harbor.config.projects import get_project
        from harbor.runner.claude import ClaudeRunner

        builder = cli._builder_environment(get_project("SampleProject"))
        builder.ensure_shim()
        worktree = base / "wt" / "SAMPLE-1"
        subprocess.run(["git", "init", "-q", "-b", "main", str(worktree)], check=True)
        runner = ClaudeRunner(worktree, model=args.model, reasoning_effort="low", executable="claude",
                              writable_roots=(str(base / "state"),))
        runner.allow_harbor = True
        runner.workflow_package = repo / ".harbor" / "workflow"
        runner.builder_env = builder.variables()
        argv = runner.command("x", run_id="live")
        settings = argv[argv.index("--settings") + 1]
        print("permissions.allow:", json.loads(settings)["permissions"]["allow"])
        steps = "\n".join(f"{n}. {c.replace('SHIM', str(builder.shim))}" for n, c in enumerate(COMMANDS, 1))
        prompt = ("Use the Bash tool to run each of these commands exactly as written, one per Bash call, in this "
                  "order, and do nothing else. If a command is denied, do not ask: go on to the next one.\n"
                  f"{steps}\nThen reply with one line per command: its number, ALLOWED or DENIED, and the first "
                  "60 characters of its output.")
        command = ["claude", "--print", "--verbose", "--output-format", "stream-json", "--model", args.model,
                   "--permission-mode", "dontAsk", "--tools", "Bash", "--setting-sources", "",
                   "--strict-mcp-config", "--settings", settings, prompt]
        result = subprocess.run(command, cwd=worktree, env=runner.environment(),
                                capture_output=True, text=True, timeout=300)
        for line in result.stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "assistant":
                for item in event["message"].get("content", []):
                    if item.get("type") == "tool_use":
                        print("TOOL_USE:", item["input"].get("command"))
            if event.get("type") == "result":
                print("RESULT:", event.get("result"))
                print("permission_denials:", json.dumps([d["tool_input"]["command"]
                                                         for d in event.get("permission_denials") or []]))
        print("claude exit", result.returncode)
        return result.returncode
    finally:
        if args.keep:
            print("kept:", base)
        else:
            shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
