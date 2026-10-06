import tempfile
import threading
import time
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from anton import config, poller
from anton.config import ConfigError
from anton.events import Dispatcher
from anton.github import GitHub, state_labels
from anton.queue import MAX_TASK_CHARS, Queue
from anton.service import Service

OWNER = "owner"
CONF = (
    '[github]\napp_id=1\nbot_name="b[bot]"\nbot_id=2\n'
    '[repos."o/r"]\ninstallation_id=7\nallowed_authors=["Owner"]\n'
)


def labeled(actor, label="anton"):
    return {"event": "labeled", "label": {"name": label}, "actor": {"login": actor}}


class FakeGitHub:
    def __init__(self):
        self.open_issues, self.events_by_issue = [], {}
        self.calls = []

    def issues(self, label):
        self.calls.append(("issues", label))
        return list(self.open_issues)

    def events(self, number):
        return self.events_by_issue.get(number, [])

    def add_labels(self, n, labels):
        self.calls.append(("add", n, tuple(labels)))

    def remove_label(self, n, label):
        self.calls.append(("remove", n, label))

    def comment(self, n, body):
        self.calls.append(("comment", n, body))

    def ensure_labels(self, trigger):
        self.calls.append(("ensure", trigger))

    def of(self, kind):
        return [c for c in self.calls if c[0] == kind]


def issue(number=12, author="owner", title="Fix footer", body="The link is broken", **extra):
    return {"number": number, "title": title, "body": body, "user": {"login": author}, **extra}


class PollerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = config.parse_settings(tomllib.loads(CONF))
        self.repo = self.settings.repos["o/r"]
        self.queue = Queue(Path(self.tmp.name) / "q.db", daily_limit=5, max_queued=5)
        self.svc = Service(self.queue, self.settings.repos)
        self.gh = FakeGitHub()
        self.seen = set()

    def tearDown(self):
        self.tmp.cleanup()

    def poll(self):
        return poller.poll_once(self.repo, self.gh, self.svc, self.seen)

    def add(self, **kw):
        n = kw.get("number", 12)
        self.gh.open_issues.append(issue(**kw))
        self.gh.events_by_issue[n] = [labeled("owner")]

    def test_own_issue_labelled_by_the_owner_becomes_a_job(self):
        self.add(number=12)
        res = self.poll()
        self.assertEqual(res.queued, [12])
        job = self.queue.list()[0]
        self.assertEqual((job.repo, job.issue, job.requested_by), ("o/r", 12, "gh:owner"))
        self.assertEqual(job.task, "Fix footer\n\nThe link is broken")
        self.assertIn(("add", 12, ("anton:queued",)), self.gh.calls)
        self.assertIn(("remove", 12, "anton"), self.gh.calls)  # trigger label is consumed

    def test_a_strangers_issue_is_ignored_silently(self):
        self.add(number=5, author="stranger")
        res = self.poll()
        self.assertEqual(res.queued, [])
        self.assertEqual(self.queue.list(), [])
        self.assertEqual(self.gh.of("add") + self.gh.of("remove") + self.gh.of("comment"), [])  # no signal
        self.assertEqual(len(res.ignored), 1)

    def test_the_label_must_come_from_an_allowed_user_too(self):
        self.gh.open_issues.append(issue(number=6))  # own issue ...
        self.gh.events_by_issue[6] = [labeled("cofounder")]  # ... but a cofounder labelled it
        self.assertEqual(self.poll().queued, [])
        self.assertEqual(self.queue.list(), [])

    def test_the_most_recent_labeler_wins(self):
        self.gh.open_issues.append(issue(number=7))
        self.gh.events_by_issue[7] = [labeled("owner"), {"event": "unlabeled", "label": {"name": "anton"}},
                                      labeled("stranger")]
        self.assertEqual(self.poll().queued, [])

    def test_other_labels_do_not_count(self):
        self.gh.open_issues.append(issue(number=8))
        self.gh.events_by_issue[8] = [labeled("owner", "bug")]
        self.assertEqual(self.poll().queued, [])

    def test_missing_events_never_start_a_job_but_are_retried_quietly(self):
        # real GitHub lists the `labeled` event a few seconds after creation
        self.gh.open_issues.append(issue(number=9))
        first = self.poll()
        self.assertEqual((first.queued, first.ignored), ([], []))  # waiting, not rejected
        self.gh.events_by_issue[9] = [labeled("owner")]  # the event arrives
        self.assertEqual(self.poll().queued, [9])

    def test_pull_requests_are_skipped(self):
        self.gh.open_issues.append(issue(number=3, pull_request={"url": "x"}))
        self.gh.events_by_issue[3] = [labeled("owner")]
        self.assertEqual(self.poll().queued, [])

    def test_logins_are_compared_case_insensitively(self):
        self.gh.open_issues.append(issue(number=4, author="OWNER"))
        self.gh.events_by_issue[4] = [labeled("oWnEr")]
        self.assertEqual(self.poll().queued, [4])

    def test_ignored_issues_are_reported_once_not_every_poll(self):
        self.add(number=5, author="stranger")
        self.assertEqual(len(self.poll().ignored), 1)
        self.assertEqual(self.poll().ignored, [])

    def test_a_second_poll_does_not_queue_the_same_issue_twice(self):
        self.add(number=12)
        self.poll()
        self.assertEqual(self.poll().queued, [])
        self.assertEqual(len(self.queue.list()), 1)

    def test_limit_leaves_the_label_for_a_later_poll(self):
        small = Queue(Path(self.tmp.name) / "small.db", daily_limit=1)
        svc = Service(small, self.settings.repos)
        for n in (1, 2):
            self.add(number=n)
        res = poller.poll_once(self.repo, self.gh, svc, set())
        self.assertEqual(res.queued, [1])
        self.assertTrue(res.limited)
        self.assertNotIn(("remove", 2, "anton"), self.gh.calls)  # issue 2 keeps its label

    def test_long_bodies_are_cut_and_nul_bytes_removed(self):
        self.add(number=13, body="x" * 50_000 + "\0")
        self.poll()
        task = self.queue.list()[0].task
        self.assertEqual(len(task), MAX_TASK_CHARS)
        self.assertNotIn("\0", task)

    def test_issue_without_body_still_works(self):
        self.add(number=14, body=None)
        self.assertEqual(self.poll().queued, [14])

    def test_relabelling_a_failed_issue_clears_the_old_state_labels(self):
        self.add(number=15)
        self.poll()
        for stale in ("anton:pr", "anton:failed"):
            self.assertIn(("remove", 15, stale), self.gh.calls)

    def test_event_pagination_is_followed(self):
        calls = []

        def api(method, path, token, body=None):
            calls.append(path)
            return [labeled("x")] * 100 if path.endswith("&page=1") else [labeled("owner")]

        gh = GitHub(1, Path("k"), self.repo, api=api, mint=lambda *a: "tok", clock=lambda: 0)
        events = gh.events(5)
        self.assertEqual(len(events), 101)
        self.assertEqual(poller.last_labeler(events, "anton"), "owner")
        self.assertEqual(len(calls), 2)


