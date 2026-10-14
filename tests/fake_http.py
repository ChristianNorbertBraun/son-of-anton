"""Runs the bridge's HTTP handler on a fake connection: no socket, not even loopback, which the sandboxes
that run these tests (and the egress rule of the runner user) block."""
from __future__ import annotations

import io
import json


class FakeConnection:
    """Stands in for the client socket."""

    def __init__(self, raw: bytes):
        self._in, self.sent = io.BytesIO(raw), io.BytesIO()

    def makefile(self, mode, *args, **kwargs):
        return self._in if "r" in mode else self.sent

    def sendall(self, data):
        self.sent.write(data)


def request(handler, method="POST", path="/mcp", body=None, headers=None) -> tuple[int, bytes]:
    """One HTTP/1.1 request through `handler` (from bridge.make_handler). `body` may be bytes, None or JSON data."""
    payload = body if isinstance(body, bytes) else b"" if body is None else json.dumps(body).encode()
    hdrs = {"Host": "127.0.0.1:8765", "Content-Type": "application/json", "Content-Length": str(len(payload)),
            **(headers or {})}
    raw = (f"{method} {path} HTTP/1.1\r\n".encode() + "".join(f"{k}: {v}\r\n" for k, v in hdrs.items()).encode()
           + b"\r\n" + payload)
    conn = FakeConnection(raw)
    handler(conn, ("127.0.0.1", 50000), None)
    head, _, rest = conn.sent.getvalue().partition(b"\r\n\r\n")
    return int(head.split()[1]), rest
