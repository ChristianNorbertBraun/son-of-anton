"""The conductor: compares what the tracker, the code host and the queue say with each flow, turns differences into
events, and carries out what the core decides. One process per machine, under a lock.

The core has no clock and no I/O, so everything here is a thin layer: `reconcile` produces events, `effect` runs one
action. An action can run twice after a crash, so each one is idempotent. Everything that goes to GitHub passes
`outbound` first. A flow writes to GitHub by itself, and no person looks at the text before it is public."""
from __future__ import annotations

import re
import secrets
import sys
import threading
import time
from typing import Callable

from . import runner, tripwire
from .config import RepoConfig, Settings
from .flow import Event, Flow, Policy, classify, close_event, gate_events, step
from .flowdb import FlowDb, Pending
from .ports import AdapterError, CodeHostAdapter, PrRef, Task, TrackerAdapter
from .queue import ACTIVE, FINAL, MAX_TASK_CHARS, InputError, JobRow, LimitError, Queue
from .runner import neutralize, safe
from .service import Service
from .steps import STEP_SECONDS
from .workflows import LABEL_PREFIX, flow_label

STALL_MARGIN = 15 * 60  # a step ends by itself after STEP_SECONDS: past this one, nothing is watching it any more
MAX_TRIES = 6
READY_AGAIN = ("This pull request was marked ready for review before the current version of the plan was up, so it is "
               "a draft again. Read the plan, then click **Ready for review** again to start the work.")
OUTAGE_RE = re.compile(r": (?:429|5\d\d) |rate limit", re.I)
MAX_PASSES = 50  # actions of one flow run in order: one pass per action, or a start would take several ticks
BASE_DELAY = 60
LIMIT_DELAY = 300  # a daily limit or a full queue is waiting, not failing
Ports = Callable[[RepoConfig], tuple[TrackerAdapter, CodeHostAdapter]]


class Refused(Exception):
    """Text that may not be published. The message names kind and place, never the value."""


def github_ports(gh_for, bot: str) -> Ports:
    from .adapters.github import GitHubCodeHost, GitHubTracker

    def ports(repo: RepoConfig):
        gh = gh_for(repo)
        return GitHubTracker(gh, bot), GitHubCodeHost(gh, bot)

    return ports


def outage(error: Exception) -> bool:
    """GitHub being away says nothing about the action: waiting costs no try, however long it takes."""
    return isinstance(error, OSError) or bool(OUTAGE_RE.search(str(error)))


def policy_for(settings: Settings, chat_clients: tuple[str, ...]) -> Callable[[RepoConfig], Policy]:
    return lambda repo: Policy.of(repo.allowed_authors, never=chat_clients, bot=settings.github.bot_name)


def brief_of(task: Task) -> str:
    title, body = task.title.replace("\0", "").strip(), task.body.replace("\0", "").strip()
    return f"{title}\n\n{body}".strip()[:MAX_TASK_CHARS]


