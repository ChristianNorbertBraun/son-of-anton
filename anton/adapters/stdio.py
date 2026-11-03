"""Adapters as separate programs: JSON lines over stdin and stdout. Each request is one line, each response one line.

    {"id": 1, "method": "get", "params": {"key": "7"}}
    {"id": 1, "result": {"key": "7", "title": "...", ...}}
    {"id": 2, "error": {"kind": "adapter", "message": "no task 7"}}

The methods are those of ports.TrackerAdapter and ports.CodeHostAdapter, with dataclasses as objects and tuples as
lists: `mark_draft` and `is_draft` answer true or false, `last_draft_mark` [kind, actor] or null. An adapter runs
outside the sandbox and holds its tracker's token: it is trusted code, and nothing installs one by itself. A program
that dies is started again for the next request. Requests are not repeated by this layer: the outbox does that, and
the methods are idempotent."""
from __future__ import annotations

import json
import os
import select
import subprocess
import time
from dataclasses import asdict
from typing import Any

from ..ports import AdapterError, Note, PrRef, PrState, Review, Task

TIMEOUT = 60
MAX_LINE = 4_000_000


class StdioClient:
    def __init__(self, cmd: list[str], env: dict | None = None, timeout: float = TIMEOUT):
        self.cmd, self.env, self.timeout = cmd, env, timeout
        self.proc: subprocess.Popen | None = None
        self.ids = 0

    def _ensure(self) -> subprocess.Popen:
        if self.proc is None or self.proc.poll() is not None:
            self.proc = subprocess.Popen(self.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, env=self.env)
            self.buffer = b""
        return self.proc

    def stop(self) -> None:
        if self.proc is not None:
            self.proc.kill()
            self.proc.wait()
            self.proc = None

    def _readline(self, proc: subprocess.Popen) -> bytes:
        """Unbuffered, so the timeout holds. A program that hangs or floods must not stop the conductor."""
        deadline = time.monotonic() + self.timeout
        while b"\n" not in self.buffer:
            left = deadline - time.monotonic()
            ready, _, _ = select.select([proc.stdout], [], [], max(left, 0))
            if not ready:
                self.stop()
                raise AdapterError("the adapter did not answer in time")
            chunk = os.read(proc.stdout.fileno(), 65536)
            if not chunk:
                self.stop()
                raise AdapterError("the adapter stopped")
            self.buffer += chunk
            if len(self.buffer) > MAX_LINE:
                self.stop()
                raise AdapterError("the adapter's answer is too long")
        line, self.buffer = self.buffer.split(b"\n", 1)
        return line

    def call(self, method: str, **params) -> Any:
        proc = self._ensure()
        self.ids += 1
        request = json.dumps({"id": self.ids, "method": method, "params": params}) + "\n"
        try:
            proc.stdin.write(request.encode())
            proc.stdin.flush()
        except OSError:
            self.stop()
            raise AdapterError("the adapter stopped") from None
        try:
            response = json.loads(self._readline(proc))
        except ValueError:
            self.stop()
            raise AdapterError("the adapter's answer is no JSON") from None
        if not isinstance(response, dict) or response.get("id") != self.ids:
            self.stop()  # out of step: a later answer would be read as the answer to another question
            raise AdapterError("the adapter's answer does not match the request")
        if "error" in response:
            error = response["error"] if isinstance(response["error"], dict) else {}
            raise AdapterError(str(error.get("message") or "the adapter reported an error")[:300])
        return response.get("result")


def as_task(data: dict) -> Task:
    return Task(**{**data, "labels": tuple(data.get("labels") or ())})


def as_pr_state(data: dict) -> PrState:
    return PrState(PrRef(**data["ref"]), data["head"], data["draft"], data["state"], data["merged"],
                   tuple(Review(**r) for r in data.get("reviews") or ()), tuple(Note(**n) for n in data.get("notes") or ()))


def as_mark(data) -> tuple[str, str] | None:
    """null, or a [kind, actor] pair of strings. Anything else is an error, never a mark."""
    if data is None:
        return None
    if not (isinstance(data, list) and len(data) == 2 and all(isinstance(x, str) for x in data)):
        raise AdapterError("the adapter's draft mark is no [kind, actor] pair")
    return data[0], data[1]


class StdioTracker:
    def __init__(self, client: StdioClient, capabilities: set[str] | None = None):
        self.client, self.capabilities = client, capabilities or set()

    def prepare(self) -> None:
        self.client.call("prepare")

    def list_ready(self) -> list[Task]:
        return [as_task(t) for t in self.client.call("list_ready")]

    def get(self, key: str) -> Task:
        return as_task(self.client.call("get", key=key))

    def claim(self, key: str, label: str) -> None:
        self.client.call("claim", key=key, label=label)

    def report(self, key: str, state: str, text: str | None, marker: str) -> None:
        self.client.call("report", key=key, state=state, text=text, marker=marker)

    def link(self, key: str, url: str, marker: str) -> None:
        self.client.call("link", key=key, url=url, marker=marker)


class StdioCodeHost:
    def __init__(self, client: StdioClient):
        self.client = client

    def head_of(self, branch: str) -> str | None:
        return self.client.call("head_of", branch=branch)

    def pr_for(self, branch: str, base: str) -> PrRef | None:
        found = self.client.call("pr_for", branch=branch, base=base)
        return PrRef(**found) if found else None

    def open_draft_pr(self, branch: str, base: str, title: str, body: str) -> PrRef:
        return PrRef(**self.client.call("open_draft_pr", branch=branch, base=base, title=title, body=body))

    def update_pr(self, pr: PrRef, title: str, body: str) -> None:
        self.client.call("update_pr", pr=asdict(pr), title=title, body=body)

    def pr_state(self, pr: PrRef) -> PrState:
        return as_pr_state(self.client.call("pr_state", pr=asdict(pr)))

    def comment(self, pr: PrRef, text: str, marker: str) -> None:
        self.client.call("comment", pr=asdict(pr), text=text, marker=marker)

    def mark_ready(self, pr: PrRef) -> None:
        self.client.call("mark_ready", pr=asdict(pr))

    def mark_draft(self, pr: PrRef) -> bool:
        return self.client.call("mark_draft", pr=asdict(pr)) is True

    def is_draft(self, pr: PrRef) -> bool:
        answer = self.client.call("is_draft", pr=asdict(pr))
        if not isinstance(answer, bool):
            raise AdapterError("the adapter's is_draft is no boolean")
        return answer

    def last_draft_mark(self, pr: PrRef) -> tuple[str, str] | None:
        return as_mark(self.client.call("last_draft_mark", pr=asdict(pr)))

    def close(self, pr: PrRef) -> None:
        self.client.call("close", pr=asdict(pr))
