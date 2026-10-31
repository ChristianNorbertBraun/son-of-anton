import unittest
from dataclasses import replace
from unittest import mock

from anton import config, flow as F, runner, steps
from anton.conductor import MAX_TRIES, STALL_MARGIN, brief_of
from anton.config import ConfigError
from anton.ports import Task
from anton.queue import LimitError
from anton.steps import STEP_SECONDS
from flow_rig import CLEAN, OWNER, FakeClaude, Rig, write


class ByPrompt(FakeClaude):
    """Flows run side by side, so the answer depends on the question, not on the order."""

    def __call__(self, job, should_cancel, argv=None, prompt=None):
        if "Review the change" in prompt:
            return {"result": CLEAN}
        write(f"{job.id}.txt")(job.work)
        return {"result": "TITLE: Change things\n\nA summary."}


class WholeFlowTests(Rig):
    def test_quick_from_label_to_merge(self):
        flow = self.quick_to_merge_gate()
        pr = self.pr()
        self.assertFalse(pr.draft)
        self.assertEqual(pr.head, flow.head)
        details = self.host.details(self.pr_ref())
        self.assertTrue(details["body"].startswith("Closes #7"))
        self.assertEqual(details["title"], "Anton: Change things")
        self.assertEqual(self.tracker.state_of("7"), "pr")
        self.assertNotIn("flow:quick", self.tracker.get("7").labels)
        self.host.owner_merge(self.pr_ref())
        self.conductor.tick()
        self.assertEqual(self.flow().state, "done")
        self.assertEqual(self.tracker.state_of("7"), "done")

    def test_the_issue_links_the_pull_request_once(self):
        self.quick_to_merge_gate()
        self.assertEqual(len([c for c in self.tracker.comments("7") if "example.com/pull" in c]), 1)

    def test_each_step_records_its_cost(self):
        flow = self.quick_to_merge_gate()
        runs = self.db.runs(flow.id)
        self.assertEqual([r.stage for r in runs], ["implement", "review"])
        self.assertEqual(runs[0].usage["cost_usd"], 0.5)

    def test_standard_from_plan_to_merge_leaves_no_plan_behind(self):
        flow = self.standard_to_plan_gate()
        self.assertTrue(self.pr().draft)
        self.assertEqual(self.tracker.state_of("7"), "awaiting-plan-review")
        self.assertIn(steps.PLAN_PATH, self.remote_files(flow.branch))
        self.host.owner_review(self.pr_ref(), OWNER, "approved", flow.head)
        self.settle(write("new.txt"), CLEAN)
        flow = self.flow()
        self.assertEqual(flow.state, "awaiting-gate:merge")
        self.assertEqual(sorted(self.remote_files(flow.branch)), ["app.txt", "new.txt"])
        self.assertFalse(self.pr().draft)
        self.host.owner_merge(self.pr_ref())
        self.conductor.tick()
        self.assertEqual(self.flow().state, "done")

    def test_request_changes_revises_on_the_same_pull_request(self):
        flow = self.standard_to_plan_gate()
        number = flow.pr
        self.host.owner_review(self.pr_ref(), OWNER, "changes_requested", flow.head, "Use sqlite")
        claude = self.settle(write(steps.PLAN_PATH, "better plan\n"))
        after = self.flow()
        self.assertEqual((after.state, after.pr), ("awaiting-gate:plan", number))
        self.assertNotEqual(after.head, flow.head)
        self.assertIn("Use sqlite", claude.prompts[0])
        self.host.owner_review(self.pr_ref(), OWNER, "approved", flow.head)  # the old head
        self.settle()
        self.assertEqual(self.flow().state, "awaiting-gate:plan")
        self.host.owner_review(self.pr_ref(), OWNER, "approved", after.head)
        self.settle(write("new.txt"), CLEAN)
        self.assertEqual(self.flow().state, "awaiting-gate:merge")

    def test_a_broken_change_loops_three_times_then_blocks_and_pushes_nothing(self):
        self.tracker.owner_add("7")
        broken = [lambda work, n=n: write("broken", f"{n}\n")(work) for n in range(4)]
        claude = self.settle(*broken)
        self.assertEqual(self.flow().state, "blocked")
        self.assertEqual(len(claude.prompts), 4)
        self.assertIsNone(self.host.head_of(self.flow().branch))
        self.assertEqual(self.tracker.state_of("7"), "blocked")
        self.assertIn("gates-failed", self.tracker.comments("7")[-1])

    def test_findings_get_a_fix_and_the_flow_goes_on(self):
        self.tracker.owner_add("7")
        finding = '{"findings": [{"severity": "high", "title": "Bad", "detail": "Fix it"}]}'
        claude = self.settle(write("new.txt"), finding, write("new.txt", "better\n"))
        self.assertEqual(self.flow().state, "awaiting-gate:merge")
        self.assertEqual(len(claude.prompts), 3)
        self.assertIn("Bad", claude.prompts[2])

    def test_a_fix_that_changes_nothing_goes_on_with_a_note(self):
        self.tracker.owner_add("7")
        finding = '{"findings": [{"severity": "medium", "title": "Bad"}]}'
        self.settle(write("new.txt"), finding, lambda work: None)
        self.assertEqual(self.flow().state, "awaiting-gate:merge")
        self.assertTrue(any("changed nothing" in c for c in self.host.comments(self.pr_ref())))

    def test_a_protected_path_blocks_and_the_patch_is_posted_nowhere(self):
        self.tracker.owner_add("7")
        self.settle(write(".github/workflows/x.yml", "name: secret-looking\n"))
        self.assertEqual(self.flow().state, "blocked")
        self.assertFalse(any("name: secret-looking" in c for c in self.tracker.comments("7")))

    def test_a_flow_needs_no_second_label_and_a_finished_issue_is_not_taken_again(self):
        flow = self.quick_to_merge_gate()
        self.host.owner_merge(self.pr_ref())
        self.settle()
        self.assertEqual([f.id for f in self.db.recent()], [flow.id])

    def test_labelling_again_starts_a_new_flow(self):
        self.quick_to_merge_gate()
        self.host.owner_merge(self.pr_ref())
        self.conductor.tick()
        self.tracker.owner_add("7")
        self.conductor.tick()
        self.assertEqual(len(self.db.recent()), 2)

    def test_an_issue_with_two_flow_labels_asks_for_nothing(self):
        self.tracker.owner_add("7", labels=("flow:quick", "flow:standard"))
        self.conductor.tick()
        self.assertEqual(self.db.recent(), [])

    def test_a_repo_without_a_tracker_has_no_flows(self):
        self.settings.repos["example/repo"] = replace(self.repo, tracker=None)
        self.tracker.owner_add("7")
        self.conductor.tick()
        self.assertEqual(self.db.recent(), [])

    def test_the_brief_is_what_the_issue_said_at_claim(self):
        self.assertEqual(brief_of(Task("1", " T ", " B\0 ", "a")), "T\n\nB")


