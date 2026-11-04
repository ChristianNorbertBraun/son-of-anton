"""The core of a flow: `step(flow, event, policy) -> (flow, actions)`. Pure: no I/O, no clock, no adapters.

TABLE holds a decision for every state and every event, and a test enforces that. A step result the table has no
answer for blocks the flow, it never moves it on. Trust is decided here and only here: an event carries the actor
the tracker recorded, and Policy says whether that actor counts."""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field, fields, replace
from typing import Callable

from .ports import PrState, Task
from .workflows import FLOW_BRANCH_PREFIX, WORKFLOWS

TERMINAL = ("done", "cancelled")
STATE_KINDS = ("queued", "running", "awaiting-gate", "blocked", "cancelling", "done", "cancelled")
JOB_STAGES = ("plan", "revise", "implement", "rework", "review", "fix", "cleanup")
EVENT_KINDS = ("start", "step_finished", "gate_passed", "gate_rejected", "approved", "stale_review", "draft_marked",
               "gate_rearm", "pr_opened", "pr_merged", "pr_closed", "issue_closed", "head_moved", "stall_detected",
               "retry", "cancel", "effect_failed")
GATES = ("gate:plan", "gate:merge")
MAX_REVISIONS = 3
MAX_REWORKS = 3  # rounds of requested changes at the merge gate, then the owner
MAX_RETRIES = 1  # a failed step, a stalled one: once more, then the owner
MAX_RESTARTS = 5  # a restart costs no attempt, but a step that kills its worker must not run all day
RETRY_DELAY = 60  # seconds
BLOCKING_SEVERITIES = ("high", "medium")


@dataclass(frozen=True)
class Policy:
    allowed: frozenset[str]  # lower case
    never: frozenset[str] = frozenset()  # the App, chat agents: they never count, even if listed in allowed
    bot: str = ""  # the App's login: its issues may be started by an allowed human, it can start nothing itself

    @classmethod
    def of(cls, allowed, never=(), bot: str = "") -> "Policy":
        return cls(frozenset(a.lower() for a in allowed), frozenset(n.lower() for n in (*never, bot) if n), bot.lower())

    def trusted(self, actor: str | None) -> bool:
        login = (actor or "").lower()
        return login in self.allowed and login not in self.never and not login.endswith("[bot]")

    def may_start(self, task: Task) -> bool:
        return (self.trusted(task.author) or task.author.lower() == self.bot) and self.trusted(task.label_actor)


@dataclass(frozen=True)
class Flow:
    id: str
    repo: str
    key: str  # the task
    workflow: str
    brief: str  # title and body at claim: later edits by anyone do not reach the model
    base: str
    base_sha: str
    actor: str  # who started it, recorded before the label goes away
    state: str = "queued"
    stage: str = ""
    jobs: int = 0  # jobs submitted, so a retry is a new job and a replay is the same one
    retries: int = 0
    revisions: int = 0
    head: str | None = None
    pr: int | None = None
    reason: str = ""
    feedback: str = ""
    findings: tuple[dict, ...] = ()
    stalled: bool = False
    foreign_head: bool = False  # the branch has a head this flow did not push: only then retry adopts one
    restarts: int = 0
    title: str = ""
    summary: str = ""
    gates: tuple[str, ...] = ()
    reworks: int = 0
    # reviews a gate has answered, as review_key: each one is acted on or commented on once. None: a flow from before
    # this field, whose older reviews were answered by a version that did not record them
    noted: tuple[str, ...] | None = ()
    # at the plan gate: the pull request was made a draft after the gate was entered, so a Ready from now on is new.
    # False for a flow from before this field: it is armed again first
    draft_checked: bool = False

    @property
    def branch(self) -> str:
        return f"{FLOW_BRANCH_PREFIX}{self.id}"

    @property
    def kind(self) -> str:
        return self.state.split(":", 1)[0]

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL


@dataclass(frozen=True)
class Event:
    kind: str
    actor: str | None = None
    commit: str | None = None
    text: str = ""
    job: int = 0  # step_finished: which job of the flow this is about
    result: str = ""  # step_finished: see classify
    head: str | None = None
    title: str = ""
    summary: str = ""
    gates: tuple[str, ...] = ()
    findings: tuple[dict, ...] = ()
    number: int = 0  # pr_opened
    keys: tuple[str, ...] = ()  # gate events: the reviews they answer, as review_key; a review's own key first


