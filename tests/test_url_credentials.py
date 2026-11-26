"""API URLs never carry credentials, and HTTP errors never repeat them.

Regression: ``require_https_api_url`` accepted ``https://user:SECRET@host/...``.
httpx repeats the full request URL in its error messages (``Server error '502
Bad Gateway' for url 'https://user:SECRET@host/graphql'``), so a GitHub or
Linear endpoint with userinfo put the secret into tracebacks and ``Harbor:``
error lines. All values here are dummies.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from harbor.clients.github import GitHubClient, GitHubError
from harbor.clients.linear import LinearClient, LinearClientError
from harbor.config.projects import ProjectConfig
from harbor.github_rest import Client, require_https_api_url, rest_base_url
from harbor.poll.github_poller import github_web_host
from harbor.url_credentials import has_userinfo, redact_userinfo

SECRET = "DUMMYSECRET0000"
GITHUB_WITH_USERINFO = [
    f"https://u:{SECRET}@ghes.example.test/api/graphql",
    f"https://{SECRET}@ghes.example.test/api/graphql",
    f"https://u:{SECRET}@api.github.com/graphql",
    f"https://u:{SECRET}@[2001:db8::1]:8443/api/graphql",
    # NFKC turns these into '@', and urlsplit and httpx normalize them.
    f"https://u:{SECRET}＠ghes.example.test/api/graphql",
    f"https://u:{SECRET}﹫ghes.example.test/api/graphql",
]
UPPERCASE_WITH_USERINFO = f"HTTPS://u:{SECRET}@ghes.example.test/api/graphql"
LINEAR_WITH_USERINFO = f"https://u:{SECRET}@linear.example.test/graphql"
LINEAR_FULLWIDTH = f"https://u:{SECRET}＠linear.example.test/graphql"


def _chain(exc: BaseException) -> list[BaseException]:
    """``exc`` and every exception reachable through ``__cause__`` and ``__context__``."""

    seen: list[BaseException] = []
    pending: list[BaseException | None] = [exc]
    while pending:
        current = pending.pop()
        if current is None or any(current is item for item in seen):
            continue
        seen.append(current)
        pending.extend((current.__cause__, current.__context__))
    return seen


def _assert_no_secret_anywhere(exc: BaseException) -> None:
    """Not in the message, the formatted traceback, or anything the chain holds."""

    assert SECRET not in str(exc) + "".join(traceback.format_exception(exc))
    for item in _chain(exc):
        assert not isinstance(item, httpx.HTTPError), f"the httpx error is still reachable: {item!r}"
        assert SECRET not in repr(item) + repr(vars(item))


# ------------------------------------------------------------ detection and redaction


@pytest.mark.parametrize("url", [
    *GITHUB_WITH_USERINFO,
    UPPERCASE_WITH_USERINFO,
    "https://@h.example.test/x",
    "  https://u:p@h.example.test  ",
    f"\x00https://u:{SECRET}@h.example.test/g",
    f"https://u:pa'{SECRET}@h.example.test/g",
    f"http://u:{SECRET}@[::1]/g",
])
def test_userinfo_is_detected(url: str) -> None:
    assert has_userinfo(url)


@pytest.mark.parametrize("url", [
    "https://api.github.com/graphql",
    "https://[2001:db8::1]:8443/api/graphql",
    "HTTPS://ghes.example.test/api/graphql",
    "https://ghes.example.test/api/graphql?owner=a@b",
    "https://ghes.example.test/users/a@b",
    "https://ghes.example.test#a@b",
    "",
    None,
])
def test_an_at_sign_outside_the_authority_is_not_userinfo(url: str | None) -> None:
    assert not has_userinfo(url)


def test_redaction_removes_userinfo_from_every_url_in_a_message() -> None:
    text = (f"Server error '502 Bad Gateway' for url 'https://u:{SECRET}@h.example/graphql'\n"
            f"retry https://{SECRET}@other.example/x and https://plain.example/a@b")
    redacted = redact_userinfo(text)
    assert SECRET not in redacted
    assert "'https://[redacted]@h.example/graphql'" in redacted
    assert "https://[redacted]@other.example/x" in redacted
    assert "https://plain.example/a@b" in redacted


@pytest.mark.parametrize(("text", "expected"), [
    # An unencoded '@' in the password: everything up to the last '@' goes.
    (f"for url 'https://u:{SECRET}@x@h.example/g'", "for url 'https://[redacted]@h.example/g'"),
    # httpx leaves a quote in userinfo unencoded; it does not end the userinfo.
    (f"for url 'https://u:pa'{SECRET}@h.example/g'", "for url 'https://[redacted]@h.example/g'"),
    (f"for url 'https://u:{SECRET}@[2001:db8::1]:8443/g'", "for url 'https://[redacted]@[2001:db8::1]:8443/g'"),
    (f"for url 'HTTPS://u:{SECRET}@h.example/g'", "for url 'HTTPS://[redacted]@h.example/g'"),
    (f"for url 'https://u:{SECRET}\uff20h.example/g'", "for url 'https://[redacted]@h.example/g'"),
    (f"for url 'https://u:{SECRET}\ufe6bh.example/g'", "for url 'https://[redacted]@h.example/g'"),
    ("for url 'https://h.example/g' by x@y", "for url 'https://h.example/g' by x@y"),
])
def test_redaction_forms(text: str, expected: str) -> None:
    assert redact_userinfo(text) == expected


@pytest.mark.parametrize("text", [
    "a" * 200_000,
    "a+" * 100_000,
    "a://" * 50_000,
    "https://" + "x" * 200_000,
    ("https://" + "x" * 1_000 + " ") * 200,
], ids=["letters", "scheme-chars", "repeated-schemes", "one-long-authority", "many-authorities"])
def test_redaction_is_linear_on_large_input(text: str) -> None:
    import time

    started = time.perf_counter()
    redact_userinfo(text)
    assert time.perf_counter() - started < 1.0


# ------------------------------------------------------------ fail closed at config load


@pytest.mark.parametrize("url", GITHUB_WITH_USERINFO)
def test_require_https_api_url_refuses_userinfo_without_repeating_it(url: str) -> None:
    with pytest.raises(ValueError, match="must not contain a user name, password or token") as caught:
        require_https_api_url(url)
    assert SECRET not in str(caught.value)
    assert "GITHUB_TOKEN" in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__context__ is None


def test_an_uppercase_scheme_is_refused_by_the_userinfo_check() -> None:
    # require_https_api_url refuses "HTTPS://" for its scheme first, so go
    # through the checks that accept any scheme case: the userinfo rule must
    # catch it on its own.
    from harbor.url_credentials import reject_userinfo

    with pytest.raises(ValueError, match="must not contain a user name, password or token"):
        reject_userinfo(UPPERCASE_WITH_USERINFO, "GitHub API", "GITHUB_TOKEN")
    for field in ("linear_api_url", "github_api_url"):
        with pytest.raises(ValueError, match="must not contain a user name, password or token") as caught:
            ProjectConfig(name="SampleProject", linear_project_slug="sample", **{field: UPPERCASE_WITH_USERINFO})
        assert SECRET not in str(caught.value)


@pytest.mark.parametrize("url", [
    "https://hé.example.test/graphql",
    "https://h.example.test／u/graphql",
])
def test_a_non_ascii_authority_is_refused(url: str) -> None:
    from harbor.url_credentials import reject_userinfo

    with pytest.raises(ValueError, match="host must be ASCII"):
        reject_userinfo(url, "Linear API", "LINEAR_API_KEY")


def test_a_url_urlsplit_refuses_raises_without_its_message_or_chain() -> None:
    with pytest.raises(ValueError, match="the GitHub API URL must name a host") as caught:
        require_https_api_url("https://[bad/graphql")
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    with pytest.raises(ValueError, match="must be a URL with a host name") as caught:
        github_web_host("https://[bad/graphql")
    assert caught.value.__cause__ is None and caught.value.__context__ is None


@pytest.mark.parametrize("url", GITHUB_WITH_USERINFO)
def test_the_builder_rest_helper_refuses_userinfo(url: str) -> None:
    with pytest.raises(ValueError, match="must not contain") as caught:
        rest_base_url(url)
    assert SECRET not in str(caught.value)
    with pytest.raises(ValueError, match="must not contain"):
        Client({"HARBOR_GITHUB_API_URL": url, "GITHUB_TOKEN": "dummy-token"})


@pytest.mark.parametrize("url", [
    LINEAR_WITH_USERINFO,
    LINEAR_FULLWIDTH,
    f"HTTP://u:{SECRET}@127.0.0.1:8765/graphql",
    f"http://u:{SECRET}@[::1]:8765/graphql",
])
def test_project_config_refuses_a_linear_url_with_userinfo(url: str) -> None:
    with pytest.raises(ValueError, match="Linear API URL must not contain a user name") as caught:
        ProjectConfig(name="SampleProject", linear_project_slug="sample", linear_api_url=url)
    assert SECRET not in str(caught.value)
    assert "LINEAR_API_KEY" in str(caught.value)


@pytest.mark.parametrize("url", GITHUB_WITH_USERINFO)
def test_project_config_refuses_a_registry_github_url_with_userinfo(url: str) -> None:
    with pytest.raises(ValueError, match="GitHub API URL must not contain a user name") as caught:
        ProjectConfig(name="SampleProject", linear_project_slug="sample", github_api_url=url)
    assert SECRET not in str(caught.value)


@pytest.mark.parametrize("field", ["linear_api_url", "github_api_url"])
@pytest.mark.parametrize("url", [
    f"\x00https://u:{SECRET}@h.example.test/g",
    "https://h.example.test/g\x00",
    "https://h.exa\x01mple.test/g",
    "https://h.example.test/g\x7f",
])
def test_project_config_refuses_control_characters(field: str, url: str) -> None:
    with pytest.raises(ValueError, match="must not contain control characters") as caught:
        ProjectConfig(name="SampleProject", linear_project_slug="sample", **{field: url})
    assert SECRET not in str(caught.value)


def test_project_config_still_accepts_plain_endpoints() -> None:
    config = ProjectConfig(
        name="SampleProject", linear_project_slug="sample",
        linear_api_url="http://127.0.0.1:8765/graphql", github_api_url="https://[2001:db8::1]:8443/api/graphql",
    )
    assert config.linear_api_url == "http://127.0.0.1:8765/graphql"


@pytest.mark.parametrize("url", [*GITHUB_WITH_USERINFO, f"http://u:{SECRET}@[::1]/api/graphql"])
def test_the_poller_web_host_uses_the_same_userinfo_rule(url: str) -> None:
    with pytest.raises(ValueError, match="must be a URL with a host name") as caught:
        github_web_host(url)
    _assert_no_secret_anywhere(caught.value)


# ------------------------------------------------------------ harbor doctor and harbor start


def _doctor(tmp_path: Path, provider: str, github_api_url: str | None, linear_api_url: str = "https://linear.invalid/graphql",
            *, registry: dict[str, str] | None = None):
    from test_github_transport import run_doctor

    result = run_doctor(tmp_path, provider, github_api_url, linear_api_url, registry=registry)
    assert SECRET not in result.stdout + result.stderr, result.stdout + result.stderr
    return result


def test_doctor_fails_closed_on_a_pipeline_linear_url_with_userinfo(tmp_path: Path) -> None:
    result = _doctor(tmp_path, "codex", None, LINEAR_WITH_USERINFO)
    assert "Harbor: the Linear API URL must not contain a user name, password or token" in result.stderr, result.stderr
    assert "LINEAR_API_KEY or HARBOR_LINEAR_API_KEY" in result.stderr
    assert result.returncode == 2


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_doctor_fails_closed_on_a_pipeline_github_url_with_userinfo(tmp_path: Path, provider: str) -> None:
    result = _doctor(tmp_path, provider, GITHUB_WITH_USERINFO[0])
    assert "GitHub transport: invalid (the GitHub API URL must not contain" in result.stdout, result.stdout
    assert result.returncode == 1


def test_doctor_fails_closed_on_a_registry_github_url_with_userinfo(tmp_path: Path) -> None:
    result = _doctor(tmp_path, "codex", None, registry={"github_api_url": GITHUB_WITH_USERINFO[0]})
    assert "Harbor: the GitHub API URL must not contain" in result.stderr, result.stderr
    assert result.returncode == 2


def _cli(tmp_path: Path, pipeline_extra: str, *command: str) -> subprocess.CompletedProcess[str]:
    """Run the real CLI on a throwaway registry, so the project goes through ProjectConfig."""

    from workflow_support import materialize

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    materialize(repo)
    pipeline = tmp_path / "pipeline.md"
    pipeline.write_text("# Pipeline\nlinear_project_slug: sample\nlinear_team: SAMPLE\ninstall_command: none\n" + pipeline_extra)
    projects = tmp_path / "projects.md"
    projects.write_text(
        "| name | repo_path | pipeline_path | linear_project_slug | linear_team | github_repository |\n"
        "| --- | --- | --- | --- | --- | --- |\n"
        f"| SampleProject | {repo} | {pipeline} | sample | SAMPLE | owner/repo |\n"
    )
    env = {k: v for k, v in os.environ.items() if k not in ("LINEAR_API_KEY", "HARBOR_LINEAR_API_KEY")}
    env.update(HARBOR_PROJECTS_FILE=str(projects), HARBOR_DB_PATH=str(tmp_path / "harbor.db"), GITHUB_TOKEN="dummy")
    result = subprocess.run([sys.executable, "-c", "import sys; from harbor.cli import main; sys.exit(main())",
                             *command], env=env, capture_output=True, text=True, timeout=60)
    assert SECRET not in result.stdout + result.stderr, result.stdout + result.stderr
    assert "Traceback" not in result.stderr, result.stderr
    assert result.returncode == 2, result.stdout + result.stderr
    return result


GITHUB_REFUSAL = (
    "Harbor: the GitHub API URL must not contain a user name, password or token (the part before '@'); "
    "remove it and supply the credential through GITHUB_TOKEN, GH_TOKEN or the variable named by github_token_env"
)
START = ("start", "SampleProject", "--once")
FACTS = ("facts", "SampleProject")
DOCTOR = ("doctor", "SampleProject")


@pytest.mark.parametrize("command", [START, FACTS], ids=["start", "facts"])
@pytest.mark.parametrize("url", [GITHUB_WITH_USERINFO[0], GITHUB_WITH_USERINFO[-2]], ids=["ascii-at", "fullwidth-at"])
def test_start_and_facts_refuse_a_pipeline_github_url_with_userinfo(tmp_path: Path, command, url: str) -> None:
    result = _cli(tmp_path, f"github_api_url: {url}\n", *command)
    assert result.stderr.strip() == GITHUB_REFUSAL, result.stderr


@pytest.mark.parametrize("command", [START, FACTS, DOCTOR], ids=["start", "facts", "doctor"])
@pytest.mark.parametrize("url", [LINEAR_WITH_USERINFO, LINEAR_FULLWIDTH], ids=["ascii-at", "fullwidth-at"])
def test_every_command_refuses_a_pipeline_linear_url_with_userinfo(tmp_path: Path, command, url: str) -> None:
    result = _cli(tmp_path, f"linear_api_url: {url}\n", *command)
    assert result.stderr.strip().startswith("Harbor: the Linear API URL must not contain a user name"), result.stderr
    assert len(result.stderr.strip().splitlines()) == 1, result.stderr


# ------------------------------------------------------------ harbor workflow check


class _Linear:
    def __init__(self, **kwargs):
        pass

    async def fetch_blocker_context(self, ticket):
        return {
            "identifier": ticket,
            "project": {"slugId": "canonical"},
            "description": "Risk: low",
            "state": {"name": "In Progress", "type": "started"},
            "attachments": {"nodes": [{"sourceType": "github", "url": "https://github.com/example/repo/pull/1"}]},
        }

    async def close(self):
        pass


def _workflow_check(tmp_path: Path, monkeypatch, capsys, handler, github_api_url: str) -> tuple[int, str]:
    import harbor.cli as cli
    import harbor.clients.github as github_module
    from harbor.db import HarborDB
    from harbor.wake import apply_phase3_migration
    from workflow_support import config

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        github_module.httpx, "AsyncClient",
        lambda *args, **kwargs: real_async_client(*args, transport=httpx.MockTransport(handler), **kwargs),
    )
    monkeypatch.setattr("harbor.clients.linear.LinearClient", _Linear)
    monkeypatch.setenv("GITHUB_TOKEN", "dummy-test-token")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    pipeline = tmp_path / "pipeline.md"
    pipeline.write_text("```yaml\nrepo: example/repo\n```\n", encoding="utf-8")
    cfg = SimpleNamespace(
        name="A", linear_api_url="https://linear.invalid", linear_project_scope=("canonical",),
        github_repository="example/repo", github_api_url=github_api_url, required_checks=("tests",),
        pipeline_path=str(pipeline), workflow_path=config(tmp_path, "autonomous"),
    )
    db = HarborDB(":memory:")
    apply_phase3_migration(db)
    db.replace_tickets("A", [{"identifier": "A-1", "state": "In Progress", "owned": True, "hold": False}])
    with db.connection:
        db.connection.execute("INSERT INTO runs (id, ticket, worktree, status) VALUES (?, ?, ?, ?)",
                              ("run", "A-1", str(tmp_path), "running"))
    monkeypatch.setattr(cli, "HarborDB", lambda path: db)
    monkeypatch.setattr(cli, "get_project", lambda name: cfg)
    exit_code = cli.main(["workflow", "check", "A", "--ticket", "A-1", "--event", "pre_merge"])
    err = capsys.readouterr().err
    assert SECRET not in err and "Traceback" not in err, err
    assert len(err.strip().splitlines()) == 1, err
    return exit_code, err.strip()


def test_workflow_check_is_one_line_exit_2_for_a_non_json_200(tmp_path: Path, monkeypatch, capsys) -> None:
    code, line = _workflow_check(
        tmp_path, monkeypatch, capsys,
        lambda request: httpx.Response(200, text="<html>SSO login</html>"), "https://api.github.com/graphql",
    )
    assert code == 2
    assert line.startswith("Harbor: GitHub "), line
    assert "GitHub returned a response that is not JSON" in line, line


def test_workflow_check_refuses_a_github_url_with_userinfo(tmp_path: Path, monkeypatch, capsys) -> None:
    def never(request):
        raise AssertionError("no request may be sent to a credential-bearing endpoint")

    code, line = _workflow_check(tmp_path, monkeypatch, capsys, never, GITHUB_WITH_USERINFO[2])
    assert code == 2
    assert line.startswith("Harbor: the GitHub API URL must not contain a user name"), line


# ------------------------------------------------------------ defence in depth: client errors


def _bad_gateway(request: httpx.Request) -> httpx.Response:
    return httpx.Response(502, request=request)


def _unreachable(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("All connection attempts failed", request=request)


@pytest.mark.parametrize(("endpoint", "shown"), [
    (GITHUB_WITH_USERINFO[0], "https://[redacted]@ghes.example.test/api/graphql"),
    (f"https://u:pa'{SECRET}@ghes.example.test/api/graphql", "https://[redacted]@ghes.example.test/api/graphql"),
    (f"https://u:{SECRET}@[2001:db8::1]:8443/api/graphql", "https://[redacted]@[2001:db8::1]:8443/api/graphql"),
])
def test_a_github_http_error_never_repeats_userinfo(endpoint: str, shown: str) -> None:
    client = GitHubClient(
        token="dummy-token", endpoint=endpoint,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(_bad_gateway)),
    )
    with pytest.raises(GitHubError, match="502 Bad Gateway") as caught:
        asyncio.run(client.query("query { viewer { login } }"))
    _assert_no_secret_anywhere(caught.value)
    assert f"for url '{shown}'" in str(caught.value), str(caught.value)


@pytest.mark.parametrize("handler", [_bad_gateway, _unreachable], ids=["http-502", "unreachable"])
def test_the_default_github_http_client_drops_the_httpx_error(monkeypatch, handler) -> None:
    real = httpx.AsyncClient
    monkeypatch.setattr(
        "harbor.clients.github.httpx.AsyncClient",
        lambda *args, **kwargs: real(*args, transport=httpx.MockTransport(handler), **kwargs),
    )
    client = GitHubClient(token="dummy-token", endpoint=GITHUB_WITH_USERINFO[0])
    with pytest.raises(GitHubError, match="GitHub request failed") as caught:
        asyncio.run(client.query("query { viewer { login } }"))
    # A connection error's message has no URL, but exc.request.url does.
    _assert_no_secret_anywhere(caught.value)
    assert caught.value.__cause__ is None and caught.value.__context__ is None


@pytest.mark.parametrize("endpoint", [LINEAR_WITH_USERINFO, f"https://u:pa'{SECRET}@linear.example.test/graphql"])
@pytest.mark.parametrize("handler", [_bad_gateway, _unreachable], ids=["http-502", "unreachable"])
def test_a_linear_http_error_never_repeats_userinfo(endpoint: str, handler) -> None:
    client = LinearClient(token="dummy-key", endpoint=endpoint, transport=httpx.MockTransport(handler))
    with pytest.raises(LinearClientError, match="Linear request failed") as caught:
        asyncio.run(client._post("query { viewer { id } }"))
    _assert_no_secret_anywhere(caught.value)
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    if handler is _bad_gateway:
        assert "for url 'https://[redacted]@linear.example.test/graphql'" in str(caught.value), str(caught.value)


def test_an_invalid_url_error_never_repeats_userinfo() -> None:
    # httpx reads "u:SECRET＠host" as host "u" with port "SECRET＠host" and
    # raises InvalidURL (not an HTTPError) naming that "port".
    github = GitHubClient(token="dummy-token", endpoint=GITHUB_WITH_USERINFO[-2],
                          http_client=httpx.AsyncClient(transport=httpx.MockTransport(_bad_gateway)))
    with pytest.raises(GitHubError, match="the GitHub API URL is not a valid URL") as caught:
        asyncio.run(github.query("query { viewer { login } }"))
    _assert_no_secret_anywhere(caught.value)
    assert caught.value.__cause__ is None and caught.value.__context__ is None

    linear = LinearClient(token="dummy-key", endpoint=LINEAR_FULLWIDTH, transport=httpx.MockTransport(_bad_gateway))
    with pytest.raises(LinearClientError, match="the Linear API URL is not a valid URL") as caught_linear:
        asyncio.run(linear._post("query { viewer { id } }"))
    _assert_no_secret_anywhere(caught_linear.value)
    assert caught_linear.value.__cause__ is None and caught_linear.value.__context__ is None


def test_an_error_without_userinfo_keeps_its_cause() -> None:
    client = LinearClient(token="dummy-key", endpoint="https://linear.example.test/graphql",
                          transport=httpx.MockTransport(_bad_gateway))
    with pytest.raises(LinearClientError) as caught:
        asyncio.run(client._post("query { viewer { id } }"))
    assert isinstance(caught.value.__cause__, httpx.HTTPStatusError)
    github = GitHubClient(token="dummy-token", endpoint="https://ghes.example.test/api/graphql",
                          http_client=httpx.AsyncClient(transport=httpx.MockTransport(_bad_gateway)))
    with pytest.raises(GitHubError) as caught_github:
        asyncio.run(github.query("query { viewer { login } }"))
    assert isinstance(caught_github.value.__cause__, httpx.HTTPStatusError)
