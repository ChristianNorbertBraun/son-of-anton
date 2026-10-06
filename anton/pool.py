"""Worker pool: takes jobs from the queue and runs them, at most max_parallel at a time."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

from .queue import JobRow, Queue


@dataclass(frozen=True)
class Outcome:
    status: str  # pr-open | no-changes | failed | cancelled
    pr: str | None = None
    reason: str | None = None


RunFn = Callable[[JobRow, Callable[[], bool]], Outcome]
EventFn = Callable[[str, JobRow], None]


class Pool:
    def __init__(self, queue: Queue, run_fn: RunFn, max_parallel: int, on_event: EventFn | None = None):
        self.queue, self.run_fn, self.max_parallel = queue, run_fn, max_parallel
        self.on_event = on_event or (lambda event, row: None)
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()

    def tick(self) -> int:
        """Start as many queued jobs as capacity allows. Returns how many were started."""
        started = 0
        while (row := self.queue.claim_next(self.max_parallel)) is not None:
            t = threading.Thread(target=self._work, args=(row,), name=f"job-{row.id}", daemon=True)
            with self._lock:
                self._threads = [x for x in self._threads if x.is_alive()] + [t]
            self._emit("started", row)
            t.start()
            started += 1
        return started

    def _emit(self, event: str, row: JobRow) -> None:
        try:
            self.on_event(event, self.queue.get(row.id) or row)
        except Exception:  # notifications must never break job handling
            pass

    def _work(self, row: JobRow) -> None:
        try:
            out = self.run_fn(row, lambda: self.queue.is_cancel_requested(row.id))
            self.queue.finish(row.id, out.status, out.pr, out.reason)
        except Exception as e:
            self.queue.finish(row.id, "failed", reason=f"{type(e).__name__}: {e}"[:300])
        self._emit("finished", row)

    def run_forever(self, stop: threading.Event, interval: float = 2.0) -> None:
        while not stop.is_set():
            self.tick()
            stop.wait(interval)

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """Block until no job thread is alive and nothing is queued/running (for tests and shutdown)."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.tick()
            with self._lock:
                alive = [t for t in self._threads if t.is_alive()]
            if not alive and not self.queue.list(active_only=True):
                return True
            time.sleep(0.02)
        return False
