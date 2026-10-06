"""The ONE way to submit work. CLI, label poller and chat bridge must all go through here,
so the allowlist and the input limits cannot be bypassed by a new front end."""
from __future__ import annotations

from . import config
from .queue import JobRow, LimitError, Queue


class Service:
    def __init__(self, queue: Queue, repos: dict[str, config.RepoConfig],
                 requester_limits: dict[str, int] | None = None, write_limit: int | None = None,
                 ask_limit: int | None = None):
        self.queue, self.repos = queue, repos
        self.requester_limits = requester_limits or {}  # e.g. {"merlin": 5}: less trusted front ends
        self.write_limit = write_limit  # GitHub write actions (issues, comments) per requester and 24h
        self.ask_limit = ask_limit  # read-only questions about a repo per requester and 24h

    def submit(self, repo: str, task: str, issue: int | None = None, requested_by: str = "cli",
               pr_number: int | None = None) -> tuple[JobRow, bool]:
        config.get_repo(self.repos, repo)  # ConfigError for anything not on the allowlist
        limit = self.requester_limits.get(requested_by)
        if limit is not None and self.queue.used_by(requested_by) >= limit:
            raise LimitError(f"{requested_by} may start at most {limit} jobs per 24h")
        return self.queue.enqueue(repo, task, issue, requested_by, pr_number)

    def submit_ask(self, repo: str, question: str, requested_by: str = "cli") -> JobRow:
        """Queue a read-only question about a repo. Counted separately from code changes."""
        config.get_repo(self.repos, repo)
        if self.ask_limit is not None and self.queue.actions_by(requested_by, kind="ask") >= self.ask_limit:
            raise LimitError(f"{requested_by} may ask at most {self.ask_limit} questions per 24h")
        row, _ = self.queue.enqueue(repo, question, None, requested_by, kind="ask")
        self.queue.record_action(requested_by, "ask")
        return row

    def allow_write(self, requester: str, kind: str) -> None:
        """Count one GitHub write action (create/update issue, comment) or refuse when the day's budget is used."""
        if self.write_limit is not None and self.queue.actions_by(requester, exclude_kind="ask") >= self.write_limit:
            raise LimitError(f"{requester} may do at most {self.write_limit} GitHub write actions per 24h")
        self.queue.record_action(requester, kind)

    def cancel(self, job_id: str) -> str:
        return self.queue.cancel(job_id)

    def active(self) -> list[JobRow]:
        return self.queue.list(active_only=True)
