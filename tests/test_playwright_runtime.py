import asyncio
from contextlib import asynccontextmanager

import pytest

from harbor.config.projects import ProjectConfig
from harbor.runner.codex import CodexRunner
from harbor.runner.playwright import BrowserUnavailable, ENDPOINT_ENV, REQUIRED_ENV, browser_endpoint


IMAGE = "sha256:" + "1" * 64


def test_project_playwright_image_is_opt_in_and_must_be_pinned(tmp_path):
    profile = tmp_path / ".harbor" / "pipeline.md"
    profile.parent.mkdir()
    settings = "linear_project_slug: sample\nlinear_team: SAM\n"
    profile.write_text(settings)
    assert ProjectConfig.from_pipeline("Sample", profile).playwright_browser_image is None
    profile.write_text(settings + f"playwright_browser_image: {IMAGE}\n")
    assert ProjectConfig.from_pipeline("Sample", profile).playwright_browser_image == IMAGE
    profile.write_text(settings + "playwright_browser_image: example/browser:latest\n")
    with pytest.raises(ValueError, match="digest-pinned"):
        ProjectConfig.from_pipeline("Sample", profile)


@pytest.mark.parametrize("ending", ["success", "failure", "cancellation"])
def test_docker_browser_uses_loopback_and_cleans_up(monkeypatch, ending):
    calls = []

    async def docker(*args):
        calls.append(args)
        return "127.0.0.1:30303" if args[0] == "port" else ""

    async def waiting(port, ws_path):
        assert port == 30303
        assert ws_path.startswith("/")

    monkeypatch.setattr("harbor.runner.playwright._docker", docker)
    monkeypatch.setattr("harbor.runner.playwright._wait_until_listening", waiting)

    async def scenario():
        async with browser_endpoint(IMAGE, lifetime_seconds=120) as (endpoint, reason):
            assert endpoint.startswith("ws://127.0.0.1:30303/")
            assert reason == ""
            if ending == "failure":
                raise RuntimeError("builder failed")
            if ending == "cancellation":
                raise asyncio.CancelledError

    if ending == "success":
        asyncio.run(scenario())
    else:
        with pytest.raises(RuntimeError if ending == "failure" else asyncio.CancelledError):
            asyncio.run(scenario())
    launch = calls[2]
    assert launch[0] == "run" and "--pull=never" in launch
    assert launch[launch.index("--publish") + 1] == "127.0.0.1::3000"
    assert calls[-1] == ("rm", "--force", launch[launch.index("--name") + 1])


def test_unavailable_runtime_reports_docker_error(monkeypatch):
    async def docker(*args):
        assert args[0] == "info"
        raise BrowserUnavailable("Docker is unavailable (start the local Docker runtime)")

    monkeypatch.setattr("harbor.runner.playwright._docker", docker)

    async def scenario():
        async with browser_endpoint(IMAGE, lifetime_seconds=120) as (endpoint, reason):
            assert endpoint == ""
            assert "start the local Docker runtime" in reason

    asyncio.run(scenario())


def test_linked_worktree_profile_grants_exact_git_index_directory(tmp_path):
    common_git = tmp_path / "parent repo" / ".git"
    admin = common_git / "worktrees" / "current"
    admin.mkdir(parents=True)
    worktree = tmp_path / "linked"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {admin}\n")
    runner = CodexRunner(worktree, playwright_browser_image=IMAGE)
    command = runner.command("capture", run_id="existing", resume=True)
    assert f'permissions.harbor-playwright.filesystem={{"{common_git}"="write", "{admin}"="write"}}' in command
    assert f'"{worktree / ".git"}"' not in " ".join(command)


def test_builder_refreshes_browser_without_changing_session_or_workspace(monkeypatch, tmp_path):
    opened = []
    observed = []

    @asynccontextmanager
    async def runtime(image, *, lifetime_seconds):
        assert image == IMAGE
        endpoint = f"ws://127.0.0.1:{31000 + len(opened)}/private"
        opened.append(endpoint)
        yield endpoint, ""

    async def execute(self, context, instruction, *, resume):
        env = self.environment()
        observed.append((context, resume, env[ENDPOINT_ENV]))
        assert env[REQUIRED_ENV] == "1"
        command = self.command(instruction, run_id="same-session", resume=resume)
        assert 'default_permissions="harbor-playwright"' in command
        assert "-s" not in command
        assert f'permissions.harbor-playwright.filesystem={{"{tmp_path}"="write"}}' in command
        assert opened[-1] not in instruction and opened[-1] not in command
        if resume:
            assert command[1:4] == ["exec", "resume", "same-session"]

    monkeypatch.setenv(ENDPOINT_ENV, "stale")
    monkeypatch.setattr("harbor.runner.codex.browser_endpoint", runtime)
    monkeypatch.setattr(CodexRunner, "_execute_with_model_fallback", execute)
    runner = CodexRunner(tmp_path, writable_roots=(tmp_path,), playwright_browser_image=IMAGE)

    async def scenario():
        context = runner._new_context("same-run")
        await runner._execute(context, "capture", resume=False)
        await runner._execute(context, "continue", resume=True)
        assert observed == [(context, False, opened[0]), (context, True, opened[1])]
        assert ENDPOINT_ENV not in runner.environment()
        assert ENDPOINT_ENV not in CodexRunner(tmp_path).environment()

    asyncio.run(scenario())
