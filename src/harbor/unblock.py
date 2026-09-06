"""Harbor's Phase 7 unblock ladder.

The ladder is deliberately independent from the supervisor and console.  It
accepts observed run/ticket facts, tries a small deterministic remediation,
then asks a read-only Terra-compatible First Mate to *cite or escalate*.  Only
after those lower rungs fail does it write a Linear comment/status and notify
the operator.  A successful rung always resumes the retained builder session;
there is no replacement-session fallback.
"""

from __future__ import annotations

import fnmatch
import inspect
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from harbor.db import apply_phase7_migration
from harbor.facts.remediation import RemediationResult, execute_remediation
from harbor.resources import runtime_asset
from harbor.wake import apply_phase3_migration


MAX_TRIAGE_SOURCE_BYTES = 4_000
MAX_TRIAGE_PROMPT_BYTES = 12_000
QUESTION_WORDS = {
    "what",
    "when",
    "where",
    "which",
    "would",
    "could",
    "should",
    "this",
    "that",
    "from",
    "with",
    "have",
    "does",
    "will",
}
_PRODUCT_RE = re.compile(
    r"\b(?:new\s+product|product\s+decision|user[- ]facing|ux\s+decision|design\s+decision)\b",
    re.IGNORECASE,
)


def _value(value: Any, *names: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
        return default
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    return default


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return bool(value)


def _identifier(ticket: Any) -> str:
    return _text(_value(ticket, "identifier", "key", "id", default=""))


def _issue_id(ticket: Any) -> str:
    return _text(_value(ticket, "linear_id", "issue_id", "id", "identifier", default=""))


def _run_id(run: Any) -> str:
    return _text(_value(run, "id", "run_id", "session_id", default=""))


def _is_parked_for_human(final: Any) -> bool:
    """Whether the run finished merge-ready and is waiting only on the operator."""

    if not isinstance(final, Mapping):
        return False
    outcome = _text(_value(final, "outcome", default="")).casefold()
    waiting_on = _text(_value(final, "waiting_on", default="")).casefold()
    return outcome == "parked" and waiting_on == "human"


def _blocker(final: Any) -> Mapping[str, Any]:
    value = _value(final, "blocker", "block", "reason", default=final)
    if isinstance(value, Mapping):
        return dict(value)
    return {"question": _text(value)}


def _kind(blocker: Mapping[str, Any]) -> str:
    value = _value(blocker, "kind", "type", "category", default="env_blocker")
    return " ".join(_text(value, "env_blocker").strip().casefold().replace("-", "_").split()) or "env_blocker"


def _question(blocker: Mapping[str, Any]) -> str:
    value = _value(blocker, "question", "prompt", "message", "error", default="")
    if isinstance(value, (list, tuple)):
        value = " ".join(_text(item) for item in value)
    return _text(value).strip()


def deterministic_capability_adapter(
    blocker: Mapping[str, Any], repo_path: str | Path | None = None
) -> tuple[str, str, str] | None:
    """Return a safe tool adapter for a known non-product capability mismatch."""

    kind = _kind(blocker)
    question = _question(blocker)
    evidence = _text(_value(blocker, "evidence", default=""))
    combined = f"{question} {evidence}"
    lowered = combined.casefold()
    root = Path(repo_path) if repo_path is not None else None

    role_match = re.search(r"\b([a-z0-9][a-z0-9_-]*auditor)\b", combined, re.IGNORECASE)
    if kind == "missing_proof_capability" and "unknown agent_type" in lowered and root is not None and role_match:
        role_path = root / ".claude" / "agents" / f"{role_match.group(1)}.md"
        if role_path.is_file():
            relative = role_path.relative_to(root)
            return (
                f"Invoke a fresh supported `default` subagent in clean context. Have it read `{relative}` "
                "as its role contract, then give it only the PR URL required by that contract. "
                "Do not retry the repository role name as agent_type.",
                f"{relative}:1",
                "repository agent role adapted to a supported clean-context subagent",
            )

    if kind == "env_blocker" and "linear" in lowered and "requires approval" in lowered:
        return (
            "Retry the Linear operation with the available `codex_apps` Linear connector "
            "(`mcp__codex_apps__linear_*`) instead of the approval-gated `mcp__linear__*` connector. "
            "Keep the same ticket, worktree, and builder session.",
            "deterministic:linear-connector-adapter",
            "approval-gated Linear connector adapted to the non-interactive connector",
        )
    return None


def _answer(blocker: Mapping[str, Any], final: Any) -> str:
    value = _value(blocker, "answer", "recommendation", default=None)
    if value is None:
        value = _value(final, "answer", "recommendation", default="")
    return _text(value).strip()


def _paths(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, Mapping):
        value = value.values()
    try:
        return [str(item).strip() for item in value if str(item).strip()]
    except TypeError:
        return [str(value).strip()] if str(value).strip() else []


def _normalise_paths(value: Any) -> tuple[str, ...]:
    return tuple(item.replace("\\", "/").lstrip("./") for item in _paths(value))


@dataclass(frozen=True)
class AuthoritySource:
    label: str
    text: str
    line: int | None = None
    kind: str = "repository"

    @property
    def citation(self) -> str:
        return f"{self.label}:{self.line}" if self.line else self.label

    @property
    def excerpt(self) -> str:
        value = self.text.strip()
        return value[:MAX_TRIAGE_SOURCE_BYTES]


@dataclass(frozen=True)
class TriageResult:
    disposition: Literal["resolve", "escalate"]
    answer: str = ""
    citation: str = ""
    searched: tuple[str, ...] = ()
    reason: str = ""
    source: str = ""
    from_resolution: bool = False

    @property
    def resolved(self) -> bool:
        return self.disposition == "resolve"

    @property
    def escalated(self) -> bool:
        return self.disposition == "escalate"

    def as_dict(self) -> dict[str, Any]:
        return {
            "disposition": self.disposition,
            "answer": self.answer,
            "citation": self.citation,
            "searched": list(self.searched),
            "reason": self.reason,
            "source": self.source,
            "from_resolution": self.from_resolution,
        }


@dataclass(frozen=True)
class ResumeCommand:
    """An explicit same-session continuation request."""

    run_id: str
    prompt: str
    command: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "prompt": self.prompt, "command": list(self.command)}