class CrashTests(Rig):
    def test_stopping_after_any_action_repeats_nothing_and_loses_nothing(self):
        """A crash between doing an action and marking it done: for each action of a whole flow in turn."""
        for crash_at in range(0, 14):
            with self.subTest(crash_at=crash_at):
                self.doCleanups()
                self.setUp()
                real, count = self.db.finish, [0]

                def finish(action_id, real=real, count=count, crash_at=crash_at):
                    count[0] += 1
                    if count[0] == crash_at + 1:
                        raise RuntimeError("crash")
                    real(action_id)
                self.db.finish = finish
                self.conductor = self.new_conductor()
                self.tracker.owner_add("7")
                self.settle(write("new.txt"), CLEAN)
                flow = self.flow()
                self.assertEqual(flow.state, "awaiting-gate:merge")
                self.assertEqual(len(self.db.recent()), 1)
                self.assertEqual(len(self.queue.list()), 2)
                self.assertEqual(len(self.host.store.load()), 1)
                comments = [c for c in self.tracker.comments("7") if "example.com/pull" in c]
                self.assertEqual(len(comments), 1)

    def test_a_new_conductor_goes_on_where_the_last_one_stopped(self):
        self.tracker.owner_add("7")
        self.conductor.tick()
        self.run_jobs(FakeClaude(write("new.txt")))
        self.conductor = self.new_conductor()
        self.settle(CLEAN)
        self.assertEqual(self.flow().state, "awaiting-gate:merge")

    def job(self):
        row = self.queue.claim_next(1)
        self.assertIsNotNone(row)
        return row

    def test_a_restart_before_the_push_runs_the_step_again_and_costs_no_attempt(self):
        self.tracker.owner_add("7")
        self.conductor.tick()
        self.job()
        self.queue.recover()
        self.settle(write("new.txt"), CLEAN)
        flow = self.flow()
        self.assertEqual(flow.state, "awaiting-gate:merge")
        self.assertEqual(self.db.runs(flow.id)[0].result, "restarted")

    def test_a_restart_after_the_push_counts_as_done(self):
        self.tracker.owner_add("7")
        self.conductor.tick()
        row = self.job()
        with mock.patch.object(runner, "run_claude", FakeClaude(write("new.txt"))), \
                mock.patch.object(steps, "conclude", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            steps.run_step(row, self.settings, lambda: False, self.queue, remote=self.remote)
        self.queue.recover()
        claude = self.settle(CLEAN)
        flow = self.flow()
        self.assertEqual(flow.state, "awaiting-gate:merge")
        self.assertEqual(len(claude.prompts), 1)  # only the review: the implement step is not repeated
        self.assertEqual(self.host.details(self.pr_ref())["title"], "Anton: Change things")

    def test_a_retry_after_a_failure_waits_for_its_backoff(self):
        self.tracker.owner_add("7")
        self.settle({"is_error": True, "subtype": "error"})
        flow = self.flow()
        self.assertEqual((flow.state, flow.retries, len(self.queue.list())), ("running:implement", 1, 1))
        self.now += F.RETRY_DELAYS[0]
        self.settle(write("new.txt"), CLEAN)
        self.assertEqual(self.flow().state, "awaiting-gate:merge")


class StallTests(Rig):
    def running_job(self):
        self.tracker.owner_add("7")
        self.conductor.tick()
        return self.queue.claim_next(1)

    def test_a_running_job_past_its_budget_is_cancelled_and_the_retry_waits_for_the_end(self):
        job = self.running_job()
        self.now = job.started + STEP_SECONDS + STALL_MARGIN + 1
        self.conductor.tick()
        self.assertTrue(self.queue.is_cancel_requested(job.id))
        self.conductor.tick()
        self.assertEqual(len(self.queue.list()), 1)  # no second attempt while the first one lives
        self.queue.finish(job.id, "cancelled")
        self.conductor.tick()
        self.now += F.RETRY_DELAYS[0]
        self.conductor.tick()
        self.assertEqual(len(self.queue.list()), 2)

    def test_a_queued_job_never_stalls(self):
        self.tracker.owner_add("7")
        self.conductor.tick()
        self.now += 10 * STEP_SECONDS
        self.conductor.tick()
        self.assertFalse(self.flow().stalled)


class EffectTests(Rig):
    def test_a_daily_limit_means_wait_and_costs_no_try(self):
        self.tracker.owner_add("7")
        with mock.patch.object(self.conductor.service, "submit_step", side_effect=LimitError("full")):
            self.conductor.tick()
        self.assertEqual(self.flow().state, "running:implement")
        self.assertEqual(self.queue.list(), [])
        self.now += 1000
        self.conductor.run_outbox()
        self.assertEqual(len(self.queue.list()), 1)

    def test_an_action_that_keeps_failing_blocks_the_flow(self):
        self.tracker.owner_add("7")
        self.conductor.tick()
        self.run_jobs(FakeClaude(write("new.txt")))
        with mock.patch.object(self.host, "open_draft_pr", side_effect=RuntimeError("down")):
            for _ in range(MAX_TRIES + 1):
                self.conductor.tick()
                self.run_jobs(FakeClaude(CLEAN))
                self.now += 10_000
        flow = self.flow()
        self.assertEqual(flow.state, "blocked")
        self.assertIn("open_pr failed", flow.reason)

    def test_retry_opens_the_pull_request_an_action_failed_to_open(self):
        flow = self.standard_blocked_without_pr()
        self.db.add_command(flow.id, "retry")
        self.settle()
        self.assertEqual(self.flow().state, "awaiting-gate:plan")
        self.assertIsNotNone(self.flow().pr)

    def standard_blocked_without_pr(self):
        self.tracker.owner_add("7", labels=("flow:standard",))
        self.conductor.tick()
        self.run_jobs(FakeClaude(write(steps.PLAN_PATH)))
        with mock.patch.object(self.host, "open_draft_pr", side_effect=RuntimeError("down")):
            for _ in range(MAX_TRIES + 1):
                self.conductor.tick()
                self.now += 10_000
        self.assertEqual(self.flow().state, "blocked")
        return self.flow()

    def test_one_broken_flow_does_not_stop_another(self):
        self.tracker.owner_add("7")
        self.tracker.owner_add("8")
        real = self.tracker.get

        def get(key):
            if key == "7":
                raise RuntimeError("down")
            return real(key)
        self.conductor.tick()
        with mock.patch.object(self.tracker, "get", side_effect=get):
            self.settle(ByPrompt(), rounds=6)
        self.assertEqual({f.key: f.state for f in self.db.recent()}["8"], "awaiting-gate:merge")


class CommandTests(Rig):
    def test_retry_runs_a_blocked_step_again(self):
        self.tracker.owner_add("7")
        self.settle(lambda work: None)
        flow = self.flow()
        self.assertEqual(flow.state, "blocked")
        self.db.add_command(flow.id, "retry")
        self.settle(write("new.txt"), CLEAN)
        self.assertEqual(self.flow().state, "awaiting-gate:merge")

    def test_retry_waits_for_a_job_that_still_runs(self):
        self.tracker.owner_add("7")
        self.conductor.tick()
        row = self.queue.claim_next(1)
        flow = self.flow()
        self.db.commit(flow, replace(flow, state="blocked"), [], F.Event("cancel"))
        self.db.add_command(flow.id, "retry")
        self.conductor.run_commands()
        self.assertEqual(self.flow().state, "blocked")
        self.queue.finish(row.id, "cancelled")
        self.conductor.run_commands()
        self.assertEqual(self.flow().state, "running:implement")

    def test_cancel_waits_for_the_job_then_closes_the_pull_request(self):
        flow = self.quick_to_merge_gate()
        self.db.add_command(flow.id, "cancel")
        self.conductor.tick()
        self.assertEqual(self.flow().state, "cancelled")
        self.assertEqual(self.pr().state, "closed")

    def test_cancel_during_a_job(self):
        self.tracker.owner_add("7")
        self.conductor.tick()
        row = self.queue.claim_next(1)
        self.db.add_command(self.flow().id, "cancel")
        self.conductor.tick()
        self.assertEqual(self.flow().state, "cancelling")
        self.assertTrue(self.queue.is_cancel_requested(row.id))
        self.queue.finish(row.id, "cancelled")
        self.conductor.tick()
        self.assertEqual(self.flow().state, "cancelled")

    def test_cancel_during_a_backoff_ends_without_a_job(self):
        self.tracker.owner_add("7")
        self.settle({"is_error": True, "subtype": "error"})
        self.db.add_command(self.flow().id, "cancel")
        self.conductor.tick()
        self.assertEqual(self.flow().state, "cancelling")
        self.now += F.RETRY_DELAYS[0]
        self.conductor.tick()
        self.conductor.tick()
        self.assertEqual(self.flow().state, "cancelled")
        self.assertEqual(len(self.queue.list()), 1)

    def test_a_job_cancelled_from_outside_blocks_and_never_cancels_the_flow(self):
        self.tracker.owner_add("7")
        self.conductor.tick()
        self.queue.cancel(self.queue.list()[0].id)
        self.conductor.tick()
        self.assertEqual(self.flow().state, "blocked")


class EndingTests(Rig):
    def test_a_merge_with_the_issue_closed_by_it_ends_as_done(self):
        self.quick_to_merge_gate()
        self.host.owner_merge(self.pr_ref())
        self.tracker.owner_close("7", "anton[bot]")
        self.conductor.tick()
        self.assertEqual(self.flow().state, "done")

    def test_a_pull_request_closed_unmerged_cancels(self):
        self.quick_to_merge_gate()
        self.host.close(self.pr_ref())
        self.conductor.tick()
        self.assertEqual(self.flow().state, "cancelled")

    def test_an_issue_closed_by_a_stranger_changes_nothing(self):
        self.quick_to_merge_gate()
        self.tracker.owner_close("7", "stranger")
        self.conductor.tick()
        self.assertEqual(self.flow().state, "awaiting-gate:merge")

    def test_an_issue_closed_by_the_owner_cancels(self):
        self.quick_to_merge_gate()
        self.tracker.owner_close("7", OWNER)
        self.conductor.tick()
        self.assertEqual(self.flow().state, "cancelled")

    def test_a_push_at_a_gate_blocks_until_retry_adopts_it(self):
        flow = self.standard_to_plan_gate()
        moved = self.push_to(flow.branch)
        self.conductor.tick()
        self.assertEqual(self.flow().state, "blocked")
        self.db.add_command(flow.id, "retry")
        self.conductor.tick()
        self.assertEqual((self.flow().state, self.flow().head), ("awaiting-gate:plan", moved))

    def push_to(self, branch):
        from test_steps import sh
        other = self.seed.parent / "other"
        sh(["clone", "-q", "-b", branch, f"file://{self.bare}", str(other)])
        (other / "z.txt").write_text("z\n")
        sh(["add", "-A"], other)
        sh(["commit", "-q", "-m", "someone"], other)
        sh(["push", "-q", "origin", branch], other)
        return sh(["rev-parse", "HEAD"], other)


class ConfigTests(unittest.TestCase):
    BASE = {"installation_id": 1, "tracker": "github", "allowed_authors": ["owner"],
            "checks": [{"name": "t", "cmd": "true", "kind": "gate"}]}

    def parse(self, **extra):
        return config.parse_repos({"repos": {"example/repo": {**self.BASE, **extra}}})

    def test_a_flow_repo(self):
        self.assertEqual(self.parse()["example/repo"].tracker, "github")

    def test_no_tracker_no_flows(self):
        self.assertIsNone(config.parse_repos({"repos": {"example/repo": {"installation_id": 1}}})["example/repo"].tracker)

    def test_a_tracker_without_checks_is_refused(self):
        with self.assertRaises(ConfigError):
            self.parse(checks=[])

    def test_a_tracker_without_allowed_authors_is_refused(self):
        with self.assertRaises(ConfigError):
            self.parse(allowed_authors=[])

    def test_an_unknown_tracker_is_refused(self):
        with self.assertRaises(ConfigError):
            self.parse(tracker="jira")


if __name__ == "__main__":
    unittest.main()
