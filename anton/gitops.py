"""Git helpers. The GitHub token only travels via environment variables, never argv or URLs."""
from __future__ import annotations

import base64
import fnmatch
import os
import re
import subprocess
from pathlib import Path

from .config import BRANCH_PREFIX

def bot_email(bot_name: str, bot_id: int) -> str:
    return f"{bot_id}+{bot_name}@users.noreply.github.com"


def auth_env(token: str, bot_name: str, bot_id: int) -> dict:
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    email = bot_email(bot_name, bot_id)
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": bot_name, "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": bot_name, "GIT_COMMITTER_EMAIL": email,
    }


# The working tree is attacker-influenced (npm scripts, model output). Never run hooks or
# fsmonitor programs from it, even though .git is read-only inside the sandbox.
SAFE_FLAGS = ["-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
              "-c", "protocol.ext.allow=never"]


def git_cmd(args: list[str]) -> list[str]:
    return ["git", *SAFE_FLAGS, *args]


def git(args: list[str], cwd: Path | None = None, env: dict | None = None) -> str:
    full = {**os.environ, **(env or {})}
    p = subprocess.run(git_cmd(args), cwd=cwd, env=full, capture_output=True, text=True)
    if p.returncode != 0:
        # never echo env; stderr from git does not contain the header
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {p.stderr.strip()[:400]}")
    return p.stdout


def branch_name(issue: int | None, job_id: str, task: str) -> str:
    if issue is not None:
        return f"{BRANCH_PREFIX}issue-{int(issue)}"
    slug = re.sub(r"[^a-z0-9]+", "-", task.lower()).strip("-")[:30].strip("-")
    return f"{BRANCH_PREFIX}{slug or 'task'}-{job_id[-6:]}"


def assert_pushable(branch: str, base: str) -> None:
    if not branch.startswith(BRANCH_PREFIX) or branch == base:
        raise RuntimeError(f"refusing to push branch {branch!r}")


def violations(changed: list[str], protected: tuple[str, ...]) -> list[str]:
    """Paths Claude must not change (workflows, deploy scripts, secrets, build output)."""
    always = ("node_modules/*", "build/*", ".env*", "*.pem", ".github/workflows/*")
    bad = []
    for path in changed:
        if any(fnmatch.fnmatch(path, pat) for pat in (*always, *protected)):
            bad.append(path)
    return bad


def changed_files(cwd: Path) -> list[str]:
    out = git(["status", "--porcelain", "-z", "--untracked-files=all"], cwd)
    return [e[3:] for e in out.split("\0") if e]
