"""Persistent job queue (SQLite). Survives restarts, dedupes issue jobs, bounds every input."""
from __future__ import annotations

import contextlib
import os
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from .config import SLUG_RE

ACTIVE = ("queued", "running")
FINAL = ("pr-open", "no-changes", "answered", "failed", "cancelled")
DAY = 86400
MAX_TASK_CHARS = 20_000
REQUESTER_RE = re.compile(r"[A-Za-z0-9_.:@-]{1,64}")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, repo TEXT NOT NULL, issue INTEGER, task TEXT NOT NULL,
  status TEXT NOT NULL, requested_by TEXT NOT NULL DEFAULT 'cli',
  created INTEGER NOT NULL, started INTEGER, finished INTEGER,
  pr TEXT, reason TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0,
  pr_number INTEGER,
  kind TEXT NOT NULL DEFAULT 'change',  -- 'change' = a code change, 'ask' = a read-only question about a repo
  answer TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs (status, created);
CREATE TABLE IF NOT EXISTS actions (  -- GitHub write actions of chat agents (issues, comments), for a daily limit
  id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, requester TEXT NOT NULL, kind TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS actions_requester ON actions (requester, ts);
"""


class LimitError(Exception):
    pass


class InputError(ValueError):
    pass


@dataclass(frozen=True)
class JobRow:
    id: str
    repo: str
    issue: int | None
    task: str
    status: str
    requested_by: str
    created: int
    started: int | None
    finished: int | None
    pr: str | None
    reason: str | None
    cancel_requested: bool
    pr_number: int | None = None  # set for "extend this existing pull request" jobs
    kind: str = "change"
    answer: str | None = None


def _row(r: sqlite3.Row) -> JobRow:
    d = dict(r)
    d["cancel_requested"] = bool(d["cancel_requested"])
    return JobRow(**d)


def validate(repo: str, task: str, issue: int | None, requested_by: str, pr_number: int | None = None) -> None:
    if not isinstance(repo, str) or not SLUG_RE.fullmatch(repo):
        raise InputError("invalid repo")
    if not isinstance(task, str) or not task.strip():
        raise InputError("task must not be empty")
    if len(task) > MAX_TASK_CHARS or "\0" in task:
        raise InputError(f"task too long (max {MAX_TASK_CHARS} characters) or contains NUL")
    if issue is not None and (isinstance(issue, bool) or not isinstance(issue, int) or not 0 < issue < 2**31):
        raise InputError("issue must be an integer between 1 and 2^31-1")
    if pr_number is not None and (isinstance(pr_number, bool) or not isinstance(pr_number, int)
                                  or not 0 < pr_number < 2**31):
        raise InputError("pr must be an integer between 1 and 2^31-1")
    if issue is not None and pr_number is not None:
        raise InputError("a job is either for an issue or for a pull request, not both")
    if not isinstance(requested_by, str) or not REQUESTER_RE.fullmatch(requested_by):
        raise InputError("invalid requested_by")


class Queue:
    def __init__(self, path: Path, clock: Callable[[], float] = time.time, daily_limit: int = 10,
                 max_queued: int = 20):
        self.path, self.clock = Path(path), clock
        self.daily_limit, self.max_queued = daily_limit, max_queued
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        with self._conn() as c:
            c.executescript(SCHEMA)
            columns = {r["name"] for r in c.execute("PRAGMA table_info(jobs)")}
            for column, ddl in (("pr_number", "INTEGER"), ("kind", "TEXT NOT NULL DEFAULT 'change'"), ("answer", "TEXT")):
                if column not in columns:  # databases created before this feature existed
                    c.execute(f"ALTER TABLE jobs ADD COLUMN {column} {ddl}")
        os.chmod(self.path, 0o600)  # task texts are private

    @contextlib.contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        # one short-lived connection per operation: safe across worker threads
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        try:
            yield c
        finally:
            c.close()

    def _now(self) -> int:
        return int(self.clock())

    def _used(self, c: sqlite3.Connection) -> int:
        # a job that actually started cost Claude tokens, even if it was cancelled afterwards
        return c.execute("SELECT COUNT(*) FROM jobs WHERE kind='change' AND created>=? "
                         "AND (status!='cancelled' OR started IS NOT NULL)", (self._now() - DAY,)).fetchone()[0]

    def enqueue(self, repo: str, task: str, issue: int | None = None,
                requested_by: str = "cli", pr_number: int | None = None,
                kind: str = "change") -> tuple[JobRow, bool]:
        """Return (job, created). An active job for the same repo+issue (or +pull request) is returned,
        not duplicated: two jobs must never push to the same branch at once."""
        validate(repo, task, issue, requested_by, pr_number)
        if kind not in ("change", "ask"):
            raise InputError("invalid job kind")
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                for column, value in (("issue", issue), ("pr_number", pr_number)):
                    if value is None:
                        continue
                    dup = c.execute(
                        f"SELECT * FROM jobs WHERE repo=? AND {column}=? AND status IN ('queued','running')",
                        (repo, value)).fetchone()
                    if dup:
                        c.execute("COMMIT")
                        return _row(dup), False
                waiting = c.execute("SELECT COUNT(*) FROM jobs WHERE status='queued'").fetchone()[0]
                if waiting >= self.max_queued:
                    raise LimitError(f"queue is full ({waiting}/{self.max_queued} waiting)")
                used = self._used(c)
                if kind == "change" and used >= self.daily_limit:  # questions have their own, separate limit
                    raise LimitError(f"daily limit reached ({used}/{self.daily_limit} jobs in 24h)")
                job_id = time.strftime("%Y%m%d-%H%M%S-", time.localtime(self._now())) + secrets.token_hex(3)
                c.execute("INSERT INTO jobs (id, repo, issue, task, status, requested_by, created, pr_number, kind) "
                          "VALUES (?,?,?,?, 'queued', ?, ?, ?, ?)",
                          (job_id, repo, issue, task, requested_by, self._now(), pr_number, kind))
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
            return _row(c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()), True

    def claim_next(self, max_parallel: int) -> JobRow | None:
        """Atomically move the oldest queued job to running, if capacity allows."""
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                running = c.execute("SELECT COUNT(*) FROM jobs WHERE status='running'").fetchone()[0]
                nxt = c.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created, rowid LIMIT 1").fetchone()
                if running >= max_parallel or nxt is None:
                    c.execute("COMMIT")
                    return None
                c.execute("UPDATE jobs SET status='running', started=? WHERE id=?", (self._now(), nxt["id"]))
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
            return self.get(nxt["id"])

    def finish(self, job_id: str, status: str, pr: str | None = None, reason: str | None = None,
               answer: str | None = None) -> bool:
        """Only a RUNNING job can finish: a late result never overwrites cancelled/failed rows."""
        if status not in FINAL:
            raise ValueError(f"not a final status: {status}")
        with self._conn() as c:
            cur = c.execute("UPDATE jobs SET status=?, finished=?, pr=?, reason=?, answer=? "
                            "WHERE id=? AND status='running'", (status, self._now(), pr, reason, answer, job_id))
            return cur.rowcount == 1

    def cancel(self, job_id: str) -> str:
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            r = c.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if r is None:
                c.execute("COMMIT")
                return "not-found"
            if r["status"] == "queued":
                c.execute("UPDATE jobs SET status='cancelled', finished=? WHERE id=?", (self._now(), job_id))
                result = "cancelled"
            elif r["status"] == "running":
                c.execute("UPDATE jobs SET cancel_requested=1 WHERE id=?", (job_id,))
                result = "cancel-requested"
            else:
                result = "already-finished"
            c.execute("COMMIT")
            return result

    def is_cancel_requested(self, job_id: str) -> bool:
        with self._conn() as c:
            r = c.execute("SELECT cancel_requested FROM jobs WHERE id=?", (job_id,)).fetchone()
            return bool(r and r["cancel_requested"])

    def recover(self) -> int:
        """After a daemon restart nothing is really running: fail those jobs honestly.
        Only call this while holding the serve lock (see cli.cmd_serve)."""
        with self._conn() as c:
            cur = c.execute("UPDATE jobs SET status='failed', finished=?, reason='daemon restarted' "
                            "WHERE status='running'", (self._now(),))
            return cur.rowcount

    def get(self, job_id: str) -> JobRow | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return _row(r) if r else None

    def list(self, limit: int = 20, active_only: bool = False) -> list[JobRow]:
        sql = "SELECT * FROM jobs " + ("WHERE status IN ('queued','running') " if active_only else "")
        with self._conn() as c:
            return [_row(r) for r in c.execute(sql + "ORDER BY created DESC, rowid DESC LIMIT ?", (limit,))]

    def used_by(self, requester: str) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM jobs WHERE kind='change' AND requested_by=? AND created>=? "
                             "AND (status!='cancelled' OR started IS NOT NULL)",
                             (requester, self._now() - DAY)).fetchone()[0]

    def record_action(self, requester: str, kind: str) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO actions (ts, requester, kind) VALUES (?,?,?)", (self._now(), requester, kind))

    def actions_by(self, requester: str, kind: str | None = None, exclude_kind: str | None = None) -> int:
        sql, args = "SELECT COUNT(*) FROM actions WHERE requester=? AND ts>=?", [requester, self._now() - DAY]
        if kind is not None:
            sql, args = sql + " AND kind=?", args + [kind]
        if exclude_kind is not None:
            sql, args = sql + " AND kind!=?", args + [exclude_kind]
        with self._conn() as c:
            return c.execute(sql, args).fetchone()[0]

    def used_today(self) -> int:
        with self._conn() as c:
            return self._used(c)