@dataclass(frozen=True)
class Action:
    kind: str  # submit_step | open_pr | mark_ready | mark_draft | report | comment | claim | cancel_job | close_pr
    payload: dict = field(default_factory=dict)
    delay: int = 0  # seconds; the effect layer waits, the core has no clock


def classify(status: str, output: dict | None, reason: str | None) -> str:
    """A finished job, in the words of the outcome table in one value."""
    if status == "cancelled":
        return "cancelled"
    if status == "proposed":
        return "protected"
    if status == "failed":
        return "restarted" if reason == "daemon restarted" else "error"
    if status == "step-done":
        return str((output or {}).get("outcome") or "unreadable")
    return "unreadable"


def report(state: str, text: str | None = None) -> Action:
    return Action("report", {"state": state, "text": text})


def submit(flow: Flow, stage: str, delay: int = 0) -> tuple[Flow, list[Action]]:
    jobs = flow.jobs + 1
    name = "plan" if stage == "revise" else stage
    spec = {"step": name, "branch": flow.branch, "base_sha": flow.base_sha, "expected_head": flow.head,
            "issue": flow.key, "brief": flow.brief}
    if stage in ("revise", "rework"):
        spec["feedback"] = flow.feedback
    if stage == "fix":
        spec["findings"] = list(flow.findings)
    return (replace(flow, state=f"running:{stage}", stage=stage, jobs=jobs, stalled=False),
            [Action("submit_step", {"step_id": stage, "attempt": jobs, "spec": spec}, delay)])


def pr_text(flow: Flow) -> dict:
    return {"title": flow.title, "summary": flow.summary, "gates": list(flow.gates)}


def enter(flow: Flow, stage: str | None) -> tuple[Flow, list[Action]]:
    """Move to a stage. `pr` and `ready` are actions, not states: the outbox carries them through a crash."""
    if stage == "pr":
        return _then(flow, stage, Action("open_pr", pr_text(flow)))
    if stage == "ready":
        return _then(flow, stage, Action("mark_ready", pr_text(flow)))
    if stage == "gate:plan":
        # a plan PR is a draft: Ready for review is the owner's go, so one clicked before this plan was up is undone
        return replace(flow, state="awaiting-gate:plan", stage=stage, draft_checked=False), [
            Action("mark_draft"), report("awaiting-plan-review")]
    if stage == "gate:merge":
        return replace(flow, state="awaiting-gate:merge", stage=stage), [report("pr")]
    if stage is None:  # a workflow always ends in a gate; this is what a broken one gets
        return block(flow, "the workflow has no next stage")
    flow, actions = submit(flow, stage)
    return flow, [report("running"), *actions]


def _then(flow: Flow, stage: str, action: Action) -> tuple[Flow, list[Action]]:
    flow, actions = enter(flow, next_stage(flow, stage))
    return flow, [action, *actions]


def next_stage(flow: Flow, stage: str) -> str | None:
    if stage == "revise":
        return "gate:plan"
    if stage == "rework":  # requested changes get the same review as the first version
        return "review"
    stages = WORKFLOWS[flow.workflow].stages
    i = stages.index("review" if stage == "fix" else stage) + 1
    return stages[i] if i < len(stages) else None


def block(flow: Flow, reason: str) -> tuple[Flow, list[Action]]:
    return replace(flow, state="blocked", reason=reason), [report("blocked", f"Blocked: {reason}")]


def done(flow: Flow) -> tuple[Flow, list[Action]]:
    running = [Action("cancel_job")] if flow.kind == "running" else []
    return replace(flow, state="done", reason=""), [*running, report("done")]


def cancelled(flow: Flow, why: str) -> tuple[Flow, list[Action]]:
    close = [Action("close_pr")] if flow.pr else []
    return replace(flow, state="cancelled", reason=why), [report("cancelled", f"Cancelled: {why}"), *close]


def request_cancel(flow: Flow, why: str) -> tuple[Flow, list[Action]]:
    """A running job must end first: closing the PR under it would race its push."""
    if flow.kind == "running":
        return replace(flow, state="cancelling", reason=why), [Action("cancel_job")]
    return cancelled(flow, why)


def on_start(flow: Flow, event: Event, policy: Policy) -> tuple[Flow, list[Action]]:
    flow, actions = enter(flow, WORKFLOWS[flow.workflow].stages[0])
    return flow, [Action("claim"), *actions]


