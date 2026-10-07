import io
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from anton import config, pool, updater
from anton.config import UpdateConfig
from anton.updater import Deps, Layout, UpdateRefused

CFG = UpdateConfig("owner/son-of-anton", "Owner")


def api(tag="v0.2.0", login="owner", kind="User", **kw):
    return {"tag_name": tag, "draft": False, "prerelease": False, "body": "Fixes things",
            "author": {"login": login, "type": kind}, **kw}


def archive(version="0.2.0", extra=(), files=None):
    """A GitHub-style source archive: one top directory."""
    top = f"son-of-anton-{version}"
    files = files if files is not None else {
        "anton/__init__.py": "", "anton/version.py": f'__version__ = "{version}"\n', "tests/test_x.py": "x = 1\n"}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, text in files.items():
            info = tarfile.TarInfo(f"{top}/{name}")
            data = text.encode()
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        for info in extra:
            tf.addfile(info)
    return buf.getvalue()


def link(name, target, hard=False):
    info = tarfile.TarInfo(name)
    info.type = tarfile.LNKTYPE if hard else tarfile.SYMTYPE
    info.linkname = target
    return info


class ReleaseTests(unittest.TestCase):
    def test_a_release_by_the_owner_is_accepted_case_insensitively(self):
        r = updater.release_from_api(api(), CFG)
        self.assertEqual((r.tag, r.version, r.name), ("v0.2.0", (0, 2, 0), "0.2.0"))
        self.assertEqual(r.url, "https://github.com/owner/son-of-anton/archive/refs/tags/v0.2.0.tar.gz")

    def test_everything_else_is_refused(self):
        bad = {"another publisher": api(login="mallory"), "the app bot": api(login="owner", kind="Bot"),
               "a draft": {**api(), "draft": True}, "a pre-release": {**api(), "prerelease": True},
               "a odd tag": api(tag="v1.2"), "a tag with a path": api(tag="v1.0.0/../x"), "no tag": {"author": {}}}
        for name, data in bad.items():
            with self.subTest(name), self.assertRaises(UpdateRefused):
                updater.release_from_api(data, CFG)

    def test_only_newer_versions_count(self):
        r = updater.release_from_api(api("v0.2.0"), CFG)
        self.assertTrue(updater.is_newer(r, "0.1.9"))
        self.assertFalse(updater.is_newer(r, "0.2.0"))
        self.assertFalse(updater.is_newer(r, "0.10.0") if False else updater.is_newer(r, "1.0.0"))

    def test_find_release_reports_network_problems_as_refusals(self):
        with self.assertRaises(UpdateRefused):
            updater.find_release(CFG, fetch=mock.Mock(side_effect=OSError("down")))
        with self.assertRaises(UpdateRefused):
            updater.find_release(CFG, fetch=lambda url: api(), to="latest; rm -rf")


class UnpackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)

    def test_a_normal_archive_is_unpacked_and_verified(self):
        top = updater.unpack(archive(), self.tmp / "in")
        release = updater.release_from_api(api(), CFG)
        updater.verify_tree(top, release)
        self.assertEqual((top / "anton/version.py").read_text(), '__version__ = "0.2.0"\n')

    def test_links_absolute_paths_and_dotdot_refuse_the_whole_archive(self):
        for name, extra in {"symlink": [link("son-of-anton-0.2.0/l", "/etc/passwd")],
                            "hardlink": [link("son-of-anton-0.2.0/h", "anton/version.py", hard=True)]}.items():
            with self.subTest(name), self.assertRaises(UpdateRefused):
                updater.unpack(archive(extra=extra), self.tmp / f"in-{name}")
        with self.assertRaises(UpdateRefused):
            updater.unpack(archive(files={"../evil": "x"}), self.tmp / "in-dotdot")
        with self.assertRaises(UpdateRefused):
            updater.unpack(archive(extra=[tarfile.TarInfo("/etc/evil")]), self.tmp / "in-absolute")
        self.assertFalse((self.tmp / "evil").exists())

    def test_two_top_directories_garbage_and_wrong_version_are_refused(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for name in ("a/x", "b/y"):
                info = tarfile.TarInfo(name)
                tf.addfile(info, io.BytesIO(b""))
        with self.assertRaises(UpdateRefused):
            updater.unpack(buf.getvalue(), self.tmp / "two")
        with self.assertRaises(UpdateRefused):
            updater.unpack(b"not a tarball", self.tmp / "junk")
        top = updater.unpack(archive("0.9.9"), self.tmp / "ver")  # says 0.9.9, the tag says 0.2.0
        with self.assertRaises(UpdateRefused):
            updater.verify_tree(top, updater.release_from_api(api(), CFG))
        top = updater.unpack(archive(files={"README.md": "x"}), self.tmp / "other")
        with self.assertRaises(UpdateRefused):
            updater.verify_tree(top, updater.release_from_api(api(), CFG))


class UpdateFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.layout = Layout(self.tmp)
        old = self.layout.releases / "0.1.0"
        (old / "anton").mkdir(parents=True)
        os.symlink(old, self.layout.current)
        self.restarts, self.notes, self.logs, self.waited = [], [], [], []
        self.paused_during_restart = []
        self.release_json, self.tests_ok, self.config_ok, self.health, self.jobs = api(), True, True, True, 0
        self.now = 0.0

    def deps(self, **kw):
        def restart():
            self.paused_during_restart.append(self.layout.pause.exists())
            self.restarts.append(updater.current_name(self.layout))

        def sleep(s):
            self.now += s
            self.waited.append(s)

        base = dict(
            get_json=lambda url: self.release_json, get_bytes=lambda url: archive(),
            run_tests=lambda top: (self.tests_ok, "1 failed"), config_check=lambda top: (self.config_ok, "bad toml"),
            restart=restart, healthy=lambda version, layout: self.health if updater.current_name(layout) == version
            else True, running_jobs=lambda: self.jobs, notify=self.notes.append, sleep=sleep,
            clock=lambda: self.now, log=self.logs.append)
        return Deps(**{**base, **kw})

    def run_update(self, **kw):
        return updater.update(CFG, self.layout, self.deps(), installed="0.1.0", **kw)

    def test_a_good_release_is_installed_and_the_old_one_stays_for_a_rollback(self):
        res = self.run_update()
        self.assertEqual(res.status, "updated")
        self.assertEqual(updater.current_name(self.layout), "0.2.0")
        self.assertTrue((self.layout.releases / "0.1.0").is_dir())
        self.assertEqual(self.restarts, ["0.2.0"])
        self.assertEqual(self.paused_during_restart, [True])  # no new job starts while the service restarts
        self.assertFalse(self.layout.pause.exists())
        self.assertFalse(list(self.layout.releases.glob(".incoming*")))
        self.assertIn("updated 0.1.0 -> 0.2.0", self.notes[-1])

    def test_failing_tests_or_an_unreadable_config_change_nothing(self):
        for attr in ("tests_ok", "config_ok"):
            with self.subTest(attr):
                setattr(self, attr, False)
                res = self.run_update()
                setattr(self, attr, True)
                self.assertEqual(res.status, "failed")
                self.assertEqual(updater.current_name(self.layout), "0.1.0")
                self.assertEqual(self.restarts, [])
                self.assertFalse((self.layout.releases / "0.2.0").exists())
                self.assertFalse(self.layout.pause.exists())

    def test_a_failed_health_check_goes_back_to_the_old_version(self):
        self.health = False
        res = self.run_update()
        self.assertEqual(res.status, "rolled-back")
        self.assertEqual(updater.current_name(self.layout), "0.1.0")
        self.assertEqual(self.restarts, ["0.2.0", "0.1.0"])
        self.assertFalse((self.layout.releases / "0.2.0").exists())  # the failed release is removed again
        self.assertIn("went back to 0.1.0", self.notes[-1])
        self.assertFalse(self.layout.pause.exists())

    def test_a_service_that_will_not_restart_is_rolled_back_too(self):
        calls = []

        def restart():
            calls.append(updater.current_name(self.layout))
            if len(calls) == 1:
                raise UpdateRefused("no systemd")

        res = updater.update(CFG, self.layout, self.deps(restart=restart), installed="0.1.0")
        self.assertEqual((res.status, updater.current_name(self.layout), calls), ("rolled-back", "0.1.0", ["0.2.0", "0.1.0"]))

    def test_it_waits_for_running_jobs_and_gives_up_without_changing_anything(self):
        self.jobs = 1
        res = self.run_update(wait_seconds=60)
        self.assertEqual(res.status, "failed")
        self.assertIn("jobs are still running", res.message)
        self.assertEqual((updater.current_name(self.layout), self.restarts), ("0.1.0", []))
        self.assertFalse(self.layout.pause.exists())
        self.assertGreaterEqual(sum(self.waited), 60)

    def test_it_proceeds_as_soon_as_the_jobs_are_done(self):
        left = iter([2, 1, 0])
        res = updater.update(CFG, self.layout, self.deps(running_jobs=lambda: next(left)), installed="0.1.0")
        self.assertEqual(res.status, "updated")

    def test_nothing_is_downloaded_when_there_is_no_newer_release(self):
        get_bytes = mock.Mock()
        res = updater.update(CFG, self.layout, self.deps(get_bytes=get_bytes), installed="0.2.0")
        self.assertEqual(res.status, "current")
        get_bytes.assert_not_called()
        self.assertEqual(self.notes, [])  # "nothing new" is not worth a message

    def test_a_release_from_the_wrong_publisher_is_refused(self):
        self.release_json = api(login="mallory")
        res = self.run_update()
        self.assertEqual(res.status, "failed")
        self.assertIn("not published by Owner", res.message)
        self.assertEqual(updater.current_name(self.layout), "0.1.0")

    def test_force_reinstalls_but_never_the_running_version(self):
        self.release_json = api("v0.1.0")
        self.assertEqual(updater.update(CFG, self.layout, self.deps(), installed="0.1.0", force=True).status, "failed")
        self.assertEqual(updater.current_name(self.layout), "0.1.0")

    def test_old_versions_are_pruned(self):
        for v in ("0.0.1", "0.0.2", "0.0.3"):
            (self.layout.releases / v).mkdir()
        self.run_update()
        left = sorted(p.name for p in self.layout.releases.iterdir())
        self.assertEqual(left, ["0.0.3", "0.1.0", "0.2.0"])  # the newest three


class SmallPartsTests(unittest.TestCase):
    def test_pause_file_counts_only_while_fresh(self):
        with tempfile.TemporaryDirectory() as d:
            layout = Layout(Path(d))
            self.assertFalse(updater.paused(layout))
            layout.state.mkdir(parents=True)
            layout.pause.touch()
            self.assertTrue(updater.paused(layout))
            self.assertFalse(updater.paused(layout, now=lambda: layout.pause.stat().st_mtime + updater.PAUSE_MAX_AGE + 1))

    def test_a_paused_pool_starts_nothing(self):
        q = mock.Mock()
        p = pool.Pool(q, lambda row, sc: None, 2, paused=lambda: True)
        self.assertEqual(p.tick(), 0)
        q.claim_next.assert_not_called()

    def test_healthy_needs_an_active_service_that_reports_the_new_version_for_a_while(self):
        with tempfile.TemporaryDirectory() as d:
            layout = Layout(Path(d))
            layout.state.mkdir(parents=True)
            clock = {"t": 0.0}
            sleep = lambda s: clock.__setitem__("t", clock["t"] + s)  # noqa: E731
            active = mock.Mock(return_value=mock.Mock(stdout="active\n"))
            with mock.patch.object(updater, "systemctl", active):
                layout.running.write_text("0.1.0")  # still the old daemon
                self.assertFalse(updater.healthy("0.2.0", layout, timeout=30, sleep=sleep, clock=lambda: clock["t"]))
                layout.running.write_text("0.2.0")
                clock["t"] = 0.0
                self.assertTrue(updater.healthy("0.2.0", layout, timeout=60, settle=15, sleep=sleep, clock=lambda: clock["t"]))
            with mock.patch.object(updater, "systemctl", mock.Mock(return_value=mock.Mock(stdout="activating\n"))):
                clock["t"] = 0.0
                self.assertFalse(updater.healthy("0.2.0", layout, timeout=30, sleep=sleep, clock=lambda: clock["t"]))

    def test_downloads_retry_dropped_connections_but_not_http_errors(self):
        import urllib.error
        ok = mock.MagicMock()
        ok.__enter__.return_value.read.return_value = b"data"
        with mock.patch("urllib.request.urlopen", side_effect=[OSError("dns"), OSError("dns"), ok]) as op:
            self.assertEqual(updater._get("https://x", 100, sleep=lambda s: None), b"data")
            self.assertEqual(op.call_count, 3)
        err = urllib.error.HTTPError("https://x", 404, "nf", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err) as op, self.assertRaises(urllib.error.HTTPError):
            updater._get("https://x", 100, sleep=lambda s: None)
        self.assertEqual(op.call_count, 1)

    def test_restart_clears_a_start_limit_failure_before_restarting(self):
        calls = []
        ok = mock.Mock(returncode=0, stderr="")
        with mock.patch.object(updater, "systemctl", side_effect=lambda *a: calls.append(a) or ok):
            updater.restart()
        self.assertEqual(calls, [("reset-failed", "anton.service"), ("restart", "anton.service")])
        with mock.patch.object(updater, "systemctl", return_value=mock.Mock(returncode=1, stderr="Start request repeated too quickly")):
            with self.assertRaises(UpdateRefused):
                updater.restart()

    def test_update_section_of_the_config(self):
        self.assertIsNone(config.parse_update({}))
        self.assertEqual(config.parse_update({"update": {"repo": "o/r", "publisher": "me"}}), UpdateConfig("o/r", "me"))
        for bad in ({"repo": "nope", "publisher": "me"}, {"repo": "o/r", "publisher": "a b"}, {"repo": "o/r"}):
            with self.subTest(bad=bad), self.assertRaises(config.ConfigError):
                config.parse_update({"update": bad})