@dataclass(frozen=True)
class UnblockResult:
    ticket: str
    run_id: str
    kind: str
    question: str
    rung: int
    action: Literal["resume", "park", "escalate"]
    resumed: bool
    notified: bool = False
    answer: str = ""
    citation: str = ""
    searched: tuple[str, ...] = ()
    remediation: RemediationResult | None = None
    resume_command: ResumeCommand | None = None
    linear_writes: tuple[str, ...] = ()
    reason: str = ""
    escalation: Mapping[str, Any] | None = None

    @property
    def same_session(self) -> bool:
        return bool(self.run_id and self.resume_command and self.resume_command.run_id == self.run_id)

    @property
    def rung_resolved(self) -> int:
        return self.rung

    def as_dict(self) -> dict[str, Any]:
        return {
            "ticket": self.ticket,
            "run_id": self.run_id,
            "kind": self.kind,
            "question": self.question,
            "rung": self.rung,
            "rung_resolved": self.rung,
            "action": self.action,
            "resumed": self.resumed,
            "same_session": self.same_session,
            "notified": self.notified,
            "answer": self.answer,
            "citation": self.citation,
            "searched": list(self.searched),
            "remediation": self.remediation.as_dict() if self.remediation else None,
            "resume_command": self.resume_command.as_dict() if self.resume_command else None,
            "linear_writes": list(self.linear_writes),
            "reason": self.reason,
            "escalation": dict(self.escalation) if self.escalation else None,
        }


def _tokens(question: str) -> set[str]:
    return {
        token
        # Keep punctuation out of the token so ``unavailable.`` and
        # ``unavailable?`` match the same authority line.
        for token in re.findall(r"[a-z0-9][a-z0-9_/-]{2,}", question.casefold())
        if token not in QUESTION_WORDS
    }


def _ticket_source(ticket: Mapping[str, Any], question: str) -> AuthoritySource | None:
    fields = (
        "description",
        "body",
        "acceptance_criteria",
        "acceptanceCriteria",
        "spec",
        "product_spec",
        "comments",
    )
    question_folded = " ".join(question.split()).casefold()
    identifier = _identifier(ticket) or "ticket"
    for field_name in fields:
        value = _value(ticket, field_name, default=None)
        if isinstance(value, Mapping):
            value = value.get("nodes", value.get("body", ""))
        if isinstance(value, (list, tuple)):
            value = "\n".join(_text(item) for item in value)
        text = _text(value).strip()
        if not text:
            continue
        if question_folded and question_folded not in " ".join(text.split()).casefold():
            terms = _tokens(question)
            if not terms or len(terms & _tokens(text)) < min(2, len(terms)):
                continue
        line = next((i for i, line in enumerate(text.splitlines(), 1) if line.strip()), None)
        return AuthoritySource(f"ticket:{identifier}", text, line, "ticket")
    return None


def _candidate_paths(root: Path) -> list[tuple[str, Path, str]]:
    # Explicit precedence is part of the safety contract.  Case variants are
    # included for repositories that use lower-case docs names.
    candidates: list[tuple[str, Path, str]] = []
    for path in (root / "AGENTS.md", root / "agents.md"):
        if path.is_file():
            candidates.append(("AGENTS.md", path, "agents"))
            break
    for name, label, kind in (
        ("docs/ProductSpec.md", "docs/ProductSpec.md", "product_spec"),
        ("docs/productspec.md", "docs/ProductSpec.md", "product_spec"),
        ("docs/DISCOVERIES.md", "docs/DISCOVERIES.md", "discoveries"),
        ("docs/discoveries.md", "docs/DISCOVERIES.md", "discoveries"),
    ):
        path = root / name
        if path.is_file() and not any(existing == path for _, existing, _ in candidates):
            candidates.append((label, path, kind))
    return candidates


def _line_for_terms(text: str, terms: set[str], phrase: str = "") -> tuple[str, int] | None:
    lines = text.splitlines()
    phrase_folded = " ".join(phrase.split()).casefold()
    if phrase_folded:
        for number, line in enumerate(lines, 1):
            if phrase_folded in " ".join(line.split()).casefold():
                return line.strip(), number
    scored: list[tuple[int, int, str]] = []
    for number, line in enumerate(lines, 1):
        score = len(terms & _tokens(line))
        if score:
            scored.append((score, number, line.strip()))
    if not scored:
        return None
    score, number, line = max(scored)
    if score < min(2, len(terms)):
        return None
    return line, number


