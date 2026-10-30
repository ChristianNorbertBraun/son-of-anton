import itertools
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from anton import flow as F, steps, workflows
from anton.flow import Action, Event, Flow, Policy, classify, close_event, gate_event, step
from anton.flowdb import FlowDb
from anton.ports import Note, PrRef, PrState, Review, Task

OWNER = "owner"
POLICY = Policy.of([OWNER, "helper-bot[bot]", "merlin"], never=["merlin"], bot="anton[bot]")
SHA, OTHER = "a" * 40, "b" * 40
RESULTS = ("ok", "no-change", "gates-failed", "protected", "hard-path", "secret", "unknown-head", "diff-too-large",
           "error", "unreadable", "restarted", "cancelled")


def new(workflow="standard", **kw):
    return replace(Flow("abc", "example/repo", "7", workflow, "Do it", "main", "c" * 40, OWNER), **kw)


def running(stage, workflow="standard", **kw):
    return replace(new(workflow), state=f"running:{stage}", stage=stage, jobs=1, head=SHA, **kw)


def finished(flow, result, **kw):
    return Event("step_finished", job=flow.jobs, result=result, **kw)


def kinds(actions):
    return [a.kind for a in actions]


def submitted(actions):
    return [a.payload["step_id"] for a in actions if a.kind == "submit_step"]


class TableTests(unittest.TestCase):
    def test_every_state_has_a_decision_for_every_event(self):
        self.assertEqual(set(F.TABLE), set(F.STATE_KINDS))
        for state, event in itertools.product(F.STATE_KINDS, F.EVENT_KINDS):
            with self.subTest(state=state, event=event):
                self.assertIn(event, F.TABLE[state])

    def test_terminal_states_have_nowhere_to_go_except_merged_wins(self):
        for event in F.EVENT_KINDS:
            with self.subTest(event=event):
                self.assertEqual(step(replace(new(), state="done"), Event(event), POLICY)[0].state, "done")
                if event != "pr_merged":
                    self.assertEqual(step(replace(new(), state="cancelled"), Event(event), POLICY)[0].state, "cancelled")

    def test_each_runner_outcome_is_known_to_the_table(self):
        self.assertEqual(set(steps.OUTCOMES) - set(RESULTS), set())


class ClassifyTests(unittest.TestCase):
    def test_job_results(self):
        self.assertEqual(classify("step-done", {"outcome": "no-change"}, None), "no-change")
        self.assertEqual(classify("proposed", None, None), "protected")
        self.assertEqual(classify("cancelled", None, None), "cancelled")
        self.assertEqual(classify("failed", {"outcome": "error"}, "boom"), "error")
        self.assertEqual(classify("failed", None, "daemon restarted"), "restarted")

    def test_a_job_without_a_readable_result_is_never_ok(self):
        self.assertEqual(classify("step-done", None, None), "unreadable")
        self.assertEqual(classify("step-done", {}, None), "unreadable")
        self.assertEqual(classify("pr-open", {"outcome": "ok"}, None), "unreadable")


class StartTests(unittest.TestCase):
    def task(self, author=OWNER, actor=OWNER):
        return Task("7", "T", "B", author, ("flow:quick",), actor)

    def test_who_may_start(self):
        self.assertTrue(POLICY.may_start(self.task()))
        self.assertTrue(POLICY.may_start(self.task(author="anton[bot]")))  # issues created through chat
        self.assertFalse(POLICY.may_start(self.task(author="stranger")))
        self.assertFalse(POLICY.may_start(self.task(actor="stranger")))
        self.assertFalse(POLICY.may_start(self.task(actor=None)))
        self.assertFalse(POLICY.may_start(self.task(actor="anton[bot]")))
        self.assertFalse(POLICY.may_start(self.task(actor="helper-bot[bot]")))
        self.assertFalse(POLICY.may_start(self.task(actor="merlin")))

    def test_case_does_not_matter_for_logins(self):
        self.assertTrue(POLICY.trusted("OWNER"))

    def test_start_claims_and_runs_the_first_stage(self):
        for name, first in (("quick", "implement"), ("standard", "plan")):
            flow, actions = step(new(name), Event("start"), POLICY)
            self.assertEqual(flow.state, f"running:{first}")
            self.assertEqual(kinds(actions), ["claim", "report", "submit_step"])
            spec = actions[-1].payload["spec"]
            self.assertEqual((spec["branch"], spec["expected_head"], spec["base_sha"]),
                             ("anton/flow-abc", None, flow.base_sha))
            steps.validate_spec(spec)

    def test_two_labels_ask_for_nothing(self):
        self.assertIsNone(workflows.flow_label(["flow:quick", "flow:standard"]))
        self.assertEqual(workflows.flow_label(["bug", "flow:quick"]).name, "quick")
        self.assertIsNone(workflows.flow_label(["flow:nonsense"]))


