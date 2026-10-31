"""`anton dashboard`: one read-only page of all flows. It never writes and holds no GitHub token, and everything on
it that came from a tracker or a model is escaped: an issue title is written by whoever wrote the issue.

Loopback is no protection on a machine with other users, and flows.db is private. So the page lives under a secret
path: the token in the config directory, `http://127.0.0.1:8766/<token>`."""
from __future__ import annotations

import hmac
import html
import os
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .flow import Flow
from .flowdb import FlowDb
from .workflows import WORKFLOWS

LOOPBACK = ("127.0.0.1", "localhost")
TOKEN_FILE = "dashboard-token"
REFRESH_SECONDS = 15
RECENT = 50
STYLE = """
:root{color-scheme:light dark;--bg:#fff;--fg:#1f2328;--dim:#59636e;--line:#d1d9e0;--ok:#1a7f37;--run:#0969da;--bad:#cf222e}
@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--dim:#9198a1;--line:#3d444d;--ok:#3fb950;--run:#4493f8;--bad:#f85149}}
body{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;margin:0 auto;max-width:72rem;padding:1.5rem 1rem}
h1{font-size:1.25rem}table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid var(--line);padding:.5rem;text-align:left;vertical-align:top}
th{color:var(--dim);font-weight:500}.pips{display:flex;flex-wrap:wrap;gap:.25rem}.pip{border:1px solid var(--line);border-radius:1rem;font-size:12px;padding:0 .5rem}
.done{border-color:var(--ok);color:var(--ok)}.now{border-color:var(--run);color:var(--run);font-weight:600}.stopped{border-color:var(--bad);color:var(--bad);font-weight:600}
.todo{color:var(--dim)}.why{color:var(--bad);font-size:13px}.wrap{overflow-x:auto}a{color:var(--run)}
"""


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def pip_states(flow: Flow) -> list[tuple[str, str]]:
    """(stage, done|now|stopped|todo) for what a person cares about: `pr` and `ready` take no time and can fail
    only as a blocked flow."""
    stages = [s for s in WORKFLOWS[flow.workflow].stages if s not in ("pr", "ready")]
    current = {"fix": "review", "revise": "gate:plan"}.get(flow.stage, flow.stage)
    position = stages.index(current) if current in stages else -1
    out = []
    for i, stage in enumerate(stages):
        if flow.state == "done" or i < position:
            state = "done"
        elif i == position:
            state = "stopped" if flow.kind in ("blocked", "cancelled", "cancelling") else "now"
        else:
            state = "todo"
        name = flow.stage if i == position and flow.stage in ("fix", "revise") else stage
        out.append((name.removeprefix("gate:") + (" gate" if stage.startswith("gate:") else ""), state))
    return out


def row(db: FlowDb, flow: Flow) -> str:
    pips = "".join(f'<span class="pip {state}">{esc(name)}</span>' for name, state in pip_states(flow))
    runs = db.runs(flow.id)
    cost = sum(r.usage.get("cost_usd", 0) for r in runs)
    seconds = sum((r.finished or 0) - (r.started or 0) for r in runs if r.started and r.finished)
    link = (f'<a href="https://github.com/{esc(flow.repo)}/pull/{int(flow.pr)}" rel="noreferrer">#{int(flow.pr)}</a>'
            if flow.pr else "")
    title = flow.brief.split("\n", 1)[0][:120]
    why = f'<div class="why">{esc(flow.reason[:300])}</div>' if flow.reason else ""
    return (f"<tr><td>{esc(flow.repo)}#{esc(flow.key)}<br>{esc(title)}</td><td>{esc(flow.workflow)}</td>"
            f'<td><div class="pips">{pips}</div>{why}</td><td>{esc(flow.state)}</td><td>{link}</td>'
            f"<td>${cost:.2f}<br>{seconds // 60} min</td></tr>")


def page(db: FlowDb, nonce: str) -> str:
    flows = db.recent(RECENT)
    rows = "".join(row(db, f) for f in flows) or '<tr><td colspan="6">No flows yet.</td></tr>'
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Anton flows</title>'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<meta http-equiv="refresh" content="{REFRESH_SECONDS}"><style nonce="{nonce}">{STYLE}</style></head>'
            f'<body><h1>Anton flows</h1><div class="wrap"><table><thead><tr><th>Task</th><th>Workflow</th><th>Steps</th>'
            f"<th>State</th><th>PR</th><th>Cost</th></tr></thead><tbody>{rows}</tbody></table></div></body></html>")


def allowed_host(host: str | None, bind: str) -> bool:
    """A name that resolves to this machine must not turn a web page elsewhere into a reader of this one."""
    return (host or "").rsplit(":", 1)[0] in (*LOOPBACK, bind)


def load_token(path: Path) -> str:
    """Made on first use, in the config directory, so it also counts as a stored secret for every change."""
    if not path.exists():
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(32) + "\n")
    return path.read_text().strip()


def respond(db: FlowDb, path: str, host: str | None, token: str,
            bind: str = "127.0.0.1") -> tuple[int, dict[str, str], bytes]:
    """Everything the server does, without a socket: (status, headers, body)."""
    plain = {"Content-Type": "text/plain; charset=utf-8", "X-Content-Type-Options": "nosniff"}
    if not allowed_host(host, bind):
        return 403, plain, b"unknown host\n"
    if not token or not hmac.compare_digest(path.split("?", 1)[0].encode(), f"/{token}".encode()):
        return 404, plain, b"not found\n"
    nonce = secrets.token_urlsafe(16)
    return 200, {"Content-Type": "text/html; charset=utf-8", "X-Content-Type-Options": "nosniff",
                 "Content-Security-Policy": f"default-src 'none'; style-src 'nonce-{nonce}'; base-uri 'none'; "
                                            "form-action 'none'; frame-ancestors 'none'",
                 "Referrer-Policy": "no-referrer", "Cache-Control": "no-store"}, page(db, nonce).encode()


def serve(db: FlowDb, bind: str, port: int, token: str) -> None:
    class Handler(BaseHTTPRequestHandler):
        server_version, sys_version = "anton", ""

        def do_GET(self) -> None:  # GET only: anything else gets the server's 501
            code, headers, body = respond(db, self.path, self.headers.get("Host"), token, bind)
            self.send_response(code)
            for name, value in {**headers, "Content-Length": str(len(body))}.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:
            pass

    ThreadingHTTPServer((bind, port), Handler).serve_forever()
