"""Explicit, repository-local onboarding; never imports personal skills."""
import json
from pathlib import Path
import subprocess
import sys


def add_command(commands):
    cmd = commands.add_parser("init", help="configure a project with bundled skills")
    cmd.add_argument("project")
    cmd.add_argument("--repo", type=Path, default=Path.cwd())
    cmd.add_argument("--registry", type=Path, required=True)
    cmd.add_argument("--package", choices=["reference","work-pipeline"], default="reference")
    for name in ("preset","linear-project","linear-team","github-repository","model","install-command"):
        cmd.add_argument("--"+name)
    cmd.add_argument("--base-branch", default="main")
    cmd.add_argument("--fallback-model", action="append", default=[], help="Explicit model fallback for the controller and First Mate; repeat in priority order")
    cmd.add_argument("--required-check", action="append", default=[], help="Exact GitHub CI check name; repeat for each required check")
    cmd.add_argument("--test-command", help="JSON argument array, e.g. '[\"uv\",\"run\",\"pytest\"]'")


def initialize(args):
    from harbor.config.pipeline import parse_projects_markdown
    from harbor.config.projects import ProjectConfig
    fields = {"preset":"Preset (minimal/reviewed/autonomous)","linear_project":"Linear project slug or ID", "linear_team":"Linear team", "github_repository":"GitHub owner/repository", "model":"Agent model", "install_command":"Install command (or none)"}
    for key, label in fields.items():
        if not getattr(args,key,None) and sys.stdin.isatty():
            setattr(args,key,input(label+": ").strip())
        value = getattr(args,key,None)
        if not value or any(c in value for c in ('\n','\r','|')):
            raise ValueError(f"explicit --{key.replace('_','-')} is required")
    if args.preset not in {"minimal","reviewed","autonomous"}:
        raise ValueError("unknown preset")
    if not args.project or any(c in args.project for c in ('\n','\r','|','#')):
        raise ValueError("invalid project name")
    repo, registry = args.repo.expanduser().resolve(), args.registry.expanduser().resolve()
    check = subprocess.run(["git","-C",str(repo),"rev-parse","--show-toplevel"],capture_output=True,text=True)
    if check.returncode or Path(check.stdout.strip()).resolve() != repo:
        raise ValueError("repo must be a Git checkout root")
    folder = repo / ".harbor"
    pipeline, workflow = folder/"pipeline.md", folder/"workflow.toml"
    if folder.is_symlink() or pipeline.exists() or workflow.exists():
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
    text = f'version = 1\n[workflow]\npackage = "builtin:{args.package}"\npreset = {q(args.preset)}\n'
    for role in ("controller","planner","reviewer","implementor"):
        text += f'\n[roles.{role}]\nmodel = {q(args.model)}\nreasoning_effort = "high"\n'
    if fallbacks:
        # The controller role was already emitted; put its fallback beside its model.
        text = text.replace("[roles.controller]\n", "[roles.controller]\nfallback_models = " + q(fallbacks) + "\n")
        text += "\n[skills.blocker_assessment]\nfallback_models = " + q(fallbacks) + "\n"
    if command:
        text += f'\n[checks.tests]\ncommand = {q(command)}\nrequired = true\ntimeout_seconds = 600\n'
    required_checks = getattr(args, "required_check", [])
    if any(not name.strip() or any(c in name for c in ("\n", "\r", "|")) for name in required_checks):
        raise ValueError("required check names must be nonempty single-line names")
    profile = f"# Harbor pipeline\nlinear_project_slug: {args.linear_project}\nlinear_team: {args.linear_team}\ngithub_repository: {args.github_repository}\nbase_branch: {args.base_branch}\nworkflow_path: workflow.toml\ninstall_command: {args.install_command}\nbuilder_model: {args.model}\nfirstmate_model: {args.model}\nhold: true\n"
    if required_checks:
        profile += "required_checks: " + q(required_checks) + "\n"
    folder.mkdir(exist_ok=True)
    try:
        workflow.write_text(text)
        pipeline.write_text(profile)
        ProjectConfig.from_pipeline(args.project,pipeline)
    except Exception:
        pipeline.unlink(missing_ok=True)
        workflow.unlink(missing_ok=True)
        raise
    registry.parent.mkdir(parents=True,exist_ok=True)
    # Registry registration follows successful config validation.
    entry = f"\n## {args.project}\nrepo_path: {repo}\npipeline_path: {pipeline}\n"
    registry.write_text(previous+entry)
    print(json.dumps({"project":args.project,"registry":str(registry),"pipeline":str(pipeline),"workflow":str(workflow),"hold":True,"warnings":([] if required_checks else ["Required CI names are missing: set required_checks in pipeline.md before releasing hold, or explicitly configure evidence.ci off."]),"next":f"Set HARBOR_PROJECTS_FILE to the registry, run harbor doctor {args.project}, then release hold explicitly."},indent=2))
    return 0
