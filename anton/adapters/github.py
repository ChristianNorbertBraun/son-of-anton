"""Tracker and code host on GitHub. They report what the API recorded, with the actor it recorded. Trust is decided
in the core. Labels and comments use the issues token; changes to a pull request a write token minted per call."""
from __future__ import annotations

from ..github import EVENT_PAGES, PR_DRAFT, GitHub
from ..poller import last_labeler
from ..ports import AdapterError, Note, PrRef, PrState, Review, Task, has_marker, marked
from ..workflows import LABEL_PREFIX, WORKFLOWS, flow_label

STATE_PREFIX = "anton:"
STATE_COLOR = "1d76db"
FLOW_COLOR = "5319e7"
FLOW_STATES = ("queued", "running", "awaiting-plan-review", "pr", "approved", "blocked", "done", "cancelled")
REVIEW_STATES = {"APPROVED": "approved", "CHANGES_REQUESTED": "changes_requested", "COMMENTED": "commented"}
MARK_READY = ("mutation ($id: ID!) { markPullRequestReadyForReview(input: {pullRequestId: $id}) "
              "{ pullRequest { isDraft } } }")
MARK_DRAFT = ("mutation ($id: ID!) { convertPullRequestToDraft(input: {pullRequestId: $id}) "
              "{ pullRequest { isDraft } } }")
DRAFT_EVENTS = {"ready_for_review": "ready", "convert_to_draft": "draft"}


def not_found(error: Exception) -> bool:
    return " 404 " in str(error)


def login(item: dict) -> str | None:
    return (item.get("user") or {}).get("login")


def written_by(comments: list[dict], bot: str, marker: str) -> bool:
    """Only our own comments count: anyone can write a marker, and that would swallow a real report."""
    return any((login(c) or "").lower() == bot.lower() and has_marker(c.get("body") or "", marker) for c in comments)


class GitHubTracker:
    capabilities: set[str] = set()

    def __init__(self, gh: GitHub, bot: str):
        self.gh, self.bot = gh, bot

    def prepare(self) -> None:
        wanted = {**{f"{LABEL_PREFIX}{name}": FLOW_COLOR for name in WORKFLOWS},
                  **{f"{STATE_PREFIX}{state}": STATE_COLOR for state in FLOW_STATES}}
        for name, color in wanted.items():
            self.gh.ensure_label(name, color)

    def _task(self, issue: dict, label: str | None = None) -> Task:
        labels = tuple((x.get("name") or "") for x in issue.get("labels") or [])
        number = issue["number"]
        events = self.gh.events(number) if label else []
        # at the cap the newest events are missing, and the last labeler might be one of them
        actor = last_labeler(events, label) if label and len(events) < EVENT_PAGES * 100 else None
        closed_by = (issue.get("closed_by") or {}).get("login")
        return Task(str(number), issue.get("title") or "", issue.get("body") or "", login(issue) or "", labels, actor,
                    issue.get("state") or "open", closed_by)

    def list_ready(self) -> list[Task]:
        seen: dict[int, Task] = {}
        for name in WORKFLOWS:
            label = f"{LABEL_PREFIX}{name}"
            for issue in self.gh.issues(label):
                if "pull_request" in issue or issue["number"] in seen:
                    continue
                task = self._task(issue, label)
                if flow_label(task.labels) is not None:  # two different flow labels ask for nothing
                    seen[issue["number"]] = task
        return list(seen.values())

    def get(self, key: str) -> Task:
        try:
            return self._task(self.gh.issue(int(key)))
        except RuntimeError as e:
            if not_found(e):
                raise AdapterError(f"no issue {key}") from None
            raise

    def claim(self, key: str, label: str) -> None:
        self.gh.remove_label(int(key), label)

    def report(self, key: str, state: str, text: str | None, marker: str) -> None:
        number, wanted = int(key), f"{STATE_PREFIX}{state}"
        labels = {x.get("name") for x in self.gh.issue(number).get("labels") or []}
        if wanted not in labels:
            self.gh.add_labels(number, [wanted])
        for stale in sorted(labels - {wanted}):
            if stale in {f"{STATE_PREFIX}{s}" for s in FLOW_STATES}:
                self.gh.remove_label(number, stale)
        if text:
            self._comment(number, text, marker)

    def link(self, key: str, url: str, marker: str) -> None:
        self._comment(int(key), url, marker)

    def _comment(self, number: int, text: str, marker: str) -> None:
        if not written_by(self.gh.issue_comments(number), self.bot, marker):
            self.gh.comment(number, marked(text, marker))


