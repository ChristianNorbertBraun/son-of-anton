"""Regression tests for the findings of the security and architecture review."""
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from anton import config, gitops, runner
from anton.checks import CheckResult
from anton.config import DENY_FLOOR, ConfigError
from anton.models import Cancelled, Outcome
from anton.pool import Pool
from anton.proc import run_capped
from anton.queue import InputError, LimitError, Queue
from anton.service import Service

BASE = '[github]\napp_id=1\nbot_name="b[bot]"\nbot_id=2\n'


def repos(extra: str = "", defaults: str = ""):
    return config.parse_repos(tomllib.loads(f'{defaults}\n[repos."o/r"]\ninstallation_id=1\n{extra}'))["o/r"]


class ConfigHardeningTests(unittest.TestCase):
    def test_repo_cannot_remove_the_deny_floor(self):
        r = repos('deny_tools=["Read(x)"]', '[defaults]\ndeny_tools=["Bash(rm:*)"]')
        for denied in DENY_FLOOR:
            self.assertIn(denied, r.deny_tools)
        self.assertIn("Read(x)", r.deny_tools)
        self.assertIn("Bash(rm:*)", r.deny_tools)
        self.assertIn("Bash(git:*)", repos().deny_tools)  # even with no deny list at all

    def test_web_tools_are_denied(self):
        self.assertTrue({"WebFetch", "WebSearch"} <= set(repos().deny_tools))

    def test_lists_must_be_lists_of_strings(self):
        for bad in ('protected_paths="deploy/*"', 'deny_tools="Bash(git:*)"', "allow_tools=[1]", 'deny_tools=[""]'):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                repos(bad)

    def test_protected_paths_merge_defaults(self):
        r = repos('protected_paths=["b"]', '[defaults]\nprotected_paths=["a"]')
        self.assertEqual(r.protected_paths, ("a", "b"))

    def test_bounds_and_missing_keys_raise_config_error_not_tracebacks(self):
        for bad in ("max_turns=0", "max_turns=1000", "timeout_minutes=0", "timeout_minutes=9999",
                    'base="--force"', 'install=""'):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                repos(bad)
        with self.assertRaises(ConfigError):
            config.parse_repos(tomllib.loads('[repos."o/r"]\nmax_turns=3\n'))  # no installation_id
        with self.assertRaises(ConfigError):
            repos('[[repos."o/r".checks]]\ncmd="x"\nkind="gate"\n')  # check without name
        with self.assertRaises(ConfigError):
            repos('[[repos."o/r".checks]]\nname="a"\ncmd="x"\nkind="gate"\nmetric="("\n')  # bad regex

    def test_slug_with_trailing_newline_is_rejected(self):
        self.assertIsNone(config.SLUG_RE.fullmatch("o/r\n"))
        with self.assertRaises(ConfigError):
            config.parse_repos({"repos": {"o/r\n": {"installation_id": 1}}})

    def test_invalid_toml_is_a_config_error(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.toml"
            p.write_text("[broken")
            with self.assertRaises(ConfigError):
                config.load_settings(p)

    def test_daemon_limits(self):
        d = config.parse_daemon({"daemon": {"max_queued": 5}})
        self.assertEqual(d.max_queued, 5)
        with self.assertRaises(ConfigError):
            config.parse_daemon({"daemon": {"max_queued": 0}})

    def test_settings_load_once(self):
        s = config.parse_settings(tomllib.loads(BASE + '[repos."o/r"]\ninstallation_id=3\n'))
        self.assertEqual((s.github.app_id, s.repos["o/r"].installation_id), (1, 3))


class GitCheckTests(unittest.TestCase):
    def test_protected_paths_match_at_any_depth_and_case(self):
        bad = gitops.violations([
            "apps/web/.env", "apps/web/.env.production", "pkg/node_modules/x/index.js", "a/build/out.js",
            ".github/actions/deploy/action.yml", ".GitHub/Workflows/ci.yml", ".github/CODEOWNERS",
            "sub/.npmrc", ".gitmodules", ".claude/settings.json", "x/.mcp.json", "keys/id.PEM"], ())
        self.assertEqual(len(bad), 12)

    def test_odd_path_shapes_are_violations(self):
        for odd in ("dir/", "../escape", "a/../b", "/abs", "a//b"):
            with self.subTest(path=odd):
                self.assertEqual(gitops.violations([odd], ()), [odd])

    def test_normal_files_pass_and_repo_patterns_apply(self):
        self.assertEqual(gitops.violations(["src/a.svelte", "README.md"], ()), [])
        self.assertEqual(gitops.violations(["deploy/x.sh"], ("deploy/*",)), ["deploy/x.sh"])

    def test_parse_raw(self):
        out = (":100644 100644 aaa bbb M\0src/a.js\0:000000 100644 000 ccc A\0new file.txt\0"
               ":100644 000000 ddd 000 D\0gone.js\0")
        self.assertEqual(gitops.parse_raw(out), [("100644", "M", "src/a.js"), ("100644", "A", "new file.txt"),
                                                  ("000000", "D", "gone.js")])

    def test_staged_symlinks_and_nested_repos_are_caught(self):
        env = gitops.identity_env("t[bot]", 1)
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "r"
            gitops.git(["init", "-q", str(repo)])
            (repo / "ok.txt").write_text("fine")
            os.symlink("/etc/passwd", repo / "link")
            inner = repo / "inner"
            inner.mkdir()
            gitops.git(["init", "-q", str(inner)])
            (inner / "f").write_text("x")
            gitops.git(["add", "f"], inner)
            gitops.git(["commit", "-q", "-m", "x"], inner, env)
            (repo / ".github/actions").mkdir(parents=True)
            (repo / ".github/actions/a.yml").write_text("x")
            gitops.git(["add", "-A"], repo)
            problems = gitops.staged_problems(repo, ())
        joined = " | ".join(problems)
        self.assertIn("link (symlink)", joined)
        self.assertIn("inner (nested repository)", joined)
        self.assertIn(".github/actions/a.yml (protected)", joined)
        self.assertNotIn("ok.txt", joined)

    def test_git_ignores_system_and_global_config(self):
        with mock.patch("anton.gitops.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            gitops.git(["status"])
        env = run.call_args.kwargs["env"]
        self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], "/dev/null")

    def test_branch_must_be_an_anton_branch(self):
        for bad in ("main", "feature/x", "anton/", "anton/UPPER", "anton/a b", "anton/../x"):
            with self.subTest(branch=bad), self.assertRaises(RuntimeError):
                gitops.assert_pushable(bad, "main")


class QueueRuleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.q = Queue(Path(self.tmp.name) / "state" / "q.db", daily_limit=2, max_queued=3)

    def tearDown(self):
        self.tmp.cleanup()

    def test_cancelling_a_started_job_does_not_refund_the_quota(self):
        for i in range(2):
            job, _ = self.q.enqueue("o/r", f"t{i}")
            self.q.claim_next(4)
            self.q.cancel(job.id)
        self.assertEqual(self.q.used_today(), 2)
        with self.assertRaises(LimitError):
            self.q.enqueue("o/r", "more")

    def test_late_result_never_overwrites_a_recovered_job(self):
        job, _ = self.q.enqueue("o/r", "t")
        self.q.claim_next(2)
        self.assertEqual(self.q.recover(), 1)
        self.assertFalse(self.q.finish(job.id, "pr-open", pr="x"))
        self.assertEqual(self.q.get(job.id).status, "failed")

    def test_cancelled_while_queued_cannot_be_finished(self):
        job, _ = self.q.enqueue("o/r", "t")
        self.q.cancel(job.id)
        self.assertFalse(self.q.finish(job.id, "pr-open"))
        self.assertEqual(self.q.get(job.id).status, "cancelled")

    def test_input_bounds(self):
        bad_inputs = [("o/r", "", None, "cli"), ("o/r", "x" * 20_001, None, "cli"), ("o/r", "a\0b", None, "cli"),
                      ("o/r", "t", 0, "cli"), ("o/r", "t", -5, "cli"), ("o/r", "t", 2**40, "cli"),
                      ("o/r", "t", True, "cli"), ("o/r", "t", None, "bad name!"), ("o/r", "t", None, "x" * 65),
                      ("../etc", "t", None, "cli"), ("o/r\n", "t", None, "cli")]
        for repo, task, issue, who in bad_inputs:
            with self.subTest(repo=repo, task=task[:8], issue=issue, who=who[:8]), self.assertRaises(InputError):
                self.q.enqueue(repo, task, issue, who)
        self.assertEqual(self.q.used_today(), 0)  # rejected input costs nothing
        self.q.enqueue("o/r", "x" * 20_000, 2**31 - 1, "bot:telegram-123")

    def test_queue_cannot_grow_without_bound(self):
        daily = Queue(Path(self.tmp.name) / "big.db", daily_limit=100, max_queued=3)
        for i in range(3):
            daily.enqueue("o/r", f"t{i}")
        with self.assertRaises(LimitError):
            daily.enqueue("o/r", "one more")

    def test_files_are_private(self):
        self.assertEqual(stat.S_IMODE(os.stat(self.q.path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.q.path.parent).st_mode), 0o700)

    def test_service_enforces_the_allowlist_before_the_queue(self):
        svc = Service(self.q, {"o/r": repos()})
        with self.assertRaises(ConfigError):
            svc.submit("evil/repo", "t")
        self.assertEqual(self.q.used_today(), 0)
        job, created = svc.submit("o/r", "t", requested_by="cli")
        self.assertTrue(created)
        self.assertEqual(svc.cancel(job.id), "cancelled")


class PoolShutdownTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.q = Queue(Path(self.tmp.name) / "q.db", daily_limit=50)

    def tearDown(self):
        self.tmp.cleanup()

    def test_drain_does_not_start_new_jobs(self):
        started, release = [], threading.Event()

        def run(row, sc):
            started.append(row.task)
            release.wait(5)
            return Outcome("pr-open", pr="x")

        for i in range(3):
            self.q.enqueue("o/r", f"t{i}")
        pool = Pool(self.q, run, max_parallel=1)
        pool.tick()
        threading.Timer(0.3, release.set).start()
        self.assertEqual(pool.drain(timeout=5), 0)
        self.assertEqual(pool.tick(), 0)  # still refuses after drain
        self.assertEqual(started, ["t0"])
        self.assertEqual(len(self.q.list(active_only=True)), 2)  # the other two stay queued

    def test_run_forever_survives_a_database_error(self):
        pool = Pool(self.q, lambda r, c: Outcome("pr-open"), max_parallel=1)
        calls = {"n": 0}
        real = pool.tick

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("database is locked")
            return real()

        pool.tick = flaky
        stop = threading.Event()
        threading.Timer(0.5, stop.set).start()
        pool.run_forever(stop, interval=0.05)
        self.assertGreater(calls["n"], 1)

    def test_finish_is_retried_when_the_database_is_busy(self):
        job, _ = self.q.enqueue("o/r", "t")
        real, fails = self.q.finish, {"n": 0}

        def flaky(*a, **k):
            fails["n"] += 1
            if fails["n"] < 3:
                raise RuntimeError("locked")
            return real(*a, **k)

        self.q.finish = flaky
        with mock.patch("anton.pool.time.sleep"):
            self.assertTrue(Pool(self.q, lambda r, c: Outcome("pr-open", pr="x"), 1).wait_idle(5))
        self.assertEqual(self.q.get(job.id).status, "pr-open")


