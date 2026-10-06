"""anton run | enqueue | queue | cancel | serve | jobs"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
from pathlib import Path

from . import config, runner
from .pool import Pool
from .queue import LimitError, Queue

STATE_DIR = Path.home() / ".local/state/son-of-anton"
DB_PATH = STATE_DIR / "queue.db"


def open_queue() -> Queue:
    return Queue(DB_PATH, daily_limit=config.load_daemon().daily_limit)


def cmd_run(a: argparse.Namespace) -> int:
    """Run one job right now, bypassing the queue (manual / debugging)."""
    repo = config.get_repo(config.load_repos(), a.repo)
    job = runner.new_job(repo, a.task, a.issue)
    job.log(f"job {job.id} repo={repo.slug}")
    try:
        print(runner.execute(job, dry_run=a.dry_run))
        return 0
    except Exception as e:  # report, keep logs, free the checkout
        job.log(f"ERROR: {e}")
        if not (job.dir / "job.json").exists() or json.loads((job.dir / "job.json").read_text())["status"] == "running":
            job.state("failed", reason=str(e)[:300])
        print(f"failed: {e}", file=sys.stderr)
        return 1
    finally:
        runner.cleanup(job)


def cmd_enqueue(a: argparse.Namespace) -> int:
    config.get_repo(config.load_repos(), a.repo)  # reject unknown repos up front
    try:
        row, created = open_queue().enqueue(a.repo, a.task, a.issue, requested_by="cli")
    except LimitError as e:
        print(f"rejected: {e}", file=sys.stderr)
        return 1
    print(f"{'queued' if created else 'already active'}: {row.id} ({row.status})")
    return 0


def cmd_queue(a: argparse.Namespace) -> int:
    q = open_queue()
    for r in reversed(q.list(limit=a.limit, active_only=a.active)):
        print(f"{r.id}  {r.status:<10} {r.repo}  {r.task[:48]!r}  {r.pr or r.reason or ''}")
    print(f"-- {q.used_today()}/{q.daily_limit} jobs in the last 24h")
    return 0


def cmd_cancel(a: argparse.Namespace) -> int:
    print(open_queue().cancel(a.id))
    return 0


def cmd_serve(a: argparse.Namespace) -> int:
    repos, daemon = config.load_repos(), config.load_daemon()
    q = Queue(DB_PATH, daily_limit=daemon.daily_limit)
    dry = os.environ.get("ANTON_DRY_RUN") == "1"
    failed = q.recover()
    print(f"anton serve: max_parallel={daemon.max_parallel} daily_limit={daemon.daily_limit} "
          f"dry_run={dry} recovered_failed={failed}", flush=True)
    pool = Pool(q, lambda row, sc: runner.run_queued(row, repos, sc, dry_run=dry), daemon.max_parallel,
                on_event=lambda ev, row: print(f"{ev}: {row.id} {row.repo} -> {row.status}", flush=True))
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    pool.run_forever(stop)
    print("anton serve: stopping, waiting for running jobs", flush=True)
    pool.wait_idle(timeout=60)
    return 0


def cmd_selftest(_: argparse.Namespace) -> int:
    from . import selftest
    return selftest.run()


def cmd_jobs(_: argparse.Namespace) -> int:
    for p in sorted(runner.JOBS_DIR.glob("*/job.json")):
        d = json.loads(p.read_text())
        print(f"{d['id']}  {d['status']:<12} {d['repo']}  {d['task'][:50]}  {d.get('pr', '')}")
    return 0


def main(argv: list[str] | None = None) -> int:
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
    j = sub.add_parser("jobs", help="list job directories")
    j.set_defaults(fn=cmd_jobs)
    st = sub.add_parser("selftest", help="try to escape the sandbox (must pass before untrusted input)")
    st.set_defaults(fn=cmd_selftest)
    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except config.ConfigError as ex:
        print(f"config error: {ex}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
