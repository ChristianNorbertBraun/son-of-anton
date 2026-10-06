"""MCP endpoint for a chat agent (Hermes/Merlin). Streamable-HTTP MCP, JSON responses only, bound to
127.0.0.1 with a bearer token. Every tool goes through Service, so the allowlist, input limits and the
daily limits apply exactly as for the CLI. The chat agent can itself be prompt-injected (it reads the web),
so it gets its own, smaller daily quota and can never name a requester other than "merlin"."""
from __future__ import annotations

import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from . import config
from .config import RepoConfig, Settings
from .github import GitHub
from .poller import build_task
from .queue import InputError, LimitError
from .runner import safe, scrub
from .service import Service

PROTOCOL = "2025-03-26"
SUPPORTED = ("2025-06-18", "2025-03-26", "2024-11-05")
REQUESTER = "merlin"
MAX_BODY = 1_000_000
MAX_TEXT = 3000

TOOLS = [
    {"name": "anton_create_task",
     "description": "Queue a coding task for Son of Anton (Claude Code) in an allowlisted GitHub repo. It "
                    "implements the task in a sandbox and opens a DRAFT pull request. Only call this after "
                    "the user explicitly asked for it and confirmed the task text.",
     "inputSchema": {"type": "object", "required": ["repo", "task"], "properties": {
         "repo": {"type": "string", "description": "owner/name, must be on the allowlist (see anton_list_repos)"},
         "task": {"type": "string", "description": "Concrete instructions, what to change and what to leave alone"}}}},
    {"name": "anton_queue_issue",
     "description": "Queue an existing GitHub issue (title and body become the task). Only issues written "
                    "by an allowed author are accepted.",
     "inputSchema": {"type": "object", "required": ["repo", "issue"], "properties": {
         "repo": {"type": "string"}, "issue": {"type": "integer", "minimum": 1}}}},
    {"name": "anton_status",
     "description": "Show running and queued jobs, or one job by id, and the daily budget.",
     "inputSchema": {"type": "object", "properties": {"job_id": {"type": "string"}}}},
    {"name": "anton_cancel",
     "description": "Cancel a queued or running job by id.",
     "inputSchema": {"type": "object", "required": ["job_id"], "properties": {"job_id": {"type": "string"}}}},
    {"name": "anton_list_repos",
     "description": "List the repositories Son of Anton may work on.",
     "inputSchema": {"type": "object", "properties": {}}},
]


class RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _text(text: str, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": scrub(text)[:MAX_TEXT]}], "isError": error}


def _line(row) -> str:
    detail = row.pr or row.reason or ""
    return f"{row.id}  {row.status:<10} {row.repo}  {safe(row.task, 60)!r}  {safe(detail, 120)}".rstrip()


class Bridge:
    def __init__(self, settings: Settings, service: Service, gh_for: Callable[[RepoConfig], GitHub]):
        self.settings, self.service, self.gh_for = settings, service, gh_for

    # --- JSON-RPC -------------------------------------------------------------------------------
    def handle(self, msg: dict) -> dict | None:
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}
        if "id" not in msg:  # notification (e.g. notifications/initialized): no response
            return None
        try:
            result = self._dispatch(msg.get("method"), msg.get("params") or {})
        except RpcError as e:
            return {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": e.code, "message": e.message}}
        return {"jsonrpc": "2.0", "id": msg["id"], "result": result}

    def _dispatch(self, method, params: dict) -> dict:
        if method == "initialize":
            wanted = params.get("protocolVersion")
            return {"protocolVersion": wanted if wanted in SUPPORTED else PROTOCOL,
                    "capabilities": {"tools": {}}, "serverInfo": {"name": "son-of-anton", "version": "1"}}
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": TOOLS}
        if method == "tools/call":
            return self._call(params.get("name"), params.get("arguments") or {})
        raise RpcError(-32601, "method not found")

    # --- tools ----------------------------------------------------------------------------------
    def _call(self, name, args: dict) -> dict:
        handlers = {"anton_create_task": self._create_task, "anton_queue_issue": self._queue_issue,
                    "anton_status": self._status, "anton_cancel": self._cancel, "anton_list_repos": self._repos}
        if name not in handlers:
            raise RpcError(-32602, "unknown tool")
        try:
            return _text(handlers[name](args))
        except (config.ConfigError, LimitError, InputError, ValueError) as e:
            return _text(f"rejected: {safe(e, 300)}", error=True)
        except Exception as e:  # never leak internals (paths, tokens) to the chat agent
            return _text(f"failed: {type(e).__name__}", error=True)

    @staticmethod
    def _str(args: dict, key: str) -> str:
        value = args.get(key)
        if not isinstance(value, str):
            raise ValueError(f"'{key}' must be a string")
        return value

    def _create_task(self, args: dict) -> str:
        row, created = self.service.submit(self._str(args, "repo"), self._str(args, "task"), None, REQUESTER)
        return f"{'Queued' if created else 'Already active'}: job {row.id} ({row.status})"

    def _queue_issue(self, args: dict) -> str:
        repo = config.get_repo(self.settings.repos, self._str(args, "repo"))
        number = args.get("issue")
        if isinstance(number, bool) or not isinstance(number, int):
            raise ValueError("'issue' must be an integer")
        if not repo.allowed_authors:
            raise ValueError("no allowed_authors configured for this repo")
        issue = self.gh_for(repo).issue(number)
        if "pull_request" in issue:
            raise ValueError("that is a pull request, not an issue")
        author = (issue.get("user") or {}).get("login", "")
        if author.lower() not in {a.lower() for a in repo.allowed_authors}:
            raise ValueError(f"issue author {safe(author, 40)!r} is not an allowed author")
        row, created = self.service.submit(repo.slug, build_task(issue), number, REQUESTER)
        return f"{'Queued' if created else 'Already active'}: job {row.id} for {repo.slug}#{number} ({row.status})"

    def _status(self, args: dict) -> str:
        q = self.service.queue
        job_id = args.get("job_id")
        if job_id is not None:
            row = q.get(self._str(args, "job_id"))
            return _line(row) if row else "no such job"
        active, recent = q.list(active_only=True), q.list(limit=5)
        lines = [f"Budget: {q.used_today()}/{q.daily_limit} jobs in the last 24h"]
        lines += ["Active:"] + ([_line(r) for r in active] or ["  none"])
        lines += ["Recent:"] + ([_line(r) for r in recent] or ["  none"])
        return "\n".join(lines)

    def _cancel(self, args: dict) -> str:
        return f"{self.service.cancel(self._str(args, 'job_id'))}"

    def _repos(self, args: dict) -> str:
        return "\n".join(f"{r.slug}  (label polling {'on' if r.allowed_authors else 'off'})"
                         for r in self.settings.repos.values()) or "none"


def make_handler(bridge: Bridge, token: str):
    expected = f"Bearer {token}"

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # request lines are noise and must never include headers
            pass

        def _reply(self, code: int, body: bytes = b"", ctype: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            if code == 405:
                self.send_header("Allow", "POST")
            self.end_headers()
            self.wfile.write(body)

        def _deny_unless_trusted(self) -> bool:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
            if host not in ("127.0.0.1", "localhost") or self.headers.get("Origin"):
                self._reply(403, b'{"error":"forbidden"}')  # blocks DNS rebinding and browser pages
                return True
            if not hmac.compare_digest(self.headers.get("Authorization", ""), expected):
                self._reply(401, b'{"error":"unauthorized"}')
                return True
            return False

        def do_POST(self):
            if self.path != "/mcp":
                return self._reply(404, b'{"error":"not found"}')
            if self._deny_unless_trusted():
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._reply(413, b'{"error":"too large"}')
            try:
                msg = json.loads(self.rfile.read(length) or b"null")
            except ValueError:
                err = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
                return self._reply(400, json.dumps(err).encode())
            out = [r for r in (bridge.handle(m) for m in msg) if r] if isinstance(msg, list) else bridge.handle(msg)
            if not out:
                return self._reply(202)  # only notifications
            self._reply(200, json.dumps(out).encode())

        def do_GET(self):  # no server-initiated stream
            self._reply(405, b'{"error":"method not allowed"}')

        do_DELETE = do_GET

    return Handler


def serve(bridge: Bridge, token: str, port: int) -> ThreadingHTTPServer:
    """Start the endpoint on 127.0.0.1 in a daemon thread. Call .shutdown() to stop it."""
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(bridge, token))
    threading.Thread(target=server.serve_forever, name="bridge", daemon=True).start()
    return server
