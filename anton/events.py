"""Non-blocking event dispatch: the pool emits, handlers (labels, comments, chat) run on their own
thread, so a slow or failing handler can never stall or break job handling."""
from __future__ import annotations

import queue
import sys
import threading
from typing import Callable

from .queue import JobRow

Handler = Callable[[str, JobRow], None]
_STOP = object()


class Dispatcher:
    def __init__(self, handlers: list[Handler], maxsize: int = 200):
        self.handlers = handlers
        self._q: queue.Queue = queue.Queue(maxsize=maxsize)
        self._thread = threading.Thread(target=self._run, name="events", daemon=True)
        self._thread.start()

    def emit(self, event: str, row: JobRow) -> None:
        try:
            self._q.put_nowait((event, row))
        except queue.Full:
            print(f"events: queue full, dropped {event} for {row.id}", file=sys.stderr)

    def _run(self) -> None:
        while True:
            item = self._q.get()
            if item is _STOP:
                return
            for handler in self.handlers:
                try:
                    handler(*item)
                except Exception as e:  # one broken handler must not stop the others
                    print(f"events: handler {getattr(handler, '__name__', handler)} failed: "
                          f"{type(e).__name__}: {e}", file=sys.stderr)

    def stop(self, timeout: float = 10.0) -> None:
        """Deliver what is already queued, then stop."""
        self._q.put(_STOP)
        self._thread.join(timeout)
