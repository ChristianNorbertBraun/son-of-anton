import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from anton.adapters.github import GitHubCodeHost, GitHubTracker
from anton.github import PERMISSIONS, GitHub
from anton.ports import PrRef, marked
from fake_github import FakeGitHub

from .kit import Contract


def clients(fake):
    gh = GitHub(1, Path("/nonexistent"), mock.Mock(slug=fake.slug, installation_id=1), api=fake.api, mint=fake.mint)
    return GitHubTracker(gh, fake.bot), GitHubCodeHost(gh, fake.bot)


class GitHubConformance(Contract, unittest.TestCase):
    def make(self):
        fake = FakeGitHub()
        tracker, host = clients(fake)
        return tracker, host, SimpleNamespace(
            add=fake.add_issue, close=fake.close_issue, push=fake.push, review=fake.add_review, note=fake.add_note,
            merge=fake.merge, comments=fake.comments_of, task_comments=fake.task_comments, task_state=fake.task_state,
            pr_text=fake.pr_text)


class GitHubSpecificTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeGitHub()
        self.tracker, self.host = clients(self.fake)

    def opened(self):
        self.fake.push("anton/flow-a")
        return self.host.open_draft_pr("anton/flow-a", "main", "T", "B")

    def test_a_strangers_pull_request_on_the_flow_branch_is_never_adopted(self):
        self.fake.push("anton/flow-a")
        foreign = self.fake.add_foreign_pull("anton/flow-a")
        pr = self.host.open_draft_pr("anton/flow-a", "main", "T", "B")
        self.assertNotEqual(pr.number, foreign)
        self.assertTrue(self.fake.pulls[pr.number]["draft"])

    def test_a_pull_request_of_ours_to_another_base_or_from_a_fork_is_not_the_flows(self):
        self.fake.push("anton/flow-a")
        other_base = self.fake.add_foreign_pull("anton/flow-a", author=self.fake.bot, base="develop")
        fork = self.fake.add_foreign_pull("anton/flow-a", author=self.fake.bot, head_repo="fork/repo")
        self.assertNotIn(self.host.open_draft_pr("anton/flow-a", "main", "T", "B").number, (other_base, fork))

    def test_a_forged_marker_does_not_swallow_a_comment(self):
        self.fake.add_issue("1")
        self.fake.add_foreign_comment("1", marked("forged", "f1-9"))
        self.tracker.report("1", "blocked", "Real reason", "f1-9")
        self.assertTrue(any("Real reason" in c for c in self.fake.task_comments("1")))
        pr = self.opened()
        self.fake.add_foreign_comment(pr.number, marked("forged", "f1-8"))
        self.host.comment(pr, "Real note", "f1-8")
        self.assertTrue(any("Real note" in c for c in self.fake.comments_of(pr)))

    def test_a_review_keeps_the_commit_it_was_written_on(self):
        pr = self.opened()
        self.fake.add_review(pr, "owner", "approved", "a" * 40)
        self.fake.push("anton/flow-a")
        self.assertEqual(self.host.pr_state(pr).reviews[0].commit, "a" * 40)
        self.assertNotEqual(self.host.pr_state(pr).head, "a" * 40)

    def test_at_the_event_cap_the_label_actor_is_unknown(self):
        self.fake.add_issue("1")
        self.fake.issues[1]["_events"] *= 501
        self.assertIsNone(self.tracker.list_ready()[0].label_actor)

    def test_a_pull_request_is_no_task(self):
        self.fake.add_issue("1")
        self.fake.issues[1]["pull_request"] = {}
        self.assertEqual(self.tracker.list_ready(), [])

    def test_two_flow_labels_ask_for_nothing(self):
        self.fake.add_issue("1", labels=("flow:quick", "flow:standard"))
        self.assertEqual(self.tracker.list_ready(), [])

    def test_the_label_actor_comes_from_the_event_log(self):
        self.fake.add_issue("1", label_actor="alice")
        self.fake.issues[1]["_events"].append({"event": "labeled", "label": {"name": "flow:quick"},
                                               "actor": {"login": "mallory"}})
        self.assertEqual(self.tracker.list_ready()[0].label_actor, "mallory")

    def test_a_flow_state_replaces_only_flow_states(self):
        self.fake.add_issue("1", labels=("bug", "anton:running", "anton:failed"))
        self.tracker.report("1", "blocked", None, "m")
        names = {x["name"] for x in self.fake.issues[1]["labels"]}
        self.assertEqual(names, {"bug", "anton:blocked", "anton:failed"})

    def test_prepare_creates_every_label_a_flow_uses(self):
        self.tracker.prepare()
        self.assertIn("flow:quick", self.fake.labels)
        self.assertIn("anton:awaiting-plan-review", self.fake.labels)

    def test_dismissed_and_pending_reviews_do_not_exist(self):
        pr = self.opened()
        for state in ("dismissed", "pending"):
            self.fake.add_review(pr, "owner", state, "a" * 40)
        self.assertEqual(self.host.pr_state(pr).reviews, ())

    def test_a_review_by_a_deleted_user_does_not_exist(self):
        pr = self.opened()
        self.fake.pulls[pr.number]["_reviews"].append({"user": None, "state": "APPROVED", "commit_id": "a" * 40})
        self.assertEqual(self.host.pr_state(pr).reviews, ())

    def test_a_note_keeps_the_commit_it_was_written_on(self):
        pr = self.opened()
        self.fake.add_note(pr, "owner", "Here", "a" * 40)
        self.assertEqual(self.host.pr_state(pr).notes[0].commit, "a" * 40)

    def test_mark_ready_fails_on_a_200_with_errors(self):
        pr = self.opened()
        self.fake.fail_graphql = "Resource not accessible by integration"
        with self.assertRaises(RuntimeError):
            self.host.mark_ready(pr)
        self.assertTrue(self.host.pr_state(pr).draft)

    def test_mark_ready_of_a_ready_pull_request_makes_no_call(self):
        pr = self.opened()
        self.host.mark_ready(pr)
        calls = len(self.fake.calls)
        self.host.mark_ready(pr)
        self.assertFalse(any(path == "/graphql" for _, path, _ in self.fake.calls[calls:]))

    def test_each_write_to_a_pull_request_has_its_own_token(self):
        pr = self.opened()
        self.host.comment(pr, "a", "m1")
        self.host.close(pr)
        writes = [token for method, path, token in self.fake.calls if method != "GET" and "/labels" not in path]
        self.assertEqual(len(writes), len(set(writes)))

    def test_the_cached_token_cannot_write_pull_requests(self):
        self.assertEqual(PERMISSIONS["pull_requests"], "read")
        self.opened()
        self.assertTrue(any(t.get("pull_requests") == "write" for t in self.fake.tokens))

    def test_a_server_error_is_not_read_as_not_found(self):
        self.fake.api = mock.Mock(side_effect=RuntimeError("GitHub API GET /x: 500 b'boom'"))
        tracker, host = clients(self.fake)
        with self.assertRaises(RuntimeError):
            host.head_of("anton/flow-a")
        with self.assertRaises(RuntimeError):
            tracker.get("1")

    def test_unknown_pull_request_on_mark_ready(self):
        from anton.ports import AdapterError
        with self.assertRaises(AdapterError):
            self.host.mark_ready(PrRef(404))


if __name__ == "__main__":
    unittest.main()