def _repo_precedent(root: Path, question: str, excluded: set[Path]) -> AuthoritySource | None:
    terms = _tokens(question)
    if not terms:
        return None
    visited = 0
    try:
        paths = root.rglob("*")
    except OSError:
        return None
    for path in paths:
        if visited >= 250:
            break
        if path in excluded or not path.is_file() or any(part in {".git", ".venv", "node_modules", "__pycache__"} for part in path.parts):
            continue
        if path.suffix.casefold() not in {".md", ".txt", ".py", ".ts", ".tsx", ".js", ".jsx", ".json", ".toml", ".yaml", ".yml"}:
            continue
        visited += 1
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        match = _line_for_terms(text, terms, question)
        if match:
            line, number = match
            return AuthoritySource(str(path.relative_to(root)), line, number, "repo_precedent")
    return None


def _protected_from_agents(root: Path) -> tuple[str, ...]:
    for _, path, kind in _candidate_paths(root):
        if kind != "agents":
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ()
        values: list[str] = []
        active = False
        for raw in lines:
            line = raw.strip()
            if re.match(r"^protected[_ -]?paths\s*:", line, re.IGNORECASE):
                active = True
                inline = line.split(":", 1)[1].strip().strip("[]")
                values.extend(part.strip(" '\"") for part in inline.split(",") if part.strip())
                continue
            if active:
                item = re.match(r"^-\s*[`'\"]?([^`'\"]+?)[`'\"]?\s*$", line)
                if item:
                    values.append(item.group(1).strip())
                elif line and not line.startswith("#"):
                    active = False
        return _normalise_paths(values)
    return ()


def touches_protected_paths(
    blocker: Mapping[str, Any],
    *,
    protected_paths: Iterable[str] = (),
    repo_path: str | Path | None = None,
) -> bool:
    """Fail closed when observed paths overlap the configured protected set."""

    touched = _normalise_paths(
        list(_paths(_value(blocker, "path", "file", default=None)))
        + _paths(_value(blocker, "paths", "files", "touched_paths", "touchedFiles", default=None))
    )
    configured = _normalise_paths(protected_paths)
    if repo_path is not None:
        configured += _protected_from_agents(Path(repo_path))
    if not touched or not configured:
        return False
    for path in touched:
        for pattern in configured:
            if path == pattern or fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(path, f"*/{pattern}"):
                return True
            # A directory pattern must protect everything under it.  Plain
            # fnmatch cannot do this: "deploy/prod.yml" matches neither
            # "deploy/" nor "deploy", so a protected directory would guard
            # only itself and none of its files.  Matching is on whole path
            # segments, so "deploy/" must not catch "deployment.md".
            directory = pattern.rstrip("/")
            if directory and (
                path == directory
                or path.startswith(f"{directory}/")
                or f"/{directory}/" in f"/{path}/"
                or fnmatch.fnmatch(path, f"{directory}/*")
                or fnmatch.fnmatch(path, f"*/{directory}/*")
            ):
                return True
    return False


def is_product_decision(blocker: Mapping[str, Any]) -> bool:
    kind = _kind(blocker)
    if kind in {"product_decision", "new_product_decision", "product", "design_decision"}:
        return True
    if _bool(_value(blocker, "new_product", "novel", "is_new_product", default=False)):
        return True
    question = _question(blocker)
    return bool(_PRODUCT_RE.search(question))


def authority_sources(
    ticket: Mapping[str, Any],
    blocker: Mapping[str, Any],
    *,
    repo_path: str | Path | None = None,
    db: Any | None = None,
) -> tuple[AuthoritySource, ...]:
    """Search sources in the plan's explicit precedence order."""

    question = _question(blocker)
    found: list[AuthoritySource] = []
    source = _ticket_source(ticket, question)
    if source:
        found.append(source)
    root = Path(repo_path) if repo_path is not None else None
    excluded: set[Path] = set()
    if root is not None:
        paths = _candidate_paths(root)
        excluded = {path for _, path, _ in paths}
        terms = _tokens(question)
        for label, path, kind in paths:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            match = _line_for_terms(text, terms, question)
            if match:
                excerpt, line = match
                found.append(AuthoritySource(label, excerpt, line, kind))
        precedent = _repo_precedent(root, question, excluded)
        if precedent:
            found.append(precedent)
    if db is not None:
        resolution = None
        finder = getattr(db, "find_resolution", None) or getattr(db, "resolution_for", None)
        if callable(finder):
            resolution = finder(_kind(blocker), question)
        if isinstance(resolution, Mapping):
            citation = _text(resolution.get("citation")).strip()
            answer = _text(resolution.get("answer")).strip()
            if citation and answer:
                found.append(
                    AuthoritySource(
                        f"resolutions#{resolution.get('id', '')}".rstrip("#"),
                        f"{answer}\nCitation: {citation}",
                        None,
                        "resolution",
                    )
                )
    # Avoid presenting the same source repeatedly when a file appears under a
    # case alias or when a resolution finder is backed by a fake.
    unique: list[AuthoritySource] = []
    seen: set[tuple[str, str]] = set()
    for item in found:
        key = (item.label, item.citation)
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return tuple(unique)


