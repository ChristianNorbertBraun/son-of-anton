import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from anton.adapters.local import LocalCodeHost, LocalTracker
from anton.adapters.stdio import StdioClient, StdioCodeHost, StdioTracker, as_mark, as_pr_state
from anton.ports import PrRef
from anton.ports import AdapterError

from .kit import Contract
from .test_local import bare_repo, pusher

SHIM = Path(__file__).with_name("local_shim.py")


class Base:
    timeout = 20

    def client(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.bare = bare_repo(self.root)
        client = StdioClient([sys.executable, str(SHIM), str(self.root), str(self.bare)], timeout=self.timeout)
        self.addCleanup(client.stop)
        return client


class StdioConformance(Contract, Base, unittest.TestCase):
    def make(self):
        client = self.client()
        tracker, host = LocalTracker(self.root), LocalCodeHost(self.root, self.bare)
        return StdioTracker(client), StdioCodeHost(client), SimpleNamespace(
            add=tracker.owner_add, close=tracker.owner_close, push=pusher(self.bare), review=host.owner_review,
            note=host.owner_note, merge=host.owner_merge, comments=host.comments, task_comments=tracker.comments,
            task_state=tracker.state_of, pr_text=lambda pr: (host.details(pr)["title"], host.details(pr)["body"]),
            ready=host.owner_ready, draft=host.owner_draft, bot=host.actor)


class ClientTests(Base, unittest.TestCase):
    def test_a_program_that_died_is_started_again(self):
        client = self.client()
        with self.assertRaises(AdapterError):
            client.call("crash")
        self.assertEqual(client.call("list_ready"), [])

    def test_an_answer_that_is_no_json_is_an_error_and_the_next_call_works(self):
        client = self.client()
        with self.assertRaises(AdapterError):
            client.call("garbage")
        self.assertEqual(client.call("list_ready"), [])

    def test_a_program_that_hangs_does_not_hang_the_conductor(self):
        self.timeout = 0.3
        client = self.client()
        with self.assertRaisesRegex(AdapterError, "in time"):
            client.call("hang")
        self.timeout = 20
        client.timeout = 20
        self.assertEqual(client.call("list_ready"), [])

    def test_a_program_that_cannot_start_is_an_error(self):
        client = StdioClient([sys.executable, "-c", "import sys; sys.exit(3)"], timeout=5)
        self.addCleanup(client.stop)
        with self.assertRaises(AdapterError):
            client.call("list_ready")

    def test_an_answer_to_another_question_is_refused(self):
        client = StdioClient([sys.executable, "-c",
                              "import sys,json\n"
                              "for line in sys.stdin: print(json.dumps({'id': 999, 'result': []}), flush=True)"], timeout=5)
        self.addCleanup(client.stop)
        with self.assertRaisesRegex(AdapterError, "does not match"):
            client.call("list_ready")

    def test_a_draft_mark_that_is_no_pair_of_strings_is_an_error(self):
        self.assertEqual(as_mark(["ready", "alice"]), ("ready", "alice"))
        self.assertIsNone(as_mark(None))
        for bad in (["ready"], ["ready", "alice", "x"], ["ready", None], "ready", 5, {}):
            with self.subTest(bad=bad), self.assertRaises(AdapterError):
                as_mark(bad)

    def test_a_review_without_a_usable_id_is_an_error(self):
        data = {"ref": {"number": 1}, "head": "a" * 40, "draft": False, "state": "open", "merged": False}
        review = {"actor": "alice", "state": "approved", "commit": "a" * 40, "text": ""}
        self.assertEqual(as_pr_state({**data, "reviews": [{**review, "id": "7"}]}).reviews[0].id, "7")
        for bad in ({}, {"id": ""}, {"id": 7}, {"id": None}, {"id": "a:b"}):
            with self.subTest(bad=bad), self.assertRaises(AdapterError):
                as_pr_state({**data, "reviews": [{**review, **bad}]})

    def test_is_draft_takes_only_a_boolean(self):
        for answer, expected in ((True, True), (False, False)):
            self.assertIs(StdioCodeHost(SimpleNamespace(call=lambda *a, x=answer, **k: x)).is_draft(PrRef(1)), expected)
        for bad in ("false", "true", 0, 1, None, []):
            with self.subTest(bad=bad), self.assertRaises(AdapterError):
                StdioCodeHost(SimpleNamespace(call=lambda *a, x=bad, **k: x)).is_draft(PrRef(1))

    def test_a_flood_is_cut_off(self):
        client = StdioClient([sys.executable, "-c",
                              "import sys\nsys.stdin.readline()\nsys.stdout.write('x' * 5_000_000)\nsys.stdout.flush()"],
                             timeout=10)
        self.addCleanup(client.stop)
        with self.assertRaises(AdapterError):
            client.call("list_ready")


if __name__ == "__main__":
    unittest.main()