class ProcTests(unittest.TestCase):
    ENV = dict(os.environ)

    def test_output_is_capped_and_tail_is_kept(self):
        code, out, truncated = run_capped([sys.executable, "-c", "print('a'*1000); print('END')"],
                                          cwd=None, env=self.ENV, timeout=20, limit=100)
        self.assertEqual(code, 0)
        self.assertTrue(truncated)
        self.assertTrue(out.endswith("END\n"))
        self.assertLessEqual(len(out), 100)

    def test_head_mode_keeps_the_start(self):
        _, out, truncated = run_capped([sys.executable, "-c", "print('START'+'x'*1000)"], cwd=None,
                                       env=self.ENV, timeout=20, limit=50, tail=False)
        self.assertTrue(truncated)
        self.assertTrue(out.startswith("START"))

    def test_timeout_returns_124_and_kills(self):
        t0 = time.monotonic()
        code, _, _ = run_capped([sys.executable, "-c", "import time; time.sleep(60)"], cwd=None,
                                env=self.ENV, timeout=0.5)
        self.assertEqual(code, 124)
        self.assertLess(time.monotonic() - t0, 15)

    def test_cancel_raises_and_kills(self):
        with self.assertRaises(Cancelled):
            run_capped([sys.executable, "-c", "import time; time.sleep(60)"], cwd=None, env=self.ENV,
                       timeout=60, should_cancel=lambda: True)

    def test_stdin_text_is_delivered_without_touching_argv(self):
        code, out, _ = run_capped([sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"],
                                  cwd=None, env=self.ENV, timeout=20, stdin_text="--dangerously-skip-permissions")
        self.assertEqual(out.strip(), "--DANGEROUSLY-SKIP-PERMISSIONS")


class RunnerPieceTests(unittest.TestCase):
    def test_prompt_is_never_in_argv(self):
        argv = runner.claude_argv(repos())
        self.assertNotIn("--dangerously-skip-permissions", argv)
        for flag in ("--setting-sources", "--strict-mcp-config", "--disable-slash-commands"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "user")
        deny_start = argv.index("--disallowedTools")
        for denied in DENY_FLOOR:
            self.assertIn(denied, argv[deny_start:])

    def test_task_goes_to_stdin_not_argv(self):
        job = mock.Mock(task="--allowedTools=Bash", issue=None, repo=repos(), dir=Path(tempfile.mkdtemp()))
        job.work = job.dir / "repo"
        with mock.patch.object(runner, "run_capped", return_value=(0, '{"result":"ok"}', False)) as rc, \
                mock.patch.object(runner, "sb_cmd", side_effect=lambda w, cmd, with_claude=False: cmd), \
                mock.patch.object(runner, "clean_env", return_value={}):
            runner.run_claude(job, lambda: False)
        self.assertEqual(rc.call_args.kwargs["stdin_text"], "--allowedTools=Bash")
        self.assertNotIn("--allowedTools=Bash", rc.call_args.args[0])

    def test_claude_settings_deny_the_floor_and_secret_dirs(self):
        deny = json.loads(runner.claude_settings(repos()))["permissions"]["deny"]
        self.assertIn("Bash(curl:*)", deny)
        self.assertTrue(any(str(runner.CONF_DIR) in d for d in deny))

    def test_absolute_deny_rules_use_the_double_slash_form(self):
        # `Read(/etc/**)` is project-relative and blocks nothing; verified against the real CLI
        deny = json.loads(runner.claude_settings(repos()))["permissions"]["deny"]
        for tool, path in (("Read", "/proc"), ("Read", "/sys"), ("Read", "/etc"), ("Read", str(runner.CONF_DIR))):
            self.assertIn(f"{tool}(/{path}/**)", deny)
        self.assertEqual(runner.abs_rule("Read", "/proc"), "Read(//proc/**)")
        self.assertFalse(any(d.startswith("Read(/") and not d.startswith("Read(//") for d in deny))

    def test_scrub_removes_the_token_everywhere(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(runner, "CONF_DIR", Path(d)):
            (Path(d) / "claude-token").write_text("tok-abcdefghijklmnop\n")
            out = runner.scrub("leak: tok-abcdefghijklmnop and sk-ant-oat01-ABCDEFGH_xyz here")
            self.assertNotIn("tok-abcdefghijklmnop", out)
            self.assertNotIn("sk-ant-", out)
            self.assertEqual(out.count("[redacted]"), 2)
            self.assertNotIn("tok-abcdefghijklmnop", runner.safe("x tok-abcdefghijklmnop y"))
            self.assertNotIn("tok-abcdefghijklmnop", runner.neutralize("summary tok-abcdefghijklmnop"))

    def test_scrub_works_without_a_token_file(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(runner, "CONF_DIR", Path(d)):
            self.assertEqual(runner.scrub("plain text"), "plain text")

    def test_example_installs_without_lifecycle_scripts(self):
        r = config.load_repos(Path(__file__).parent.parent / "examples/repos.toml")["your-name/your-site"]
        self.assertIn("--ignore-scripts", r.install)

    def test_neutralize_blocks_mentions_links_and_closing_keywords(self):
        out = runner.neutralize("Fixes #12 cc @octocat and @org/team\nsecond line")
        self.assertNotIn("@octocat", out)
        self.assertNotIn("#12", out)
        self.assertNotRegex(out, r"#\d")
        self.assertFalse(any(ln.startswith(">") for ln in out.splitlines()))  # plain text, no quote block
        self.assertLessEqual(len(runner.neutralize("x" * 100_000)), runner.SUMMARY_LIMIT + 5)
        self.assertEqual(runner.neutralize(""), "(no summary)")

    def test_titles_are_cut_at_word_boundaries(self):
        task = "Rename the settings screen title to something shorter and clearer for new users"
        title = runner.shorten(task, 40)
        self.assertLessEqual(len(title), 40)
        self.assertTrue(title.endswith("\u2026"))
        self.assertTrue(task.startswith(title[:-1].rstrip()))
        self.assertEqual(runner.shorten("short title", 70), "short title")
        self.assertEqual(runner.shorten("a   b\n c", 70), "a b c")  # one line
        self.assertEqual(runner.shorten("x" * 200, 20), "x" * 19 + "\u2026")  # one long word: hard cut

    def test_everything_claude_writes_is_english(self):
        rules = runner.SYSTEM_RULES
        self.assertIn("in English", rules)
        self.assertIn("even if the task is in another language", rules)
        self.assertIn("TITLE:", rules)

    def test_split_title(self):
        job = mock.Mock(issue=None)
        self.assertEqual(runner.split_title("TITLE: Show imprint link\n\nMoved the link.", job),
                         ("Show imprint link", "Moved the link."))
        self.assertEqual(runner.split_title("  title:   Fix @bob typo  \nBody", job)[0], "Fix bob typo")
        self.assertEqual(runner.split_title("TITLE: " + "word " * 40, job)[0][-1], "\u2026")
        # no TITLE line: a neutral English fallback, never the (possibly foreign-language) task
        self.assertEqual(runner.split_title("Just a summary", job), ("Apply requested change", "Just a summary"))
        self.assertEqual(runner.split_title(None, mock.Mock(issue=12)), ("Implement issue #12", ""))

    def test_pr_body_layout(self):
        body = runner.pr_body(mock.Mock(issue=None), "Changed line 12.", ["- build: passed"], 4)
        self.assertTrue(body.startswith("## Summary\n\nChanged line 12."))
        self.assertIn("## Checks (before -> after)\n- build: passed", body)
        self.assertNotIn("\n>", body)

    def test_safe_strips_control_characters(self):
        self.assertEqual(runner.safe("a\nb\x1b[31mc\x00"), "a?b?[31mc?")
        self.assertEqual(len(runner.safe("x" * 1000, 50)), 50)

    def test_clean_env_only_carries_the_token_when_asked(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(runner, "CONF_DIR", Path(d)):
            (Path(d) / "claude-token").write_text("secret-token\n")
            plain, claude = runner.clean_env(), runner.clean_env(with_claude_token=True)
        self.assertFalse(any("TOKEN" in k or "KEY" in k for k in plain))
        self.assertEqual(claude["CLAUDE_CODE_OAUTH_TOKEN"], "secret-token")
        self.assertEqual([k for k in claude if "TOKEN" in k], ["CLAUDE_CODE_OAUTH_TOKEN"])

    def test_rmtree_force_removes_hostile_permissions(self):
        with tempfile.TemporaryDirectory() as d:
            tree = Path(d) / "repo"
            (tree / "a/b").mkdir(parents=True)
            (tree / "a/b/f").write_text("x")
            os.chmod(tree / "a/b", 0o000)
            os.chmod(tree / "a", 0o500)
            runner.rmtree_force(tree)
            self.assertFalse(tree.exists())

    def test_gc_removes_orphans_and_old_jobs_but_not_running_ones(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(runner, "JOBS_DIR", Path(d)):
            for name in ("orphan", "running", "old"):
                (Path(d) / name / "repo").mkdir(parents=True)
                (Path(d) / name / "job.json").write_text("{}")
            old = time.time() - 40 * 86400
            os.utime(Path(d) / "old", (old, old))
            runner.gc_jobs(lambda job_id: job_id == "running", keep_days=30)
            self.assertFalse((Path(d) / "orphan/repo").exists())
            self.assertTrue((Path(d) / "orphan/job.json").exists())  # recent log is kept
            self.assertTrue((Path(d) / "running/repo").exists())
            self.assertFalse((Path(d) / "old").exists())


def result(name, kind, code, metric):
    return CheckResult(name, kind, code, metric, "")


class LifecycleTests(unittest.TestCase):
    """The whole job with fakes for git, GitHub, Claude and the sandbox."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.settings = config.parse_settings(tomllib.loads(BASE + '[repos."o/r"]\ninstallation_id=9\n'))
        self.calls, self.api_calls, self.tokens = [], [], []
        self.checks = [{"build": result("build", "gate", 0, 0), "check": result("check", "regression", 1, 6)},
                       {"build": result("build", "gate", 0, 0), "check": result("check", "regression", 1, 5)}]
        self.claude = {"is_error": False, "subtype": "success", "num_turns": 4, "result": "Fixed it."}
        self.changed, self.problems, self.api_fail = ["src/a.js"], [], None
        self.cancel_after = None
        self.claude_called = False
        self.install_error = None
        P = mock.patch
        self.patches = [
            P.object(runner, "JOBS_DIR", self.tmp / "jobs"), P.object(runner, "CONF_DIR", self.tmp / "conf"),
            P.object(runner.sandbox, "available", return_value=True),
            P.object(runner.ghapp, "installation_token", side_effect=self.fake_token),
            P.object(runner.ghapp, "api", side_effect=self.fake_api),
            P.object(runner.gitops, "git", side_effect=self.fake_git),
            P.object(runner.gitops, "changed_files", side_effect=lambda cwd: list(self.changed)),
            P.object(runner.gitops, "staged_problems", side_effect=lambda cwd, prot: list(self.problems)),
            P.object(runner, "install_deps", side_effect=self.fake_install),
            P.object(runner, "run_checks", side_effect=lambda job, label, sc: self.checks.pop(0)),
            P.object(runner, "run_claude", side_effect=self.fake_claude),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        mock.patch.stopall()

    def fake_token(self, app_id, key, inst, repo, permissions):
        self.tokens.append(permissions)
        return "tok"

    def fake_api(self, method, path, token, body=None):
        self.api_calls.append((method, path, body))
        if self.api_fail:
            raise RuntimeError(self.api_fail)
        return {"html_url": "https://github.com/o/r/pull/1"}

    def fake_git(self, args, cwd=None, env=None):
        self.calls.append(args)
        if args[0] == "clone":
            Path(args[-1]).mkdir(parents=True)
        return ""

    def fake_install(self, job, should_cancel):
        if self.install_error:
            raise RuntimeError(self.install_error)

    def fake_claude(self, job, should_cancel):
        self.claude_called = True
        return self.claude

    def go(self, task="do it", issue=None, dry=False, cancel=lambda: False):
        job = runner.new_job(self.settings.repos["o/r"], task, issue)
        out = runner.run_job(job, self.settings, dry_run=dry, should_cancel=cancel)
        return job, out

    def pushes(self):
        return [c for c in self.calls if c[0] == "push"]

    def test_success_pushes_one_anton_branch_and_opens_a_draft_pr(self):
        job, out = self.go(issue=7)
        self.assertEqual((out.status, out.pr), ("pr-open", "https://github.com/o/r/pull/1"))
        self.assertEqual(len(self.pushes()), 1)
        refspec = self.pushes()[0][-1]
        self.assertRegex(refspec, r"^HEAD:refs/heads/anton/issue-7-[a-z0-9]+$")
        self.assertNotIn("+", refspec)
        method, path, body = self.api_calls[0]
        self.assertEqual((method, path, body["draft"], body["base"]), ("POST", "/repos/o/r/pulls", True, "main"))
        self.assertIn("5", body["body"])  # check table 6 -> 5
        self.assertFalse(job.work.exists())  # cleaned up
        self.assertEqual(self.tokens, [{"contents": "read"}, {"contents": "write", "pull_requests": "write"}])

    def test_a_german_task_still_produces_english_title_commit_and_branch(self):
        self.claude = {**self.claude, "result": "TITLE: Raise the experience from 7 to 9 years\n\nChanged the heading."}
        job, out = self.go(task="Jahreszahl der Erfahrung von 'sieben' auf 'neun' anpassen")
        self.assertEqual(out.status, "pr-open")
        _, _, body = self.api_calls[0]
        self.assertEqual(body["title"], "Anton: Raise the experience from 7 to 9 years")
        self.assertRegex(body["head"], r"^anton/raise-the-experience-from-7-to?-?[a-z0-9-]*-[a-z0-9]{6}$")
        self.assertTrue(body["body"].startswith("## Summary\n\nChanged the heading."))
        commit = next(c for c in self.calls if c[0] == "commit")
        self.assertEqual(commit[-1], "Anton: Raise the experience from 7 to 9 years")
        for artifact in (body["title"], body["head"], commit[-1], body["body"]):
            self.assertNotIn("sieben", artifact)
            self.assertNotIn("Jahreszahl", artifact)
        self.assertIn(["branch", "-m", body["head"]], self.calls)  # renamed before the push
        self.assertEqual(self.pushes()[0][-1], f"HEAD:refs/heads/{body['head']}")

    def test_without_a_title_line_the_names_are_neutral_english(self):
        self.go(task="Ändere die Fußzeile")
        _, _, body = self.api_calls[0]
        self.assertEqual(body["title"], "Anton: Apply requested change")
        self.assertRegex(body["head"], r"^anton/apply-requested-change-[a-z0-9]{6}$")

    def test_no_changes_means_no_push(self):
        self.changed = []
        _, out = self.go()
        self.assertEqual(out.status, "no-changes")
        self.assertEqual(self.pushes(), [])
        self.assertEqual(self.api_calls, [])

    def test_blocked_paths_fail_the_job_and_nothing_leaves_the_machine(self):
        self.problems = [".github/actions/a.yml (protected)"]
        _, out = self.go()
        self.assertEqual(out.status, "failed")
        self.assertIn("blocked changes", out.reason)
        self.assertEqual(self.pushes(), [])
        self.assertNotIn(["commit"], [c[:1] for c in self.calls if c[0] == "commit"])

    def test_worse_checks_fail_the_job(self):
        self.checks[1]["check"] = result("check", "regression", 1, 9)
        _, out = self.go()
        self.assertEqual(out.status, "failed")
        self.assertIn("WORSE", out.reason)
        self.assertEqual(self.pushes(), [])

    def test_broken_build_fails_the_gate(self):
        self.checks[1]["build"] = result("build", "gate", 1, None)
        _, out = self.go()
        self.assertEqual(out.status, "failed")
        self.assertEqual(self.pushes(), [])

    def test_claude_error_fails_before_anything_is_staged(self):
        self.claude = {"is_error": True, "subtype": "error_max_turns"}
        _, out = self.go()
        self.assertEqual(out.status, "failed")
        self.assertIn("error_max_turns", out.reason)
        self.assertNotIn("add", [c[0] for c in self.calls])

    def test_cancel_right_before_the_push_stops_the_job(self):
        _, out = self.go(cancel=lambda: self.claude_called)  # user cancels while Claude was running
        self.assertEqual(out.status, "cancelled")
        self.assertEqual(self.pushes(), [])
        self.assertEqual(self.api_calls, [])

    def test_cancel_during_install_never_reaches_claude(self):
        self.install_error = None
        _, out = self.go(cancel=lambda: True)
        self.assertEqual(out.status, "cancelled")
        self.assertFalse(self.claude_called)

    def test_failed_pr_creation_reports_the_orphaned_branch(self):
        self.api_fail = "422 Validation Failed"
        job, out = self.go(issue=3)
        self.assertEqual(out.status, "failed")
        self.assertRegex(out.reason, r"branch anton/issue-3-[a-z0-9]+ was pushed but the PR failed")
        self.assertEqual(len(self.pushes()), 1)

    def test_dry_run_never_calls_claude_or_pushes(self):
        _, out = self.go(dry=True)
        self.assertEqual(out.status, "no-changes")
        self.assertFalse(self.claude_called)
        self.assertEqual(self.pushes(), [])

    def test_install_failure_is_a_failed_job_without_claude(self):
        self.install_error = "install failed (exit 1)"
        _, out = self.go()
        self.assertEqual(out.status, "failed")
        self.assertFalse(self.claude_called)

    def test_unexpected_errors_never_escape_and_still_clean_up(self):
        def boom(args, cwd=None, env=None):
            if args[0] == "clone":
                Path(args[-1]).mkdir(parents=True)
                raise RuntimeError("git clone failed: network down")
            return ""

        runner.gitops.git.side_effect = boom
        job, out = self.go()
        self.assertEqual(out.status, "failed")
        self.assertIn("network down", out.reason)
        self.assertFalse(job.work.exists())

    def test_missing_sandbox_fails_closed(self):
        runner.sandbox.available.return_value = False
        job, out = self.go()
        self.assertEqual(out.status, "failed")
        self.assertIn("bwrap", out.reason)
        self.assertEqual(self.calls, [])  # not even a clone

    def test_task_text_cannot_inject_log_lines_or_commit_message_lines(self):
        job, out = self.go(task="innocent\nFORGED: status=ok\x1b[31m")
        text = (job.dir / "log.txt").read_text()
        self.assertNotIn("\x1b", text)
        commit = next(c for c in self.calls if c[0] == "commit")
        self.assertNotIn("\n", commit[-1])


if __name__ == "__main__":
    unittest.main()