class Conductor:
    def __init__(self, db: FlowDb, queue: Queue, service: Service, settings: Settings, ports: Ports,
                 policy: Callable[[RepoConfig], Policy], clock: Callable[[], float] = time.time,
                 log: Callable[[str], None] = lambda text: print(text, flush=True)):
        self.db, self.queue, self.service, self.settings = db, queue, service, settings
        self.ports, self.policy, self.clock, self.log = ports, policy, clock, log
        self._prepared: set[str] = set()
        self._ignored: set[tuple[str, str]] = set()

    # --- the loop

    def run_forever(self, stop: threading.Event, interval: float) -> None:
        while not stop.is_set():
            try:
                self.tick()
            except Exception as e:  # one hiccup must not stop the flows
                print(f"conductor: tick failed: {type(e).__name__}: {safe(e, 200)}", file=sys.stderr)
            stop.wait(interval)

    def tick(self) -> None:
        self.run_commands()
        for repo in self.settings.repos.values():
            if repo.tracker:
                self.isolated(f"start {repo.slug}", lambda r=repo: self.start_ready(r))
        for flow in self.db.active():
            self.isolated(f"flow {flow.id}", lambda f=flow: self.reconcile(f))
        self.run_outbox()

    @staticmethod
    def isolated(what: str, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception as e:  # one repo or one flow must not stop the others
            print(f"conductor: {what}: {type(e).__name__}: {safe(e, 200)}", file=sys.stderr)

    def repo_of(self, flow: Flow) -> RepoConfig:
        return self.settings.repos[flow.repo]

    def apply(self, flow: Flow, event: Event, abandon: str | None = None) -> Flow:
        after, actions = step(flow, event, self.policy(self.repo_of(flow)))
        if after != flow or actions or abandon:
            self.db.commit(flow, after, actions, event, abandon)
            if after.state != flow.state:
                self.log(f"flow {flow.id}: {flow.state} -> {after.state} ({event.kind})")
        return after

    # --- what the owner asked for

    def run_commands(self) -> None:
        for command_id, flow_id, kind in self.db.commands():
            self.isolated(f"command {command_id}", lambda c=command_id, f=flow_id, k=kind: self.run_command(c, f, k))

    def run_command(self, command_id: int, flow_id: str, kind: str) -> None:
        flow = self.db.get(flow_id)
        if flow is None or flow.terminal or flow.repo not in self.settings.repos:
            self.db.take_command(command_id)
            return
        _, host = self.ports(self.repo_of(flow))
        if kind == "cancel":
            pr = host.pr_state(PrRef(flow.pr)) if flow.pr else None
            self.apply(flow, Event("pr_merged" if pr is not None and pr.merged else "cancel"))  # merged wins
        elif kind == "retry" and flow.kind == "blocked":
            job = self.queue.step_job(flow.id, flow.stage, flow.jobs)
            if job is not None and job.status in ACTIVE:
                return  # two attempts never run at once: the command stays until this one has ended
            self.apply(flow, Event("retry", head=host.head_of(flow.branch)))
        self.db.take_command(command_id)

    # --- new flows

    def start_ready(self, repo: RepoConfig) -> None:
        tracker, host = self.ports(repo)
        if repo.slug not in self._prepared:
            tracker.prepare()
            self._prepared.add(repo.slug)
        policy = self.policy(repo)
        for task in tracker.list_ready():
            workflow = flow_label(task.labels)
            if workflow is None:
                continue
            if not policy.may_start(task):  # silent on the issue: no spam, no signal to a stranger
                if (repo.slug, task.key) not in self._ignored:
                    self._ignored.add((repo.slug, task.key))
                    self.log(f"conductor: ignored {repo.slug}#{task.key}: author or label actor is not allowed")
                continue
            base_sha = host.head_of(repo.base)
            if base_sha is None:
                raise AdapterError(f"no branch {repo.base}")
            flow = Flow(secrets.token_hex(4), repo.slug, task.key, workflow.name, brief_of(task), repo.base, base_sha,
                        task.label_actor or "")
            if self.db.create(flow):
                self.log(f"flow {flow.id}: {repo.slug}#{task.key} {workflow.name}, started by {safe(flow.actor, 40)}")

    # --- comparing with the world

    def reconcile(self, flow: Flow) -> None:
        if flow.state == "queued":
            self.apply(flow, Event("start", actor=flow.actor))
            return
        if flow.kind in ("running", "cancelling"):
            flow = self.read_job(flow)
            if flow.terminal:
                return
        tracker, host = self.ports(self.repo_of(flow))
        pr = host.pr_state(PrRef(flow.pr)) if flow.pr else None
        try:
            task = tracker.get(flow.key)
        except AdapterError:  # a deleted issue must not hide what happened to the pull request
            task = None
        event = close_event(flow, pr, task)
        if event and self.apply(flow, event) != flow:
            return  # a close the core ignored must not stop the gate below
        if flow.kind != "awaiting-gate" or self.db.has_open(flow.id):
            return  # a gate looks at the pull request only after everything that leads to it has happened
        if pr is None:
            self.apply(flow, Event("effect_failed", text="the flow is at a gate without a pull request"))
        elif pr.head != flow.head:
            self.apply(flow, Event("head_moved", commit=pr.head))
        elif flow.stage == "gate:plan" and not flow.draft_checked:
            self.apply(flow, Event("gate_rearm"))  # no draft was made for this gate: a Ready on it would be old
        else:
            # the mark costs a call, and it counts only for a ready pull request at an armed plan gate
            mark = host.last_draft_mark(PrRef(flow.pr)) if flow.stage == "gate:plan" and not pr.draft else None
            for event in gate_events(flow, pr, self.policy(self.repo_of(flow)), mark):
                if self.apply(flow, event) != flow:
                    return  # one step per look: what it started comes first, the rest is still there next time

    def read_job(self, flow: Flow) -> Flow:
        job = self.queue.step_job(flow.id, flow.stage, flow.jobs)
        if job is None:
            if self.db.has_open(flow.id):
                return flow  # the submit is still in the outbox
            if flow.kind == "cancelling":  # it never got as far as a job
                return self.apply(flow, Event("step_finished", job=flow.jobs, result="cancelled"))
            return self.apply(flow, Event("effect_failed", text=f"step {flow.stage} never got a job"))
        if job.status in FINAL:
            return self.apply(flow, self.finished(flow, job))
        # only a running job can stall: a queued one is waiting its turn, and costs nothing
        if flow.kind == "running" and job.status == "running" and job.started is not None:
            age = self.clock() - job.started
            if flow.stalled and age > STEP_SECONDS + 2 * STALL_MARGIN:  # the cancel did not reach it either
                return self.apply(flow, Event("effect_failed", text="the job does not end: restart `anton serve`"))
            if age > STEP_SECONDS + STALL_MARGIN:
                return self.apply(flow, Event("stall_detected"))
        return flow

    def finished(self, flow: Flow, job: JobRow) -> Event:
        run = self.queue.step(job.id)
        output = (run.output if run else None) or {}
        result = classify(job.status, run.output if run else None, job.reason)
        head = output.get("head")
        if result == "restarted" and run and run.intended_sha:
            _, host = self.ports(self.repo_of(flow))
            if host.head_of(flow.branch) == run.intended_sha:  # the push happened, only the report was lost
                result, head = "ok", run.intended_sha
        self.db.record_run(flow.id, flow.stage, flow.jobs, job.id, result, output.get("usage") or {}, job.started,
                           job.finished)
        return Event("step_finished", job=flow.jobs, result=result, head=head, text=output.get("reason") or "",
                     title=output.get("title") or "", summary=output.get("summary") or "",
                     gates=tuple(output.get("gates") or ()), findings=tuple(output.get("findings") or ()))

    # --- carrying out what the core decided

    def run_outbox(self) -> None:
        for _ in range(MAX_PASSES):
            due = self.db.due()
            if not due:
                return
            for action in due:
                self.isolated(f"action {action.id}", lambda a=action: self.run_action(a))

    def run_action(self, action: Pending) -> None:
        flow = self.db.get(action.flow_id)
        if flow.repo not in self.settings.repos:  # taken out of the config: nothing can run until it is back
            self.db.postpone(action.id, LIMIT_DELAY, "the repo is not in the config", charge=False)
            return
        try:
            self.effect(flow, action)
        except LimitError as e:
            self.db.postpone(action.id, LIMIT_DELAY, safe(e), charge=False)
        except Refused as e:
            self.give_up(action, f"nothing was published: {e}")
        except InputError as e:
            self.give_up(action, f"the step was refused: {safe(e, 200)}")
        except Exception as e:
            if outage(e):
                self.db.postpone(action.id, LIMIT_DELAY, safe(e, 300), charge=False)
            elif action.tries + 1 >= MAX_TRIES:
                self.give_up(action, f"{action.kind} failed {MAX_TRIES} times: {safe(e, 200)}")
            else:
                self.db.postpone(action.id, BASE_DELAY * 2 ** action.tries, safe(e, 300))
        else:
            self.db.finish(action.id)

    def give_up(self, action: Pending, reason: str) -> None:
        """A flow that has stopped keeps what is left in its outbox: closing its PR or showing it as blocked is still
        wanted. One that was moving stops here. The flow is read again: an action may have changed it."""
        flow = self.db.get(action.flow_id)
        if flow.terminal or flow.kind in ("blocked", "cancelling"):
            self.log(f"flow {flow.id}: gave up on {action.kind}: {safe(reason, 200)}")
            self.db.finish(action.id, reason[:300])
        else:
            self.apply(flow, Event("effect_failed", text=reason), abandon=reason[:300])

    def outbound(self, flow: Flow, text: str, where: str) -> str:
        """Model text cannot notify people or close issues, and nothing private leaves through it. The scan comes
        first: neutralize also redacts tokens, and a token must stop the post, not go out redacted."""
        repo = self.repo_of(flow)
        private = tripwire.private_strings(runner.CONF_DIR) if repo.private_check else []
        hits = tripwire.scan_text(text, where, tripwire.secret_values(runner.CONF_DIR), private)
        if hits:
            raise Refused(", ".join(hits))
        return neutralize(text)

    def pr_title(self, flow: Flow, title: str) -> str:
        return self.outbound(flow, f"Anton: {title or f'Work on issue {flow.key}'}", "the title of the pull request")

    def pr_body(self, flow: Flow, payload: dict) -> str:
        summary = self.outbound(flow, payload.get("summary") or "", "the body of the pull request")
        summary = summary.replace("<!--", "&lt;!--")  # an open comment would hide the real checks below it
        gates = self.outbound(flow, "\n".join(payload.get("gates") or ["- not run yet"]), "the checks of the pull request")
        return (f"{f'Closes #{flow.key}' if flow.key.isdigit() else f'Task {flow.key}'}\n\n## Summary\n\n{summary}\n\n"
                f"## Checks\n\n{gates}\n\n---\nDraft by Son of Anton, flow `{flow.id}`. Review before merging; "
                "deploying is a manual step.")

    def effect(self, flow: Flow, action: Pending) -> None:
        tracker, host = self.ports(self.repo_of(flow))
        ref = PrRef(flow.pr) if flow.pr else None
        if action.kind == "claim":
            tracker.claim(flow.key, f"{LABEL_PREFIX}{flow.workflow}")
        elif action.kind == "submit_step":
            if flow.kind == "cancelling" or flow.terminal:  # a delayed retry that nobody wants any more
                return
            p = action.payload
            self.service.submit_step(flow.repo, f"Flow {flow.id}: {p['step_id']}", flow.id, p["step_id"], p["attempt"],
                                     p["spec"])
        elif action.kind == "open_pr":
            if flow.terminal:
                # an attempt that looked failed may have opened it after all, and nobody else knows about it
                found = host.pr_for(flow.branch, flow.base) if flow.state == "cancelled" else None
                if found:
                    host.close(found)
                return
            pr = host.open_draft_pr(flow.branch, flow.base, self.pr_title(flow, action.payload.get("title") or ""),
                                    self.pr_body(flow, action.payload))
            flow = self.apply(flow, Event("pr_opened", number=pr.number))
            tracker.link(flow.key, f"Draft pull request: {pr.url}", action.marker)
        elif action.kind == "mark_ready":
            if flow.terminal:
                return
            if ref is None:
                raise RuntimeError("no pull request to mark ready")
            host.update_pr(ref, self.pr_title(flow, action.payload.get("title") or ""),  # it says what was planned
                           self.pr_body(flow, action.payload))
            host.mark_ready(ref)
        elif action.kind == "mark_draft":
            if flow.terminal:
                return
            if ref is None:
                raise RuntimeError("no pull request to mark as a draft")
            # the comment first, under the action's marker: a replay after a crash still finds a ready PR and posts it
            # once, and one after the mark finds a draft and posts nothing
            if not host.is_draft(ref):
                host.comment(ref, self.outbound(flow, READY_AGAIN, "a comment"), action.marker)
            if host.mark_draft(ref):
                self.log(f"flow {flow.id}: Ready came before this plan, the pull request is a draft again")
            self.apply(flow, Event("draft_marked"))
        elif action.kind == "report":
            self.report(flow, tracker, action)
        elif action.kind == "comment":
            text = self.outbound(flow, action.payload["text"], "a comment")
            marker = action.payload.get("marker") or action.marker  # about one review: posted once, whatever happens
            if ref:
                host.comment(ref, text, marker)
            else:
                tracker.link(flow.key, text, marker)
        elif action.kind == "cancel_job":
            job = self.queue.step_job(flow.id, flow.stage, flow.jobs)
            if job is not None:
                self.queue.cancel(job.id)
        elif action.kind == "close_pr":
            if ref:
                host.close(ref)
        else:
            raise RuntimeError(f"unknown action {action.kind}")

    def report(self, flow: Flow, tracker: TrackerAdapter, action: Pending) -> None:
        """The state must reach the issue even if the reason may not: a blocked flow nobody sees is worse."""
        text = action.payload.get("text")
        if text:
            try:
                text = self.outbound(flow, text, "a status comment")
            except Refused as e:
                text = f"The reason was withheld: {e}. Run `anton flow show {flow.id}`."
        tracker.report(flow.key, action.payload["state"], text, action.marker)
