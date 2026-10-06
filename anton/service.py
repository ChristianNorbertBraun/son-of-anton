"""The ONE way to submit work. CLI, label poller and chat bridge must all go through here,
so the allowlist and the input limits cannot be bypassed by a new front end."""
from __future__ import annotations

from . import config
from .queue import JobRow, LimitError, Queue


class Service:
    def __init__(self, queue: Queue, repos: dict[str, config.RepoConfig],
                 requester_limits: dict[str, int] | None = None):
        self.queue, self.repos = queue, repos
        self.requester_limits = requester_limits or {}  # e.g. {"merlin": 5}: less trusted front ends

    def submit(self, repo: str, task: str, issue: int | None = None,
               requested_by: str = "cli") -> tuple[JobRow, bool]:
        config.get_repo(self.repos, repo)  # ConfigError for anything not on the allowlist
        limit = self.requester_limits.get(requested_by)
        if limit is not None and self.queue.used_by(requested_by) >= limit:
            raise LimitError(f"{requested_by} may start at most {limit} jobs per 24h")
        return self.queue.enqueue(repo, task, issue, requested_by)

    def cancel(self, job_id: str) -> str:
        return self.queue.cancel(job_id)

    def active(self) -> list[JobRow]:
        return self.queue.list(active_only=True)