class GitHubCodeHost:
    def __init__(self, gh: GitHub, bot: str):
        self.gh, self.bot = gh, bot

    def head_of(self, branch: str) -> str | None:
        try:
            return (self.gh.branch(branch).get("commit") or {}).get("sha")
        except RuntimeError as e:
            if not_found(e):
                return None
            raise

    def pr_for(self, branch: str, base: str) -> PrRef | None:
        for pr in self.gh.pulls_with_head(branch):
            # anyone can open a pull request from a public branch: only ours may become the flow's
            head, target = pr.get("head") or {}, pr.get("base") or {}
            if ((login(pr) or "").lower() == self.bot.lower() and head.get("ref") == branch
                    and target.get("ref") == base
                    and ((head.get("repo") or {}).get("full_name") or "").lower() == self.gh.repo.slug.lower()):
                return PrRef(pr["number"], pr.get("html_url") or "")
        return None

    def open_draft_pr(self, branch: str, base: str, title: str, body: str) -> PrRef:
        found = self.pr_for(branch, base)  # a replay after a crash finds the one it made
        if found:
            return found
        pr = self.gh.write("POST", "/pulls", {"title": title, "head": branch, "base": base, "draft": True, "body": body})
        return PrRef(pr["number"], pr.get("html_url") or "")

    def update_pr(self, pr: PrRef, title: str, body: str) -> None:
        self.gh.write("PATCH", f"/pulls/{int(pr.number)}", {"title": title, "body": body})

    def _pull(self, number: int) -> dict:
        try:
            return self.gh.pull(number)
        except RuntimeError as e:
            if not_found(e):
                raise AdapterError(f"no pull request {number}") from None
            raise

    def pr_state(self, pr: PrRef) -> PrState:
        p = self._pull(pr.number)
        reviews = tuple(Review(login(r), REVIEW_STATES[r["state"]], r.get("commit_id") or "", r.get("body") or "")
                        for r in self.gh.reviews(pr.number) if login(r) and r.get("state") in REVIEW_STATES)
        # original_commit_id: the commit it was written on. commit_id moves along with the branch
        notes = tuple(Note(login(c), c.get("body") or "", c.get("original_commit_id") or c.get("commit_id"))
                      for c in self.gh.review_comments(pr.number) if login(c))
        return PrState(PrRef(p["number"], p.get("html_url") or ""), (p.get("head") or {}).get("sha") or "",
                       bool(p.get("draft")), p.get("state") or "open", bool(p.get("merged")), reviews, notes,
                       self._draft_events(pr.number))

    def _draft_events(self, number: int) -> tuple[tuple[str, str], ...]:
        """In the order GitHub recorded them. At the cap the newest are missing, and the newest is the one that
        counts: then the last word is a draft by nobody."""
        events = self.gh.events(number)
        marks = tuple((DRAFT_EVENTS[e["event"]], (e.get("actor") or {}).get("login") or "")
                      for e in events if e.get("event") in DRAFT_EVENTS)
        return (*marks, ("draft", "")) if len(events) >= EVENT_PAGES * 100 else marks

    def comment(self, pr: PrRef, text: str, marker: str) -> None:
        if not written_by(self.gh.issue_comments(pr.number), self.bot, marker):
            self.gh.write("POST", f"/issues/{int(pr.number)}/comments", {"body": marked(text, marker)})

    def mark_ready(self, pr: PrRef) -> None:
        p = self._pull(pr.number)
        if p.get("draft"):  # REST ignores `draft: false`, so this is the one way
            self.gh.graphql(MARK_READY, {"id": p["node_id"]})

    def mark_draft(self, pr: PrRef) -> bool:
        p = self._pull(pr.number)
        if p.get("draft"):
            return False
        self.gh.graphql(MARK_DRAFT, {"id": p["node_id"]}, PR_DRAFT)
        return True

    def close(self, pr: PrRef) -> None:
        self.gh.write("PATCH", f"/pulls/{int(pr.number)}", {"state": "closed"})
