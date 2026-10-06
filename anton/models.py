"""Small shared types, kept free of imports so any module can use them without cycles."""
from __future__ import annotations

from dataclasses import dataclass


class Cancelled(Exception):
    """Raised inside a job when the user asked to cancel it."""


@dataclass(frozen=True)
class Outcome:
    status: str  # pr-open | no-changes | answered | proposed | failed | cancelled
    pr: str | None = None
    reason: str | None = None
    answer: str | None = None  # the reply of a question job, or the patch of a proposed change
