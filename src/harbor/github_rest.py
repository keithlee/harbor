"""GitHub REST helper the builder uses for GitHub reads and writes.

Harbor's default builder transport for GitHub. It exists because the GitHub CLI
cannot complete a TLS handshake inside Claude Code's macOS sandbox (it hangs or
fails with ``x509: OSStatus -26276``), while ``curl`` to the same API works. The
helper therefore speaks the REST API through ``curl``.

The token is read from the environment and handed to ``curl`` on stdin as a
config file, so it never appears in a process listing, on disk, or in output.
Requests only go to the configured API host: ``api`` takes a path, never a URL.

Configuration comes from the builder environment Harbor sets:

- ``HARBOR_GITHUB_API_URL``: REST base, e.g. ``https://api.github.com``
- ``HARBOR_GITHUB_REPOSITORY``: ``owner/name`` (``--repo`` overrides it)
- ``HARBOR_GITHUB_TOKEN_ENV``: name of the variable holding the token
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Any, Mapping, Sequence

DEFAULT_REST_URL = "https://api.github.com"
TOKEN_FALLBACKS = ("GITHUB_TOKEN", "GH_TOKEN")
API_VERSION = "2022-11-28"


class GitHubRestError(RuntimeError):
    """A request failed; the message never contains the token."""


def rest_base_url(graphql_url: str | None) -> str:
    """Derive the REST base from Harbor's GraphQL endpoint setting.

    ``https://api.github.com/graphql`` -> ``https://api.github.com``;
    GitHub Enterprise Server ``https://host/api/graphql`` -> ``https://host/api/v3``.
    """

    url = (graphql_url or "").strip().rstrip("/")
    if not url:
        return DEFAULT_REST_URL
    if not url.startswith("https://"):
        raise ValueError("the GitHub API URL must use https")
    if url.endswith("/api/graphql"):
        return url[: -len("/graphql")] + "/v3"
    if url.endswith("/graphql"):
        return url[: -len("/graphql")]
    return url


def token_env_name(env: Mapping[str, str], preferred: str | None = None) -> str | None:
    """Name of the first token variable that is set, without reading its value out."""

    for name in dict.fromkeys(n for n in (preferred, *TOKEN_FALLBACKS) if n):
        if env.get(name, "").strip():
            return name
    return None


def _config_quote(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\").replace('"', '\\"')
        .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    )
    return f'"{escaped}"'


def curl_config(method: str, url: str, token: str, body: str | None) -> str:
    lines = [
        f"url = {_config_quote(url)}",
        f"request = {_config_quote(method)}",
        f"header = {_config_quote('Authorization: Bearer ' + token)}",
        f"header = {_config_quote('Accept: application/vnd.github+json')}",
        f"header = {_config_quote('X-GitHub-Api-Version: ' + API_VERSION)}",
        f"header = {_config_quote('User-Agent: harbor-github-rest')}",
    ]
    if body is not None:
        if body.startswith("@"):
            raise GitHubRestError("request bodies must be JSON, not @file references")
        lines.append(f"header = {_config_quote('Content-Type: application/json')}")
        lines.append(f"data-binary = {_config_quote(body)}")
    return "\n".join(lines) + "\n"


class Client:
    def __init__(self, env: Mapping[str, str] | None = None, *, curl: str = "curl", timeout: int = 60) -> None:
        self.env = dict(os.environ if env is None else env)
        self.base = rest_base_url(self.env.get("HARBOR_GITHUB_API_URL") or DEFAULT_REST_URL)
        name = token_env_name(self.env, self.env.get("HARBOR_GITHUB_TOKEN_ENV"))
        if name is None:
            raise GitHubRestError("no GitHub token in the environment (GITHUB_TOKEN or GH_TOKEN)")
        self._token = self.env[name].strip()
        self.curl = curl
        self.timeout = timeout

    def request(self, method: str, path: str, payload: Any = None) -> Any:
        if not path.startswith("/") or "://" in path or path.startswith("//"):
            raise GitHubRestError("API paths must be relative to the configured GitHub host, e.g. /repos/o/r")
        body = None if payload is None else (payload if isinstance(payload, str) else json.dumps(payload))
        config = curl_config(method.upper(), self.base + path, self._token, body)
        try:
            result = subprocess.run(
                [self.curl, "--silent", "--show-error", "--max-time", str(self.timeout),
                 "--config", "-", "--write-out", "\n%{http_code}"],
                input=config, capture_output=True, text=True, timeout=self.timeout + 15, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise GitHubRestError(f"curl could not run: {type(exc).__name__}") from None
        if result.returncode != 0:
            raise GitHubRestError(f"curl failed (exit {result.returncode}): {result.stderr.strip()[:300]}")
        text, _, status = result.stdout.rpartition("\n")
        code = int(status) if status.strip().isdigit() else 0
        data: Any = json.loads(text) if text.strip() else None
        if not 200 <= code < 300:
            message = data.get("message") if isinstance(data, dict) else text.strip()[:300]
            raise GitHubRestError(f"GitHub API {method.upper()} {path} returned {code}: {message}")
        return data


def _repo(args: argparse.Namespace, env: Mapping[str, str]) -> str:
    repo = (args.repo or env.get("HARBOR_GITHUB_REPOSITORY") or "").strip()
    if repo.count("/") != 1 or not all(repo.split("/")):
        raise GitHubRestError("no repository: pass --repo owner/name or set HARBOR_GITHUB_REPOSITORY")
    return repo


def _read(path: str | None) -> str | None:
    if path is None:
        return None
    if path == "-":
        return sys.stdin.read()
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _pr_summary(pr: Mapping[str, Any]) -> dict[str, Any]:
    head, base = pr.get("head") or {}, pr.get("base") or {}
    return {
        "number": pr.get("number"), "url": pr.get("html_url"), "state": pr.get("state"),
        "merged": pr.get("merged"), "draft": pr.get("draft"), "title": pr.get("title"),
        "head_ref": head.get("ref"), "head_sha": head.get("sha"), "base_ref": base.get("ref"),
        "mergeable": pr.get("mergeable"), "mergeable_state": pr.get("mergeable_state"),
        "body": pr.get("body"),
    }


def run(argv: Sequence[str], env: Mapping[str, str] | None = None, client: Client | None = None) -> Any:
    environ = dict(os.environ if env is None else env)
    parser = argparse.ArgumentParser(prog="harbor.github_rest", description=__doc__.splitlines()[0])
    parser.add_argument("--repo")
    sub = parser.add_subparsers(dest="op", required=True)
    view = sub.add_parser("pr-view"); view.add_argument("number", type=int)
    create = sub.add_parser("pr-create")
    create.add_argument("--head", required=True); create.add_argument("--base", required=True)
    create.add_argument("--title", required=True); create.add_argument("--body-file")
    create.add_argument("--draft", action="store_true")
    edit = sub.add_parser("pr-edit"); edit.add_argument("number", type=int)
    edit.add_argument("--title"); edit.add_argument("--body-file")
    comment = sub.add_parser("pr-comment"); comment.add_argument("number", type=int)
    comment.add_argument("--body-file", required=True)
    checks = sub.add_parser("pr-checks"); checks.add_argument("number", type=int)
    merge = sub.add_parser("pr-merge"); merge.add_argument("number", type=int)
    merge.add_argument("--sha", required=True)
    merge.add_argument("--method", choices=("squash", "merge", "rebase"), default="squash")
    merge.add_argument("--subject"); merge.add_argument("--body-file")
    api = sub.add_parser("api"); api.add_argument("method"); api.add_argument("path")
    api.add_argument("--input", help="JSON request body file, or - for stdin")
    args = parser.parse_args(list(argv))
    gh = client or Client(environ)
    if args.op == "api":
        return gh.request(args.method, args.path, _read(args.input))
    repo = _repo(args, environ)
    pulls = f"/repos/{repo}/pulls"
    if args.op == "pr-view":
        return _pr_summary(gh.request("GET", f"{pulls}/{args.number}"))
    if args.op == "pr-create":
        payload = {"title": args.title, "head": args.head, "base": args.base,
                   "body": _read(args.body_file) or "", "draft": args.draft}
        return _pr_summary(gh.request("POST", pulls, payload))
    if args.op == "pr-edit":
        payload = {k: v for k, v in {"title": args.title, "body": _read(args.body_file)}.items() if v is not None}
        if not payload:
            raise GitHubRestError("pr-edit needs --title and/or --body-file")
        return _pr_summary(gh.request("PATCH", f"{pulls}/{args.number}", payload))
    if args.op == "pr-comment":
        result = gh.request("POST", f"/repos/{repo}/issues/{args.number}/comments", {"body": _read(args.body_file)})
        return {"id": result.get("id"), "url": result.get("html_url")}
    if args.op == "pr-checks":
        sha = gh.request("GET", f"{pulls}/{args.number}")["head"]["sha"]
        runs = gh.request("GET", f"/repos/{repo}/commits/{sha}/check-runs?per_page=100")
        status = gh.request("GET", f"/repos/{repo}/commits/{sha}/status")
        return {
            "head_sha": sha,
            "check_runs": [{"name": r.get("name"), "status": r.get("status"), "conclusion": r.get("conclusion")}
                           for r in (runs or {}).get("check_runs", [])],
            # GitHub reports "pending" for a commit with no legacy statuses at all;
            # the count lets a reader tell "none reported" from "still running".
            "combined_status": (status or {}).get("state"),
            "combined_status_count": (status or {}).get("total_count", 0),
        }
    payload = {"sha": args.sha, "merge_method": args.method}
    if args.subject:
        payload["commit_title"] = args.subject
    if args.body_file:
        payload["commit_message"] = _read(args.body_file)
    return gh.request("PUT", f"{pulls}/{args.number}/merge", payload)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        result = run(sys.argv[1:] if argv is None else argv)
    except (GitHubRestError, ValueError, OSError) as exc:
        print(f"harbor.github_rest: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