def on_cancel(flow: Flow, event: Event, policy: Policy) -> tuple[Flow, list[Action]]:
    return request_cancel(flow, event.text or "cancelled by the owner")


def on_issue_closed(flow: Flow, event: Event, policy: Policy) -> tuple[Flow, list[Action]]:
    # a chat agent can close issues through the bridge: only a verified actor may end a flow this way
    return request_cancel(flow, "the issue was closed") if policy.trusted(event.actor) else (flow, [])


def on_pr_closed(flow: Flow, event: Event, policy: Policy) -> tuple[Flow, list[Action]]:
    # no check of the actor: a closed pull request cannot go on, whoever closed it
    return request_cancel(flow, "the pull request was closed")


def on_merged(flow: Flow, event: Event, policy: Policy) -> tuple[Flow, list[Action]]:
    return done(flow)


def on_pr_opened(flow: Flow, event: Event, policy: Policy) -> tuple[Flow, list[Action]]:
    return replace(flow, pr=event.number), []


def retry_or_block(flow: Flow, reason: str) -> tuple[Flow, list[Action]]:
    if flow.retries >= MAX_RETRIES:
        return block(flow, reason)
    return submit(replace(flow, retries=flow.retries + 1), flow.stage, RETRY_DELAY)


def has_blocking_findings(findings: tuple[dict, ...]) -> bool:
    return any(f.get("severity") in BLOCKING_SEVERITIES for f in findings)


def on_ok(flow: Flow, event: Event) -> tuple[Flow, list[Action]]:
    stage = flow.stage
    flow = replace(flow, retries=0, head=event.head or flow.head)
    flow = replace(flow, gates=event.gates or flow.gates)
    if stage in ("plan", "revise", "implement"):
        flow = replace(flow, title=event.title or flow.title, summary=event.summary or flow.summary)
    if stage == "review" and has_blocking_findings(event.findings):
        return submit(replace(flow, findings=event.findings), "fix")
    return enter(flow, next_stage(flow, stage))


def on_step_finished(flow: Flow, event: Event, policy: Policy) -> tuple[Flow, list[Action]]:
    if event.job != flow.jobs:
        return flow, []  # an older attempt: only the newest job decides
    result, stage, stalled = event.result, flow.stage, flow.stalled
    flow = replace(flow, stalled=False)
    if stalled and result == "cancelled":  # the conductor cancelled it, not the owner
        return retry_or_block(flow, "the step stalled")
    if result == "restarted":  # the daemon's fault: costs no attempt
        if flow.restarts >= MAX_RESTARTS:
            return block(flow, f"step {stage} was interrupted {MAX_RESTARTS + 1} times")
        return submit(replace(flow, restarts=flow.restarts + 1), stage)
    if result == "ok":
        return on_ok(flow, event)
    if result == "no-change" and stage in ("fix", "cleanup"):
        remark = [Action("comment", {"text": "The fix step changed nothing."})] if stage == "fix" else []
        flow, actions = on_ok(flow, event)
        return flow, [*remark, *actions]
    if result == "no-change" and stage == "rework":  # the owner's next review decides, not this one again
        flow, actions = enter(replace(flow, retries=0), "gate:merge")
        return flow, [Action("comment", {"text": "The rework changed nothing. Request changes again with more "
                                                 "detail."}), *actions]
    if result == "error":
        return retry_or_block(flow, f"step {stage} failed: {event.text}" if event.text else f"step {stage} failed")
    if result == "unreadable":
        return retry_or_block(flow, f"step {stage} left no readable result")
    if result == "cancelled":  # any chat client can cancel a job id, and a job cancel has no verified actor
        return block(flow, f"the job of step {stage} was cancelled")
    if result == "unknown-head":
        flow = replace(flow, foreign_head=True)
    return block(flow, f"step {stage} ended with {result}" + (f": {event.text}" if event.text else ""))


def on_cancelling_finished(flow: Flow, event: Event, policy: Policy) -> tuple[Flow, list[Action]]:
    if event.job != flow.jobs:
        return flow, []
    return cancelled(flow, flow.reason)


def review_key(actor: str, commit: str, state: str, review_id: str = "") -> str:
    return f"{actor.lower()}:{commit}:{state}:{review_id}"


def key_commit(key: str) -> str:
    return key.rsplit(":", 3)[1]


def note(flow: Flow, keys) -> Flow:
    known = flow.noted or ()
    return replace(flow, noted=(*known, *dict.fromkeys(k for k in keys if k not in known)))


