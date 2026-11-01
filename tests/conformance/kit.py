"""One suite every adapter must pass, against a fake backend, without a network.

A subclass mixes this into unittest.TestCase and implements `make()`, which returns (tracker, host, owner). `owner`
plays the people on the other side: add(key, ...), close(key, actor), push(branch) -> sha, review(pr, actor, state,
commit, text), note(pr, actor, text, commit), merge(pr), comments(pr), task_comments(key), task_state(key)."""
from anton.ports import AdapterError, PrRef


class Contract:
    def make(self):
        raise NotImplementedError

    def setUp(self):
        self.tracker, self.host, self.owner = self.make()

    # tracker

    def test_a_task_with_a_flow_label_is_ready(self):
        self.owner.add("1", title="T", body="B", author="alice", labels=("flow:quick", "bug"), label_actor="bob")
        tasks = self.tracker.list_ready()
        self.assertEqual([t.key for t in tasks], ["1"])
        task = tasks[0]
        self.assertEqual((task.title, task.body, task.author, task.label_actor), ("T", "B", "alice", "bob"))
        self.assertIn("flow:quick", task.labels)

    def test_a_task_without_a_flow_label_is_not_ready(self):
        self.owner.add("1", labels=("bug",), label_actor=None)
        self.assertEqual(self.tracker.list_ready(), [])

    def test_a_closed_task_is_not_ready(self):
        self.owner.add("1")
        self.owner.close("1", "alice")
        self.assertEqual(self.tracker.list_ready(), [])
        task = self.tracker.get("1")
        self.assertEqual((task.state, task.closed_by), ("closed", "alice"))

    def test_claim_takes_the_label_off_and_can_be_repeated(self):
        self.owner.add("1")
        self.tracker.claim("1", "flow:quick")
        self.tracker.claim("1", "flow:quick")
        self.assertEqual(self.tracker.list_ready(), [])
        self.assertNotIn("flow:quick", self.tracker.get("1").labels)

    def test_a_replayed_report_leaves_one_comment(self):
        self.owner.add("1")
        self.tracker.report("1", "blocked", "It broke", "f1-1")
        self.tracker.report("1", "blocked", "It broke", "f1-1")
        self.assertEqual(len(self.owner.task_comments("1")), 1)
        self.assertEqual(self.owner.task_state("1"), "blocked")

    def test_a_report_without_text_only_sets_the_state(self):
        self.owner.add("1")
        self.tracker.report("1", "running", None, "f1-2")
        self.assertEqual(self.owner.task_comments("1"), [])
        self.assertEqual(self.owner.task_state("1"), "running")

    def test_a_new_report_replaces_the_state(self):
        self.owner.add("1")
        self.tracker.report("1", "running", None, "f1-1")
        self.tracker.report("1", "done", None, "f1-2")
        self.assertEqual(self.owner.task_state("1"), "done")

    def test_a_replayed_link_leaves_one_comment(self):
        self.owner.add("1")
        self.tracker.link("1", "https://example.com/pull/1", "f1-3")
        self.tracker.link("1", "https://example.com/pull/1", "f1-3")
        self.assertEqual(len(self.owner.task_comments("1")), 1)

    def test_an_unknown_task_is_an_adapter_error(self):
        with self.assertRaises(AdapterError):
            self.tracker.get("404")

    # code host

    def opened(self, branch="anton/flow-a"):
        sha = self.owner.push(branch)
        return self.host.open_draft_pr(branch, "main", "Title", "Body"), sha

    def test_head_of_a_branch(self):
        self.assertIsNone(self.host.head_of("anton/flow-none"))
        sha = self.owner.push("anton/flow-a")
        self.assertEqual(self.host.head_of("anton/flow-a"), sha)

    def test_a_new_pull_request_is_a_draft(self):
        pr, sha = self.opened()
        state = self.host.pr_state(pr)
        self.assertEqual((state.draft, state.state, state.merged, state.head), (True, "open", False, sha))

    def test_a_replayed_open_returns_the_same_pull_request(self):
        pr, _ = self.opened()
        self.assertEqual(self.host.open_draft_pr("anton/flow-a", "main", "Title", "Body").number, pr.number)

    def test_mark_ready_can_be_repeated(self):
        pr, _ = self.opened()
        self.host.mark_ready(pr)
        self.host.mark_ready(pr)
        self.assertFalse(self.host.pr_state(pr).draft)

    def test_reviews_carry_actor_state_and_commit(self):
        pr, sha = self.opened()
        self.owner.review(pr, "alice", "changes_requested", sha, "Use sqlite")
        self.owner.review(pr, "bob", "approved", sha, "")
        reviews = self.host.pr_state(pr).reviews
        self.assertEqual([(r.actor, r.state, r.commit) for r in reviews],
                         [("alice", "changes_requested", sha), ("bob", "approved", sha)])
        self.assertEqual(reviews[0].text, "Use sqlite")

    def test_notes_carry_actor_text_and_commit(self):
        pr, sha = self.opened()
        self.owner.note(pr, "alice", "On this line", sha)
        note = self.host.pr_state(pr).notes[0]
        self.assertEqual((note.actor, note.text, note.commit), ("alice", "On this line", sha))

    def test_a_replayed_comment_appears_once(self):
        pr, _ = self.opened()
        self.host.comment(pr, "Hello", "f1-4")
        self.host.comment(pr, "Hello", "f1-4")
        self.assertEqual(len(self.owner.comments(pr)), 1)

    def test_merge_is_visible(self):
        pr, _ = self.opened()
        self.owner.merge(pr)
        state = self.host.pr_state(pr)
        self.assertEqual((state.merged, state.state), (True, "closed"))

    def test_close_can_be_repeated(self):
        pr, _ = self.opened()
        self.host.close(pr)
        self.host.close(pr)
        state = self.host.pr_state(pr)
        self.assertEqual((state.merged, state.state), (False, "closed"))

    def test_an_unknown_pull_request_is_an_adapter_error(self):
        with self.assertRaises(AdapterError):
            self.host.pr_state(PrRef(404))