class WorkflowPathTests(unittest.TestCase):
    def advance(self, flow, result="ok", **kw):
        return step(flow, finished(flow, result, **kw), POLICY)

    def test_quick_from_start_to_merge_gate(self):
        flow, _ = step(new("quick"), Event("start"), POLICY)
        flow, actions = self.advance(flow, head=SHA, title="T")
        self.assertEqual((flow.state, kinds(actions)[0], submitted(actions)), ("running:review", "open_pr", ["review"]))
        self.assertEqual(actions[0].payload["title"], "T")
        self.assertEqual(flow.head, SHA)
        flow, actions = self.advance(flow, head=SHA)
        self.assertEqual((flow.state, kinds(actions)), ("awaiting-gate:merge", ["mark_ready", "report"]))
        flow, actions = step(flow, Event("pr_merged"), POLICY)
        self.assertEqual((flow.state, kinds(actions)), ("done", ["report"]))

    def test_standard_from_start_to_merge_gate(self):
        flow, _ = step(new("standard"), Event("start"), POLICY)
        flow, actions = self.advance(flow, head=SHA, title="Plan")
        self.assertEqual((flow.state, kinds(actions)), ("awaiting-gate:plan", ["open_pr", "report"]))
        flow, _ = step(flow, Event("gate_passed", actor=OWNER, commit=SHA), POLICY)
        self.assertEqual(flow.state, "running:implement")
        flow, _ = self.advance(flow, head=OTHER)
        self.assertEqual(flow.state, "running:review")
        flow, _ = self.advance(flow)
        self.assertEqual(flow.state, "running:cleanup")
        flow, actions = self.advance(flow, head=SHA)
        self.assertEqual((flow.state, kinds(actions)), ("awaiting-gate:merge", ["mark_ready", "report"]))

    def test_each_step_expects_the_head_the_last_one_left(self):
        flow, actions = self.advance(running("implement"), head=OTHER)
        self.assertEqual(flow.head, OTHER)
        self.assertEqual(actions[-1].payload["spec"]["expected_head"], OTHER)

    def test_findings_of_medium_or_higher_get_one_fix(self):
        for severity, fixes in (("high", True), ("medium", True), ("low", False)):
            with self.subTest(severity=severity):
                flow, actions = self.advance(running("review", "quick"), findings=({"severity": severity},))
                self.assertEqual(submitted(actions) == ["fix"], fixes)

    def test_the_fix_gets_the_findings_and_the_review_is_not_repeated(self):
        flow, actions = self.advance(running("review", "quick"), findings=({"severity": "high", "title": "Bad"},))
        self.assertEqual(actions[0].payload["spec"]["findings"][0]["title"], "Bad")
        flow, actions = self.advance(flow, head=OTHER)
        self.assertEqual(flow.state, "awaiting-gate:merge")

    def test_after_a_fix_standard_goes_on_with_cleanup(self):
        flow, _ = self.advance(running("review"), findings=({"severity": "high"},))
        flow, actions = self.advance(flow, head=OTHER)
        self.assertEqual((flow.state, flow.head), ("running:cleanup", OTHER))


