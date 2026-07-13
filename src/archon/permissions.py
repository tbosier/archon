"""Team-lead auto-approval policy engine for coding-agent shell commands.

This module is **safety-critical**. It decides whether a shell command that a
coding agent (worker) wants to run should be:

* :attr:`Decision.ALLOW`    — auto-approved (low-risk, reversible, sandboxed to
  the worktree/project),
* :attr:`Decision.DENY`     — hard-denied (never auto-approved; only a human
  override may permit it), or
* :attr:`Decision.ESCALATE` — surfaced to a human via the attention inbox.

The single most important invariant: **dangerous commands must NEVER be
auto-approved.** The engine therefore fails safe — anything that is not clearly
on the allowlist and not on the denylist becomes :attr:`Decision.ESCALATE`, and
within a compound command *deny always wins*.

Everything here is pure-stdlib (``re``, ``shlex``, ``dataclasses``, ``enum``) so
it can be imported and audited in isolation.

Public interface
----------------
* :class:`Decision`
* :class:`PermissionVerdict`
* :func:`evaluate_permission`
* :data:`DENY_RULES` / :data:`ALLOW_RULES` — human-readable ``(rule_id,
  description)`` catalogues of the policy, for auditing/printing.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from enum import Enum


class Decision(str, Enum):
    """Outcome of a permission evaluation."""

    ALLOW = "allow"        # auto-approve: low-risk, reversible, sandboxed to the worktree
    DENY = "deny"          # hard-deny: never auto-approve; only a human override may permit
    ESCALATE = "escalate"  # ask the human via the attention inbox


@dataclass
class PermissionVerdict:
    """A single, auditable policy decision about one command."""

    decision: Decision
    reason: str                 # human-readable, for the attention inbox / audit log
    matched_rule: str | None    # identifier of the rule that fired, for auditing


# ---------------------------------------------------------------------------
# Policy catalogues (exposed for auditing / printing the policy).
#
# Each entry is (rule_id, human_description). These describe the *families* of
# checks; the concrete matching logic lives in the functions below and always
# reports one of these rule_ids as ``matched_rule``.
# ---------------------------------------------------------------------------

DENY_RULES: list[tuple[str, str]] = [
    ("deny:fork-bomb", "Fork bomb / self-replicating shell payload, e.g. :(){ :|:&};:"),
    ("deny:sudo", "Privilege escalation: sudo / su / doas / pkexec"),
    ("deny:rm-recursive", "Recursive removal (rm -r/-R/-rf/-fr/--recursive)"),
    ("deny:rm-force-glob", "Forced removal of a glob or directory (rm -f with * / dir)"),
    ("deny:rm-dangerous-target", "rm targeting /, ~, $HOME, * or a filesystem root"),
    ("deny:rm-absolute-outside-worktree", "rm of an absolute path outside the worktree"),
    ("deny:git-push", "git push (workers must never push — any push is denied)"),
    ("deny:git-reset-hard", "git reset --hard (destroys working tree / history)"),
    ("deny:git-rebase", "git rebase (history rewriting)"),
    ("deny:git-commit-amend", "git commit --amend (history rewriting)"),
    ("deny:git-filter", "git filter-branch / filter-repo (history rewriting)"),
    ("deny:git-reflog-delete", "git reflog delete (destroys recovery history)"),
    ("deny:git-update-ref-delete", "git update-ref -d (deletes a ref)"),
    ("deny:git-gc-prune", "git gc --prune (drops unreachable objects)"),
    ("deny:git-clean", "git clean -f/-fd/-fdx (deletes untracked files)"),
    ("deny:git-branch-delete", "git branch -d/-D (branch destruction)"),
    ("deny:git-worktree-destroy", "git worktree remove/prune (worktree destruction)"),
    ("deny:git-tag-delete", "git tag -d (tag destruction)"),
    ("deny:secrets", "Reference to credentials / secrets / sensitive config (.env, .ssh, keys, ...)"),
    ("deny:env-exfil", "Environment-variable exfiltration (env / printenv / /proc/*/environ)"),
    ("deny:pipe-to-shell", "Piping a network download straight into a shell (curl ... | sh)"),
    ("deny:rce", "Remote code execution pattern (eval $(curl ...), iex(...), bash <(curl ...))"),
    ("deny:network-tool", "Bare network tool (curl/wget/nc/ssh/scp/rsync/telnet/ftp)"),
    ("deny:system-write", "Write/redirect into a system path (/etc, /usr, /bin, /boot, /sys, /var, /dev/sd*)"),
    ("deny:chmod-chown-system", "chmod/chown on a path outside the worktree / on a system path"),
    ("deny:system-control", "System control: systemctl/service/launchctl/shutdown/reboot/crontab/iptables/ufw"),
    ("deny:disk", "Disk-destroying command (mkfs, dd if=/of=, > /dev/sd*)"),
    ("deny:kill-all", "kill -9 -1 (signals every process)"),
    ("deny:system-package-manager", "System package manager (apt/yum/dnf/pacman/brew install/snap install/...)"),
    ("deny:find-destructive", "find with -delete or -exec (destructive traversal)"),
]

ALLOW_RULES: list[tuple[str, str]] = [
    ("allow:package-install", "Project-local dependency install (pip/uv/poetry/npm/yarn/pnpm/cargo/go/bundle ...)"),
    ("allow:tests", "Running the test suite (pytest/tox/nox/unittest/jest/go test/cargo test/...)"),
    ("allow:lint-format-typecheck", "Linters/formatters/type-checkers (ruff/black/mypy/eslint/prettier/clippy/...)"),
    ("allow:git-readonly", "Read-only git inspection (status/diff/log/show/branch-list/fetch/...)"),
    ("allow:git-branch-create", "Creating a branch in the worktree (checkout -b / switch -c / branch <new>)"),
    ("allow:safe-read", "Read-only file inspection / navigation (cat/ls/grep/rg/find/sed -n/awk/jq/...)"),
    ("allow:build", "Non-destructive project-local build (npm/yarn/pnpm run build, cargo build, go build)"),
]


# ---------------------------------------------------------------------------
# Tokenisation helpers
# ---------------------------------------------------------------------------

def _tokenize(cmd: str) -> list[str]:
    """Best-effort shell tokenisation that never raises."""
    try:
        return shlex.split(cmd, comments=False, posix=True)
    except ValueError:
        # Unbalanced quotes etc. — fall back to a naive split so we still get
        # *some* tokens to inspect. Failing safe: unparsable stays escalate/deny
        # via the substring checks below.
        return cmd.split()


def _basename(prog: str) -> str:
    """Program basename, lower-cased (``/usr/bin/Sudo`` -> ``sudo``)."""
    return prog.rsplit("/", 1)[-1].lower()


def _is_flag(tok: str) -> bool:
    return tok.startswith("-") and tok != "-"


def _norm_path(path: str) -> str:
    return path.rstrip("/") or "/"


def _under_worktree(path: str, worktree_path: str | None) -> bool:
    """True if ``path`` is inside ``worktree_path`` (both absolute)."""
    if worktree_path is None:
        return False
    if not path.startswith("/"):
        return True  # relative paths are, by construction, inside the cwd/worktree
    wt = _norm_path(worktree_path)
    p = _norm_path(path)
    return p == wt or p.startswith(wt + "/")


_SYSTEM_PREFIXES = (
    "/etc", "/usr", "/bin", "/sbin", "/boot", "/sys", "/proc",
    "/var", "/dev", "/lib", "/lib64", "/system", "/opt",
)


def _is_system_path(path: str) -> bool:
    p = path.lower()
    return any(p == pre or p.startswith(pre + "/") for pre in _SYSTEM_PREFIXES)


# ---------------------------------------------------------------------------
# Compound-command splitting
# ---------------------------------------------------------------------------

def _extract_substitutions(text: str) -> tuple[str, list[str]]:
    """Pull out command/process substitutions.

    Handles ``$(...)``, backticks ``\\`...\\``` and ``<(...)`` / ``>(...)``.
    Returns the outer text (with each substitution replaced by a space) and the
    list of inner sub-command strings. Balanced parentheses are respected so
    nested substitutions survive to be recursed into by the caller.
    """
    subs: list[str] = []
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "`":
            j = text.find("`", i + 1)
            if j == -1:
                out.append(text[i:])
                break
            subs.append(text[i + 1:j])
            out.append(" ")
            i = j + 1
            continue
        if (ch == "$" and i + 1 < n and text[i + 1] == "(") or (
            ch in "<>" and i + 1 < n and text[i + 1] == "("
        ):
            start = i + 2
            depth = 1
            k = start
            while k < n and depth > 0:
                if text[k] == "(":
                    depth += 1
                elif text[k] == ")":
                    depth -= 1
                k += 1
            inner = text[start:k - 1] if depth == 0 else text[start:]
            subs.append(inner)
            out.append(" ")
            i = k
            continue
        out.append(ch)
        i += 1
    return "".join(out), subs


_OPERATOR_RE = re.compile(r"\|\||&&|;|\||\n|&")


def _atomic_subcommands(command: str) -> list[str]:
    """Split a (possibly compound) command into atomic sub-commands.

    Splits on ``;``, ``&&``, ``||``, ``|``, ``&`` and newlines, and recursively
    pulls out the inner content of command/process substitutions so each is
    evaluated on its own.
    """
    outer, subs = _extract_substitutions(command)
    parts = [p.strip() for p in _OPERATOR_RE.split(outer) if p.strip()]
    result: list[str] = list(parts)
    for s in subs:
        result.extend(_atomic_subcommands(s))
    return result


# ---------------------------------------------------------------------------
# Whole-string pre-checks (patterns that operator-splitting would mangle)
# ---------------------------------------------------------------------------

_FORK_BOMB_RE = re.compile(r":\s*\(\s*\)\s*\{")


def _whole_string_deny(command: str) -> tuple[str, str] | None:
    if _FORK_BOMB_RE.search(command):
        return "deny:fork-bomb", "Fork bomb detected"
    return None


# ---------------------------------------------------------------------------
# Secret / sensitive-path detection
# ---------------------------------------------------------------------------

# Each entry matched case-insensitively as a regex against the raw command.
_SECRET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\.env(\.|\b)"),            # .env, .env.local, .env.production
    re.compile(r"\.ssh\b"),
    re.compile(r"\bid_rsa\b"),
    re.compile(r"\bid_ed25519\b"),
    re.compile(r"\.pem\b"),
    re.compile(r"\.key\b"),
    re.compile(r"\bcredentials\b"),
    re.compile(r"\.aws\b"),
    re.compile(r"\.config/gcloud\b"),
    re.compile(r"\.kube\b"),
    re.compile(r"\.netrc\b"),
    re.compile(r"\.npmrc\b"),
    re.compile(r"\.pypirc\b"),
    re.compile(r"\.git-credentials\b"),
    re.compile(r"\bsecrets?\b"),
    re.compile(r"\.gnupg\b"),
    re.compile(r"\.docker/config\.json\b"),
    re.compile(r"\.terraformrc\b"),
]


def _secret_match(cmd_lower: str) -> tuple[str, str] | None:
    for pat in _SECRET_PATTERNS:
        m = pat.search(cmd_lower)
        if m:
            return "deny:secrets", f"References sensitive credential/config ({m.group(0)!r})"
    return None


# ---------------------------------------------------------------------------
# DENY logic (per atomic sub-command)
# ---------------------------------------------------------------------------

_GIT_REDIRECT_RE = re.compile(
    r">{1,2}\s*(/etc|/usr|/bin|/sbin|/boot|/sys|/var|/dev/sd|/system|/opt|/lib)\b",
    re.IGNORECASE,
)
_TEE_SYSTEM_RE = re.compile(
    r"\btee\b[^|]*\s(/etc|/usr|/bin|/sbin|/boot|/sys|/var|/dev/sd|/system|/opt|/lib)\b",
    re.IGNORECASE,
)
_PIPE_TO_SHELL_RE = re.compile(
    r"\b(curl|wget|fetch)\b[^|]*\|\s*(sudo\s+)?(sh|bash|zsh|dash|ksh)\b",
    re.IGNORECASE,
)

_NETWORK_TOOLS = {
    "curl", "wget", "nc", "ncat", "netcat", "telnet", "ssh", "scp",
    "rsync", "ftp", "sftp", "socat",
}
_SYSTEM_PKG = {
    "apt", "apt-get", "aptitude", "dpkg", "yum", "dnf", "pacman", "zypper",
    "rpm", "snap", "brew", "port", "emerge",
}
_SYSTEM_CONTROL = {
    "systemctl", "service", "launchctl", "shutdown", "reboot", "halt",
    "poweroff", "crontab", "iptables", "ip6tables", "ufw", "nft",
    "mount", "umount", "sysctl",
}


def _deny_rm(tokens: list[str], worktree_path: str | None) -> tuple[str, str] | None:
    flags = [t for t in tokens[1:] if _is_flag(t)]
    targets = [t for t in tokens[1:] if not _is_flag(t)]

    def has_letter(letter: str) -> bool:
        for f in flags:
            if f.startswith("--"):
                continue
            if letter in f[1:]:
                return True
        return False

    recursive = (
        has_letter("r") or has_letter("R")
        or "--recursive" in flags or "--dir" in flags or has_letter("d")
    )
    force = has_letter("f") or "--force" in flags

    if recursive:
        return "deny:rm-recursive", "Recursive removal is never auto-approved"

    for tgt in targets:
        low = tgt.lower()
        if tgt in ("/", "*", "/*", "~", "~/", "~/*") or low in ("$home", "${home}"):
            return "deny:rm-dangerous-target", f"rm of dangerous target {tgt!r}"
        if tgt.startswith("~") or "$home" in low or "${home}" in low:
            return "deny:rm-dangerous-target", f"rm of home directory {tgt!r}"
        if "*" in tgt or "?" in tgt:
            return "deny:rm-force-glob", f"rm of a glob pattern {tgt!r}"
        if tgt.startswith("/"):
            if not _under_worktree(tgt, worktree_path):
                return (
                    "deny:rm-absolute-outside-worktree",
                    f"rm of absolute path {tgt!r} outside the worktree",
                )
    if force and any(t in (".", "..") for t in targets):
        return "deny:rm-force-glob", "Forced removal of a directory"
    return None


def _deny_git(tokens: list[str]) -> tuple[str, str] | None:
    rest = tokens[1:]
    if not rest:
        return None
    sub = rest[0].lower()
    joined = " ".join(rest).lower()

    if sub == "push":
        return "deny:git-push", "Workers must never push"
    if sub == "reset" and ("--hard" in rest):
        return "deny:git-reset-hard", "git reset --hard destroys work"
    if sub == "rebase":
        return "deny:git-rebase", "git rebase rewrites history"
    if sub == "commit" and "--amend" in rest:
        return "deny:git-commit-amend", "git commit --amend rewrites history"
    if sub in ("filter-branch", "filter-repo"):
        return "deny:git-filter", "git filter-* rewrites history"
    if sub == "reflog" and "delete" in rest:
        return "deny:git-reflog-delete", "git reflog delete destroys recovery history"
    if sub == "update-ref" and "-d" in rest:
        return "deny:git-update-ref-delete", "git update-ref -d deletes a ref"
    if sub == "gc" and "--prune" in joined:
        return "deny:git-gc-prune", "git gc --prune drops objects"
    if sub == "clean" and any(_is_flag(t) and "f" in t for t in rest[1:]):
        return "deny:git-clean", "git clean -f deletes untracked files"
    if sub == "branch" and any(t in ("-d", "-D", "--delete") for t in rest[1:]):
        return "deny:git-branch-delete", "git branch delete"
    if sub == "worktree" and len(rest) > 1 and rest[1].lower() in ("remove", "prune"):
        return "deny:git-worktree-destroy", "git worktree remove/prune"
    if sub == "tag" and any(t in ("-d", "--delete") for t in rest[1:]):
        return "deny:git-tag-delete", "git tag delete"
    return None


def _deny_match(cmd: str, worktree_path: str | None) -> tuple[str, str] | None:
    """Return ``(rule_id, reason)`` if ``cmd`` (a single sub-command) is denied."""
    cmd_lower = cmd.lower()
    tokens = _tokenize(cmd)
    if not tokens:
        return None
    prog = _basename(tokens[0])

    # 1. Privilege escalation (also catches `sudo <anything>`).
    if prog in ("sudo", "su", "doas", "pkexec") or "sudo" in (
        _basename(t) for t in tokens
    ):
        return "deny:sudo", "Privilege escalation is never auto-approved"

    # 2. Secrets / sensitive config (read OR write).
    sec = _secret_match(cmd_lower)
    if sec:
        return sec

    # 3. Environment exfiltration.
    if prog in ("env", "printenv") and not any(
        t.startswith(("PATH=",)) for t in tokens
    ):
        # A bare `env`/`printenv` (or with args) dumps the environment.
        return "deny:env-exfil", "Environment dump is never auto-approved"
    if re.search(r"/proc/[^/]*/environ", cmd_lower):
        return "deny:env-exfil", "Reading a process environment is never auto-approved"

    # 4. Remote-code-execution / pipe-to-shell.
    if _PIPE_TO_SHELL_RE.search(cmd):
        return "deny:pipe-to-shell", "Piping a download into a shell"
    if prog == "iex" or re.search(r"\biex\s*\(", cmd_lower):
        return "deny:rce", "iex(...) remote code execution"
    if prog in ("eval", ".", "source") and re.search(r"(curl|wget)", cmd_lower):
        return "deny:rce", "eval/source of a network download"

    # 5. Bare network tools.
    if prog in _NETWORK_TOOLS:
        return "deny:network-tool", f"Bare network tool {prog!r} is never auto-approved"

    # 6. Find with destructive actions.
    if prog == "find" and ("-delete" in tokens or "-exec" in tokens or "-execdir" in tokens):
        return "deny:find-destructive", "find -delete/-exec is destructive"

    # 7. rm family.
    if prog == "rm":
        r = _deny_rm(tokens, worktree_path)
        if r:
            return r

    # 8. git family.
    if prog == "git":
        r = _deny_git(tokens)
        if r:
            return r

    # 9. System package managers.
    if prog in _SYSTEM_PKG:
        # brew/snap: only the install-ish subcommands are invasive, but be
        # conservative and deny the whole family (they touch the system).
        return "deny:system-package-manager", f"System package manager {prog!r}"

    # 10. System control / daemons.
    if prog in _SYSTEM_CONTROL:
        return "deny:system-control", f"System control command {prog!r}"

    # 11. Disk-destroying commands.
    if prog.startswith("mkfs"):
        return "deny:disk", "mkfs formats a filesystem"
    if prog == "dd" and re.search(r"\b(if|of)=", cmd_lower):
        return "deny:disk", "dd with if=/of= can destroy a disk"
    if re.search(r">{1,2}\s*/dev/(sd|nvme|disk|hd)", cmd_lower):
        return "deny:disk", "Redirect into a raw disk device"

    # 12. kill -9 -1 (signal everything).
    if prog in ("kill", "pkill", "killall") and "-1" in tokens:
        return "deny:kill-all", "Signalling every process is never auto-approved"

    # 13. chmod / chown outside the worktree.
    if prog in ("chmod", "chown", "chgrp"):
        for t in tokens[1:]:
            if _is_flag(t):
                continue
            if t.startswith("/") and (
                _is_system_path(t) or not _under_worktree(t, worktree_path)
            ):
                return (
                    "deny:chmod-chown-system",
                    f"{prog} on {t!r} outside the worktree",
                )

    # 14. Writes / redirects into system paths.
    if _GIT_REDIRECT_RE.search(cmd) or _TEE_SYSTEM_RE.search(cmd):
        return "deny:system-write", "Write/redirect into a system path"

    return None


# ---------------------------------------------------------------------------
# ALLOW logic (per atomic sub-command; only consulted after DENY passes)
# ---------------------------------------------------------------------------

_GLOBAL_INSTALL_FLAGS = ("--target", "-t", "--prefix", "--root", "-g", "--global", "--user")

_PKG_INSTALL_SUBS: dict[str, set[str]] = {
    "pip": {"install"},
    "pip3": {"install"},
    "uv": {"add", "sync", "lock"},
    "poetry": {"add", "install", "lock", "sync"},
    "pipenv": {"install", "sync"},
    "npm": {"install", "ci", "i"},
    "yarn": {"add", "install"},
    "pnpm": {"install", "add", "i"},
    "bun": {"install", "add", "i"},
    "cargo": {"add", "fetch"},
    "bundle": {"install"},
}

_TEST_PROGS = {
    "pytest", "tox", "nox", "jest", "vitest", "mocha", "rspec", "phpunit",
}
_TEST_SUBS: dict[str, set[str]] = {
    "go": {"test"},
    "cargo": {"test"},
    "mvn": {"test"},
    "gradle": {"test"},
    "npm": {"test"},
    "yarn": {"test"},
    "pnpm": {"test"},
}

_LINT_PROGS = {
    "ruff", "black", "isort", "flake8", "pylint", "mypy", "pyright",
    "pyflakes", "eslint", "prettier", "tsc", "gofmt", "golangci-lint",
    "clippy", "rubocop", "autopep8", "yapf",
}
_LINT_SUBS: dict[str, set[str]] = {
    "go": {"vet", "fmt"},
    "cargo": {"fmt", "clippy", "check"},
}

_GIT_READONLY_SUBS = {
    "status", "diff", "log", "show", "rev-parse", "ls-files", "blame",
    "fetch", "stash", "remote", "describe", "shortlog", "cat-file",
    "rev-list", "whatchanged", "grep",
}

_SAFE_READ_PROGS = {
    "cat", "less", "more", "head", "tail", "ls", "pwd", "echo", "which",
    "whereis", "wc", "tree", "stat", "file", "grep", "rg", "ripgrep", "fd",
    "jq", "diff", "cut", "sort", "uniq", "du", "df", "basename", "dirname",
    "realpath", "date", "true", "test", "printf", "column", "nl", "tac",
    "yq", "xxd", "od", "type", "man", "help",
}

_BUILD_SUBS: dict[str, set[str]] = {
    "npm": {"build"},      # via `npm run build`
    "yarn": {"build"},
    "pnpm": {"build"},
    "cargo": {"build"},
    "go": {"build"},
}


def _run_subcommand(tokens: list[str], prog: str) -> str | None:
    """For `npm run X` / `yarn run X`, return X; for `yarn X` return X."""
    rest = tokens[1:]
    if not rest:
        return None
    if rest[0] == "run" and len(rest) > 1:
        return rest[1].lower()
    return rest[0].lower()


def _allow_package_install(tokens: list[str], prog: str, cmd: str) -> str | None:
    rest = tokens[1:]
    # python -m pip install ...
    if prog in ("python", "python3") and len(rest) >= 3 and rest[0] == "-m" and rest[1] == "pip":
        if rest[2] == "install":
            if any(f in tokens for f in _GLOBAL_INSTALL_FLAGS):
                return None  # global/target install -> escalate
            return "allow:package-install"
        return None
    # uv pip install ...
    if prog == "uv" and len(rest) >= 2 and rest[0] == "pip" and rest[1] == "install":
        if any(f in tokens for f in _GLOBAL_INSTALL_FLAGS):
            return None
        return "allow:package-install"
    # go get / go mod download
    if prog == "go":
        if rest[:1] == ["get"] or rest[:2] == ["mod", "download"]:
            return "allow:package-install"
        return None
    # bare `yarn` (no args) == yarn install
    if prog == "yarn" and not rest:
        return "allow:package-install"
    subs = _PKG_INSTALL_SUBS.get(prog)
    if subs and rest and rest[0].lower() in subs:
        if any(f in tokens for f in _GLOBAL_INSTALL_FLAGS):
            return None  # global/target/user install -> escalate (not sandboxed)
        return "allow:package-install"
    return None


def _allow_match(cmd: str, worktree_path: str | None) -> str | None:
    tokens = _tokenize(cmd)
    if not tokens:
        return None
    prog = _basename(tokens[0])
    rest = tokens[1:]

    # Package installs (checked first; may deliberately fall through to escalate
    # for global/target installs).
    r = _allow_package_install(tokens, prog, cmd)
    if r:
        return r

    # Tests.
    if prog in _TEST_PROGS:
        return "allow:tests"
    if prog in ("python", "python3") and rest[:2] == ["-m", "pytest"]:
        return "allow:tests"
    if prog in ("python", "python3") and rest[:2] == ["-m", "unittest"]:
        return "allow:tests"
    if prog == "unittest":
        return "allow:tests"
    if prog in _TEST_SUBS and rest and rest[0].lower() in _TEST_SUBS[prog]:
        return "allow:tests"
    if prog in ("npm", "yarn", "pnpm") and rest[:2] == ["run", "test"]:
        return "allow:tests"

    # Linters / formatters / type-checkers.
    if prog in _LINT_PROGS:
        return "allow:lint-format-typecheck"
    if prog in ("python", "python3") and rest[:2] in (["-m", "ruff"], ["-m", "black"],
                                                       ["-m", "mypy"], ["-m", "flake8"],
                                                       ["-m", "isort"], ["-m", "pylint"]):
        return "allow:lint-format-typecheck"
    if prog in _LINT_SUBS and rest and rest[0].lower() in _LINT_SUBS[prog]:
        return "allow:lint-format-typecheck"
    if prog == "go" and rest[:1] == ["vet"]:
        return "allow:lint-format-typecheck"

    # git branch creation (must precede read-only branch listing).
    if prog == "git" and rest:
        sub = rest[0].lower()
        if sub == "checkout" and "-b" in rest:
            return "allow:git-branch-create"
        if sub == "switch" and "-c" in rest:
            return "allow:git-branch-create"
        if sub == "branch":
            args = rest[1:]
            # `git branch <newname>` creates; `git branch` / `git branch -a` list.
            named = [a for a in args if not _is_flag(a)]
            if named and not any(a in ("-d", "-D", "--delete") for a in args):
                return "allow:git-branch-create"
        # Read-only git.
        if sub in _GIT_READONLY_SUBS:
            return "allow:git-readonly"
        if sub == "branch":  # listing form
            return "allow:git-readonly"

    # Build (project-local, non-destructive).
    if prog in _BUILD_SUBS:
        run_sub = _run_subcommand(tokens, prog)
        if run_sub in _BUILD_SUBS[prog]:
            return "allow:build"

    # Safe reads / navigation.
    if prog in _SAFE_READ_PROGS:
        return "allow:safe-read"
    if prog == "find":
        # find is a safe read unless destructive (destructive already denied).
        return "allow:safe-read"
    if prog == "sed":
        # Only the print-only form (-n, no in-place -i) is auto-approved.
        if any(t == "-i" or t.startswith("-i") and "i" in t for t in rest):
            return None  # in-place edit -> escalate
        if "-n" in rest:
            return "allow:safe-read"
        return None
    if prog == "awk":
        if "system(" in cmd.replace(" ", "").lower():
            return None  # awk shelling out -> escalate
        return "allow:safe-read"

    return None


# ---------------------------------------------------------------------------
# Public evaluation entry point
# ---------------------------------------------------------------------------

def _evaluate_atomic(cmd: str, worktree_path: str | None) -> PermissionVerdict:
    deny = _deny_match(cmd, worktree_path)
    if deny:
        rule_id, reason = deny
        return PermissionVerdict(Decision.DENY, reason, rule_id)
    allow = _allow_match(cmd, worktree_path)
    if allow:
        return PermissionVerdict(Decision.ALLOW, "Matches auto-approve allowlist", allow)
    return PermissionVerdict(
        Decision.ESCALATE,
        "Command is neither on the allowlist nor the denylist; ask a human",
        None,
    )


def evaluate_permission(
    command: str,
    *,
    worktree_path: str | None = None,
) -> PermissionVerdict:
    """Decide whether ``command`` may be auto-approved.

    The command is split into atomic sub-commands (across ``;``, ``&&``, ``||``,
    ``|``, ``&``, newlines and command/process substitutions). Each is evaluated
    independently and the results are combined with **deny-wins** semantics:

    * any sub-command DENY  -> the whole command is DENY,
    * else any ESCALATE     -> ESCALATE,
    * else (all ALLOW)      -> ALLOW.

    Args:
        command: The shell command the worker wants to run.
        worktree_path: Absolute path the worker is sandboxed to. When provided,
            absolute-path operations outside it are treated as unsafe.

    Returns:
        A :class:`PermissionVerdict` with an auditable ``matched_rule``.
    """
    if command is None or not command.strip():
        return PermissionVerdict(
            Decision.ESCALATE, "Empty command", None
        )

    whole = _whole_string_deny(command)
    if whole:
        rule_id, reason = whole
        return PermissionVerdict(Decision.DENY, reason, rule_id)

    subcommands = _atomic_subcommands(command)
    if not subcommands:
        return PermissionVerdict(Decision.ESCALATE, "No runnable command found", None)

    verdicts = [_evaluate_atomic(sc, worktree_path) for sc in subcommands]

    # deny wins.
    for v in verdicts:
        if v.decision is Decision.DENY:
            return v
    # then escalate.
    for v in verdicts:
        if v.decision is Decision.ESCALATE:
            return v
    # all allow.
    if len(verdicts) == 1:
        return verdicts[0]
    rules = ", ".join(dict.fromkeys(v.matched_rule for v in verdicts if v.matched_rule))
    return PermissionVerdict(
        Decision.ALLOW,
        f"All {len(verdicts)} sub-commands matched the allowlist",
        verdicts[0].matched_rule if len(verdicts) == 1 else f"allow:compound[{rules}]",
    )
