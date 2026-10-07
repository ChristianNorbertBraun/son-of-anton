"""Worker pool: takes jobs from the queue and runs them, at most max_parallel at a time."""
from __future__ import annotations

import sys
import threading
import time
from typing import Callable

from .models import Outcome
from .queue import JobRow, Queue

__all__ = ["Pool", "Outcome"]

RunFn = Callable[[JobRow, Callable[[], bool]], Outcome]
EventFn = Callable[[str, JobRow], None]


class Pool:
    def __init__(self, queue: Queue, run_fn: RunFn, max_parallel: int, on_event: EventFn | None = None,
                 paused: Callable[[], bool] = lambda: False):
        self.queue, self.run_fn, self.max_parallel, self.paused = queue, run_fn, max_parallel, paused
        self.on_event = on_event or (lambda event, row: None)
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._stopping = False

    def tick(self) -> int:
        """Start as many queued jobs as capacity allows. Returns how many were started."""
        started = 0
        if self.paused():  # an update waits for the running jobs: queued jobs stay queued
            return 0
        while not self._stopping and (row := self.queue.claim_next(self.max_parallel)) is not None:
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
        except Exception as e:  # notifications must never break job handling
            print(f"pool: event handler failed: {type(e).__name__}", file=sys.stderr)

    def _finish(self, row: JobRow, status: str, pr: str | None, reason: str | None,
                answer: str | None = None) -> None:
        for attempt in range(3):  # a busy database must not leave a row stuck in 'running'
            try:
                self.queue.finish(row.id, status, pr, reason, answer)
                return
            except Exception as e:
                print(f"pool: finish({row.id}) failed (attempt {attempt + 1}): {e}", file=sys.stderr)
                time.sleep(0.5)

    def _work(self, row: JobRow) -> None:
        try:
            out = self.run_fn(row, lambda: self.queue.is_cancel_requested(row.id))
            self._finish(row, out.status, out.pr, out.reason, out.answer)
        except Exception as e:
            self._finish(row, "failed", None, f"{type(e).__name__}: {e}"[:300])
        self._emit("finished", row)

    def run_forever(self, stop: threading.Event, interval: float = 2.0) -> None:
        while not stop.is_set():
            try:
                self.tick()
            except Exception as e:  # one database hiccup must not kill the daemon
                print(f"pool: tick failed: {type(e).__name__}: {e}", file=sys.stderr)
            stop.wait(interval)

    def drain(self, timeout: float = 60.0) -> int:
        """Stop claiming new jobs and wait for the running ones. Returns how many are still alive."""
        self._stopping = True
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self._lock:
                alive = [t for t in self._threads if t.is_alive()]
            if not alive:
                return 0
            alive[0].join(0.1)
        with self._lock:
            return len([t for t in self._threads if t.is_alive()])

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """Test helper: keep ticking until the queue is empty and nothing runs."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.tick()
            with self._lock:
                alive = [t for t in self._threads if t.is_alive()]
            if not alive and not self.queue.list(active_only=True):
                return True
            time.sleep(0.02)
        return False