def review_marker(flow: Flow, key: str) -> str:
    """The comment about one review: the host finds it again by this, whatever the outbox did."""
    return f"{flow.id}-review-{hashlib.sha256(key.encode()).hexdigest()[:16]}"


def unanswered_key(flow: Flow, event: Event, policy: Policy, state: str) -> str | None:
    """The key of the review this gate event stands for, if it counts: a verified actor, the head the flow knows, a
    gate, and not answered before. None otherwise."""
    if not (flow.stage in GATES and policy.trusted(event.actor) and event.commit == flow.head):
        return None
    prefix = review_key(event.actor or "", event.commit or "", state)  # the review's own key comes first in keys
    key = event.keys[0] if event.keys else prefix
    return None if not key.startswith(prefix) or key in (flow.noted or ()) else key


def on_gate_passed(flow: Flow, event: Event, policy: Policy) -> tuple[Flow, list[Action]]:
    """Ready for review at the plan gate: see ready_event."""
    if not (flow.stage == "gate:plan" and policy.trusted(event.actor) and event.commit == flow.head):
        return flow, []
    flow = note(flow, event.keys)
    return enter(replace(flow, revisions=0, feedback=""), next_stage(flow, flow.stage))


def on_gate_rejected(flow: Flow, event: Event, policy: Policy) -> tuple[Flow, list[Action]]:
    key = unanswered_key(flow, event, policy, "changes_requested")
    if key is None:
        return flow, []
    if flow.stage == "gate:merge":
        if flow.reworks >= MAX_REWORKS:  # not noted: a retry by the owner takes this review up again
            return block(flow, f"changes were requested {MAX_REWORKS + 1} times")
        flow = note(replace(flow, reworks=flow.reworks + 1, feedback=event.text), (key, *event.keys))
        flow, actions = submit(flow, "rework")
        return flow, [report("running"), *actions]
    if flow.revisions >= MAX_REVISIONS:
        return block(flow, f"the plan was rejected {MAX_REVISIONS + 1} times")
    flow = note(replace(flow, revisions=flow.revisions + 1, feedback=event.text), (key, *event.keys))
    flow, actions = submit(flow, "revise")
    return flow, [report("running"), *actions]


def on_approved(flow: Flow, event: Event, policy: Policy) -> tuple[Flow, list[Action]]:
    """Approve is no go at the plan gate any more, and at the merge gate it is a state the owner can see: the merge
    stays the owner's, and a later Request changes still starts a round."""
    key = unanswered_key(flow, event, policy, "approved")
    if key is None:
        return flow, []
    flow = note(flow, (key, *event.keys))
    if flow.stage == "gate:plan":
        text = ("Approving the plan does not start the work. Click **Ready for review** on this pull request to "
                "start it, or request changes to get another version of the plan.")
        return flow, [Action("comment", {"text": text, "marker": review_marker(flow, key)})]
    return flow, [report("approved")]


def on_stale_review(flow: Flow, event: Event, policy: Policy) -> tuple[Flow, list[Action]]:
    """Reviews on a commit that is no longer the head change nothing: one comment each says so."""
    if flow.noted is None:  # from before the reviews were recorded: these were answered then
        return note(flow, event.keys), []
    keys = [k for k in event.keys if k not in flow.noted]
    if flow.stage not in GATES or not keys:
        return flow, []
    actions = [Action("comment", {"text": f"The review on commit {key_commit(k)[:12]} is on an older commit, so it "
                                          "changes nothing. Review the current commit.",
                                  "marker": review_marker(flow, k)}) for k in keys]
    return note(flow, keys), actions


def on_draft_marked(flow: Flow, event: Event, policy: Policy) -> tuple[Flow, list[Action]]:
    """The conductor made sure the pull request is a draft: from now on a Ready can pass the plan gate."""
    return (replace(flow, draft_checked=True), []) if flow.stage == "gate:plan" else (flow, [])


def on_gate_rearm(flow: Flow, event: Event, policy: Policy) -> tuple[Flow, list[Action]]:
    """A plan gate nobody made a draft for, like one entered by an older version: enter it again."""
    if flow.stage != "gate:plan" or flow.draft_checked:
        return flow, []
    return enter(flow, "gate:plan")