search_authority = authority_sources


def _terra_prompt(ticket: Mapping[str, Any], blocker: Mapping[str, Any], sources: Sequence[AuthoritySource]) -> str:
    sections = [
        "You are Harbor's First Mate (Terra). Triage one blocked builder run.",
        "CITE OR ESCALATE: resolve only when an authority excerpt below answers the exact question. Never invent a policy or product decision.",
        f"Ticket: {_identifier(ticket)}",
        f"Blocker kind: {_kind(blocker)}",
        f"Question: {_question(blocker)}",
        "Return JSON with disposition=resolve|escalate, answer, citation, reason. A resolve response MUST include a non-empty citation matching one source.",
        "<authority-sources>",
    ]
    for number, source in enumerate(sources, 1):
        sections.extend(
            (
                f"<source id=\"{number}\" citation=\"{source.citation}\">",
                source.excerpt,
                "</source>",
            )
        )
    sections.append("</authority-sources>")
    prompt = "\n".join(sections)
    encoded = prompt.encode("utf-8")
    return encoded[:MAX_TRIAGE_PROMPT_BYTES].decode("utf-8", errors="ignore")


def _parse_terra(value: Any) -> dict[str, str]:
    if isinstance(value, Mapping):
        result = {str(key).casefold(): _text(item).strip() for key, item in value.items()}
        return result
    text = _text(value).strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        if isinstance(parsed, Mapping):
            return {str(key).casefold(): _text(item).strip() for key, item in parsed.items()}
    except (json.JSONDecodeError, TypeError):
        pass
    result: dict[str, str] = {}
    for key in ("disposition", "decision", "action", "answer", "citation", "source", "reason"):
        match = re.search(rf"(?:^|\n)\s*{key}\s*[:=]\s*(.+)", text, re.IGNORECASE)
        if match:
            result[key] = match.group(1).strip()
    if not result:
        result["answer"] = text
    return result


