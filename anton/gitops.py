"""Git helpers. The GitHub token only travels via environment variables, never argv or URLs."""
from __future__ import annotations

import base64
import fnmatch
import os
import re
import subprocess
import unicodedata
from pathlib import Path

from .config import BRANCH_PREFIX

# The working tree is attacker-influenced (npm scripts, model output). Never run hooks or
# fsmonitor programs from it, and ignore system/global git config (no lfs or other filter drivers).
SAFE_FLAGS = ["-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
              "-c", "protocol.ext.allow=never"]
ISOLATED_CONFIG = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}

# Matched case-insensitively and at ANY depth. A job that touches these fails, nothing is pushed.
ALWAYS_PROTECTED = (
    ".github/*", ".git/*", ".gitmodules", ".gitattributes", ".npmrc", ".yarnrc*", ".env*",
    "*.pem", "*.key", "node_modules/*", "build/*", ".claude/*", ".mcp.json",
)
BRANCH_RE = re.compile(r"anton/[a-z0-9-]{1,80}")


def bot_email(bot_name: str, bot_id: int) -> str:
    return f"{bot_id}+{bot_name}@users.noreply.github.com"


def identity_env(bot_name: str, bot_id: int) -> dict:
    email = bot_email(bot_name, bot_id)
    return {"GIT_AUTHOR_NAME": bot_name, "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": bot_name, "GIT_COMMITTER_EMAIL": email}


def auth_env(token: str, bot_name: str, bot_id: int) -> dict:
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
        "GIT_TERMINAL_PROMPT": "0",
        **identity_env(bot_name, bot_id),
    }


def git_cmd(args: list[str]) -> list[str]:
    return ["git", *SAFE_FLAGS, *args]


def git(args: list[str], cwd: Path | None = None, env: dict | None = None) -> str:
    full = {**os.environ, **ISOLATED_CONFIG, **(env or {})}
    p = subprocess.run(git_cmd(args), cwd=cwd, env=full, capture_output=True, text=True)
    if p.returncode != 0:
        # never echo env; stderr from git does not contain the header
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {p.stderr.strip()[:400]}")
    return p.stdout


def ascii_slug(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")


def branch_name(issue: int | None, job_id: str, title: str | None = None) -> str:
    """Always English/ASCII and unique per job, so a retry never collides with an earlier branch.
    `title` is Claude's English title; without it the name is neutral (`anton/work-<id>`)."""
    suffix = re.sub(r"[^a-z0-9]", "", job_id.lower())[-6:] or "job"
    if issue is not None:
        return f"{BRANCH_PREFIX}issue-{int(issue)}-{suffix}"
    slug = ascii_slug(title or "")[:30].strip("-")
    return f"{BRANCH_PREFIX}{slug or 'work'}-{suffix}"


def assert_pushable(branch: str, base: str) -> None:
    if branch == base or not BRANCH_RE.fullmatch(branch):
        raise RuntimeError(f"refusing to push branch {branch!r}")


def assert_updatable(branch: str, base: str) -> None:
    """Pushing to the branch of an EXISTING pull request: any normal branch, never the base branch."""
    from .prs import safe_branch  # local import: prs is a leaf module, gitops is imported first
    if branch == base or not safe_branch(branch):
        raise RuntimeError(f"refusing to push branch {branch!r}")


def _hit(path: str, pattern: str) -> bool:
    p, q = path.casefold(), pattern.casefold()
    return fnmatch.fnmatchcase(p, q) or fnmatch.fnmatchcase(p, "*/" + q)


def odd_path(raw: str) -> bool:
    """Path shapes that never come from a normal checkout: absolute, '..', trailing slash, control characters."""
    path = raw[2:] if raw.startswith("./") else raw
    parts = path.split("/")
    return (path.startswith("/") or path.endswith("/") or ".." in parts or "" in parts
            or any(ord(c) < 32 or ord(c) == 127 for c in path))


def is_protected(raw: str, protected: tuple[str, ...]) -> bool:
    path = raw[2:] if raw.startswith("./") else raw
    return any(_hit(path, pat) for pat in (*ALWAYS_PROTECTED, *protected))


def violations(changed: list[str], protected: tuple[str, ...]) -> list[str]:
    """Paths Claude must not change. Odd path shapes are violations too."""
    return [raw for raw in changed if odd_path(raw) or is_protected(raw, protected)]


def changed_files(cwd: Path) -> list[str]:
    out = git(["status", "--porcelain", "-z", "--untracked-files=all", "--no-renames"], cwd)
    return [e[3:] for e in out.split("\0") if e]


def parse_raw(out: str) -> list[tuple[str, str, str]]:
    """Parse `git diff --cached --raw -z --no-renames` into (new_mode, status, path)."""
    toks = out.split("\0")
    entries, i = [], 0
    while i < len(toks):
        if toks[i].startswith(":") and i + 1 < len(toks):
            meta = toks[i][1:].split()
            entries.append((meta[1], meta[4][:1], toks[i + 1]))
            i += 2
        else:
            i += 1
    return entries


def split_staged(cwd: Path, protected: tuple[str, ...]) -> tuple[list[str], list[str]]:
    """Look at what is staged and split it: (hard problems, protected paths).
    Hard problems (symlinks, nested repositories, odd paths) always fail the job. Protected paths (CI workflows,
    .env, ...) are never pushed, but the change can be handed to the user as a patch."""
    out = git(["diff", "--cached", "--raw", "-z", "--no-renames", "--no-abbrev"], cwd)
    hard, protected_paths = [], []
    for mode, _status, path in parse_raw(out):
        if mode == "120000":
            hard.append(f"{path} (symlink)")
        elif mode == "160000":
            hard.append(f"{path} (nested repository)")
        elif odd_path(path):
            hard.append(f"{path!r} (unexpected path)")
        elif is_protected(path, protected):
            protected_paths.append(path)
    return hard, protected_paths


def staged_patch(cwd: Path) -> str:
    """Everything that is staged as one patch, to be applied by the user with `git apply`."""
    return git(["diff", "--cached", "--no-ext-diff", "--no-color", "--binary"], cwd)


def staged_problems(cwd: Path, protected: tuple[str, ...]) -> list[str]:
    """Hard problems plus protected paths, as one list of readable lines."""
    hard, protected_paths = split_staged(cwd, protected)
    return hard + [f"{p} (protected)" for p in protected_paths]
