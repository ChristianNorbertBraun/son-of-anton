"""Persistent job queue (SQLite). Survives restarts, dedupes issue jobs, bounds every input."""
from __future__ import annotations

import contextlib
import json
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
FINAL = ("pr-open", "no-changes", "answered", "proposed", "failed", "cancelled", "step-done")
DAY = 86400
MAX_TASK_CHARS = 20_000
MAX_SPEC_CHARS = 200_000
REQUESTER_RE = re.compile(r"[A-Za-z0-9_.:@-]{1,64}")
STEP_ID_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
COUNTED = "kind IN ('change','step')"  # jobs that run a model on a change count against the daily limit

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, repo TEXT NOT NULL, issue INTEGER, task TEXT NOT NULL,
  status TEXT NOT NULL, requested_by TEXT NOT NULL DEFAULT 'cli',
  created INTEGER NOT NULL, started INTEGER, finished INTEGER,
  pr TEXT, reason TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0,
  pr_number INTEGER,
  kind TEXT NOT NULL DEFAULT 'change',  -- 'change' = a code change, 'ask' = a read-only question, 'step' = one step of a flow
  answer TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs (status, created);
CREATE TABLE IF NOT EXISTS actions (  -- GitHub write actions of chat agents (issues, comments), for a daily limit
  id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, requester TEXT NOT NULL, kind TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS actions_requester ON actions (requester, ts);
CREATE TABLE IF NOT EXISTS step_jobs (  -- a side table: older code does JobRow(**row) and must not see new columns in jobs
  job_id TEXT PRIMARY KEY, flow_id TEXT NOT NULL, step_id TEXT NOT NULL, attempt INTEGER NOT NULL,
  spec TEXT NOT NULL,
  intended_sha TEXT,  -- written BEFORE the push: after a crash, this SHA on the remote means the step did finish
  output TEXT,
  UNIQUE (flow_id, step_id, attempt)
);
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


@dataclass(frozen=True)
class StepRow:
    job_id: str
    flow_id: str
    step_id: str
    attempt: int
    spec: dict
    intended_sha: str | None = None
    output: dict | None = None


def _step_row(r: sqlite3.Row) -> StepRow:
    return StepRow(r["job_id"], r["flow_id"], r["step_id"], r["attempt"], json.loads(r["spec"]), r["intended_sha"],
                   json.loads(r["output"]) if r["output"] else None)


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


def validate_step(flow_id: str, step_id: str, attempt: int, spec: dict) -> str:
    """Return the spec as JSON."""
    for value in (flow_id, step_id):
        if not isinstance(value, str) or not STEP_ID_RE.fullmatch(value):
            raise InputError("invalid flow or step id")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or not 0 < attempt < 2**31:
        raise InputError("attempt must be a positive integer")
    if not isinstance(spec, dict):
        raise InputError("spec must be a dictionary")
    text = json.dumps(spec)
    if len(text) > MAX_SPEC_CHARS:
        raise InputError(f"spec too long (max {MAX_SPEC_CHARS} characters)")
    return text


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
        return c.execute(f"SELECT COUNT(*) FROM jobs WHERE {COUNTED} AND created>=? "
                         "AND (status!='cancelled' OR started IS NOT NULL)", (self._now() - DAY,)).fetchone()[0]

    def _admit(self, c: sqlite3.Connection, counted: bool) -> None:
        waiting = c.execute("SELECT COUNT(*) FROM jobs WHERE status='queued'").fetchone()[0]
        if waiting >= self.max_queued:
            raise LimitError(f"queue is full ({waiting}/{self.max_queued} waiting)")
        used = self._used(c)
        if counted and used >= self.daily_limit:  # questions have their own, separate limit
            raise LimitError(f"daily limit reached ({used}/{self.daily_limit} jobs in 24h)")

    def _new_id(self) -> str:
        return time.strftime("%Y%m%d-%H%M%S-", time.localtime(self._now())) + secrets.token_hex(3)

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
                self._admit(c, counted=kind == "change")
                job_id = self._new_id()
                c.execute("INSERT INTO jobs (id, repo, issue, task, status, requested_by, created, pr_number, kind) "
                          "VALUES (?,?,?,?, 'queued', ?, ?, ?, ?)",
                          (job_id, repo, issue, task, requested_by, self._now(), pr_number, kind))
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
            return _row(c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()), True

    def enqueue_step(self, repo: str, task: str, flow_id: str, step_id: str, attempt: int, spec: dict,
                     requested_by: str = "conductor") -> tuple[JobRow, bool]:
        """Return (job, created). The same flow, step and attempt always give the same job, so a conductor that
        restarts between enqueueing and recording it cannot start a step twice. The job row has no issue: a step
        must not meet the issue dedupe of normal jobs, and the issue reporter must not comment on each step."""
        validate(repo, task, None, requested_by)
        text = validate_step(flow_id, step_id, attempt, spec)
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                dup = c.execute("SELECT j.* FROM jobs j JOIN step_jobs s ON s.job_id=j.id "
                                "WHERE s.flow_id=? AND s.step_id=? AND s.attempt=?",
                                (flow_id, step_id, attempt)).fetchone()
                if dup:
                    c.execute("COMMIT")
                    return _row(dup), False
                self._admit(c, counted=True)
                job_id = self._new_id()
                c.execute("INSERT INTO jobs (id, repo, task, status, requested_by, created, kind) "
                          "VALUES (?,?,?, 'queued', ?, ?, 'step')", (job_id, repo, task, requested_by, self._now()))
                c.execute("INSERT INTO step_jobs (job_id, flow_id, step_id, attempt, spec) VALUES (?,?,?,?,?)",
                          (job_id, flow_id, step_id, attempt, text))
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
            return _row(c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()), True

    def step(self, job_id: str) -> StepRow | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM step_jobs WHERE job_id=?", (job_id,)).fetchone()
            return _step_row(r) if r else None

    def step_job(self, flow_id: str, step_id: str, attempt: int) -> JobRow | None:
        with self._conn() as c:
            r = c.execute("SELECT j.* FROM jobs j JOIN step_jobs s ON s.job_id=j.id "
                          "WHERE s.flow_id=? AND s.step_id=? AND s.attempt=?", (flow_id, step_id, attempt)).fetchone()
            return _row(r) if r else None

    def record_intended_push(self, job_id: str, sha: str, output: dict) -> None:
        """Written before the push: if the job dies right after it, the output still says what the step did."""
        with self._conn() as c:
            c.execute("UPDATE step_jobs SET intended_sha=?, output=? WHERE job_id=?", (sha, json.dumps(output), job_id))

    def record_step_output(self, job_id: str, output: dict) -> None:
        with self._conn() as c:
            c.execute("UPDATE step_jobs SET output=? WHERE job_id=?", (json.dumps(output), job_id))

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
            return c.execute(f"SELECT COUNT(*) FROM jobs WHERE {COUNTED} AND requested_by=? AND created>=? "
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
