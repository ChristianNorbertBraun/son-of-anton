"""Built-in workflows. Plain data of the shape TOML workflows will have: a list of stages, run in order.

A stage is a step job (plan, implement, review, cleanup), `pr` (open the draft PR), `ready` (mark it ready) or
`gate:<name>` (wait for the owner). `fix` and `revise` are no stages: they are detours back into the list."""
from __future__ import annotations

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


def from_label(label: str) -> Workflow | None:
    return WORKFLOWS.get(label.removeprefix(LABEL_PREFIX)) if label.startswith(LABEL_PREFIX) else None


def flow_label(labels) -> Workflow | None:
    """The workflow an issue asks for, or None. Two different ones ask for nothing: the owner has to choose."""
    found = {w for w in map(from_label, labels) if w}
    return found.pop() if len(found) == 1 else None
