"""Deny-by-default checks on the commands First Mate's operator run may execute.

The operator runs outside the builder sandbox, so Harbor accepts only plain,
argv-style commands it can fully check before anything runs:

* simple commands joined by ``;``, ``&&``, ``||`` or ``|``, with ``>``,
  ``>>``, ``<`` and ``2>&1``-style redirects;
* no variable or command expansion (``$``, backticks, ``<(...)``), no globs,
  no ``~``, no subshells, background jobs or here-documents;
* no shells, no interpreter inline code (``python -c``, ``node -e`` ...), and
  no program that runs another command (``env``, ``xargs``, ``sudo``,
  ``find -exec`` ...);
* every word that could be a path, including the program and ``.``, is
  resolved with symlinks followed and must stay inside the ticket worktree or
  the operator's own temp directory, and must not overlap a protected path;
* no merge authority: no ``gh pr merge``/``review``, no ``gh api`` writes,
  only an allowlist of ``gh`` subcommands, no ``--admin``, and no ``git push``
  that forces, deletes, mirrors or targets the base branch.

Anything else is refused with a reason, and the ladder asks a person.
"""

from __future__ import annotations

import fnmatch
import re
import shlex
from collections.abc import Iterable, Sequence
from pathlib import Path

MAX_COMMANDS = 10
MAX_COMMAND_CHARS = 2_000

_JOINERS = {";", "&&", "||", "|"}
_REDIRECTS = {">", ">>", "<", ">|"}
_DEVICES = {"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/stdin"}
# Expanded by the shell even inside double quotes.
_EXPANDING = {"$": "variable or command expansion", "`": "command substitution"}
# Expanded only when unquoted.
_UNQUOTED = {"*": "a glob", "?": "a glob", "[": "a glob", "{": "brace expansion", "~": "home-directory expansion"}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "mksh", "fish", "csh", "tcsh", "ash", "busybox", "pwsh", "powershell"}
# Programs whose job is to run another program or arbitrary code.
INDIRECT = {
    "env", "xargs", "sudo", "su", "doas", "nohup", "timeout", "gtimeout", "nice", "ionice", "time", "command",
    "builtin", "exec", "eval", "source", ".", "watch", "script", "expect", "parallel", "osascript", "open",
    "awk", "gawk", "mawk", "nawk", "launchctl", "at", "batch", "crontab", "ssh", "scp", "rsync", "chroot",
    "unshare", "nsenter", "flock", "stdbuf", "caffeinate", "arch", "sandbox-exec",
}
_INTERPRETER = re.compile(r"(python|pypy)[0-9.]*|node|nodejs|deno|bun|perl[0-9.]*|ruby[0-9.]*|php[0-9.]*|lua[0-9.]*|"
                          r"tclsh[0-9.]*|wish[0-9.]*|R|Rscript|julia|swift|groovy|jshell|irb|pwsh")
