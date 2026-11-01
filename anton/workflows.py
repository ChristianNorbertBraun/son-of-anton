"""Built-in workflows. Plain data of the shape TOML workflows will have: a list of stages, run in order.

A stage is a step job (plan, implement, review, cleanup), `pr` (open the draft PR), `ready` (mark it ready) or
`gate:<name>` (wait for the owner). `fix` and `revise` are no stages: they are detours back into the list."""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Workflow:
    name: str
    stages: tuple[str, ...]


WORKFLOWS = {w.name: w for w in (
    Workflow("quick", ("implement", "pr", "review", "ready", "gate:merge")),
    Workflow("standard", ("plan", "pr", "gate:plan", "implement", "review", "cleanup", "ready", "gate:merge")),
)}
LABEL_PREFIX = "flow:"
FLOW_BRANCH_PREFIX = "anton/flow-"
FLOW_BRANCH_RE = re.compile(r"anton/flow-[0-9a-f]{8}")  # the fence: only a flow pushes here. Older jobs had names like
#                                                         anton/flow-chart-page-136ebb, and those stay theirs
# labels only a flow sets. anton:running and anton:pr also belong to normal jobs, so they cannot mark one
FLOW_ONLY_LABELS = ("anton:awaiting-plan-review", "anton:blocked")


def is_flow_issue(labels) -> bool:
    """For the older ways in: a shortcut with a clear message. The branch fence in prs.inspect_pr is the guarantee."""
    return any(from_label(x) is not None or x in FLOW_ONLY_LABELS for x in labels)


def from_label(label: str) -> Workflow | None:
    return WORKFLOWS.get(label.removeprefix(LABEL_PREFIX)) if label.startswith(LABEL_PREFIX) else None


def flow_label(labels) -> Workflow | None:
    """The workflow an issue asks for, or None. Two different ones ask for nothing: the owner has to choose."""
    found = {w for w in map(from_label, labels) if w}
    return found.pop() if len(found) == 1 else None
