"""flows.db: the state of each flow, what is left to do for it, and its history.

A transition and its actions commit together, so a crash leaves either neither or both, and a restart replays what
is left in the outbox. Only the conductor writes flow state. The CLI leaves a command here for it to pick up."""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from .flow import Action, Event, Flow, from_dict, to_dict

SCHEMA = """
CREATE TABLE IF NOT EXISTS flows (
  id TEXT PRIMARY KEY, repo TEXT NOT NULL, key TEXT NOT NULL, state TEXT NOT NULL, data TEXT NOT NULL,
  created INTEGER NOT NULL, updated INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS flows_one_active ON flows (repo, key) WHERE state NOT IN ('done', 'cancelled');
CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT, flow_id TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
  not_before INTEGER NOT NULL, tries INTEGER NOT NULL DEFAULT 0, error TEXT, finished INTEGER
);
CREATE INDEX IF NOT EXISTS outbox_open ON outbox (flow_id, id) WHERE finished IS NULL;
CREATE TABLE IF NOT EXISTS history (
  id INTEGER PRIMARY KEY AUTOINCREMENT, flow_id TEXT NOT NULL, ts INTEGER NOT NULL, event TEXT NOT NULL,
  actor TEXT, state_from TEXT NOT NULL, state_to TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS step_runs (  -- one line per finished job, for the dashboard and the cost report
  flow_id TEXT NOT NULL, stage TEXT NOT NULL, job INTEGER NOT NULL, job_id TEXT, result TEXT NOT NULL,
  usage TEXT NOT NULL, started INTEGER, finished INTEGER, PRIMARY KEY (flow_id, job)
);
CREATE TABLE IF NOT EXISTS commands (
  id INTEGER PRIMARY KEY AUTOINCREMENT, flow_id TEXT NOT NULL, kind TEXT NOT NULL, created INTEGER NOT NULL,
  taken INTEGER
);
"""


@dataclass(frozen=True)
class Pending:
    id: int
    flow_id: str
    kind: str
    payload: dict
    tries: int

    @property
    def marker(self) -> str:
        return f"{self.flow_id}-{self.id}"


@dataclass(frozen=True)
class StepRun:
    stage: str
    job: int
    job_id: str | None
    result: str
    usage: dict
    started: int | None
    finished: int | None


