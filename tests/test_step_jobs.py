import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from anton import bridge, config, poller, runner, telegram
from anton.queue import FINAL, InputError, JobRow, LimitError, Queue
from anton.service import Service

SPEC = {"step": "plan", "issue": 7, "expected_head": None}


class Clock:
    def __init__(self, t=1_800_000_000):
        self.t = t

    def __call__(self):
        return self.t


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "q.db"
        self.q = Queue(self.path, clock=Clock(), daily_limit=3)

    def step(self, attempt=1, step_id="plan", flow_id="f1", spec=None):
        return self.q.enqueue_step("o/r", "flow f1: plan", flow_id, step_id, attempt, spec or SPEC)


class StepQueueTests(Base):
    def test_enqueue_stores_the_step_in_the_side_table(self):
        job, created = self.step()
        self.assertTrue(created)
        self.assertEqual((job.kind, job.status, job.issue), ("step", "queued", None))
        step = self.q.step(job.id)
        self.assertEqual((step.flow_id, step.step_id, step.attempt, step.spec), ("f1", "plan", 1, SPEC))
        self.assertIsNone(step.intended_sha)
        self.assertIsNone(step.output)
        self.assertEqual(self.q.step_job("f1", "plan", 1).id, job.id)

    def test_same_attempt_is_the_same_job_even_after_it_finished(self):
        job, _ = self.step()
        self.q.claim_next(1)
        self.q.finish(job.id, "step-done")
        again, created = self.step()
        self.assertFalse(created)
        self.assertEqual(again.id, job.id)
        self.assertEqual(again.status, "step-done")

    def test_a_new_attempt_or_step_is_a_new_job(self):
        a, _ = self.step(attempt=1)
        b, created = self.step(attempt=2)
        c, created2 = self.step(step_id="implement")
        self.assertTrue(created and created2)
        self.assertEqual(len({a.id, b.id, c.id}), 3)

    def test_step_does_not_meet_the_issue_dedupe(self):
        self.q.enqueue("o/r", "normal job", issue=7)
        _, created = self.step()
        self.assertTrue(created)

    def test_steps_count_against_the_daily_limit(self):
        for n in range(1, 4):
            self.step(attempt=n)
        self.assertEqual(self.q.used_today(), 3)
        with self.assertRaises(LimitError):
            self.step(attempt=4)
        with self.assertRaises(LimitError):
            self.q.enqueue("o/r", "normal job")

    def test_steps_count_for_their_requester(self):
        self.step()
        self.assertEqual(self.q.used_by("conductor"), 1)

    def test_replaying_an_attempt_does_not_need_budget(self):
        for n in range(1, 4):
            self.step(attempt=n)
        first = self.q.step_job("f1", "plan", 3)
        job, created = self.step(attempt=3)
        self.assertFalse(created)
        self.assertEqual(job.id, first.id)

    def test_queue_limit_holds_for_steps(self):
        q = Queue(Path(self.tmp.name) / "full.db", clock=Clock(), daily_limit=9, max_queued=1)
        q.enqueue_step("o/r", "t", "f", "plan", 1, SPEC)
        with self.assertRaises(LimitError):
            q.enqueue_step("o/r", "t", "f", "plan", 2, SPEC)

    def test_invalid_input_is_refused_and_leaves_no_row(self):
        for flow, step, attempt, spec in (("a b", "plan", 1, SPEC), ("f", "../x", 1, SPEC), ("f", "plan", 0, SPEC),
                                          ("f", "plan", True, SPEC), ("f", "plan", 1, []),
                                          ("f", "plan", 1, {"x": "y" * 200_001})):
            with self.subTest(flow=flow, step=step, attempt=attempt):
                with self.assertRaises(InputError):
                    self.q.enqueue_step("o/r", "t", flow, step, attempt, spec)
        with self.assertRaises(InputError):
            self.q.enqueue_step("not a repo", "t", "f", "plan", 1, SPEC)
        self.assertEqual(self.q.list(), [])

    def test_intended_sha_and_output_are_kept(self):
        job, _ = self.step()
        self.q.record_intended_push(job.id, "abc123", {"outcome": "pushing", "title": "T"})
        self.assertEqual(self.q.step(job.id).output["title"], "T")
        self.q.record_step_output(job.id, {"outcome": "ok", "cost": 1.5})
        step = self.q.step(job.id)
        self.assertEqual(step.intended_sha, "abc123")
        self.assertEqual(step.output, {"outcome": "ok", "cost": 1.5})

    def test_step_done_is_final_and_needs_a_running_job(self):
        self.assertIn("step-done", FINAL)
        job, _ = self.step()
        self.assertFalse(self.q.finish(job.id, "step-done"))
        self.q.claim_next(1)
        self.assertTrue(self.q.finish(job.id, "step-done"))
        self.assertEqual(self.q.get(job.id).status, "step-done")

    def test_a_restart_fails_a_running_step_but_keeps_its_intended_sha(self):
        job, _ = self.step()
        self.q.claim_next(1)
        self.q.record_intended_push(job.id, "abc123", {"title": "T"})
        self.assertEqual(self.q.recover(), 1)
        self.assertEqual(self.q.get(job.id).reason, "daemon restarted")
        self.assertEqual(self.q.step(job.id).intended_sha, "abc123")

    def test_a_cancelled_queued_step_is_final(self):
        job, _ = self.step()
        self.assertEqual(self.q.cancel(job.id), "cancelled")
        self.assertIsNone(self.q.claim_next(1))

    def test_opening_an_older_database_adds_the_side_table(self):
        path = Path(self.tmp.name) / "old.db"
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, repo TEXT NOT NULL, issue INTEGER, task TEXT NOT NULL, "
                  "status TEXT NOT NULL, requested_by TEXT NOT NULL DEFAULT 'cli', created INTEGER NOT NULL, "
                  "started INTEGER, finished INTEGER, pr TEXT, reason TEXT, "
                  "cancel_requested INTEGER NOT NULL DEFAULT 0)")
        c.execute("INSERT INTO jobs (id, repo, task, status, created) VALUES ('old', 'o/r', 't', 'pr-open', 1)")
        c.commit()
        c.close()
        q = Queue(path, clock=Clock())
        self.assertEqual(q.get("old").kind, "change")
        _, created = q.enqueue_step("o/r", "t", "f", "plan", 1, SPEC)
        self.assertTrue(created)

    def test_jobs_table_has_no_step_columns(self):
        # a rollback runs JobRow(**row): any new column in jobs would crash the older release
        c = sqlite3.connect(self.path)
        columns = {r[1] for r in c.execute("PRAGMA table_info(jobs)")}
        c.close()
        self.assertEqual(columns, set(JobRow.__dataclass_fields__))


