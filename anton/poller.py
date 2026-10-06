"""GitHub label trigger. A job starts only if BOTH hold:
  1. the issue was written by an allowed author, and
  2. the trigger label was put on it (most recent `labeled` event) by an allowed user.
Anything else is ignored silently: no comment, no label change (no spam, no signal to a stranger).
Issue comments are never read; only title and body become the task."""
from __future__ import annotations

import re
import sys
import threading
from dataclasses import dataclass, field
from typing import Callable

from .config import RepoConfig, Settings
from .github import GitHub, state_labels
from .queue import MAX_TASK_CHARS, InputError, JobRow, LimitError
from .runner import safe, scrub
from .service import Service

MAX_PER_POLL = 10


@dataclass
class PollResult:
    queued: list[int] = field(default_factory=list)
    ignored: list[tuple[int, str]] = field(default_factory=list)  # new ones only
    limited: bool = False


def build_task(issue: dict) -> str:
    title = (issue.get("title") or "").replace("\0", "").strip()
    body = (issue.get("body") or "").replace("\0", "").strip()
    return f"{title}\n\n{body}".strip()[:MAX_TASK_CHARS]


def last_labeler(events: list[dict], label: str) -> str | None:
    actor = None
    for e in events:  # oldest first: the last match wins
        if e.get("event") == "labeled" and (e.get("label") or {}).get("name") == label:
            actor = (e.get("actor") or {}).get("login")
    return actor


def poll_once(repo: RepoConfig, gh, service: Service, seen: set | None = None,
              trusted_extra: tuple[str, ...] = ()) -> PollResult:
    """`trusted_extra`: additional AUTHORS (our own bot: an issue it created for the user may be started by
    the user's label). Label setters must still be in allowed_authors."""
    seen = seen if seen is not None else set()
    allowed = {a.lower() for a in repo.allowed_authors}
    authors = allowed | {a.lower() for a in trusted_extra}
    labels = state_labels(repo.trigger_label)
    res = PollResult()

    def ignore(number: int, why: str) -> None:
        key = (repo.slug, number, why)
        if key not in seen:
            seen.add(key)
            res.ignored.append((number, why))

    for issue in gh.issues(repo.trigger_label)[:MAX_PER_POLL]:
        if "pull_request" in issue:
            continue
        number = issue["number"]
        author = (issue.get("user") or {}).get("login", "")
        if author.lower() not in authors:
            ignore(number, f"author {safe(author, 40)!r} is not allowed")
            continue
        actor = last_labeler(gh.events(number), repo.trigger_label)
        if actor is None:
            continue  # GitHub lists a new label's event a few seconds late: check again next poll
        if actor.lower() not in allowed:
            ignore(number, f"label was set by {safe(actor, 40)!r}, not an allowed user")
            continue
        task = build_task(issue)
        try:
            row, created = service.submit(repo.slug, task, number, requested_by=f"gh:{actor}")
        except LimitError:
            res.limited = True  # leave the label on: it is picked up again once there is room
            break
        except InputError as e:
            ignore(number, f"invalid task: {safe(e, 80)}")
            continue
        # also when the job already existed: this repairs a label update that failed last time
        gh.add_labels(number, [labels["queued"]])
        gh.remove_label(number, repo.trigger_label)
        for stale in ("pr", "patch", "failed"):
            gh.remove_label(number, labels[stale])
        if created:
            res.queued.append(number)
    return res


COMMENT_PATCH_LIMIT = 50_000  # a GitHub comment holds 65,536 characters


def patch_comment(row: JobRow) -> str:
    """The comment for a proposed change: the patch inside a code fence that no line of it can close."""
    reason = safe(row.reason or "", 400).replace("`", "'")
    patch = row.answer or ""
    if len(patch) > COMMENT_PATCH_LIMIT:
        return (f"Anton prepared a change that touches protected paths, so nothing was pushed: {reason}\n\n"
                f"The patch is too large for a comment. Get it with `anton patch {row.id}` on the machine "
                "that runs Anton, or from the Telegram message.")
    ticks = max((len(m) for m in re.findall(r"`+", patch)), default=0)
    fence = "`" * max(3, ticks + 1)
    return (f"Anton prepared a change that touches protected paths, so nothing was pushed: {reason}\n\n"
            f"Save the block as `anton.patch` and run `git apply anton.patch` on a checkout of the base branch.\n\n"
            f"{fence}diff\n{patch}\n{fence}")


class IssueReporter:
    """Event handler: keeps the state labels on the issue and posts the outcome as a comment."""

    def __init__(self, settings: Settings, gh_for: Callable[[RepoConfig], GitHub]):
        self.settings, self.gh_for = settings, gh_for
        self.__name__ = "issue-reporter"

    def __call__(self, event: str, row: JobRow) -> None:
        if row.issue is None or not row.requested_by.startswith("gh:"):
            return
        repo = self.settings.repos.get(row.repo)
        if repo is None:
            return
        gh, labels = self.gh_for(repo), state_labels(repo.trigger_label)
        if event == "started":
            gh.add_labels(row.issue, [labels["running"]])
            gh.remove_label(row.issue, labels["queued"])
        elif event == "finished":
            gh.remove_label(row.issue, labels["running"])
            if row.status == "pr-open" and row.pr:
                gh.add_labels(row.issue, [labels["pr"]])
                gh.comment(row.issue, f"Draft PR is ready for review: {row.pr}")
            elif row.status == "proposed":
                gh.add_labels(row.issue, [labels["patch"]])
                gh.comment(row.issue, patch_comment(row))
            else:
                gh.add_labels(row.issue, [labels["failed"]])
                reason = safe(row.reason or row.status, 300).replace("`", "'")
                gh.comment(row.issue, f"Job ended with `{row.status}`:\n\n```\n{reason}\n```")


def poll_forever(settings: Settings, service: Service, gh_for: Callable[[RepoConfig], GitHub],
                 stop: threading.Event, log: Callable[[str], None] = print) -> None:
    seen: set = set()
    ready: set[str] = set()
    while not stop.is_set():
        for repo in settings.repos.values():
            if not repo.allowed_authors:
                continue  # polling is opt-in per repo
            try:
                gh = gh_for(repo)
                if repo.slug not in ready:
                    gh.ensure_labels(repo.trigger_label)
                    ready.add(repo.slug)
                res = poll_once(repo, gh, service, seen, trusted_extra=(settings.github.bot_name,))
                for n in res.queued:
                    log(f"poller: queued {repo.slug}#{n}")
                for n, why in res.ignored:
                    log(f"poller: ignored {repo.slug}#{n}: {why}")
                if res.limited:
                    log(f"poller: {repo.slug}: queue full or daily limit reached, will retry")
            except Exception as e:  # one repo or one API hiccup must not stop the loop
                print(f"poller: {repo.slug}: {type(e).__name__}: {scrub(e)[:200]}", file=sys.stderr)
        stop.wait(settings.daemon.poll_seconds)
