"""A whole conductor in one process: local adapters, a bare git repo, the real step runner, a fake Claude."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from anton import runner, steps
from anton.adapters.local import LocalCodeHost, LocalTracker
from anton.conductor import Conductor
from anton.flow import Policy
from anton.flowdb import FlowDb
from anton.queue import Queue
from anton.service import Service
from test_steps import Base, FakeClaude, write  # noqa: F401  (used by the tests built on this rig)

OWNER = "owner"
CLEAN = '{"findings": []}'


class Rig(Base):
    def setUp(self):
        super().setUp()
        root = Path(self.tmp.name).resolve()
        self.now = 1_800_000_000
        self.repo = replace(self.repo, tracker="github", allowed_authors=(OWNER,))
        self.settings = SimpleNamespace(repos={"example/repo": self.repo}, github=self.settings.github,
                                        daemon=SimpleNamespace(poll_seconds=120))
        self.queue = Queue(root / "rig.db", daily_limit=100)
        self.db = FlowDb(root / "flows.db", clock=lambda: self.now)
        self.tracker, self.host = LocalTracker(root / "t"), LocalCodeHost(root / "h", self.bare)
        self.logged = []
        self.conductor = self.new_conductor()

    def new_conductor(self):
        return Conductor(self.db, self.queue, Service(self.queue, self.settings.repos), self.settings,
                         lambda repo: (self.tracker, self.host),
                         lambda repo: Policy.of(repo.allowed_authors, never=("merlin",), bot="anton[bot]"),
                         clock=lambda: self.now, log=self.logged.append)

    def run_jobs(self, claude):
        ran = 0
        while (row := self.queue.claim_next(1)) is not None:
            with mock.patch.object(runner, "run_claude", claude):
                out = steps.run_step(row, self.settings, lambda: False, self.queue, remote=self.remote)
            self.queue.finish(row.id, out.status, out.pr, out.reason, out.answer)
            ran += 1
        return ran

    def settle(self, *actions, rounds=14):
        """Tick and run jobs until nothing moves. `actions` are what the fake Claude does, in order."""
        claude = actions[0] if len(actions) == 1 and isinstance(actions[0], FakeClaude) else FakeClaude(*actions)
        for _ in range(rounds):
            self.conductor.tick()
            self.run_jobs(claude)
        self.conductor.tick()
        return claude

    def flow(self):
        return self.db.recent(1)[0]

    def pr(self):
        return self.host.pr_state(self.pr_ref())

    def pr_ref(self):
        from anton.ports import PrRef
        return PrRef(self.flow().pr)

    def quick_to_merge_gate(self):
        self.tracker.owner_add("7")
        self.settle(write("new.txt"), CLEAN)
        self.assertEqual(self.flow().state, "awaiting-gate:merge")
        return self.flow()

    def standard_to_plan_gate(self):
        self.tracker.owner_add("7", labels=("flow:standard",))
        self.settle(write(steps.PLAN_PATH, "the plan\n"))
        self.assertEqual(self.flow().state, "awaiting-gate:plan")
        return self.flow()
