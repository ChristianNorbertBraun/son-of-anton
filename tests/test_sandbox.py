import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
        self.assertEqual(cmd[-3:], ["_", "echo", "hi"])  # "_" is $0 of the limit wrapper
        i = cmd.index("--")
        self.assertEqual(cmd[i + 1:i + 3], ["bash", "-c"])
        self.assertIn("ulimit -f", cmd[i + 3])

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


class MaskTests(WrapTests):
    def test_normal_jobs_have_no_masks(self):
        cmd = self.wrap()
        self.assertNotIn("/work/.claude", cmd)
        self.assertNotIn("/work/.mcp.json", cmd)

    def test_masks_come_after_the_checkout_mount(self):
        cmd = self.wrap(masks=True)
        i = cmd.index("/work/.claude")
        self.assertEqual(cmd[i - 1], "--tmpfs")
        self.assertEqual(cmd[i - 3:i - 1], ["--size", str(sandbox.MASK_BYTES)])
        self.assertLess(cmd.index("/work"), i)
        for name in ("/work/.mcp.json", "/work/CLAUDE.local.md"):
            j = cmd.index(name)
            self.assertEqual(cmd[j - 2:j], ["--ro-bind", str(self.work.parent / "mask-empty")])
        self.assertEqual((self.work.parent / "mask-empty").stat().st_size, 0)
        self.assertLess(max(cmd.index("/work/.mcp.json"), i), cmd.index("--"))

    def test_a_symlink_at_a_mask_is_refused(self):
        (self.work / ".claude").symlink_to("/etc")
        with self.assertRaises(SandboxError):
            self.wrap(masks=True)

    def test_a_mask_of_the_wrong_type_is_refused(self):
        (self.work / ".mcp.json").mkdir()
        with self.assertRaises(SandboxError):
            self.wrap(masks=True)

    def test_existing_plain_paths_are_fine(self):
        (self.work / ".claude").mkdir()
        (self.work / ".mcp.json").write_text("{}")
        self.assertIn("/work/.claude", self.wrap(masks=True))


class GitHardeningTests(unittest.TestCase):
    def test_a_hung_git_ends_with_an_error(self):
        with mock.patch("anton.gitops.subprocess.run", side_effect=subprocess.TimeoutExpired("git", 1)):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                gitops.git(["push", "origin"])

    def test_git_output_that_is_no_utf8_does_not_crash(self):
        with tempfile.TemporaryDirectory() as d:
            work = Path(d)
            gitops.git(["init", "-q", "."], work)
            (work / "latin1.txt").write_bytes("caf\xe9\n".encode("latin-1"))
            gitops.git(["add", "-A"], work)
            self.assertIn("latin1.txt", gitops.staged_text(work))

    def test_the_users_ignore_file_cannot_hide_a_change(self):
        with tempfile.TemporaryDirectory() as d:
            home, work = Path(d) / "home", Path(d) / "work"
            (home / ".config/git").mkdir(parents=True)
            (home / ".config/git/ignore").write_text("secret.txt\n")
            work.mkdir()
            with mock.patch.dict(os.environ, {"HOME": str(home)}):
                os.environ.pop("XDG_CONFIG_HOME", None)
                gitops.git(["init", "-q", "."], work)
                (work / "secret.txt").write_text("x\n")
                self.assertEqual(gitops.changed_files(work), ["secret.txt"])

    def test_git_never_runs_hooks_or_fsmonitor(self):
        cmd = gitops.git_cmd(["status"])
        self.assertEqual(cmd[0], "git")
        self.assertIn("core.hooksPath=/dev/null", cmd)
        self.assertIn("core.fsmonitor=false", cmd)
        self.assertEqual(cmd[-1], "status")


if __name__ == "__main__":
    unittest.main()