class OutcomeTableTests(unittest.TestCase):
    """Every cell of the table in section 2 of the plan."""
    EXPECTED = {
        "ok": {"plan": "awaiting-gate:plan", "revise": "awaiting-gate:plan", "implement": "running:review",
               "fix": "running:cleanup", "review": "running:cleanup", "cleanup": "awaiting-gate:merge"},
        "no-change": {"plan": "blocked", "revise": "blocked", "implement": "blocked", "fix": "running:cleanup",
                      "review": "blocked", "cleanup": "awaiting-gate:merge"},
    }
    BLOCKED = ("gates-failed", "protected", "hard-path", "secret", "unknown-head", "diff-too-large", "cancelled")
    RETRIED = ("error", "unreadable")

    def test_ok_and_no_change(self):
        for result, cells in self.EXPECTED.items():
            for stage, state in cells.items():
                with self.subTest(result=result, stage=stage):
                    self.assertEqual(step(running(stage), finished(running(stage), result), POLICY)[0].state, state)

    def test_results_that_block(self):
        for result, stage in itertools.product(self.BLOCKED, F.JOB_STAGES):
            with self.subTest(result=result, stage=stage):
                flow, actions = step(running(stage), finished(running(stage), result, text="why"), POLICY)
                self.assertEqual(flow.state, "blocked")
                self.assertEqual(flow.stage, stage)
                self.assertEqual(kinds(actions), ["report"])
                self.assertIn(result if result != "cancelled" else "cancelled", flow.reason)

    def test_failures_retry_once_with_backoff_then_block(self):
        for result, stage in itertools.product(self.RETRIED, F.JOB_STAGES):
            with self.subTest(result=result, stage=stage):
                first = running(stage)
                flow, actions = step(first, finished(first, result), POLICY)
                self.assertEqual((flow.state, flow.retries, flow.jobs), (f"running:{stage}", 1, 2))
                self.assertGreater(actions[0].delay, 0)
                flow, _ = step(flow, finished(flow, result), POLICY)
                self.assertEqual(flow.state, "blocked")

    def test_a_restart_costs_no_attempt(self):
        for stage in F.JOB_STAGES:
            with self.subTest(stage=stage):
                flow = running(stage)
                for _ in range(5):
                    flow, actions = step(flow, finished(flow, "restarted"), POLICY)
                    self.assertEqual((flow.state, flow.retries), (f"running:{stage}", 0))
                    self.assertEqual(kinds(actions), ["submit_step"])

    def test_a_retry_expects_the_same_head_and_brings_the_same_input(self):
        flow = running("fix", findings=({"severity": "high"},))
        flow, actions = step(flow, finished(flow, "error"), POLICY)
        spec = actions[0].payload["spec"]
        self.assertEqual((spec["expected_head"], spec["findings"]), (SHA, [{"severity": "high"}]))

    def test_success_clears_the_retries(self):
        flow = running("implement", "quick", retries=1)
        self.assertEqual(step(flow, finished(flow, "ok"), POLICY)[0].retries, 0)

    def test_a_late_result_of_an_older_attempt_changes_nothing(self):
        flow = replace(running("implement"), jobs=3)
        self.assertEqual(step(flow, Event("step_finished", job=2, result="ok"), POLICY), (flow, []))

    def test_every_result_of_every_stage_has_an_answer(self):
        for result, stage in itertools.product(RESULTS, F.JOB_STAGES):
            with self.subTest(result=result, stage=stage):
                flow, _ = step(running(stage), finished(running(stage), result), POLICY)
                self.assertNotEqual((flow.state, flow.jobs, flow.head), (running(stage).state, 1, SHA))


