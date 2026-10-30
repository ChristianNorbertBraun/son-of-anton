"""The reference adapters: tasks and pull requests as JSON files, branches in a bare git repo. They need no network
and pass the same conformance kit as every other adapter. The `owner_*` methods play the people on the other side."""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from .. import gitops
from ..ports import AdapterError, Note, PrRef, PrState, Review, Task, has_marker, marked


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> dict:
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data))


class LocalTracker:
    capabilities: set[str] = set()

    def __init__(self, root: Path):
        self.store = Store(Path(root) / "tasks.json")

    def _task(self, data: dict, key: str) -> dict:
        if key not in data:
            raise AdapterError(f"no task {key}")
        return data[key]

    def prepare(self) -> None:
        pass

    def list_ready(self) -> list[Task]:
        return [self._as_task(t) for t in self.store.load().values()
                if t["state"] == "open" and any(label.startswith("flow:") for label in t["labels"])]

    def get(self, key: str) -> Task:
        return self._as_task(self._task(self.store.load(), key))

    @staticmethod
    def _as_task(t: dict) -> Task:
        return Task(t["key"], t["title"], t["body"], t["author"], tuple(t["labels"]), t["label_actor"], t["state"],
                    t["closed_by"])

    def claim(self, key: str, label: str) -> None:
        data = self.store.load()
        task = self._task(data, key)
        task["labels"] = [x for x in task["labels"] if x != label]
        self.store.save(data)

    def report(self, key: str, state: str, text: str | None, marker: str) -> None:
        data = self.store.load()
        task = self._task(data, key)
        task["report_state"] = state
        if text and not any(has_marker(c, marker) for c in task["comments"]):
            task["comments"].append(marked(text, marker))
        self.store.save(data)

    def link(self, key: str, url: str, marker: str) -> None:
        self.report(key, self._task(self.store.load(), key).get("report_state", ""), url, marker)

    def owner_add(self, key: str, title: str = "A task", body: str = "Do it", author: str = "owner",
                  labels: tuple[str, ...] = ("flow:quick",), label_actor: str | None = "owner") -> None:
        data = self.store.load()
        data[key] = {"key": key, "title": title, "body": body, "author": author, "labels": list(labels),
                     "label_actor": label_actor, "state": "open", "closed_by": None, "comments": []}
        self.store.save(data)

    def owner_close(self, key: str, actor: str | None = "owner") -> None:
        data = self.store.load()
        self._task(data, key).update(state="closed", closed_by=actor)
        self.store.save(data)

    def comments(self, key: str) -> list[str]:
        return list(self._task(self.store.load(), key)["comments"])

    def state_of(self, key: str) -> str:
        return self._task(self.store.load(), key).get("report_state", "")


class LocalCodeHost:
    def __init__(self, root: Path, bare: Path):
        self.store, self.bare = Store(Path(root) / "prs.json"), Path(bare)

    def _pr(self, data: dict, number: int) -> dict:
        if str(number) not in data:
            raise AdapterError(f"no pull request {number}")
        return data[str(number)]

    def head_of(self, branch: str) -> str | None:
        try:
            return gitops.git(["rev-parse", "--verify", "-q", f"refs/heads/{branch}"], self.bare).strip() or None
        except RuntimeError:
            return None

    def open_draft_pr(self, branch: str, base: str, title: str, body: str) -> PrRef:
        data = self.store.load()
        for pr in data.values():
            if pr["branch"] == branch:
                return PrRef(pr["number"], pr["url"])
        if self.head_of(branch) is None:
            raise AdapterError(f"no branch {branch}")
        number = len(data) + 1
        url = f"https://example.com/pull/{number}"
        data[str(number)] = {"number": number, "url": url, "branch": branch, "base": base, "title": title, "body": body,
                             "draft": True, "state": "open", "merged": False, "reviews": [], "notes": [],
                             "comments": []}
        self.store.save(data)
        return PrRef(number, url)

    def pr_state(self, pr: PrRef) -> PrState:
        p = self._pr(self.store.load(), pr.number)
        head = p.get("merged_head") or self.head_of(p["branch"]) or ""
        return PrState(PrRef(p["number"], p["url"]), head, p["draft"], p["state"], p["merged"],
                       tuple(Review(**r) for r in p["reviews"]), tuple(Note(**n) for n in p["notes"]))

    def comment(self, pr: PrRef, text: str, marker: str) -> None:
        data = self.store.load()
        p = self._pr(data, pr.number)
        if not any(has_marker(c, marker) for c in p["comments"]):
            p["comments"].append(marked(text, marker))
            self.store.save(data)

    def mark_ready(self, pr: PrRef) -> None:
        data = self.store.load()
        self._pr(data, pr.number)["draft"] = False
        self.store.save(data)

    def close(self, pr: PrRef) -> None:
        data = self.store.load()
        self._pr(data, pr.number)["state"] = "closed"
        self.store.save(data)

    def owner_review(self, pr: PrRef, actor: str, state: str, commit: str, text: str = "") -> None:
        data = self.store.load()
        self._pr(data, pr.number)["reviews"].append(asdict(Review(actor, state, commit, text)))
        self.store.save(data)

    def owner_note(self, pr: PrRef, actor: str, text: str, commit: str | None) -> None:
        data = self.store.load()
        self._pr(data, pr.number)["notes"].append(asdict(Note(actor, text, commit)))
        self.store.save(data)

    def owner_merge(self, pr: PrRef) -> None:
        data = self.store.load()
        p = self._pr(data, pr.number)
        p.update(state="closed", merged=True, merged_head=self.head_of(p["branch"]))
        self.store.save(data)

    def comments(self, pr: PrRef) -> list[str]:
        return list(self._pr(self.store.load(), pr.number)["comments"])

    def details(self, pr: PrRef) -> dict:
        return dict(self._pr(self.store.load(), pr.number))
