import tempfile
import threading
import time
import unittest
from pathlib import Path

from anton import config
from anton.config import ConfigError
from anton.pool import Outcome, Pool
from anton.queue import DAY, LimitError, Queue


class Clock:
    def __init__(self, t=1_800_000_000):
        self.t = t

    def __call__(self):
        return self.t


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.q = Queue(Path(self.tmp.name) / "q.db", clock=self.clock, daily_limit=3)

    def tearDown(self):
        self.tmp.cleanup()

    def test_enqueue_and_fifo_claim(self):
        a, _ = self.q.enqueue("o/r", "first")
        self.clock.t += 1
        b, _ = self.q.enqueue("o/r", "second")
        self.assertEqual(self.q.claim_next(2).id, a.id)
        self.assertEqual(self.q.claim_next(2).id, b.id)
        self.assertIsNone(self.q.claim_next(2))

    def test_same_issue_is_deduped_while_active(self):
        a, created = self.q.enqueue("o/r", "t", issue=12)
        b, created2 = self.q.enqueue("o/r", "t", issue=12)
        self.assertTrue(created)
        self.assertFalse(created2)
        self.assertEqual(a.id, b.id)
        other, created3 = self.q.enqueue("o/r", "t", issue=13)
        self.assertTrue(created3)
        self.assertNotEqual(other.id, a.id)

    def test_issue_can_be_requeued_after_it_finished(self):
        a, _ = self.q.enqueue("o/r", "t", issue=12)
        self.q.claim_next(2)
        self.q.finish(a.id, "failed", reason="x")
        _, created = self.q.enqueue("o/r", "t", issue=12)
        self.assertTrue(created)

    def test_free_text_tasks_are_not_deduped(self):
        a, _ = self.q.enqueue("o/r", "same")
        b, created = self.q.enqueue("o/r", "same")
        self.assertTrue(created)
        self.assertNotEqual(a.id, b.id)

    def test_daily_limit_and_window(self):
        for i in range(3):
            self.q.enqueue("o/r", f"t{i}")
        with self.assertRaises(LimitError):
            self.q.enqueue("o/r", "one too many")
        self.clock.t += DAY + 1
        self.q.enqueue("o/r", "next day works")

    def test_cancelled_jobs_do_not_count_against_limit(self):
        ids = [self.q.enqueue("o/r", f"t{i}")[0].id for i in range(3)]
        self.assertEqual(self.q.cancel(ids[0]), "cancelled")
        self.q.enqueue("o/r", "fits again")
        self.assertEqual(self.q.used_today(), 3)

    def test_max_parallel_is_respected(self):
        for i in range(3):
            self.q.enqueue("o/r", f"t{i}")
        self.assertIsNotNone(self.q.claim_next(2))
        self.assertIsNotNone(self.q.claim_next(2))
        self.assertIsNone(self.q.claim_next(2))

    def test_recover_fails_running_jobs_only(self):
        run, _ = self.q.enqueue("o/r", "running")
        self.q.claim_next(2)
        waiting, _ = self.q.enqueue("o/r", "waiting")
        self.assertEqual(self.q.recover(), 1)
        self.assertEqual(self.q.get(run.id).status, "failed")
        self.assertEqual(self.q.get(run.id).reason, "daemon restarted")
        self.assertEqual(self.q.get(waiting.id).status, "queued")

    def test_state_survives_reopening(self):
        a, _ = self.q.enqueue("o/r", "persist", issue=5)
        again = Queue(Path(self.tmp.name) / "q.db", clock=self.clock)
        self.assertEqual(again.get(a.id).task, "persist")

    def test_cancel_variants(self):
        queued, _ = self.q.enqueue("o/r", "q")
        self.assertEqual(self.q.cancel(queued.id), "cancelled")
        running, _ = self.q.enqueue("o/r", "r")
        self.q.claim_next(2)
        self.assertEqual(self.q.cancel(running.id), "cancel-requested")
        self.assertTrue(self.q.is_cancel_requested(running.id))
        self.assertEqual(self.q.cancel(queued.id), "already-finished")
        self.assertEqual(self.q.cancel("nope"), "not-found")

    def test_finish_rejects_non_final_status(self):
        a, _ = self.q.enqueue("o/r", "t")
        with self.assertRaises(ValueError):
            self.q.finish(a.id, "running")


class PoolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.q = Queue(Path(self.tmp.name) / "q.db", daily_limit=50)

    def tearDown(self):
        self.tmp.cleanup()

    def test_never_more_than_max_parallel_and_all_finish(self):
        lock, state = threading.Lock(), {"now": 0, "peak": 0}

        def run(row, should_cancel):
            with lock:
                state["now"] += 1
                state["peak"] = max(state["peak"], state["now"])
            time.sleep(0.15)
            with lock:
                state["now"] -= 1
            return Outcome("pr-open", pr=f"https://example/pr/{row.id}")

        ids = [self.q.enqueue("o/r", f"t{i}")[0].id for i in range(5)]
        pool = Pool(self.q, run, max_parallel=2)
        self.assertTrue(pool.wait_idle(timeout=10))
        self.assertEqual(state["peak"], 2)
        self.assertTrue(all(self.q.get(i).status == "pr-open" and self.q.get(i).pr for i in ids))

    def test_crashing_job_is_failed_and_pool_keeps_going(self):
        def run(row, should_cancel):
            if row.task == "boom":
                raise RuntimeError("kaputt")
            return Outcome("pr-open", pr="https://example/pr/1")

        bad, _ = self.q.enqueue("o/r", "boom")
        good, _ = self.q.enqueue("o/r", "fine")
        self.assertTrue(Pool(self.q, run, max_parallel=1).wait_idle(timeout=10))
        self.assertEqual(self.q.get(bad.id).status, "failed")
        self.assertIn("kaputt", self.q.get(bad.id).reason)
        self.assertEqual(self.q.get(good.id).status, "pr-open")

    def test_running_job_can_be_cancelled(self):
        started = threading.Event()

        def run(row, should_cancel):
            started.set()
            for _ in range(200):
                if should_cancel():
                    return Outcome("cancelled")
                time.sleep(0.02)
            return Outcome("pr-open")

        job, _ = self.q.enqueue("o/r", "long")
        pool = Pool(self.q, run, max_parallel=1)
        pool.tick()
        self.assertTrue(started.wait(5))
        self.assertEqual(self.q.cancel(job.id), "cancel-requested")
        self.assertTrue(pool.wait_idle(timeout=10))
        self.assertEqual(self.q.get(job.id).status, "cancelled")

    def test_event_callback_errors_do_not_break_jobs(self):
        def boom(event, row):
            raise RuntimeError("telegram down")

        job, _ = self.q.enqueue("o/r", "t")
        pool = Pool(self.q, lambda r, c: Outcome("pr-open", pr="x"), max_parallel=1, on_event=boom)
        self.assertTrue(pool.wait_idle(timeout=10))
        self.assertEqual(self.q.get(job.id).status, "pr-open")


class DaemonConfigTests(unittest.TestCase):
    def test_defaults_and_example(self):
        d = config.parse_daemon({})
        self.assertEqual((d.max_parallel, d.daily_limit), (2, 10))
        ex = config.load_daemon(Path(__file__).parent.parent / "examples/repos.toml")
        self.assertEqual((ex.max_parallel, ex.daily_limit, ex.poll_seconds), (2, 10, 120))

    def test_limits_are_validated(self):
        for bad in ({"max_parallel": 0}, {"max_parallel": 9}, {"daily_limit": 0}, {"poll_seconds": 5}):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                config.parse_daemon({"daemon": bad})


if __name__ == "__main__":
    unittest.main()
