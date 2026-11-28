"""Explicit, repository-local onboarding; never imports personal skills.

Copies the bundled work-pipeline into ``<repo>/.harbor/workflow`` so each
project owns (and version-controls) its dev workflow.
"""
import json
from pathlib import Path
import shutil
import sys

from harbor.safe_git import run_git


def add_command(commands):
    cmd = commands.add_parser("init", help="configure a project with a copy of the work-pipeline")
    cmd.add_argument("project")
    cmd.add_argument("--repo", type=Path, default=Path.cwd())
    cmd.add_argument("--registry", type=Path, required=True)
    for name in ("linear-project","linear-team","github-repository","model","install-command"):
        cmd.add_argument("--"+name)
    cmd.add_argument("--base-branch", default="main")
    cmd.add_argument("--fallback-model", action="append", default=[], help="Explicit model fallback for the controller and First Mate; repeat in priority order")
    cmd.add_argument("--required-check", action="append", default=[], help="Exact GitHub CI check name; repeat for each required check")
    cmd.add_argument("--test-command", help="JSON argument array, e.g. '[\"uv\",\"run\",\"pytest\"]'")


def initialize(args):
    from harbor.config.pipeline import parse_projects_markdown
    from harbor.config.projects import ProjectConfig
    fields = {"linear_project":"Linear project slug or ID", "linear_team":"Linear team", "github_repository":"GitHub owner/repository", "model":"Agent model", "install_command":"Install command (or none)"}
    for key, label in fields.items():
        if not getattr(args,key,None) and sys.stdin.isatty():
            setattr(args,key,input(label+": ").strip())
        value = getattr(args,key,None)
        if not value or any(c in value for c in ('\n','\r','|')):
            raise ValueError(f"explicit --{key.replace('_','-')} is required")
    if not args.project or any(c in args.project for c in ('\n','\r','|','#')):
        raise ValueError("invalid project name")
    repo, registry = args.repo.expanduser().resolve(), args.registry.expanduser().resolve()
    check = run_git(repo,"rev-parse","--show-toplevel")
    if check.returncode or Path(check.stdout.strip()).resolve() != repo:
        raise ValueError("repo must be a Git checkout root")
    folder = repo / ".harbor"
    pipeline, package, legacy = folder/"pipeline.md", folder/"workflow", folder/"workflow.toml"
    if folder.is_symlink() or pipeline.exists() or package.exists() or legacy.exists():
        raise ValueError("existing project configuration retained; edit it explicitly")
    previous = registry.read_text() if registry.exists() else "# Harbor projects\n"
    if any(p.name.casefold() == args.project.casefold() for p in parse_projects_markdown(previous, source=registry)):
        raise ValueError("project already registered")
    if any(line.lstrip().startswith("|") for line in previous.splitlines()):
        raise ValueError("table registry retained; choose a new heading-format registry or add this project manually")
    command = json.loads(args.test_command) if args.test_command else None
    if command is not None and (not isinstance(command,list) or not command or not all(isinstance(x,str) and x for x in command)):
        raise ValueError("test command must be a nonempty JSON argument array")
    from harbor.model_policy import validate_fallbacks
    fallbacks = validate_fallbacks(args.model, getattr(args,"fallback_model",[]))
    q = json.dumps
    models = f'[controller]\nmodel = {q(args.model)}\nreasoning_effort = "medium"\n'
    if fallbacks:
        models += "fallback_models = " + q(list(fallbacks)) + "\n"
    models += "\n[planner]\n[reviewer]\n[implementor]\n\n[first_mate]\nmax_recoveries = 3\nallow_fresh_session = true\nrecheck_minutes = 10\nstall_minutes = 30\n"
    checks = f'\n[checks.tests]\ncommand = {q(command)}\nrequired = true\ntimeout_seconds = 600\n' if command else ""
    required_checks = getattr(args, "required_check", [])
    if any(not name.strip() or any(c in name for c in ("\n", "\r", "|")) for name in required_checks):
        raise ValueError("required check names must be nonempty single-line names")
    profile = f"# Harbor pipeline\nlinear_project_slug: {args.linear_project}\nlinear_team: {args.linear_team}\ngithub_repository: {args.github_repository}\nbase_branch: {args.base_branch}\ninstall_command: {args.install_command}\nhold: true\n"
    if required_checks:
        profile += "required_checks: " + q(required_checks) + "\n"
    from harbor.resources import runtime_asset
    source = runtime_asset("workflows", "work-pipeline/workflow.toml").parent
    folder.mkdir(exist_ok=True)
    try:
        shutil.copytree(source, package, ignore=shutil.ignore_patterns("__pycache__", ".DS_Store"))
        (package / "config.toml").write_text(models)
        if checks:
            with (package / "workflow.toml").open("a") as handle:
                handle.write(checks)
        pipeline.write_text(profile)
        ProjectConfig.from_pipeline(args.project,pipeline)
    except Exception:
        pipeline.unlink(missing_ok=True)
        shutil.rmtree(package, ignore_errors=True)
        raise
    registry.parent.mkdir(parents=True,exist_ok=True)
    # Registry registration follows successful config validation.
    entry = f"\n## {args.project}\nrepo_path: {repo}\npipeline_path: {pipeline}\n"
    registry.write_text(previous+entry)
    print(json.dumps({"project":args.project,"registry":str(registry),"pipeline":str(pipeline),"workflow":str(package),"models":str(package/"config.toml"),"hold":True,"warnings":([] if required_checks else ["Required CI names are missing: set required_checks in pipeline.md before releasing hold, or explicitly configure evidence.ci off."]),"next":f"Commit .harbor/workflow/ (your editable work-pipeline), set HARBOR_PROJECTS_FILE to the registry, run harbor doctor {args.project}, then release hold explicitly."},indent=2))
    return 0
