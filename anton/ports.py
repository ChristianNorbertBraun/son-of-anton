"""What the conductor needs from a tracker and a code host. An adapter reports facts with the actor the API
recorded; it never decides trust: that is flow.Policy, in the core. The ports hold what the conductor calls today."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Task:
    key: str  # "42" on GitHub, "ABC-123" on Jira
    title: str
    body: str
    author: str
    labels: tuple[str, ...] = ()
    label_actor: str | None = None  # who set the workflow label, from the tracker's own records
    state: str = "open"  # open | closed
    closed_by: str | None = None


@dataclass(frozen=True)
class PrRef:
    number: int
    url: str = ""


@dataclass(frozen=True)
class Review:
    actor: str
    state: str  # approved | changes_requested | commented
    commit: str
    text: str = ""


@dataclass(frozen=True)
class Note:
    """A comment on a pull request. `commit` is None for a comment on the conversation."""
    actor: str
    text: str
    commit: str | None = None


@dataclass(frozen=True)
class PrState:
    ref: PrRef
    head: str
    draft: bool
    state: str  # open | closed
    merged: bool
    reviews: tuple[Review, ...] = ()
    notes: tuple[Note, ...] = ()


class AdapterError(Exception):
    pass


class TrackerAdapter(Protocol):
    capabilities: set[str]

    def prepare(self) -> None: ...  # once per start: whatever the tracker needs before the first task
    def list_ready(self) -> list[Task]: ...  # open tasks with a workflow label
    def get(self, key: str) -> Task: ...
    def claim(self, key: str, label: str) -> None: ...  # take the label off; repeating it changes nothing
    def report(self, key: str, state: str, text: str | None, marker: str) -> None: ...  # state, and one comment
    def link(self, key: str, url: str, marker: str) -> None: ...


class CodeHostAdapter(Protocol):
    def head_of(self, branch: str) -> str | None: ...
    def open_draft_pr(self, branch: str, base: str, title: str, body: str) -> PrRef: ...  # by branch: never twice
    def pr_state(self, pr: PrRef) -> PrState: ...
    def comment(self, pr: PrRef, text: str, marker: str) -> None: ...  # a marker that is there already: no-op
    def mark_ready(self, pr: PrRef) -> None: ...
    def close(self, pr: PrRef) -> None: ...


def marked(text: str, marker: str) -> str:
    """Comments are plain POSTs: the marker is how a replay after a crash finds the one it already wrote."""
    return f"{text}\n\n<!-- anton:{marker} -->"


def has_marker(text: str, marker: str) -> bool:
    return f"<!-- anton:{marker} -->" in text
