import tempfile
import unittest
from pathlib import Path

from anton import gitops, sandbox
from anton.sandbox import SandboxError


class WrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.work = self.root / "job/repo"
        (self.work / ".git").mkdir(parents=True)
        self.conf = self.root / "conf"
        self.conf.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def wrap(self, **kw):
        return sandbox.wrap(["echo", "hi"], self.work, etc_dir=self.root / "etc", protected=(self.conf,), **kw)

    def test_command_comes_last_after_separator(self):
        cmd = self.wrap()
        self.assertEqual(cmd[0], "bwrap")
        self.assertEqual(cmd[-3:], ["--", "echo", "hi"])

    def test_isolation_flags(self):
        cmd = self.wrap()
        for flag in ("--die-with-parent", "--new-session", "--unshare-pid", "--unshare-ipc"):
            self.assertIn(flag, cmd)
        self.assertNotIn("--share-net", cmd)
        self.assertNotIn("--unshare-net", cmd)  # network stays open on purpose (Claude API, npm)

    def test_system_is_read_only_and_home_is_tmpfs(self):
        cmd = self.wrap()
        i = cmd.index("--ro-bind")
        self.assertEqual(cmd[i:i + 3], ["--ro-bind", "/usr", "/usr"])
        j = cmd.index("--tmpfs", cmd.index(sandbox.SANDBOX_HOME) - 1)
        self.assertEqual(cmd[j + 1], sandbox.SANDBOX_HOME)

    def test_git_dir_is_bound_read_only_after_the_work_dir(self):
        cmd = self.wrap()
        work_i = cmd.index("--bind")
        self.assertEqual(cmd[work_i:work_i + 3], ["--bind", str(self.work), sandbox.WORK])
        ro = [i for i, a in enumerate(cmd) if a == "--ro-bind" and cmd[i + 1] == str(self.work / ".git")]
        self.assertEqual(len(ro), 1)
        self.assertGreater(ro[0], work_i)  # later mount wins, so .git is read-only

    def test_no_secret_locations_are_ever_mounted(self):
        joined = " ".join(self.wrap())
        self.assertNotIn(str(self.conf), joined)
        self.assertNotIn("app-key", joined)
        self.assertNotIn("claude-token", joined)

    def test_only_listed_etc_files_are_bound(self):
        etc = self.root / "etc"
        etc.mkdir()
        (etc / "hosts").write_text("x")
        (etc / "shadow").write_text("secret")
        cmd = sandbox.wrap(["true"], self.work, etc_dir=etc)
        self.assertIn("/etc/hosts", cmd)
        self.assertNotIn("/etc/shadow", cmd)

    def test_claude_binary_is_only_mounted_when_asked(self):
        binary = self.root / "claude-bin"
        binary.write_text("#!/bin/sh")
        self.assertNotIn(sandbox.CLAUDE_IN_SANDBOX, self.wrap())
        with_claude = self.wrap(claude_bin=binary)
        i = with_claude.index(sandbox.CLAUDE_IN_SANDBOX)
        self.assertEqual(with_claude[i - 2], "--ro-bind")

    def test_checkout_inside_protected_dir_is_refused(self):
        inside = self.conf / "repo"
        inside.mkdir()
        with self.assertRaises(SandboxError):
            sandbox.wrap(["true"], inside, protected=(self.conf,))
        with self.assertRaises(SandboxError):  # checkout that contains the protected dir
            sandbox.wrap(["true"], self.root, protected=(self.conf,))


class GitHardeningTests(unittest.TestCase):
    def test_git_never_runs_hooks_or_fsmonitor(self):
        cmd = gitops.git_cmd(["status"])
        self.assertEqual(cmd[0], "git")
        self.assertIn("core.hooksPath=/dev/null", cmd)
        self.assertIn("core.fsmonitor=false", cmd)
        self.assertEqual(cmd[-1], "status")


if __name__ == "__main__":
    unittest.main()