class FlowDb:
    def __init__(self, path: Path, clock: Callable[[], float] = time.time):
        self.path, self.clock = Path(path), clock
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._conn() as c:
            c.executescript(SCHEMA)
        os.chmod(self.path, 0o600)  # task texts are private

    @contextlib.contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        try:
            yield c
        finally:
            c.close()

    @contextlib.contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise

    def _now(self) -> int:
        return int(self.clock())

    def create(self, flow: Flow) -> bool:
        """False if the task already has an active flow."""
        try:
            with self._tx() as c:
                c.execute("INSERT INTO flows (id, repo, key, state, data, created, updated) VALUES (?,?,?,?,?,?,?)",
                          (flow.id, flow.repo, flow.key, flow.state, json.dumps(to_dict(flow)), self._now(),
                           self._now()))
        except sqlite3.IntegrityError:
            return False
        return True

    def get(self, flow_id: str) -> Flow | None:
        with self._conn() as c:
            r = c.execute("SELECT data FROM flows WHERE id=?", (flow_id,)).fetchone()
            return from_dict(json.loads(r["data"])) if r else None

    def active(self) -> list[Flow]:
        with self._conn() as c:
            return [from_dict(json.loads(r["data"])) for r in c.execute(
                "SELECT data FROM flows WHERE state NOT IN ('done', 'cancelled') ORDER BY created, id")]

    def recent(self, limit: int = 50) -> list[Flow]:
        with self._conn() as c:
            return [from_dict(json.loads(r["data"])) for r in c.execute(
                "SELECT data FROM flows ORDER BY updated DESC, id LIMIT ?", (limit,))]

    def commit(self, before: Flow, after: Flow, actions: list[Action], event: Event) -> None:
        """The transition, its actions and the history line: all of it or none."""
        now = self._now()
        with self._tx() as c:
            c.execute("UPDATE flows SET state=?, data=?, updated=? WHERE id=?",
                      (after.state, json.dumps(to_dict(after)), now, after.id))
            for action in actions:
                c.execute("INSERT INTO outbox (flow_id, kind, payload, not_before) VALUES (?,?,?,?)",
                          (after.id, action.kind, json.dumps(action.payload), now + action.delay))
            c.execute("INSERT INTO history (flow_id, ts, event, actor, state_from, state_to) VALUES (?,?,?,?,?,?)",
                      (after.id, now, event.kind, event.actor, before.state, after.state))

    def due(self) -> list[Pending]:
        """The oldest open action of each flow, if its time has come: actions of one flow run strictly in order."""
        with self._conn() as c:
            rows = c.execute("SELECT o.* FROM outbox o JOIN (SELECT MIN(id) AS id FROM outbox WHERE finished IS NULL "
                             "GROUP BY flow_id) f ON o.id=f.id WHERE o.not_before<=? ORDER BY o.id",
                             (self._now(),)).fetchall()
        return [Pending(r["id"], r["flow_id"], r["kind"], json.loads(r["payload"]), r["tries"]) for r in rows]

    def has_open(self, flow_id: str) -> bool:
        with self._conn() as c:
            return c.execute("SELECT 1 FROM outbox WHERE flow_id=? AND finished IS NULL LIMIT 1",
                             (flow_id,)).fetchone() is not None

    def finish(self, action_id: int) -> None:
        with self._conn() as c:
            c.execute("UPDATE outbox SET finished=?, error=NULL WHERE id=?", (self._now(), action_id))

    def postpone(self, action_id: int, delay: int, error: str, charge: bool = True) -> None:
        """Try again later. `charge`: False for waiting that says nothing about the action (a daily limit)."""
        with self._conn() as c:
            c.execute("UPDATE outbox SET not_before=?, error=?, tries=tries+? WHERE id=?",
                      (self._now() + delay, error, 1 if charge else 0, action_id))

    def abandon(self, flow_id: str, error: str) -> None:
        """Close what is left of a flow's outbox: nothing of it may run after the flow stopped."""
        with self._conn() as c:
            c.execute("UPDATE outbox SET finished=?, error=? WHERE flow_id=? AND finished IS NULL",
                      (self._now(), error, flow_id))

    def record_run(self, flow_id: str, stage: str, job: int, job_id: str | None, result: str, usage: dict,
                   started: int | None, finished: int | None) -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO step_runs VALUES (?,?,?,?,?,?,?,?)",
                      (flow_id, stage, job, job_id, result, json.dumps(usage), started, finished))

    def runs(self, flow_id: str) -> list[StepRun]:
        with self._conn() as c:
            return [StepRun(r["stage"], r["job"], r["job_id"], r["result"], json.loads(r["usage"]), r["started"],
                            r["finished"]) for r in c.execute(
                                "SELECT * FROM step_runs WHERE flow_id=? ORDER BY job", (flow_id,))]

    def history(self, flow_id: str) -> list[sqlite3.Row]:
        with self._conn() as c:
            return c.execute("SELECT * FROM history WHERE flow_id=? ORDER BY id", (flow_id,)).fetchall()

    def add_command(self, flow_id: str, kind: str) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO commands (flow_id, kind, created) VALUES (?,?,?)", (flow_id, kind, self._now()))

    def take_commands(self) -> list[tuple[str, str]]:
        with self._tx() as c:
            rows = c.execute("SELECT id, flow_id, kind FROM commands WHERE taken IS NULL ORDER BY id").fetchall()
            c.execute("UPDATE commands SET taken=? WHERE taken IS NULL", (self._now(),))
        return [(r["flow_id"], r["kind"]) for r in rows]
