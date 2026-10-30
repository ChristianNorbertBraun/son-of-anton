import tempfile
import unittest
from pathlib import Path
from unittest import mock

from anton import gitops, runner, selftest


class MaskSelftestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name) / "work"
        self.work.mkdir()
        gitops.git(["init", "-q", str(self.work)])

    def run_masks(self, output):
        with mock.patch.object(selftest, "run_capped", return_value=(1, output, False)), \
                mock.patch.object(runner, "sb_cmd", return_value=["true"]), \
                mock.patch.object(runner, "clean_env", return_value={}):
            return selftest.check_masks(self.work)

    def test_a_sandbox_that_never_started_is_a_failure(self):
        lines = self.run_masks("bwrap: boom\n")
        self.assertTrue(any(ln.startswith("FAIL mask-mcp-is-empty") for ln in lines))
        self.assertTrue(any(ln.startswith("FAIL repo-instructions-visible") for ln in lines))

    def test_all_checks_reported_is_no_failure(self):
        names = selftest.re.findall(r"^[ty] (\S+)", selftest.MASK_SCRIPT, selftest.re.M)
        self.assertGreater(len(names), 4)
        lines = self.run_masks("".join(f"PASS {n}\n" for n in names))
        self.assertEqual([ln for ln in lines if ln.startswith("FAIL")], [])


if __name__ == "__main__":
    unittest.main()