def on_head_moved(flow: Flow, event: Event, policy: Policy) -> tuple[Flow, list[Action]]:
    # a push has no verified actor: only `anton flow retry` adopts it
    return block(replace(flow, foreign_head=True),
                 "the branch has a head this flow does not know; `anton flow retry` adopts it")


def on_stall(flow: Flow, event: Event, policy: Policy) -> tuple[Flow, list[Action]]:
    return (flow, []) if flow.stalled else (replace(flow, stalled=True), [Action("cancel_job")])


def on_effect_failed(flow: Flow, event: Event, policy: Policy) -> tuple[Flow, list[Action]]:
    """An action gave up: its outbox entries are gone. The job may still run, and nothing would read its result."""
    running = [Action("cancel_job")] if flow.kind == "running" else []
    flow, actions = block(flow, event.text)
    return flow, [*running, *actions]


def repairs(flow: Flow) -> list[Action]:
    """A failed action left the outbox: past its stage, a flow may have no pull request or a draft that should be ready."""
    stages = WORKFLOWS[flow.workflow].stages
    past_pr = flow.stage in ("revise", "rework", "fix") or (
        flow.stage in stages and stages.index(flow.stage) > stages.index("pr"))
    return ([Action("open_pr", pr_text(flow))] if past_pr and flow.pr is None else []) + (
        [Action("mark_ready", pr_text(flow))] if flow.stage == "gate:merge" else [])


def on_retry(flow: Flow, event: Event, policy: Policy) -> tuple[Flow, list[Action]]:
    """Adopts a head only if an unknown head is what stopped the flow: a retry for failed gates must not take over
    a push nobody looked at. An adopted head before `implement` may have changed the plan: it needs approval again."""
    adopted = flow.foreign_head and event.head is not None
    stage = flow.stage
    if adopted and stage == "implement" and "gate:plan" in WORKFLOWS[flow.workflow].stages:
        stage = "gate:plan"
    was_rework = stage == "rework"
    if was_rework:  # its review is answered: the owner's next one says what to change now
        stage = "gate:merge"
    flow = replace(flow, retries=0, revisions=0, reworks=0, restarts=0, reason="", foreign_head=False, stage=stage,
                   head=event.head if adopted else flow.head)
    fixes = repairs(flow)
    if was_rework:
        fixes.append(Action("comment", {"text": "The retry does not repeat the rework: request changes again to start "
                                                "another round."}))
    if stage.startswith("gate:"):
        flow, actions = enter(flow, stage)
        return flow, [*fixes, *actions]
    flow, actions = submit(flow, stage)
    return flow, [*fixes, report("running"), *actions]


Handler = Callable[[Flow, Event, Policy], "tuple[Flow, list[Action]]"]
IGNORE: Handler = lambda flow, event, policy: (flow, [])
_ends = {"pr_merged": on_merged, "pr_closed": on_pr_closed, "issue_closed": on_issue_closed, "cancel": on_cancel,
         "pr_opened": on_pr_opened, "effect_failed": on_effect_failed}
_reviews = {"gate_passed": IGNORE, "gate_rejected": IGNORE, "approved": IGNORE, "stale_review": IGNORE,
            "draft_marked": IGNORE, "gate_rearm": IGNORE}
TABLE: dict[str, dict[str, Handler]] = {
    "queued": {**_ends, **_reviews, "start": on_start, "step_finished": IGNORE, "head_moved": IGNORE,
               "stall_detected": IGNORE, "retry": IGNORE},
    "running": {**_ends, **_reviews, "start": IGNORE, "step_finished": on_step_finished,
                "head_moved": IGNORE,  # the next step refuses an unknown head itself
                "stall_detected": on_stall, "retry": IGNORE},
    "awaiting-gate": {**_ends, "start": IGNORE, "step_finished": IGNORE, "gate_passed": on_gate_passed,
                      "gate_rejected": on_gate_rejected, "approved": on_approved, "stale_review": on_stale_review,
                      "draft_marked": on_draft_marked, "gate_rearm": on_gate_rearm, "head_moved": on_head_moved,
                      "stall_detected": IGNORE, "retry": IGNORE},
    "blocked": {**_ends, **_reviews, "effect_failed": IGNORE, "start": IGNORE, "step_finished": IGNORE,
                "head_moved": IGNORE, "stall_detected": IGNORE, "retry": on_retry},
    "cancelling": {**_reviews, "start": IGNORE, "step_finished": on_cancelling_finished, "pr_opened": on_pr_opened,
                   "pr_merged": on_merged, "pr_closed": IGNORE, "issue_closed": IGNORE, "head_moved": IGNORE,
                   "stall_detected": IGNORE, "retry": IGNORE, "cancel": IGNORE, "effect_failed": IGNORE},
    "done": {kind: IGNORE for kind in EVENT_KINDS},
    "cancelled": {kind: IGNORE for kind in EVENT_KINDS},
}


