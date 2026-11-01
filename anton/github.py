"""Small GitHub client for issues and labels. One instance per repo; the installation token is
cached for 45 minutes and only carries the Issues permission."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from . import ghapp
from .config import RepoConfig

TOKEN_TTL = 45 * 60
# issues: write (create, edit, comment, labels); pull requests and contents: read-only (inspect a PR and its branch)
PERMISSIONS = {"issues": "write", "pull_requests": "read", "contents": "read"}
# changing a pull request: a separate token, minted for each call and never cached
PR_WRITE = {"pull_requests": "write"}
EVENT_PAGES = 5
STATES = ("queued", "running", "pr", "patch", "failed")
COLORS = {"": "5319e7", "queued": "fbca04", "running": "0e8a16", "pr": "1d76db", "patch": "c5def5", "failed": "d93f0b"}


def state_labels(trigger: str) -> dict[str, str]:
    return {state: f"{trigger}:{state}" for state in STATES}


class GitHub:
    def __init__(self, app_id: int, key_path: Path, repo: RepoConfig,
                 api: Callable = ghapp.api, mint: Callable = ghapp.installation_token,
                 clock: Callable[[], float] = time.monotonic):
        self.app_id, self.key_path, self.repo = app_id, key_path, repo
        self._api, self._mint, self._clock = api, mint, clock
        self._token: str | None = None
        self._minted = 0.0

    def _call(self, method: str, path: str, body: dict | None = None):
        if self._token is None or self._clock() - self._minted > TOKEN_TTL:
            self._token = self._mint(self.app_id, self.key_path, self.repo.installation_id, self.repo.slug,
                                     dict(PERMISSIONS))
            self._minted = self._clock()
        return self._api(method, f"/repos/{self.repo.slug}{path}", self._token, body)

    def write(self, method: str, path: str, body: dict | None = None):
        return self._api(method, f"/repos/{self.repo.slug}{path}", self._write_token(), body)

    def graphql(self, query: str, variables: dict) -> dict:
        """A 200 with `errors` is a failure: GraphQL reports nearly everything that way."""
        res = self._api("POST", "/graphql", self._write_token(), {"query": query, "variables": variables})
        if res.get("errors"):
            raise RuntimeError(f"GitHub GraphQL: {str(res['errors'])[:300]}")
        return res.get("data") or {}

    def _write_token(self) -> str:
        return self._mint(self.app_id, self.key_path, self.repo.installation_id, self.repo.slug, dict(PR_WRITE))

    def _pages(self, path: str, pages: int = 5) -> list[dict]:
        out: list[dict] = []
        for page in range(1, pages + 1):
            chunk = self._call("GET", f"{path}{'&' if '?' in path else '?'}per_page=100&page={page}")
            out += chunk
            if len(chunk) < 100:
                break
        return out

    def issue_comments(self, number: int) -> list[dict]:
        return self._pages(f"/issues/{int(number)}/comments")

    def reviews(self, number: int) -> list[dict]:
        return self._pages(f"/pulls/{int(number)}/reviews", 20)  # a flood of foreign reviews must not hide the owner's

    def review_comments(self, number: int) -> list[dict]:
        return self._pages(f"/pulls/{int(number)}/comments")

    def pulls_with_head(self, branch: str) -> list[dict]:
        owner = self.repo.slug.split("/", 1)[0]
        return self._call("GET", f"/pulls?state=all&head={quote(f'{owner}:{branch}', safe='')}")

    def issues(self, label: str) -> list[dict]:
        return self._call("GET", f"/issues?labels={quote(label, safe='')}&state=open&per_page=30&sort=updated")

    def issue(self, number: int) -> dict:
        return self._call("GET", f"/issues/{int(number)}")

    def create_issue(self, title: str, body: str) -> dict:
        return self._call("POST", "/issues", {"title": title, "body": body})

    def update_issue(self, number: int, fields: dict) -> dict:
        allowed = {k: v for k, v in fields.items() if k in ("title", "body", "state", "state_reason")}
        return self._call("PATCH", f"/issues/{int(number)}", allowed)

    def pull(self, number: int) -> dict:
        return self._call("GET", f"/pulls/{int(number)}")

    def pull_files(self, number: int) -> list[dict]:
        return self._call("GET", f"/pulls/{int(number)}/files?per_page=100")

    def branch(self, name: str) -> dict:
        return self._call("GET", f"/branches/{quote(name, safe='/')}")

    def events(self, number: int) -> list[dict]:
        out: list[dict] = []
        for page in range(1, EVENT_PAGES + 1):  # oldest first; 500 events is far more than any normal issue
            chunk = self._call("GET", f"/issues/{int(number)}/events?per_page=100&page={page}")
            out += chunk
            if len(chunk) < 100:
                break
        return out

    def add_labels(self, number: int, labels: list[str]) -> None:
        self._call("POST", f"/issues/{int(number)}/labels", {"labels": labels})

    def remove_label(self, number: int, label: str) -> None:
        try:
            self._call("DELETE", f"/issues/{int(number)}/labels/{quote(label, safe='')}")
        except RuntimeError as e:
            if " 404 " not in str(e):  # already gone is fine
                raise

    def comment(self, number: int, body: str) -> None:
        self._call("POST", f"/issues/{int(number)}/comments", {"body": body})

    def ensure_label(self, name: str, color: str) -> None:
        try:
            self._call("POST", "/labels", {"name": name, "color": color})
        except RuntimeError as e:
            if " 422 " not in str(e):  # 422 = label already exists
                raise

    def ensure_labels(self, trigger: str) -> None:
        wanted = {trigger: COLORS[""], **{name: COLORS[state] for state, name in state_labels(trigger).items()}}
        for name, color in wanted.items():
            self.ensure_label(name, color)