async def _invoke_terra(first_mate: Any, prompt: str, ticket: Mapping[str, Any], blocker: Mapping[str, Any]) -> Any:
    target = first_mate
    for name in ("triage", "answer", "ask", "respond", "invoke", "run"):
        candidate = getattr(target, name, None)
        if callable(candidate):
            target = candidate
            break
    if not callable(target):
        raise TypeError("First Mate triage target must be callable or expose triage/answer")
    try:
        signature = inspect.signature(target)
    except (TypeError, ValueError):
        signature = None
    if signature is None:
        value = target(prompt)
    else:
        positional = [
            parameter
            for parameter in signature.parameters.values()
            if parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        names = {parameter.name for parameter in signature.parameters.values()}
        if {"ticket", "blocker", "prompt"} <= names:
            value = target(ticket=ticket, blocker=blocker, prompt=prompt)
        elif len(positional) <= 1:
            value = target(prompt)
        else:
            value = target(prompt, blocker)
    return await value if inspect.isawaitable(value) else value


def _validated_triage(
    raw: Any,
    sources: Sequence[AuthoritySource],
    *,
    force_escalate: bool = False,
    reason: str = "",
) -> TriageResult:
    parsed = _parse_terra(raw)
    searched = tuple(source.citation for source in sources)
    if force_escalate:
        return TriageResult("escalate", searched=searched, reason=reason or "protected path or new product decision")
    disposition = parsed.get("disposition", parsed.get("decision", parsed.get("action", ""))).casefold()
    if disposition in {"escalate", "park", "blocked", "human_review", "no"}:
        return TriageResult("escalate", searched=searched, reason=parsed.get("reason", reason or "no authoritative answer"))
    answer = parsed.get("answer", "").strip()
    citation = parsed.get("citation", parsed.get("source", "")).strip()
    if not citation and answer:
        # A tiny fake First Mate often returns only the source label.  It is
        # safe to infer it only when there is exactly one source.
        matching = [source for source in sources if source.excerpt.casefold() in answer.casefold()]
        if len(matching) == 1:
            citation = matching[0].citation
    source = next(
        (
            item
            for item in sources
            if citation and (citation.casefold() in item.citation.casefold() or item.citation.casefold() in citation.casefold())
        ),
        None,
    )
    if not answer or not citation or source is None:
        return TriageResult("escalate", searched=searched, reason=parsed.get("reason", reason or "resolution requires a valid citation"))
    return TriageResult("resolve", answer=answer, citation=citation, searched=searched, source=source.citation)


async def triage(
    ticket: Mapping[str, Any],
    blocker: Mapping[str, Any],
    *,
    db: Any | None = None,
    first_mate: Any | None = None,
    repo_path: str | Path | None = None,
    protected_paths: Iterable[str] = (),
) -> TriageResult:
    """Run rung 1's citation validation with an optional Terra call."""

    sources = authority_sources(ticket, blocker, repo_path=repo_path, db=db)
    configured_protected = list(protected_paths) + _paths(
        _value(blocker, "protected_paths", "protectedPaths", default=None)
    )
    protected = touches_protected_paths(blocker, protected_paths=configured_protected, repo_path=repo_path)
    product = is_product_decision(blocker)
    # A protected path is an absolute bar: never auto-resolved, whatever the
    # ledger says.  A product decision is different -- it is refused because
    # it is NEW, and a question the operator has already answered is not new.
    # The plan lists Harbor's own resolutions table among the authorities, so
    # that lookup has to come before the product-decision refusal or ticket C
    # (which cites ticket B's answer) could never resolve.
    if protected:
        return TriageResult(
            "escalate",
            searched=tuple(source.citation for source in sources),
            reason="protected path",
        )
    resolution = next((source for source in sources if source.kind == "resolution"), None)
    if product and resolution is None:
        return TriageResult(
            "escalate",
            searched=tuple(source.citation for source in sources),
            reason="genuinely new product decision",
        )
    if resolution is not None:
        text = resolution.text
        answer, _, citation = text.partition("\nCitation:")
        citation = citation.strip()
        if answer.strip() and citation:
            return TriageResult(
                "resolve",
                answer=answer.strip(),
                citation=citation,
                searched=tuple(source.citation for source in sources),
                source=resolution.citation,
                from_resolution=True,
            )
    if not sources:
        return TriageResult("escalate", searched=(), reason="no authoritative source found")
    if first_mate is None:
        # Embedded callers may choose deterministic citation-only mode.  This
        # remains cite-or-escalate and never invents an answer.
        source = sources[0]
        if source.text.strip():
            return TriageResult(
                "resolve",
                answer=source.excerpt,
                citation=source.citation,
                searched=tuple(item.citation for item in sources),
                source=source.citation,
            )
        return TriageResult("escalate", searched=tuple(item.citation for item in sources), reason="empty authority excerpt")
    prompt = _terra_prompt(ticket, blocker, sources)
    raw = await _invoke_terra(first_mate, prompt, ticket, blocker)
    return _validated_triage(raw, sources)


triage_with_terra = triage
rung_one = triage


def build_resume_command(run_id: str, prompt: str, *, output_schema: str | None = None) -> ResumeCommand:
    """Build the documented same-session Codex resume command."""

    run = str(run_id).strip()
    if not run:
        raise ValueError("a builder run/session id is required")
    text = str(prompt)
    schema = output_schema or str(runtime_asset("schemas", "run-result.json"))
    command: list[str] = ["codex", "exec", "resume", run, text, "--json", "--output-schema", schema]
    return ResumeCommand(run, text, tuple(command))


resume_command = build_resume_command


async def _invoke_resume(resumer: Any, command: ResumeCommand) -> bool:
    if resumer is None:
        return False
    target = resumer
    for name in ("resume", "reengage", "continue_run", "continue", "run"):
        candidate = getattr(target, name, None)
        if callable(candidate):
            target = candidate
            break
    if not callable(target):
        return False
    try:
        signature = inspect.signature(target)
    except (TypeError, ValueError):
        signature = None
    if signature is None:
        value = target(command.run_id, command.prompt)
    else:
        positional = [
            parameter
            for parameter in signature.parameters.values()
            if parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        names = {parameter.name for parameter in signature.parameters.values()}
        if {"run_id", "prompt"} <= names:
            value = target(run_id=command.run_id, prompt=command.prompt)
        elif {"command", "prompt"} <= names:
            value = target(command=list(command.command), prompt=command.prompt)
        elif len(positional) <= 1:
            value = target(command)
        else:
            value = target(command.run_id, command.prompt)
    value = await value if inspect.isawaitable(value) else value
    return _bool(value, True)


def _extract_comment_id(result: Any) -> str | None:
    value = result
    if isinstance(value, Mapping):
        for key in ("comment", "data", "commentCreate"):
            if key in value:
                value = value[key]
        if isinstance(value, Mapping):
            value = value.get("id", value.get("comment_id"))
    return _text(value).strip() or None


async def _linear_comment(linear: Any, db: Any, issue_id: str, body: str) -> bool:
    if linear is None:
        return False
    if db is not None:
        exists = getattr(db, "comment_receipt_exists", None)
        if callable(exists) and exists(issue_id, body=body):
            return False
    target = None
    for name in ("create_comment_once", "create_comment", "post_comment", "add_comment", "comment"):
        candidate = getattr(linear, name, None)
        if callable(candidate):
            target = candidate
            break
    if target is None and callable(linear):
        target = linear
    if target is None:
        return False
    try:
        signature = inspect.signature(target)
    except (TypeError, ValueError):
        signature = None
    if signature is not None and "db" in signature.parameters:
        value = target(issue_id, body, db=db)
    else:
        value = target(issue_id, body)
    result = await value if inspect.isawaitable(value) else value
    if result is None and getattr(target, "__name__", "") == "create_comment_once":
        # ``create_comment_once`` uses None as its duplicate receipt signal;
        # do not report a Linear write when the HTTP request was suppressed.
        return False
    if db is not None:
        record = getattr(db, "record_comment_receipt", None)
        if callable(record) and not getattr(target, "__name__", "").endswith("_once"):
            record(issue_id, _extract_comment_id(result), body)
    return True


async def _linear_state(linear: Any, issue_id: str, state: str, *, state_id: str | None = None) -> bool:
    if linear is None:
        return False
    target = None
    for name in ("update_issue_state", "set_issue_state", "set_state", "update_progress"):
        candidate = getattr(linear, name, None)
        if callable(candidate):
            target = candidate
            break
    if target is None:
        return False
    selected = state_id or state
    try:
        signature = inspect.signature(target)
    except (TypeError, ValueError):
        signature = None
    if signature is not None and "state_id" in signature.parameters:
        value = target(issue_id, state_id=selected)
    elif signature is not None and "state" in signature.parameters:
        value = target(issue_id, state=selected)
    else:
        value = target(issue_id, selected)
    result = await value if inspect.isawaitable(value) else value
    return result is not False


class UnblockLadder:
    """Run deterministic rung 0, Terra rung 1, and operator rung 2."""

    def __init__(
        self,
        db: Any | None = None,
        *,
        linear: Any | None = None,
        first_mate: Any | None = None,
        resumer: Any | None = None,
        notifier: Any | None = None,
        repo_path: str | Path | None = None,
        protected_paths: Iterable[str] = (),
        remediation_executor: Any | None = None,
        blocked_state_id: str | None = None,
        human_review_state_id: str | None = None,
        rung_one_cap: int = 2,
    ) -> None:
        self.db = db
        if db is not None:
            try:
                # The comment receipt table is owned by Phase 3 and is kept
                # separate so reopening an older HarborDB remains additive.
                apply_phase3_migration(db)
                apply_phase7_migration(db)
            except (FileNotFoundError, TypeError):
                # Tiny in-memory fakes need not implement the migration seam.
                pass
        self.linear = linear
        self.first_mate = first_mate
        self.resumer = resumer
        self.notifier = notifier
        self.repo_path = Path(repo_path) if repo_path is not None else None
        self.protected_paths = tuple(protected_paths)
        self.remediation_executor = remediation_executor
        self.blocked_state_id = blocked_state_id
        self.human_review_state_id = human_review_state_id
        self.rung_one_cap = max(0, int(rung_one_cap))

    def _prior_rung_one(self, ticket_id: str, kind: str) -> int:
        if self.db is None:
            return 0
        counter = getattr(self.db, "count_escalations", None)
        if callable(counter):
            try:
                return int(counter(ticket_id, kind=kind, rung_resolved=1))
            except TypeError:
                return int(counter(ticket_id, kind=kind))
        return 0

    def _prior_rung_zero(self, ticket_id: str, kind: str) -> int:
        if self.db is None:
            return 0
        counter = getattr(self.db, "count_escalations", None)
        if callable(counter):
            try:
                return int(counter(ticket_id, kind=kind, rung_resolved=0))
            except TypeError:
                return 0
        return 0

    def can_retry_deterministically(
        self, ticket: Mapping[str, Any], run: Mapping[str, Any], final: Mapping[str, Any]
    ) -> bool:
        """Whether a restart may safely replay a known capability adapter."""

        blocker_value = dict(_blocker(final))
        run_repo_value = _value(run, "worktree", "cwd", default=None)
        run_repo = Path(str(run_repo_value)) if run_repo_value else None
        authority_repo = run_repo if run_repo is not None and run_repo.is_dir() else self.repo_path
        return (
            self._prior_rung_zero(_identifier(ticket), _kind(blocker_value)) < 2
            and deterministic_capability_adapter(blocker_value, authority_repo) is not None
        )

    def _record(
        self,
        ticket_id: str,
        run_id: str,
        kind: str,
        question: str,
        rung: int,
        *,
        citation: str = "",
        answer: str = "",
        notified: bool = False,
    ) -> Mapping[str, Any] | None:
        if self.db is None:
            return None
        method = getattr(self.db, "record_escalation", None)
        if not callable(method):
            return None
        return method(
            ticket_id,
            run_id or None,
            kind,
            question,
            rung,
            citation=citation or None,
            answer=answer or None,
            notified=notified,
        )

    async def _resume(
        self,
        ticket_id: str,
        run_id: str,
        *,
        answer: str = "",
        citation: str = "",
        reason: str = "",
    ) -> tuple[bool, ResumeCommand | None]:
        if not run_id:
            return False, None
        prompt = f"Continue ticket {ticket_id} in the existing builder session {run_id}."
        if reason:
            prompt += f" Harbor observed: {reason}"
        if answer:
            prompt += f"\nAnswer: {answer}"
        if citation:
            prompt += f"\nAuthority citation: {citation}"
        prompt += "\nContinue your workflow; do not load the skill again and do not create a replacement session."
        command = build_resume_command(run_id, prompt)
        resumed = await _invoke_resume(self.resumer, command)
        return resumed, command

    async def _notify(self, reason: str) -> bool:
        if self.notifier is None:
            return False
        target = getattr(self.notifier, "notify", None) or getattr(self.notifier, "send", None) or self.notifier
        if not callable(target):
            return False
        value = target(reason)
        if inspect.isawaitable(value):
            await value
        return True

    async def handle(
        self,
        ticket: Mapping[str, Any],
        run: Mapping[str, Any],
        final: Mapping[str, Any] | None = None,
        *,
        blocker: Mapping[str, Any] | None = None,
    ) -> UnblockResult:
        final = final or {}
        blocker_value = dict(blocker or _blocker(final))
        ticket_id = _identifier(ticket)
        run_id = _run_id(run)
        kind = _kind(blocker_value)
        question = _question(blocker_value) or _text(_value(final, "summary", "next_action", default="")).strip()
        question = question or f"How should Harbor unblock {ticket_id or 'this ticket'}?"
        run_repo_value = _value(run, "worktree", "cwd", default=None)
        run_repo = Path(str(run_repo_value)) if run_repo_value else None
        authority_repo = run_repo if run_repo is not None and run_repo.is_dir() else self.repo_path

        adapter = (
            deterministic_capability_adapter(blocker_value, authority_repo)
            if self._prior_rung_zero(ticket_id, kind) < 2
            else None
        )
        if adapter is not None:
            answer, citation, reason = adapter
            escalation = self._record(ticket_id, run_id, kind, question, 0, citation=citation, answer=answer)
            resumed, command = await self._resume(
                ticket_id, run_id, answer=answer, citation=citation, reason=reason,
            )
            return UnblockResult(
                ticket_id, run_id, kind, question, 0, "resume" if resumed or command else "escalate", resumed,
                answer=answer, citation=citation, resume_command=command,
                reason=reason, escalation=escalation,
            )
        remediation = await execute_remediation(
            blocker_value,
            executor=self.remediation_executor,
            cwd=run_repo_value,
        )
        if remediation.fixed:
            citation = f"deterministic:{remediation.plan.kind if remediation.plan else kind}"
            escalation = self._record(ticket_id, run_id, kind, question or remediation.plan.reason, 0, citation=citation, answer=remediation.plan.reason if remediation.plan else "")
            resumed, command = await self._resume(ticket_id, run_id, reason=remediation.plan.reason if remediation.plan else "deterministic remediation completed")
            return UnblockResult(
                ticket_id,
                run_id,
                kind,
                question,
                0,
                "resume" if resumed or command else "escalate",
                resumed,
                remediation=remediation,
                resume_command=command,
                reason="deterministic remediation completed",
                escalation=escalation,
            )

        # GitHub delivery transport is an observed runtime capability, never
        # a repository-policy question.  A failed read-only `gh auth status`
        # check has one deterministic operator remedy, and must not consume a
        # First Mate turn or rewrite the ticket as a product blocker.
        if remediation.plan is not None and remediation.plan.kind == "github_transport":
            reason = (
                "GitHub CLI authentication is unavailable. Run `gh auth login --hostname github.com` "
                "in the environment that starts Harbor before retrying this builder session."
            )
            escalation = self._record(ticket_id, run_id, kind, question, 0, answer=reason)
            notified = await self._notify(f"{ticket_id} requires GitHub CLI authentication: {reason}")
            return UnblockResult(
                ticket_id,
                run_id,
                kind,
                question,
                0,
                "escalate",
                False,
                notified=notified,
                remediation=remediation,
                reason=reason,
                escalation=escalation,
            )

        triage_result: TriageResult
        if self._prior_rung_one(ticket_id, kind) >= self.rung_one_cap:
            triage_result = TriageResult("escalate", reason="same-kind rung-1 cap reached")
        else:
            triage_result = await triage(
                ticket,
                blocker_value,
                db=self.db,
                first_mate=self.first_mate,
                repo_path=authority_repo,
                protected_paths=self.protected_paths,
            )
        # Keep the citation boundary defensive even if an injected Terra
        # adapter returns a hand-built TriageResult rather than going through
        # ``triage``'s validator.
        if triage_result.resolved and triage_result.citation.strip() and triage_result.answer.strip():
            escalation = self._record(
                ticket_id,
                run_id,
                kind,
                question,
                1,
                citation=triage_result.citation,
                answer=triage_result.answer,
                notified=False,
            )
            body = (
                f"[harbor] [override] Unblocked {ticket_id} at rung 1.\n"
                f"Answer: {triage_result.answer}\n"
                f"Citation: {triage_result.citation}"
            )
            writes: list[str] = []
            if await _linear_comment(self.linear, self.db, _issue_id(ticket), body):
                writes.append("comment")
            resumed, command = await self._resume(
                ticket_id,
                run_id,
                answer=triage_result.answer,
                citation=triage_result.citation,
                reason="rung 1 authority found",
            )
            return UnblockResult(
                ticket_id,
                run_id,
                kind,
                question,
                1,
                "resume" if resumed or command else "escalate",
                resumed,
                answer=triage_result.answer,
                citation=triage_result.citation,
                searched=triage_result.searched,
                remediation=remediation,
                resume_command=command,
                linear_writes=tuple(writes),
                reason="rung 1 authority found",
                escalation=escalation,
            )

        # Rung 2 is the first point where Harbor parks the ticket, comments on
        # Linear, and pages the operator.  The reason includes every source it
        # checked so an operator can answer without repeating the search.
        searched = triage_result.searched
        searched_text = ", ".join(searched) if searched else "none"
        reason = triage_result.reason or "no authoritative answer found"
        body = (
            f"[harbor] Blocked {ticket_id} after deterministic remediation and rung-1 triage.\n"
            f"Question: {question}\n"
            f"Reason: {reason}\n"
            f"Searched: {searched_text}\n"
            f"Reply with an answer and citation to resume this same builder session."
        )
        writes: list[str] = []
        if await _linear_comment(self.linear, self.db, _issue_id(ticket), body):
            writes.append("comment")
        human = is_product_decision(blocker_value) or _kind(blocker_value) in {
            "product_decision",
            "new_product_decision",
            "design_decision",
        }
        # A run that parked with waiting_on=human has ALL the work done and a
        # merge-ready PR open; only the merge click is the operator's.  That is
        # Human Review by the plan's own table, whatever the advisory blocker
        # kind happens to be -- routing it to Blocked would misreport a
        # finished ticket as a broken one.
        if _is_parked_for_human(final):
            human = True
        desired_state = "Human Review" if human else "Blocked"
        desired_state_id = self.human_review_state_id if human else self.blocked_state_id
        if await _linear_state(self.linear, _issue_id(ticket), desired_state, state_id=desired_state_id):
            writes.append("state")
        notified = await self._notify(f"{ticket_id} parked in {desired_state}: {question} ({reason}; tried rung 0 and rung 1)")
        escalation = self._record(ticket_id, run_id, kind, question, 2, answer="", notified=notified)
        return UnblockResult(
            ticket_id,
            run_id,
            kind,
            question,
            2,
            "park",
            False,
            notified=notified,
            searched=searched,
            remediation=remediation,
            linear_writes=tuple(writes),
            reason=reason,
            escalation=escalation,
        )

    unblock = handle
    resolve = handle
    run = handle


async def handle_blocked(*args: Any, **kwargs: Any) -> UnblockResult:
    ladder = kwargs.pop("ladder", None)
    if ladder is None:
        ladder = UnblockLadder(
            kwargs.pop("db", None),
            linear=kwargs.pop("linear", None),
            first_mate=kwargs.pop("first_mate", None),
            resumer=kwargs.pop("resumer", kwargs.pop("runner", None)),
            notifier=kwargs.pop("notifier", None),
            repo_path=kwargs.pop("repo_path", None),
            protected_paths=kwargs.pop("protected_paths", ()),
            remediation_executor=kwargs.pop("remediation_executor", None),
        )
    return await ladder.handle(*args, **kwargs)


unblock = handle_blocked


# ---------------------------------------------------------------------------
# Merge observations/commands.  Harbor reports these to the builder skill;
# it does not merge, set Done, or mutate GitHub itself.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BuilderCommand:
    action: Literal["merge", "park", "reengage"]
    ticket: str = ""
    run_id: str = ""
    reason: str = ""


@dataclass(frozen=True)
class MergeOutcome:
    risk: str
    hold: bool
    outcome: Literal["merged", "parked"]
    waiting_on: Literal["human", "none"]
    linear_state: str | None
    command: BuilderCommand
    reason: str = ""

    @property
    def harbor_writes(self) -> tuple[str, ...]:
        return ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "risk": self.risk,
            "hold": self.hold,
            "outcome": self.outcome,
            "waiting_on": self.waiting_on,
            "linear_state": self.linear_state,
            "command": {
                "action": self.command.action,
                "ticket": self.command.ticket,
                "run_id": self.command.run_id,
                "reason": self.command.reason,
            },
            "reason": self.reason,
            "harbor_writes": [],
        }


def model_merge_outcome(
    risk: str,
    hold: bool,
    *,
    ticket: str = "",
    reason: str = "",
) -> MergeOutcome:
    """Model the builder's allowed merge outcome without performing it."""

    normalized = _text(risk, "high").strip().casefold()
    if normalized == "low" and not _bool(hold):
        return MergeOutcome(
            normalized,
            False,
            "merged",
            "none",
            None,
            BuilderCommand("merge", ticket, reason=reason),
            reason or "low-risk ticket has no hold",
        )
    why = reason or ("hold is on" if _bool(hold) else f"{normalized or 'unknown'} risk requires human merge")
    return MergeOutcome(
        normalized or "high",
        _bool(hold),
        "parked",
        "human",
        "Human Review",
        BuilderCommand("park", ticket, reason=why),
        why,
    )


decide_merge = model_merge_outcome
merge_outcome = model_merge_outcome
merge_decision = model_merge_outcome


def observe_manual_merge(
    ticket: Mapping[str, Any],
    run: Mapping[str, Any],
    facts: Mapping[str, Any],
) -> BuilderCommand | None:
    """Return a re-engagement command after the operator merges by hand."""

    merged = facts.get("merged") is True
    waiting = _text(_value(run, "waiting_on", "waitingOn", default="")).casefold()
    if not merged or waiting != "human":
        return None
    run_id = _run_id(run)
    if not run_id:
        return None
    return BuilderCommand(
        "reengage",
        _identifier(ticket),
        run_id=run_id,
        reason="operator merged the Human Review PR; resume the same builder session",
    )


manual_merge_reengagement = observe_manual_merge
observe_merge = observe_manual_merge


__all__ = [
    "AuthoritySource",
    "BuilderCommand",
    "MergeOutcome",
    "ResumeCommand",
    "TriageResult",
    "UnblockLadder",
    "UnblockResult",
    "authority_sources",
    "build_resume_command",
    "decide_merge",
    "handle_blocked",
    "is_product_decision",
    "manual_merge_reengagement",
    "merge_decision",
    "merge_outcome",
    "model_merge_outcome",
    "observe_manual_merge",
    "observe_merge",
    "resume_command",
    "rung_one",
    "search_authority",
    "touches_protected_paths",
    "triage",
    "triage_with_terra",
    "unblock",
]