class GitHubClientTests(unittest.TestCase):
    def setUp(self):
        self.repo = config.parse_settings(tomllib.loads(CONF)).repos["o/r"]
        self.calls, self.minted, self.now = [], [], [0.0]
        self.gh = GitHub(1, Path("k"), self.repo, api=self.api, mint=self.mint, clock=lambda: self.now[0])

    def api(self, method, path, token, body=None):
        self.calls.append((method, path, token, body))
        return []

    def mint(self, app_id, key, inst, slug, permissions):
        self.minted.append(permissions)
        return f"tok{len(self.minted)}"

    def test_token_has_only_the_needed_permissions_and_is_cached(self):
        self.gh.issues("anton")
        self.gh.comment(1, "hi")
        # issues: write; pull requests and contents only to READ a PR and its branch
        self.assertEqual(self.minted, [{"issues": "write", "pull_requests": "read", "contents": "read"}])
        self.now[0] = 46 * 60
        self.gh.comment(1, "again")
        self.assertEqual(len(self.minted), 2)  # renewed after 45 minutes

    def test_label_names_with_colons_are_url_encoded(self):
        self.gh.remove_label(3, "anton:running")
        self.assertEqual(self.calls[0][:2], ("DELETE", "/repos/o/r/issues/3/labels/anton%3Arunning"))
        self.gh.issues("anton:pr")
        self.assertIn("labels=anton%3Apr", self.calls[-1][1])

    def test_removing_a_missing_label_is_fine_other_errors_are_not(self):
        self.gh._api = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("GitHub API DELETE x: 404 b'nope'"))
        self.gh.remove_label(3, "anton")
        self.gh._api = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("GitHub API DELETE x: 500 b'boom'"))
        with self.assertRaises(RuntimeError):
            self.gh.remove_label(3, "anton")

    def test_ensure_labels_ignores_existing_ones(self):
        def api(method, path, token, body=None):
            self.calls.append(body["name"])
            if body["name"] == "anton":
                raise RuntimeError("GitHub API POST x: 422 b'already_exists'")
            return {}

        self.gh._api = api
        self.gh.ensure_labels("anton")
        self.assertEqual(self.calls, ["anton", "anton:queued", "anton:running", "anton:pr", "anton:failed"])