class StepFrontEndTests(Base):
    def repos(self):
        return {"o/r": mock.Mock(spec=config.RepoConfig)}

    def test_service_refuses_a_repo_off_the_allowlist(self):
        svc = Service(self.q, self.repos())
        with self.assertRaises(config.ConfigError):
            svc.submit_step("o/other", "t", "f", "plan", 1, SPEC)
        self.assertEqual(self.q.list(), [])

    def test_service_queues_a_step_as_the_conductor(self):
        job, created = Service(self.q, self.repos()).submit_step("o/r", "t", "f", "plan", 1, SPEC)
        self.assertTrue(created)
        self.assertEqual(job.requested_by, "conductor")

    def test_telegram_stays_silent_for_steps(self):
        job, _ = self.step()
        self.assertIsNone(telegram.format_event("started", job))
        self.assertIsNone(telegram.format_event("finished", self.q.get(job.id)))

    def test_issue_reporter_stays_silent_for_steps(self):
        gh = mock.Mock()
        settings = SimpleNamespace(repos={"o/r": mock.Mock()})
        reporter = poller.IssueReporter(settings, lambda repo: gh)
        job, _ = self.step()
        reporter("started", job)
        reporter("finished", self.q.get(job.id))
        self.assertEqual(gh.mock_calls, [])

    def test_chat_clients_see_a_step_as_a_flow_step(self):
        job, _ = self.step()
        self.assertIn("(flow step)", bridge._line(job))

    def test_a_step_never_runs_as_a_change_job(self):
        job, _ = self.step()
        with mock.patch.object(runner, "run_job") as run_job, mock.patch.object(runner, "new_job") as new_job:
            out = runner.run_queued(job, mock.Mock(), lambda: False)
        self.assertEqual(out.status, "failed")
        run_job.assert_not_called()
        new_job.assert_not_called()


if __name__ == "__main__":
    unittest.main()