class GateTests(unittest.TestCase):
    def gated(self, **kw):
        return replace(new(), state="awaiting-gate:plan", stage="gate:plan", head=SHA, pr=3, **kw)

    def pr(self, *reviews, notes=()):
        return PrState(PrRef(3), SHA, True, "open", False, tuple(reviews), tuple(notes))

    def test_an_approval_passes(self):
        event = gate_event(self.gated(), self.pr(Review(OWNER, "approved", SHA)), POLICY)
        self.assertEqual(event.kind, "gate_passed")
        self.assertEqual(step(self.gated(), event, POLICY)[0].state, "running:implement")

    def test_no_review_no_event(self):
        self.assertIsNone(gate_event(self.gated(), self.pr(), POLICY))

    def test_later_review_of_the_same_actor_wins(self):
        event = gate_event(self.gated(), self.pr(Review(OWNER, "changes_requested", SHA),
                                                 Review(OWNER, "approved", SHA)), POLICY)
        self.assertEqual(event.kind, "gate_passed")

    def test_a_comment_review_does_not_undo_an_approval(self):
        event = gate_event(self.gated(), self.pr(Review(OWNER, "approved", SHA), Review(OWNER, "commented", SHA)), POLICY)
        self.assertEqual(event.kind, "gate_passed")

    def test_changes_requested_by_anyone_trusted_beats_an_approval(self):
        policy = Policy.of([OWNER, "second"])
        event = gate_event(self.gated(), self.pr(Review(OWNER, "approved", SHA),
                                                 Review("second", "changes_requested", SHA, "No")), policy)
        self.assertEqual(event.kind, "gate_rejected")

    def test_rejection_revises_with_verified_text_for_the_current_head(self):
        pr = self.pr(Review(OWNER, "changes_requested", SHA, "Use sqlite"), Review("stranger", "commented", SHA, "EVIL1"),
                     Review(OWNER, "commented", OTHER, "old"),
                     notes=(Note(OWNER, "On this line", SHA), Note("stranger", "EVIL2", SHA), Note(OWNER, "stale", OTHER),
                            Note(OWNER, "conversation", None)))
        event = gate_event(self.gated(), pr, POLICY)
        flow, actions = step(self.gated(), event, POLICY)
        self.assertEqual(flow.state, "running:revise")
        feedback = actions[0].payload["spec"]["feedback"]
        self.assertIn("Use sqlite", feedback)
        self.assertIn("On this line", feedback)
        for text in ("EVIL", "old", "stale", "conversation"):
            self.assertNotIn(text, feedback)

    def test_a_rejection_without_text_still_says_something(self):
        event = gate_event(self.gated(), self.pr(Review(OWNER, "changes_requested", SHA)), POLICY)
        self.assertTrue(event.text)

    def test_revise_returns_to_the_gate_on_the_new_head(self):
        flow = step(self.gated(), Event("gate_rejected", actor=OWNER, commit=SHA, text="x"), POLICY)[0]
        flow, _ = step(flow, finished(flow, "ok", head=OTHER), POLICY)
        self.assertEqual((flow.state, flow.head), ("awaiting-gate:plan", OTHER))
        self.assertEqual(step(flow, Event("gate_passed", actor=OWNER, commit=SHA), POLICY)[0], flow)  # the old head

    def test_three_revisions_then_blocked(self):
        flow = self.gated()
        for _ in range(3):
            flow = step(flow, Event("gate_rejected", actor=OWNER, commit=SHA, text="x"), POLICY)[0]
            flow = step(flow, finished(flow, "ok", head=SHA), POLICY)[0]
        self.assertEqual(flow.state, "awaiting-gate:plan")
        flow = step(flow, Event("gate_rejected", actor=OWNER, commit=SHA, text="x"), POLICY)[0]
        self.assertEqual(flow.state, "blocked")

    def test_a_review_does_nothing_at_the_merge_gate(self):
        flow = replace(self.gated(), state="awaiting-gate:merge", stage="gate:merge")
        for kind in ("gate_passed", "gate_rejected"):
            self.assertEqual(step(flow, Event(kind, actor=OWNER, commit=SHA), POLICY)[0], flow)


class EndingTests(unittest.TestCase):
    TASK = Task("7", "T", "B", OWNER)
    PR = PrState(PrRef(3), SHA, False, "open", False)

    def test_merged_wins_over_a_closed_issue(self):
        closed = replace(self.TASK, state="closed", closed_by="anton[bot]")
        merged = replace(self.PR, state="closed", merged=True)
        self.assertEqual(close_event(new(), merged, closed).kind, "pr_merged")

    def test_merge_and_close_in_both_orders_end_as_done(self):
        flow = replace(new(), state="awaiting-gate:merge", stage="gate:merge", pr=3)
        first = step(flow, Event("issue_closed", actor=OWNER), POLICY)[0]
        self.assertEqual(step(first, Event("pr_merged"), POLICY)[0].state, "done")
        first = step(flow, Event("pr_merged"), POLICY)[0]
        self.assertEqual(step(first, Event("issue_closed", actor=OWNER), POLICY)[0].state, "done")

    def test_a_closed_pr_cancels(self):
        self.assertEqual(close_event(new(), replace(self.PR, state="closed"), self.TASK).kind, "pr_closed")
        flow = replace(new(), state="awaiting-gate:merge", stage="gate:merge", pr=3)
        self.assertEqual(step(flow, Event("pr_closed"), POLICY)[0].state, "cancelled")

    def test_a_closed_issue_ends_a_flow_only_for_a_verified_actor(self):
        flow = replace(new(), state="awaiting-gate:plan", stage="gate:plan")
        for actor in ("stranger", "anton[bot]", "merlin", None):
            with self.subTest(actor=actor):
                self.assertEqual(step(flow, Event("issue_closed", actor=actor), POLICY)[0], flow)
        self.assertEqual(step(flow, Event("issue_closed", actor=OWNER), POLICY)[0].state, "cancelled")

    def test_cancelling_a_running_flow_waits_for_the_job(self):
        flow = running("implement", pr=3)
        flow, actions = step(flow, Event("cancel"), POLICY)
        self.assertEqual((flow.state, kinds(actions)), ("cancelling", ["cancel_job"]))
        flow, actions = step(flow, finished(flow, "cancelled"), POLICY)
        self.assertEqual((flow.state, kinds(actions)), ("cancelled", ["report", "close_pr"]))

    def test_cancel_at_a_gate_closes_the_pull_request(self):
        flow = replace(new(), state="awaiting-gate:plan", stage="gate:plan", pr=3)
        self.assertEqual(kinds(step(flow, Event("cancel"), POLICY)[1]), ["report", "close_pr"])

    def test_cancel_before_a_pull_request_has_none_to_close(self):
        flow = replace(new(), state="blocked", stage="implement")
        self.assertEqual(kinds(step(flow, Event("cancel"), POLICY)[1]), ["report"])

    def test_a_merge_during_a_step_ends_the_flow_and_its_job(self):
        flow, actions = step(running("review"), Event("pr_merged"), POLICY)
        self.assertEqual((flow.state, kinds(actions)), ("done", ["cancel_job", "report"]))


