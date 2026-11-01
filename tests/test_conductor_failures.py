"""What happens when an action fails: outages, giving up, and adopting a pull request that may not be ours."""
import sqlite3
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest import mock

from anton.adapters.github import GitHubCodeHost, written_by
from anton.conductor import MAX_TRIES
from anton.flow import Action, Event
from anton.ports import marked
from flow_rig import Rig
from test_steps import FakeClaude, write


class OutageTests(Rig):
    def _at_open_pr(self):
        self.tracker.owner_add("7")
        self.conductor.tick()
        self.run_jobs(FakeClaude(write("new.txt")))

    def _wait(self, error):
        self._at_open_pr()
        with mock.patch.object(self.host, "open_draft_pr", side_effect=error) as m:
            self.conductor.tick()  # this is what puts open_pr in the outbox
            for _ in range(MAX_TRIES + 2):
                self.conductor.run_outbox()
                self.now += 10_000
        self.assertGreaterEqual(m.call_count, MAX_TRIES + 2)
        self.assertNotEqual(self.flow().state, "blocked")
        self.assertEqual(self.db.due()[0].kind, "open_pr")
        self.assertEqual(self.db.due()[0].tries, 0)

    def test_an_outage_costs_no_try_oserror(self):
        self._wait(OSError("no route"))

    def test_an_outage_costs_no_try_5xx(self):
        self._wait(RuntimeError("GitHub API POST /pulls: 502 b'bad'"))

    def test_an_outage_costs_no_try_429(self):
        self._wait(RuntimeError("GitHub API GET /x: 429 b''"))

    def test_an_outage_costs_no_try_rate_limit(self):
        self._wait(RuntimeError("GitHub API GET /x: 403 b'API rate limit exceeded'"))

    def test_an_outage_costs_no_try_rate_limit_capitalised(self):
        self._wait(RuntimeError("GitHub API GET /x: 403 b'Rate limit'"))

    def test_a_real_refusal_is_charged(self):
        self._at_open_pr()
        with mock.patch.object(self.host, "open_draft_pr", side_effect=RuntimeError("GitHub API: 422 no")):
            self.conductor.tick()
            self.now += 10_000
        self.assertEqual(self.db.due()[0].tries, 1)


class GiveUpTests(Rig):
    def _stopped(self, state):
        flow = self.quick_to_merge_gate()
        stopped = replace(flow, state=state, reason="x")
        (self.conf / "private-strings").write_text("owner@example.org\n")
        self.db.commit(flow, stopped, [Action("comment", {"text": "Mail owner@example.org"}),
                                       Action("report", {"state": "blocked", "text": "later"})], Event("start"))
        with mock.patch.object(self.tracker, "report") as report:
            self.conductor.run_outbox()
        self.assertEqual(report.call_count, 1)

    def test_a_blocked_flow_keeps_its_outbox(self):
        self._stopped("blocked")

    def test_a_cancelling_flow_keeps_its_outbox(self):
        self._stopped("cancelling")

    def test_moving_flow_abandons_what_follows(self):
        self.tracker.owner_add("7")
        self.conductor.tick()
        self.run_jobs(FakeClaude(write("new.txt")))
        jobs = len(self.queue.list(limit=100))
        with mock.patch.object(self.host, "open_draft_pr", side_effect=RuntimeError("GitHub API: 422 no")):
            for _ in range(MAX_TRIES + 1):
                self.conductor.tick()
                self.now += 10_000
        self.assertEqual(self.flow().state, "blocked")
        self.assertEqual(len(self.queue.list(limit=100)), jobs)  # no step of a blocked flow

    def test_abandon_shares_the_transaction(self):
        flow = self.quick_to_merge_gate()
        self.db.commit(flow, flow, [Action("comment", {"text": "x"})], Event("start"))
        c = sqlite3.connect(self.db.path)
        c.execute("CREATE TRIGGER nope BEFORE INSERT ON history BEGIN SELECT RAISE(ABORT, 'boom'); END")
        c.commit()
        c.close()
        with self.assertRaises(sqlite3.Error):
            self.db.commit(flow, replace(flow, state="blocked"), [], Event("effect_failed"), "gave up")
        self.assertTrue(self.db.has_open(flow.id))
        self.assertEqual(self.flow().state, flow.state)

    def test_give_up_reads_the_flow_again(self):
        self.tracker.owner_add("7")
        self.conductor.tick()
        self.run_jobs(FakeClaude(write("new.txt")))
        with mock.patch.object(self.tracker, "link", side_effect=RuntimeError("GitHub API: 422 no")):
            for _ in range(MAX_TRIES + 1):
                self.conductor.tick()
                self.now += 10_000
        self.assertEqual(self.flow().state, "blocked")
        self.assertIsNotNone(self.flow().pr)


def code_host(pulls):
    gh = SimpleNamespace(pulls_with_head=lambda b: pulls, repo=SimpleNamespace(slug="example/repo"),
                         write=mock.Mock(return_value={"number": 99}))
    return GitHubCodeHost(gh, "anton[bot]"), gh


def pull(login="anton[bot]", ref="anton/flow-a", full="example/repo"):
    return {"number": 5, "user": {"login": login}, "head": {"ref": ref, "repo": {"full_name": full}},
            "base": {"ref": "main"}}


class AdoptionTests(unittest.TestCase):
    def open(self, pr):
        host, gh = code_host([pr])
        return host.open_draft_pr("anton/flow-a", "main", "t", "b").number

    def test_another_head_ref_is_not_adopted(self):
        self.assertEqual(self.open(pull(ref="other")), 99)

    def test_login_case_does_not_matter(self):
        self.assertEqual(self.open(pull(login="Anton[bot]")), 5)

    def test_repo_case_does_not_matter(self):
        self.assertEqual(self.open(pull(full="Example/Repo")), 5)

    def test_the_login_of_a_comment_is_compared_without_case(self):
        self.assertTrue(written_by([{"user": {"login": "Anton[bot]"}, "body": marked("x", "m1")}], "anton[bot]", "m1"))


class StaleFlowTests(Rig):
    def test_the_sixth_try_opens_the_pr_and_the_link_fails(self):
        self.tracker.owner_add("7")
        self.conductor.tick()
        self.run_jobs(FakeClaude(write("new.txt")))
        real, calls = self.host.open_draft_pr, []

        def flaky(*a):
            calls.append(1)
            if len(calls) < MAX_TRIES:
                raise RuntimeError("GitHub API: 422 no")
            return real(*a)

        with mock.patch.object(self.host, "open_draft_pr", side_effect=flaky), \
                mock.patch.object(self.tracker, "link", side_effect=RuntimeError("GitHub API: 422 no")):
            for _ in range(MAX_TRIES + 1):
                self.conductor.tick()
                self.now += 10_000
        self.assertEqual(self.flow().state, "blocked")
        self.assertIsNotNone(self.flow().pr)


if __name__ == "__main__":
    unittest.main()