def step(flow: Flow, event: Event, policy: Policy) -> tuple[Flow, list[Action]]:
    return TABLE[flow.kind][event.kind](flow, event, policy)


def ready_event(flow: Flow, pr: PrState, mark: tuple[str, str] | None, policy: Policy) -> Event | None:
    """Ready for review passes the plan gate. The pull request was made a draft after the gate was entered
    (draft_checked), so a Ready counts only if it is the newest ready or draft mark, by a verified actor, on the head
    the flow knows. The App's own mark-ready is never one."""
    if not flow.draft_checked or flow.stage != "gate:plan" or pr.draft or pr.head != flow.head or mark is None:
        return None
    kind, actor = mark
    if kind != "ready" or not policy.trusted(actor):
        return None
    return Event("gate_passed", actor=actor, commit=flow.head)


def gate_events(flow: Flow, pr: PrState, policy: Policy, mark: tuple[str, str] | None = None) -> list[Event]:
    """What the pull request means for the gate the flow waits at, in the order to apply them. A review counts only
    by a verified actor and for the head the flow knows, so an approval cannot pass a plan its author never saw, and
    each actor's last one counts. Requested changes win over a Ready and an approval. A review answered before (see
    Flow.noted) does nothing again; one on an older commit gets one comment. `mark`: see ready_event."""
    known = set(flow.noted or ())
    keyed = [(review_key(r.actor, r.commit, r.state, r.id), r) for r in pr.reviews
             if policy.trusted(r.actor) and r.state != "commented"]
    on_head = tuple(dict.fromkeys(k for k, r in keyed if r.commit == flow.head))
    stale = tuple(dict.fromkeys(k for k, r in keyed if r.commit != flow.head and k not in known))
    events = [Event("stale_review", keys=stale)] if stale else []
    latest = {}
    for key, review in keyed:
        if review.commit == flow.head:
            latest[review.actor.lower()] = (key, review)
    fresh = [(k, r) for k, r in latest.values() if k not in known]
    rejected = [(k, r) for k, r in fresh if r.state == "changes_requested"]
    if rejected:
        key, review = rejected[0]
        events.append(Event("gate_rejected", actor=review.actor, commit=flow.head, keys=(key, *on_head),
                            text=feedback(flow, pr, policy) or "Changes were requested, without a comment."))
    if ready := ready_event(flow, pr, mark, policy):
        events.append(replace(ready, keys=on_head))
    approved = [(k, r) for k, r in fresh if r.state == "approved"]
    if approved:
        key, review = approved[0]
        events.append(Event("approved", actor=review.actor, commit=flow.head, keys=(key, *on_head)))
    return events


def feedback(flow: Flow, pr: PrState, policy: Policy) -> str:
    """Only what verified actors wrote about the current head reaches the model."""
    texts = [r.text for r in pr.reviews if policy.trusted(r.actor) and r.commit == flow.head and r.text]
    texts += [n.text for n in pr.notes if policy.trusted(n.actor) and n.commit == flow.head and n.text]
    return "\n\n".join(texts)


def close_event(flow: Flow, pr: PrState | None, task: Task | None) -> Event | None:
    """Read the pull request first: merged wins, so `Closes #N` cannot turn a merge into `cancelled`."""
    if pr is not None and pr.merged:
        return Event("pr_merged")
    if pr is not None and pr.state == "closed":
        return Event("pr_closed")
    if task is not None and task.state == "closed":
        return Event("issue_closed", actor=task.closed_by)
    return None


def to_dict(flow: Flow) -> dict:
    return asdict(flow)


def from_dict(data: dict) -> Flow:
    """Fields a later version added are left out: going back a version must not break the flows it wrote."""
    names = {f.name for f in fields(Flow)}
    data = {k: v for k, v in data.items() if k in names}
    noted = data.get("noted")
    return Flow(**{**data, "findings": tuple(data.get("findings") or ()), "gates": tuple(data.get("gates") or ()),
                   "noted": None if noted is None else tuple(noted)})