class ReporterTests(unittest.TestCase):
    def setUp(self):
        self.settings = config.parse_settings(tomllib.loads(CONF))
        self.gh = FakeGitHub()
        self.reporter = poller.IssueReporter(self.settings, lambda repo: self.gh)

    def row(self, **kw):
        base = dict(id="j1", repo="o/r", issue=12, task="t", status="running", requested_by="gh:owner",
                    created=0, started=None, finished=None, pr=None, reason=None, cancel_requested=False)
        return mock.Mock(**{**base, **kw})

    def test_started_moves_queued_to_running(self):
        self.reporter("started", self.row())
        self.assertEqual(self.gh.calls, [("add", 12, ("anton:running",)), ("remove", 12, "anton:queued")])

    def test_success_labels_and_links_the_draft_pr(self):
        self.reporter("finished", self.row(status="pr-open", pr="https://github.com/o/r/pull/9"))
        self.assertIn(("add", 12, ("anton:pr",)), self.gh.calls)
        self.assertIn(("remove", 12, "anton:running"), self.gh.calls)
        self.assertIn("https://github.com/o/r/pull/9", self.gh.of("comment")[0][2])

    def test_failure_is_commented_inside_a_code_block_without_backticks(self):
        self.reporter("finished", self.row(status="failed", reason="oops ``` @everyone\nsecond"))
        comment = self.gh.of("comment")[0][2]
        self.assertIn(("add", 12, ("anton:failed",)), self.gh.calls)
        self.assertNotIn("```\noops ```", comment)
        self.assertEqual(comment.count("```"), 2)  # only our own fence
        self.assertNotIn("\nsecond", comment)  # control characters flattened

    def test_cancelled_counts_as_failed_label(self):
        self.reporter("finished", self.row(status="cancelled"))
        self.assertIn(("add", 12, ("anton:failed",)), self.gh.calls)

    def test_jobs_not_started_from_github_are_left_alone(self):
        self.reporter("finished", self.row(requested_by="cli"))
        self.reporter("finished", self.row(issue=None))
        self.reporter("finished", self.row(repo="other/repo"))
        self.assertEqual(self.gh.calls, [])


class DispatcherTests(unittest.TestCase):
    def test_emit_never_blocks_on_a_slow_handler(self):
        release = threading.Event()
        d = Dispatcher([lambda e, r: release.wait(5)], maxsize=2)
        t0 = time.monotonic()
        for _ in range(10):
            d.emit("started", mock.Mock(id="x"))
        self.assertLess(time.monotonic() - t0, 1.0)
        release.set()
        d.stop()

    def test_a_failing_handler_does_not_stop_the_others_or_the_thread(self):
        seen = []

        def bad(event, row):
            raise RuntimeError("telegram down")

        d = Dispatcher([bad, lambda e, r: seen.append((e, r.id))])
        d.emit("started", mock.Mock(id="a"))
        d.emit("finished", mock.Mock(id="a"))
        d.stop()
        self.assertEqual(seen, [("started", "a"), ("finished", "a")])


class PollLoopTests(unittest.TestCase):
    def test_polling_is_opt_in_per_repo_and_survives_errors(self):
        text = (CONF + '[repos."p/q"]\ninstallation_id=8\n')  # second repo has no allowed_authors
        settings = config.parse_settings(tomllib.loads(text))
        stop, polled = threading.Event(), []

        def gh_for(repo):
            polled.append(repo.slug)
            raise RuntimeError("api down")  # must not kill the loop

        threading.Timer(0.3, stop.set).start()
        with mock.patch.object(settings.daemon.__class__, "poll_seconds", 0.05, create=True):
            object.__setattr__(settings.daemon, "poll_seconds", 0.05)
            poller.poll_forever(settings, mock.Mock(), gh_for, stop, log=lambda m: None)
        self.assertGreater(len(polled), 1)  # retried after the error
        self.assertEqual(set(polled), {"o/r"})  # p/q never polled

    def test_config_rejects_bad_authors_and_labels(self):
        for extra in ('allowed_authors=["bad name"]', 'allowed_authors=["-x"]', 'allowed_authors="owner"',
                      'trigger_label=""', 'trigger_label="a/b"'):
            with self.subTest(extra=extra), self.assertRaises(ConfigError):
                config.parse_repos(tomllib.loads(f'[repos."o/r"]\ninstallation_id=1\n{extra}\n'))
        ok = config.parse_repos(tomllib.loads(
            '[repos."o/r"]\ninstallation_id=1\nallowed_authors=["me", "my-bot[bot]"]\n'))["o/r"]
        self.assertEqual(ok.allowed_authors, ("me", "my-bot[bot]"))
        self.assertEqual(ok.trigger_label, "anton")

    def test_state_label_names(self):
        self.assertEqual(state_labels("anton"), {"queued": "anton:queued", "running": "anton:running",
                                                 "pr": "anton:pr", "failed": "anton:failed"})


if __name__ == "__main__":
    unittest.main()
