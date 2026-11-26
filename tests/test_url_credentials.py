"""API URLs never carry credentials, and HTTP errors never repeat them.

Regression: ``require_https_api_url`` accepted ``https://user:SECRET@host/...``.
httpx repeats the full request URL in its error messages (``Server error '502
Bad Gateway' for url 'https://user:SECRET@host/graphql'``), so a GitHub or
Linear endpoint with userinfo put the secret into tracebacks and ``Harbor:``
error lines. All values here are dummies.
"""
from __future__ import annotations

import asyncio
import traceback
from pathlib import Path

import httpx
import pytest

from harbor.clients.github import GitHubClient, GitHubError
from harbor.clients.linear import LinearClient, LinearClientError
from harbor.config.projects import ProjectConfig
from harbor.github_rest import Client, require_https_api_url, rest_base_url
from harbor.url_credentials import has_userinfo, redact_userinfo

SECRET = "DUMMYSECRET0000"
GITHUB_WITH_USERINFO = [
    f"https://u:{SECRET}@ghes.example.test/api/graphql",
    f"https://{SECRET}@ghes.example.test/api/graphql",
    f"https://u:{SECRET}@api.github.com/graphql",
]
LINEAR_WITH_USERINFO = f"https://u:{SECRET}@linear.example.test/graphql"


def _everything(exc: BaseException) -> str:
    """The message plus the whole traceback, chained causes included."""

    return str(exc) + "".join(traceback.format_exception(exc))


# ------------------------------------------------------------ detection


@pytest.mark.parametrize("url", [*GITHUB_WITH_USERINFO, "https://@h.example.test/x", "  https://u:p@h.example.test  "])
def test_userinfo_is_detected(url: str) -> None:
    assert has_userinfo(url)


@pytest.mark.parametrize("url", [
    "https://api.github.com/graphql",
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


def test_redaction_handles_an_unencoded_at_sign_in_the_password() -> None:
    assert redact_userinfo(f"for url 'https://u:{SECRET}@x@h.example/g'") == "for url 'https://[redacted]@h.example/g'"


# ------------------------------------------------------------ fail closed at config load


@pytest.mark.parametrize("url", GITHUB_WITH_USERINFO)
def test_require_https_api_url_refuses_userinfo_without_repeating_it(url: str) -> None:
    with pytest.raises(ValueError, match="must not contain a user name, password or token") as caught:
        require_https_api_url(url)
    assert SECRET not in str(caught.value)
    assert "GITHUB_TOKEN" in str(caught.value)


@pytest.mark.parametrize("url", GITHUB_WITH_USERINFO)
def test_the_builder_rest_helper_refuses_userinfo(url: str) -> None:
    with pytest.raises(ValueError, match="must not contain") as caught:
        rest_base_url(url)
    assert SECRET not in str(caught.value)
    with pytest.raises(ValueError, match="must not contain"):
        Client({"HARBOR_GITHUB_API_URL": url, "GITHUB_TOKEN": "dummy-token"})


def test_project_config_refuses_a_linear_url_with_userinfo() -> None:
    with pytest.raises(ValueError, match="Linear API URL must not contain") as caught:
        ProjectConfig(name="SampleProject", linear_project_slug="sample", linear_api_url=LINEAR_WITH_USERINFO)
    assert SECRET not in str(caught.value)
    assert "LINEAR_API_KEY" in str(caught.value)


def test_project_config_refuses_a_registry_github_url_with_userinfo() -> None:
    with pytest.raises(ValueError, match="GitHub API URL must not contain") as caught:
        ProjectConfig(name="SampleProject", linear_project_slug="sample", github_api_url=GITHUB_WITH_USERINFO[0])
    assert SECRET not in str(caught.value)


def test_project_config_still_accepts_plain_endpoints() -> None:
    config = ProjectConfig(
        name="SampleProject", linear_project_slug="sample",
        linear_api_url="http://127.0.0.1:8765/graphql", github_api_url="https://ghes.example.test/api/graphql",
    )
    assert config.linear_api_url == "http://127.0.0.1:8765/graphql"


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


# ------------------------------------------------------------ defence in depth: client errors


def _bad_gateway(request: httpx.Request) -> httpx.Response:
    return httpx.Response(502, request=request)


def test_a_github_http_error_never_repeats_userinfo() -> None:
    client = GitHubClient(
        token="dummy-token", endpoint=GITHUB_WITH_USERINFO[0],
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(_bad_gateway)),
    )
    with pytest.raises(GitHubError, match="502 Bad Gateway") as caught:
        asyncio.run(client.query("query { viewer { login } }"))
    assert SECRET not in _everything(caught.value)
    assert "https://[redacted]@ghes.example.test/api/graphql" in str(caught.value)


def test_the_default_github_http_client_wraps_and_redacts_errors(monkeypatch) -> None:
    real = httpx.AsyncClient
    monkeypatch.setattr(
        "harbor.clients.github.httpx.AsyncClient",
        lambda **kwargs: real(transport=httpx.MockTransport(_bad_gateway), **kwargs),
    )
    client = GitHubClient(token="dummy-token", endpoint=GITHUB_WITH_USERINFO[0])
    with pytest.raises(GitHubError, match="502 Bad Gateway") as caught:
        asyncio.run(client.query("query { viewer { login } }"))
    assert SECRET not in _everything(caught.value)


def test_a_linear_http_error_never_repeats_userinfo() -> None:
    client = LinearClient(token="dummy-key", endpoint=LINEAR_WITH_USERINFO, transport=httpx.MockTransport(_bad_gateway))
    with pytest.raises(LinearClientError, match="502 Bad Gateway") as caught:
        asyncio.run(client._post("query { viewer { id } }"))
    assert SECRET not in _everything(caught.value)
    assert "https://[redacted]@linear.example.test/graphql" in str(caught.value)


def test_an_error_without_userinfo_keeps_its_cause() -> None:
    client = LinearClient(token="dummy-key", endpoint="https://linear.example.test/graphql",
                          transport=httpx.MockTransport(_bad_gateway))
    with pytest.raises(LinearClientError) as caught:
        asyncio.run(client._post("query { viewer { id } }"))
    assert isinstance(caught.value.__cause__, httpx.HTTPStatusError)