_INLINE_FLAGS = {"-c", "-e", "-E", "-r", "-p", "--eval", "--print", "-x", "-m"}
_FIND_EXEC = {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprintf", "-fls"}
GH_ALLOWED = {
    "pr": {"create", "list", "view", "checks", "diff", "status", "comment", "edit", "checkout"},
    "issue": {"list", "view", "status", "comment"},
    "run": {"list", "view", "watch", "download"},
    "repo": {"view"},
    "workflow": {"list", "view"},
    "auth": {"status"},
    "release": {"list", "view"},
    "search": {"prs", "issues", "code", "commits", "repos"},
}
_GH_WRITE_FIELDS = {"-f", "-F", "--field", "--raw-field", "--input"}
_GIT_FORBIDDEN = {
    "clean": "git clean deletes untracked files across the worktree",
    "filter-branch": "git filter-branch rewrites history",
    "filter-repo": "git filter-repo rewrites history",
    "config": "git config can install commands that run later",
    "update-ref": "git update-ref moves refs directly",
}
_PUSH_FORBIDDEN = {"-f", "--force", "--force-with-lease", "--force-if-includes", "--mirror", "--delete", "-d",
                   "--all", "--prune", "--no-verify"}


def _expansion(command: str) -> str | None:
    """The first shell expansion in ``command``, honouring quotes the way Bash does."""

    if "\n" in command or "\r" in command:
        return "a line break"
    quote = ""
    index = 0
    while index < len(command):
        char = command[index]
        if quote == "'":
            if char == "'":
                quote = ""
        elif char == "\\":
            index += 1  # the next character is literal
        elif quote == '"':
            if char == '"':
                quote = ""
            elif char in _EXPANDING:
                return _EXPANDING[char]
        elif char in "'\"":
            quote = char
        elif char in _EXPANDING:
            return _EXPANDING[char]
        elif char in _UNQUOTED:
            return _UNQUOTED[char]
        index += 1
    return None


_SAFE_SCHEMES = ("http://", "https://")


def _host_args(program: str, args: list[str]) -> list[str]:
    """The arguments that name paths on this machine (device-side and API paths are not)."""

    if program == "adb":
        words = list(args)
        while words and words[0].startswith("-"):
            words = words[2:] if words[0] in {"-s", "-t", "-H", "-P", "-L"} else words[1:]
        if not words:
            return []
        sub, rest = words[0], words[1:]
        if sub in {"shell", "exec-out", "logcat", "reboot", "devices", "wait-for-device", "forward", "reverse",
                   "start-server", "kill-server", "get-state", "get-serialno", "emu", "uninstall"}:
            return []
        if sub == "pull":
            plain = [arg for arg in rest if not arg.startswith("-")]
            return plain[1:]  # the host destination; the source is on the device
        if sub == "push":
            plain = [arg for arg in rest if not arg.startswith("-")]
            return plain[:-1]  # the host sources; the destination is on the device
        return rest
    if program == "gh":
        words = [arg for arg in args if not arg.startswith("-")]
        if words[:1] == ["api"] and len(words) > 1:
            skip = words[1]  # the API endpoint
            return [arg for arg in args if arg != skip]
    return list(args)


def _segments(command: str) -> list[list[str]]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()")
    lexer.whitespace_split = True
    lexer.commenters = ""
    segments: list[list[str]] = [[]]
    for token in lexer:
        if token in _JOINERS:
            if not segments[-1]:
                raise ValueError(f"empty command before {token!r}")
            segments.append([])
            continue
        if token and set(token) <= set(";&|<>()") and token not in _REDIRECTS and token != ">&":
            raise ValueError(f"{token!r} is not allowed (only ;, &&, ||, |, >, >>, < and 2>&1)")
        segments[-1].append(token)
    if not segments[-1]:
        raise ValueError("a command ends with an operator")
    return segments


def _split_redirects(words: list[str]) -> tuple[list[str], list[str]]:
    """Separate redirect targets from argv. ``2>&1`` and ``>&2`` are fine."""

    argv: list[str] = []
    targets: list[str] = []
    index = 0
    while index < len(words):
        word = words[index]
        if word in _REDIRECTS or word == ">&":
            if argv and argv[-1].isdigit() and argv[-1] in {"0", "1", "2"}:
                argv.pop()  # the file-descriptor number in front of the operator
            if index + 1 >= len(words):
                raise ValueError("a redirect has no target")
            target = words[index + 1]
            if word == ">&":
                if target not in {"1", "2"}:
                    raise ValueError("only 2>&1 and >&2 descriptor redirects are allowed")
            else:
                targets.append(target)
            index += 2
            continue
        argv.append(word)
        index += 1
    return argv, targets


def _program_refusal(argv: list[str], base_branch: str) -> str | None:
    program = Path(argv[0]).name
    args = argv[1:]
    if program in SHELLS:
        return f"{program} would run commands Harbor cannot check; give the commands themselves"
    if program in INDIRECT:
        return f"{program} runs other commands or code; give the command it would run instead"
    if _INTERPRETER.fullmatch(program):
        if not args or any(arg in _INLINE_FLAGS or (arg.startswith("-") and not arg.startswith("--")
                                                   and any(flag in arg for flag in "cepxm")) for arg in args):
            return f"{program} with inline code or no script cannot be checked; run a script file in the worktree"
    if program in {"ln", "link", "mount", "bindfs"}:
        # A link target is resolved from the link's directory, not from here,
        # so Harbor cannot check where it points.
        return f"{program} can make paths inside the worktree point outside it"
    if program == "find" and any(arg in _FIND_EXEC for arg in args):
        return "find -exec/-delete runs or deletes beyond what Harbor can check"
    if "--admin" in args:
        return "--admin bypasses branch protection"
    if program == "gh":
        return _gh_refusal(args)
    if program == "git":
        return _git_refusal(args, base_branch)
    return None


def _gh_refusal(args: list[str]) -> str | None:
    words = [arg for arg in args if not arg.startswith("-")]
    if not words:
        return "gh needs a subcommand"
    group = words[0]
    if group == "api":
        method = None
        for index, arg in enumerate(args):
            if arg in {"-X", "--method"} and index + 1 < len(args):
                method = args[index + 1].upper()
            elif arg.startswith("--method="):
                method = arg.split("=", 1)[1].upper()
            elif arg.startswith("-X") and len(arg) > 2:
                method = arg[2:].upper()
        if method not in {None, "GET", "HEAD"} or any(arg.split("=", 1)[0] in _GH_WRITE_FIELDS for arg in args):
            return "gh api writes are not delegated; only GET requests are"
        if any("graphql" in arg for arg in words[1:]):
            return "gh api graphql can mutate; use a read-only gh subcommand"
        return None
    allowed = GH_ALLOWED.get(group)
    sub = words[1] if len(words) > 1 else ""
    if allowed is None or sub not in allowed:
        return f"gh {group} {sub}".strip() + " is not on Harbor's list of delegated gh commands (merges, reviews and other writes stay with the builder's workflow or a person)"
    return None


def _git_refusal(args: list[str], base_branch: str) -> str | None:
    index = 0
    while index < len(args) and args[index].startswith("-"):
        if args[index] == "-c" or args[index].startswith("-c") or args[index].startswith("--config"):
            return "git -c can run arbitrary commands through configuration"
        if args[index] in {"-C", "--git-dir", "--work-tree"}:
            index += 1  # its value is checked as a path
        index += 1
    if index >= len(args):
        return None
    sub, rest = args[index], args[index + 1:]
    if sub in _GIT_FORBIDDEN:
        return _GIT_FORBIDDEN[sub]
    if sub == "reset" and "--hard" in rest:
        return "git reset --hard discards work across the worktree"
    if sub in {"checkout", "restore", "stash"} and ("." in rest or sub == "stash"):
        return f"git {sub} affects work across the whole worktree"
    if sub == "push":
        if any(arg.split("=", 1)[0] in _PUSH_FORBIDDEN for arg in rest):
            return "git push that forces, deletes, mirrors or skips hooks is not delegated"
        refs = [arg for arg in rest if not arg.startswith("-")][1:]
        base = base_branch.strip() or "main"
        for ref in refs:
            if ref.startswith("+"):
                return "a forced refspec is not delegated"
            if ref.startswith(":"):
                return "deleting a remote branch is not delegated"
            target = ref.split(":", 1)[-1]
            if target in {base, f"refs/heads/{base}"}:
                return f"pushing to or deleting the base branch {base} is not delegated"
    return None


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _static_prefix(pattern: str) -> str:
    cut = len(pattern)
    for char in "*?[":
        found = pattern.find(char)
        if found != -1:
            cut = min(cut, found)
    return pattern[:cut].rstrip("/")


def protected_hit(paths: Iterable[str], patterns: Iterable[str]) -> str | None:
    """The first path that overlaps a protected pattern: a match, inside it, or a directory that contains it."""

    patterns = [p.strip().replace("\\", "/").lstrip("./") for p in patterns if p and p.strip()]
    for path in paths:
        path = path.strip("/")
        if path in {"", "."}:
            if patterns:
                return "."  # the worktree root contains every protected path
            continue
        path = path.lstrip("./")  # the same normalisation as the ladder's protected patterns
        for pattern in patterns:
            prefix = _static_prefix(pattern)
            if (fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(path, pattern.rstrip("/*") or pattern)
                    or (prefix and (path == prefix or path.startswith(prefix + "/") or prefix.startswith(path + "/")))):
                return path
    return None


def command_refusal(
    commands: Sequence[str],
    cwd: Path,
    worktree: Path,
    *,
    protected: Iterable[str] = (),
    temp_dir: Path | None = None,
    secrets: Sequence[str] = (),
    base_branch: str = "main",
) -> str | None:
    """Why Harbor will not hand these commands to the operator, or None when every check passes."""

    worktree = worktree.resolve()
    cwd = cwd.resolve()
    if not commands:
        return "the blocker names no commands"
    if len(commands) > MAX_COMMANDS:
        return f"the blocker names {len(commands)} commands; Harbor delegates at most {MAX_COMMANDS}"
    if not _inside(cwd, worktree):
        return f"the requested working directory {cwd} is outside the ticket worktree"
    roots = [worktree] + ([temp_dir.resolve()] if temp_dir is not None else [])
    touched: list[str] = []
    protected = list(protected)
    for command in commands:
        if len(command) > MAX_COMMAND_CHARS:
            return "a command is longer than Harbor delegates"
        if any(value and value in command for value in secrets):
            return "a command contains a credential value"
        what = _expansion(command)
        if what is not None:
            return f"{what} cannot be checked before it runs: {command}"
        try:
            segments = _segments(command)
        except ValueError as exc:
            return f"cannot check command {command!r}: {exc}"
        for words in segments:
            try:
                argv, targets = _split_redirects(words)
            except ValueError as exc:
                return f"cannot check command {command!r}: {exc}"
            if not argv:
                return f"cannot check command {command!r}: no program"
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", argv[0]):
                return f"a variable assignment in front of a command is not delegated: {command}"
            refusal = _program_refusal(argv, base_branch)
            if refusal:
                return refusal
            program, *args = argv
            candidates = [*targets]
            if "/" in program:
                candidates.append(program)  # a program by path must live in the worktree
            for arg in _host_args(Path(program).name, args):
                values = [arg]
                if arg.startswith("-"):
                    # A flag's value: --out=PATH, or a short flag glued to it (-oPATH).
                    if "=" in arg:
                        values = [arg.split("=", 1)[1]]
                    elif not arg.startswith("--") and len(arg) > 2 and ("/" in arg or ".." in arg):
                        values = [arg[2:]]
                    else:
                        continue
                for value in values:
                    if not value:
                        continue
                    if value.startswith(_SAFE_SCHEMES):
                        continue  # a web URL, not a path
                    if "://" in value or value.startswith("file:"):
                        return f"only http(s) URLs are delegated: {value}"
                    candidates.append(value)
                    if value.startswith("@"):
                        candidates.append(value[1:])  # curl/gh read @FILE
            for word in candidates:
                if word in _DEVICES:
                    continue
                resolved = (cwd / word).resolve()  # follows symlinks
                inside = next((root for root in roots if _inside(resolved, root)), None)
                if inside is None:
                    return f"a command reaches outside the ticket worktree: {word}"
                if inside == worktree:
                    touched.append(str(resolved.relative_to(worktree)))
    hit = protected_hit(touched, protected)
    if hit is not None:
        return f"a command touches a protected path: {hit}"
    return None


__all__ = ["GH_ALLOWED", "INDIRECT", "SHELLS", "command_refusal", "protected_hit"]