class BlockedTests(unittest.TestCase):
    def test_retry_runs_the_failed_step_again_with_its_input(self):
        flow = replace(running("revise", feedback="Use sqlite"), state="blocked", reason="x", retries=1, revisions=3)
        flow, actions = step(flow, Event("retry"), POLICY)
        self.assertEqual((flow.state, flow.retries, flow.revisions, flow.reason), ("running:revise", 0, 0, ""))
        self.assertEqual(actions[0].payload["spec"]["feedback"], "Use sqlite")

    def test_retry_adopts_a_head_it_is_given(self):
        flow = replace(running("review"), state="blocked")
        flow, actions = step(flow, Event("retry", head=OTHER), POLICY)
        self.assertEqual(actions[0].payload["spec"]["expected_head"], OTHER)

    def test_retry_of_a_flow_that_never_pushed_starts_over(self):
        flow = replace(new("quick"), state="blocked", stage="implement", jobs=1)
        flow, actions = step(flow, Event("retry"), POLICY)
        self.assertIsNone(actions[0].payload["spec"]["expected_head"])

    def test_an_unknown_head_blocks_a_flow_at_a_gate_and_retry_adopts_it(self):
        flow = replace(new(), state="awaiting-gate:plan", stage="gate:plan", head=SHA, pr=3)
        flow, _ = step(flow, Event("head_moved", commit=OTHER), POLICY)
        self.assertEqual(flow.state, "blocked")
        flow, _ = step(flow, Event("retry", head=OTHER), POLICY)
        self.assertEqual((flow.state, flow.head), ("awaiting-gate:plan", OTHER))

    def test_an_approval_does_not_unblock(self):
        flow = replace(new(), state="blocked", stage="gate:plan", head=SHA)
        self.assertEqual(step(flow, Event("gate_passed", actor=OWNER, commit=SHA), POLICY)[0], flow)


class StallTests(unittest.TestCase):
    def test_a_stall_cancels_the_job_and_only_then_retries(self):
        flow, actions = step(running("implement"), Event("stall_detected"), POLICY)
        self.assertEqual(kinds(actions), ["cancel_job"])
        self.assertEqual(flow.jobs, 1)  # two attempts never run at once
        flow, actions = step(flow, finished(flow, "cancelled"), POLICY)
        self.assertEqual((flow.state, flow.jobs, submitted(actions)), ("running:implement", 2, ["implement"]))

    def test_a_stall_is_reported_once(self):
        flow, _ = step(running("implement"), Event("stall_detected"), POLICY)
        self.assertEqual(step(flow, Event("stall_detected"), POLICY)[1], [])

    def test_the_second_stall_blocks(self):
        flow, _ = step(running("implement", retries=1), Event("stall_detected"), POLICY)
        flow, _ = step(flow, finished(flow, "cancelled"), POLICY)
        self.assertEqual(flow.state, "blocked")

    def test_a_job_that_ends_after_all_counts(self):
        flow, _ = step(running("implement", "quick"), Event("stall_detected"), POLICY)
        flow, _ = step(flow, finished(flow, "ok", head=OTHER), POLICY)
        self.assertEqual((flow.state, flow.stalled), ("running:review", False))


class FlowDbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = 1000
        self.db = FlowDb(Path(self.tmp.name) / "flows.db", clock=lambda: self.now)

    def test_a_flow_survives_the_round_trip(self):
        flow = replace(new(), findings=({"severity": "high"},), gates=("- a: passed",), head=SHA)
        self.assertTrue(self.db.create(flow))
        self.assertEqual(self.db.get(flow.id), flow)

    def test_one_active_flow_per_task(self):
        self.assertTrue(self.db.create(new()))
        self.assertFalse(self.db.create(replace(new(), id="def")))

    def test_a_task_can_flow_again_after_the_last_one_ended(self):
        self.db.create(new())
        self.db.commit(new(), replace(new(), state="done"), [], Event("pr_merged"))
        self.assertTrue(self.db.create(replace(new(), id="def")))

    def test_transition_and_actions_commit_together(self):
        self.db.create(new())
        with self.assertRaises(TypeError):  # the second action cannot be stored
            self.db.commit(new(), replace(new(), state="running:plan"),
                           [Action("claim"), Action("report", {"bad": object()})], Event("start"))
        self.assertEqual(self.db.get("abc").state, "queued")
        self.assertEqual(self.db.due(), [])
        self.assertEqual(self.db.history("abc"), [])

    def test_due_gives_one_action_per_flow_in_order(self):
        self.db.create(new())
        self.db.commit(new(), new(), [Action("claim"), Action("report", {"state": "running"})], Event("start"))
        due = self.db.due()
        self.assertEqual([a.kind for a in due], ["claim"])
        self.db.finish(due[0].id)
        self.assertEqual([a.kind for a in self.db.due()], ["report"])

    def test_a_delayed_action_waits_and_holds_back_what_follows(self):
        self.db.create(new())
        self.db.commit(new(), new(), [Action("submit_step", {}, delay=60), Action("report")], Event("start"))
        self.assertEqual(self.db.due(), [])
        self.now += 60
        self.assertEqual([a.kind for a in self.db.due()], ["submit_step"])

    def test_postpone_counts_tries_unless_told_otherwise(self):
        self.db.create(new())
        self.db.commit(new(), new(), [Action("claim")], Event("start"))
        action = self.db.due()[0]
        self.db.postpone(action.id, 30, "boom")
        self.assertEqual(self.db.due(), [])
        self.now += 30
        self.assertEqual(self.db.due()[0].tries, 1)
        self.db.postpone(action.id, 30, "limit", charge=False)
        self.now += 30
        self.assertEqual(self.db.due()[0].tries, 1)

    def test_abandon_closes_what_is_left(self):
        self.db.create(new())
        self.db.commit(new(), new(), [Action("claim"), Action("report")], Event("start"))
        self.db.abandon("abc", "stopped")
        self.assertEqual(self.db.due(), [])
        self.assertFalse(self.db.has_open("abc"))

    def test_marker_is_stable_for_a_replay(self):
        self.db.create(new())
        self.db.commit(new(), new(), [Action("comment", {"text": "x"})], Event("start"))
        self.assertEqual(self.db.due()[0].marker, self.db.due()[0].marker)

    def test_commands_are_taken_once(self):
        self.db.add_command("abc", "retry")
        self.assertEqual(self.db.take_commands(), [("abc", "retry")])
        self.assertEqual(self.db.take_commands(), [])

    def test_runs_and_history_are_kept(self):
        self.db.create(new())
        self.db.commit(new(), replace(new(), state="running:plan"), [], Event("start", actor=OWNER))
        self.db.record_run("abc", "plan", 1, "job1", "ok", {"cost_usd": 0.5}, 10, 20)
        self.assertEqual(self.db.runs("abc")[0].usage, {"cost_usd": 0.5})
        self.assertEqual(self.db.history("abc")[0]["actor"], OWNER)

    def test_the_file_is_private(self):
        self.assertEqual(oct(self.db.path.stat().st_mode & 0o777), "0o600")


if __name__ == "__main__":
    unittest.main()
