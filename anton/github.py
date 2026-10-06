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
STATES = ("queued", "running", "pr", "failed")
COLORS = {"": "5319e7", "queued": "fbca04", "running": "0e8a16", "pr": "1d76db", "failed": "d93f0b"}


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
                                     {"issues": "write"})
            self._minted = self._clock()
        return self._api(method, f"/repos/{self.repo.slug}{path}", self._token, body)

    def issues(self, label: str) -> list[dict]:
        return self._call("GET", f"/issues?labels={quote(label, safe='')}&state=open&per_page=30&sort=updated")

    def issue(self, number: int) -> dict:
        return self._call("GET", f"/issues/{int(number)}")

    def events(self, number: int) -> list[dict]:
        out: list[dict] = []
        for page in range(1, 6):  # oldest first; 500 events is far more than any normal issue
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

    def ensure_labels(self, trigger: str) -> None:
        wanted = {trigger: COLORS[""], **{name: COLORS[state] for state, name in state_labels(trigger).items()}}
        for name, color in wanted.items():
            try:
                self._call("POST", "/labels", {"name": name, "color": color})
            except RuntimeError as e:
                if " 422 " not in str(e):  # 422 = label already exists
                    raise
