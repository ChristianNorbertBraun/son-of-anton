"""anton run | enqueue | queue | cancel | serve | jobs | selftest"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import sys
import threading
from pathlib import Path

from . import bridge, config, poller, runner
from .events import Dispatcher
from .github import GitHub
from .pool import Pool
from .queue import InputError, LimitError, Queue
from .runner import safe
from .service import Service
from .telegram import TelegramNotifier

STATE_DIR = Path.home() / ".local/state/son-of-anton"
DB_PATH = STATE_DIR / "queue.db"
LOCK_PATH = STATE_DIR / "serve.lock"


def open_queue(settings: config.Settings) -> Queue:
    return Queue(DB_PATH, daily_limit=settings.daemon.daily_limit, max_queued=settings.daemon.max_queued)


def github_factory(settings: config.Settings):
    """One GitHub client per repo, so the installation token is reused instead of minted per call."""
    clients: dict[str, GitHub] = {}

    def gh_for(repo: config.RepoConfig) -> GitHub:
        if repo.slug not in clients:
            clients[repo.slug] = GitHub(settings.github.app_id, runner.KEY_PATH, repo)
        return clients[repo.slug]

    return gh_for


def cmd_run(a: argparse.Namespace) -> int:
    """Run one job right now. Bypasses the queue, the daily limit and the parallelism cap."""
    settings = config.load_settings()
    repo = config.get_repo(settings.repos, a.repo)
    job = runner.new_job(repo, a.task, a.issue)
    job.log(f"job {job.id} repo={repo.slug} (direct run: no queue, no limits)")
    out = runner.run_job(job, settings, dry_run=a.dry_run)
    print(f"{out.status}: {out.pr or out.reason or ''}")
    return 0 if out.status in ("pr-open", "no-changes") else 1


def cmd_enqueue(a: argparse.Namespace) -> int:
    settings = config.load_settings()
    svc = Service(open_queue(settings), settings.repos)
    try:
        row, created = svc.submit(a.repo, a.task, a.issue, requested_by="cli")
    except (LimitError, InputError) as e:
        print(f"rejected: {e}", file=sys.stderr)
        return 1
    print(f"{'queued' if created else 'already active'}: {row.id} ({row.status})")
    return 0


def cmd_queue(a: argparse.Namespace) -> int:
    q = open_queue(config.load_settings())
    for r in reversed(q.list(limit=a.limit, active_only=a.active)):
        print(f"{r.id}  {r.status:<10} {r.repo}  {safe(r.task, 48)!r}  {safe(r.pr or r.reason or '', 120)}")
    print(f"-- {q.used_today()}/{q.daily_limit} jobs in the last 24h")
    return 0


def cmd_cancel(a: argparse.Namespace) -> int:
    print(open_queue(config.load_settings()).cancel(a.id))
    return 0


def cmd_serve(a: argparse.Namespace) -> int:
    settings = config.load_settings()
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
    try:  # a second daemon would fail the first one's running jobs in recover()
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another `anton serve` is already running", file=sys.stderr)
        return 3
    q = open_queue(settings)
    dry = os.environ.get("ANTON_DRY_RUN") == "1"
    failed = q.recover()
    removed = runner.gc_jobs(lambda job_id: (r := q.get(job_id)) is not None and r.status == "running")
    print(f"anton serve: max_parallel={settings.daemon.max_parallel} daily_limit={settings.daemon.daily_limit} "
          f"dry_run={dry} recovered_failed={failed} gc_removed={removed}", flush=True)
    gh_for = github_factory(settings)
    svc = Service(q, settings.repos, requester_limits={bridge.REQUESTER: settings.daemon.bridge_daily_limit})

    def log_event(ev, row):
        print(f"{ev}: {row.id} {row.repo} -> {row.status}", flush=True)

    dispatcher = Dispatcher([log_event, poller.IssueReporter(settings, gh_for), TelegramNotifier()])
    pool = Pool(q, lambda row, sc: runner.run_queued(row, settings, sc, dry_run=dry), settings.daemon.max_parallel,
                on_event=dispatcher.emit)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    polling = [r.slug for r in settings.repos.values() if r.allowed_authors]
    if polling:
        print(f"anton serve: polling labels on {', '.join(polling)} every {settings.daemon.poll_seconds}s", flush=True)
        threading.Thread(target=poller.poll_forever, args=(settings, svc, gh_for, stop), name="poller",
                         daemon=True).start()
    server = None
    try:
        token = (runner.CONF_DIR / "bridge-token").read_text().strip()
    except OSError:
        token = ""
    if len(token) >= 32:  # the chat bridge is opt-in: no token file, no endpoint
        server = bridge.serve(bridge.Bridge(settings, svc, gh_for), token, settings.daemon.bridge_port)
        print(f"anton serve: chat bridge on 127.0.0.1:{settings.daemon.bridge_port}", flush=True)
    pool.run_forever(stop)
    if server:
        server.shutdown()
    print("anton serve: stopping, waiting for running jobs", flush=True)
    left = pool.drain(timeout=60)
    dispatcher.stop()
    if left:
        print(f"anton serve: {left} job(s) still running, they will be marked failed on next start", flush=True)
    return 0


def cmd_poll(a: argparse.Namespace) -> int:
    """Check GitHub for labelled issues once and queue what qualifies (the daemon does this on a timer)."""
    settings = config.load_settings()
    svc = Service(open_queue(settings), settings.repos)
    gh_for = github_factory(settings)
    for repo in settings.repos.values():
        if not repo.allowed_authors:
            continue
        gh = gh_for(repo)
        gh.ensure_labels(repo.trigger_label)
        res = poller.poll_once(repo, gh, svc)
        print(f"{repo.slug}: queued={res.queued} limited={res.limited}")
        for n, why in res.ignored:
            print(f"  ignored #{n}: {why}")
    return 0


def cmd_jobs(_: argparse.Namespace) -> int:
    for p in sorted(runner.JOBS_DIR.glob("*/job.json")):
        d = json.loads(p.read_text())
        print(f"{d['id']}  {d['status']:<12} {d['repo']}  {safe(d['task'], 50)}  {safe(d.get('pr', ''))}")
    return 0


def cmd_selftest(_: argparse.Namespace) -> int:
    from . import selftest
    return selftest.run()


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)  # task texts, logs and the database are private
    ap = argparse.ArgumentParser(prog="anton")
    sub = ap.add_subparsers(required=True)
    r = sub.add_parser("run", help="run one job now, no queue")
    r.add_argument("--repo", required=True)
    r.add_argument("--task", required=True)
    r.add_argument("--issue", type=int)
    r.add_argument("--dry-run", action="store_true")
    r.set_defaults(fn=cmd_run)
    e = sub.add_parser("enqueue", help="add a job to the queue")
    e.add_argument("--repo", required=True)
    e.add_argument("--task", required=True)
    e.add_argument("--issue", type=int)
    e.set_defaults(fn=cmd_enqueue)
    q = sub.add_parser("queue", help="show the queue")
    q.add_argument("--active", action="store_true")
    q.add_argument("--limit", type=int, default=20)
    q.set_defaults(fn=cmd_queue)
    c = sub.add_parser("cancel")
    c.add_argument("id")
    c.set_defaults(fn=cmd_cancel)
    s = sub.add_parser("serve", help="run the worker pool (daemon)")
    s.set_defaults(fn=cmd_serve)
    po = sub.add_parser("poll", help="check GitHub for labelled issues once")
    po.add_argument("--once", action="store_true", required=True)
    po.set_defaults(fn=cmd_poll)
    j = sub.add_parser("jobs", help="list job directories")
    j.set_defaults(fn=cmd_jobs)
    st = sub.add_parser("selftest", help="try to escape the sandbox (must pass before untrusted input)")
    st.set_defaults(fn=cmd_selftest)
    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except config.ConfigError as ex:
        print(f"config error: {safe(ex)}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
