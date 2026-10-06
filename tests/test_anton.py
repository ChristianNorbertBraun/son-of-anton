import tomllib
import unittest
from pathlib import Path

from anton import checks as chk
from anton import config, gitops
from anton.config import Check, ConfigError

EXAMPLE = Path(__file__).parent.parent / "examples/repos.toml"


def repos_from(text: str):
    return config.parse_repos(tomllib.loads(text))


class AllowlistTests(unittest.TestCase):
    def test_example_config_loads(self):
        repos = config.load_repos(EXAMPLE)
        r = config.get_repo(repos, "your-name/your-site")
        self.assertEqual(r.installation_id, 12345678)
        self.assertEqual([c.name for c in r.checks], ["build", "check", "lint"])
        self.assertIn("Bash(git:*)", r.deny_tools)

    def test_model_defaults_to_opus_55_and_can_be_pinned(self):
        self.assertEqual(repos_from('[repos."a/b"]\ninstallation_id=1\n')["a/b"].model, "claude-opus-5-5")
        pinned = repos_from('[defaults]\nmodel="x"\n[repos."a/b"]\ninstallation_id=1\n')
        self.assertEqual(pinned["a/b"].model, "x")
        self.assertEqual(config.load_repos(EXAMPLE)["your-name/your-site"].model,
                         "claude-opus-5-5")

    def test_unknown_repo_rejected(self):
        with self.assertRaises(ConfigError):
            config.get_repo(config.load_repos(EXAMPLE), "someone/else")

    def test_other_hosts_rejected(self):
        # only github.com is allowed
        with self.assertRaises(ConfigError):
            repos_from('[repos."a/b"]\ninstallation_id=1\nhost="github.example.com"\n')

    def test_bad_slug_rejected(self):
        for bad in ("../etc", "a/..", "a/.", "a/b/c", "a", "-a/b", "a b/c"):
            with self.subTest(slug=bad), self.assertRaises(ConfigError):
                repos_from(f'[repos."{bad}"]\ninstallation_id=1\n')

    def test_valid_slugs_accepted(self):
        for ok in ("your-name/your-site", "a-b/c.d_e", "x/.github"):
            with self.subTest(slug=ok):
                self.assertIn(ok, repos_from(f'[repos."{ok}"]\ninstallation_id=1\n'))

    def test_fork_mode_not_implemented(self):
        with self.assertRaises(ConfigError):
            repos_from('[repos."a/b"]\ninstallation_id=1\nmode="fork"\n')

    def test_missing_allowlist_file(self):
        with self.assertRaises(ConfigError):
            config.load_repos(Path("/nonexistent/repos.toml"))


class GithubConfigTests(unittest.TestCase):
    def test_example_has_placeholders_only(self):
        g = config.load_github(EXAMPLE)
        self.assertEqual((g.app_id, g.bot_name, g.bot_id), (123456, "my-anton-app[bot]", 123456789))

    def test_missing_or_broken_section_is_rejected(self):
        for data in ({}, {"github": {"app_id": 1}}, {"github": {"app_id": "x", "bot_name": "b", "bot_id": 2}}):
            with self.subTest(data=data), self.assertRaises(ConfigError):
                config.parse_github(data)


class GitTests(unittest.TestCase):
    def test_branch_names(self):
        job = "20261006-120000-abcdef"
        self.assertEqual(gitops.branch_name(7, job), "anton/issue-7-abcdef")
        self.assertEqual(gitops.branch_name(None, job, "Fix the Footer typo!"), "anton/fix-the-footer-typo-abcdef")
        self.assertEqual(gitops.branch_name(None, job), "anton/work-abcdef")  # neutral until Claude gave a title
        self.assertEqual(gitops.branch_name(None, job, "!!!"), "anton/work-abcdef")
        # two attempts at the same issue never collide on the remote
        self.assertNotEqual(gitops.branch_name(7, "20261006-120000-aaaaaa"), gitops.branch_name(7, "20261006-120001-bbbbbb"))

    def test_branch_names_are_always_plain_ascii(self):
        for title in ("\u00c4ndere die Fu\u00dfzeile", "\u65e5\u672c\u8a9e", "caf\u00e9 au lait", "a" * 80):
            name = gitops.branch_name(None, "20261006-120000-abcdef", title)
            self.assertRegex(name, r"^anton/[a-z0-9-]+$", title)
            gitops.assert_pushable(name, "main")
            self.assertLessEqual(len(name), 46)

    def test_never_push_base_or_foreign_branch(self):
        with self.assertRaises(RuntimeError):
            gitops.assert_pushable("main", "main")
        with self.assertRaises(RuntimeError):
            gitops.assert_pushable("feature/x", "main")
        gitops.assert_pushable("anton/issue-1", "main")

    def test_protected_paths(self):
        changed = ["src/a.svelte", ".github/workflows/ci.yml", "gh-pages.js", "build/index.html", ".env"]
        bad = gitops.violations(changed, ("gh-pages.js",))
        self.assertEqual(sorted(bad), sorted([".github/workflows/ci.yml", "gh-pages.js", "build/index.html", ".env"]))
        self.assertEqual(gitops.violations(["src/a.svelte"], ()), [])

    def test_auth_env_has_token_only_in_env(self):
        env = gitops.auth_env("tok123", "my-anton-app[bot]", 424242)
        self.assertIn("Authorization: Basic", env["GIT_CONFIG_VALUE_0"])
        self.assertNotIn("tok123", env["GIT_CONFIG_VALUE_0"])  # base64, not plaintext
        self.assertEqual(env["GIT_AUTHOR_NAME"], "my-anton-app[bot]")
        self.assertEqual(env["GIT_COMMITTER_EMAIL"], "424242+my-anton-app[bot]@users.noreply.github.com")


class CheckTests(unittest.TestCase):
    check = Check("check", "npm run check", "regression", r"svelte-check found (\d+) errors?")

    def test_parse_metric(self):
        self.assertEqual(chk.parse_metric(self.check, "svelte-check found 6 errors and 0 warnings", 1), 6)
        self.assertEqual(chk.parse_metric(self.check, "all fine", 0), 0)
        self.assertIsNone(chk.parse_metric(self.check, "crashed", 1))

    def test_prettier_one_file_wording_is_counted_through_the_warn_lines(self):
        lint = Check("lint", "npm run lint", "regression", r"Code style issues found in (\d+) files?",
                     r"^\[warn\] (?!Code style)")
        one = "Checking formatting...\n[warn] gh-pages.js\n[warn] Code style issues found in the above file.\n"
        two = "[warn] a.js\n[warn] b.js\n[warn] Code style issues found in 2 files. Forgot to run Prettier?\n"
        self.assertEqual(chk.parse_metric(lint, one, 1), 1)  # "the above file" has no number to capture
        self.assertEqual(chk.parse_metric(lint, two, 1), 2)
        self.assertEqual(chk.parse_metric(lint, "All matched files use Prettier code style!", 0), 0)
        self.assertIsNone(chk.parse_metric(lint, "SyntaxError somewhere", 1))  # failing, nothing countable

    def test_a_check_that_was_clean_and_now_fails_is_worse_even_if_uncountable(self):
        base = {"lint": self.r("lint", "regression", 0, 0)}
        broken = {"lint": self.r("lint", "regression", 1, None)}
        ok, lines = chk.compare(base, broken)
        self.assertFalse(ok)
        self.assertIn("WORSE", lines[0])
        # failing before and after with nothing countable stays "not comparable" (nothing got visibly worse)
        both = {"lint": self.r("lint", "regression", 1, None)}
        self.assertTrue(chk.compare(both, both)[0])

    def test_metric_lines_must_be_a_valid_regex(self):
        with self.assertRaises(ConfigError):
            repos_from('[repos."a/b"]\ninstallation_id=1\n[[repos."a/b".checks]]\nname="x"\ncmd="y"\nkind="gate"\nmetric_lines="("\n')

    def r(self, name, kind, code, metric):
        return chk.CheckResult(name, kind, code, metric, "")

    def test_compare_ok_when_not_worse(self):
        base = {"build": self.r("build", "gate", 0, 0), "check": self.r("check", "regression", 1, 6)}
        after = {"build": self.r("build", "gate", 0, 0), "check": self.r("check", "regression", 1, 6)}
        ok, lines = chk.compare(base, after)
        self.assertTrue(ok)
        self.assertIn("- check: 6 -> 6 (not worse)", lines)

    def test_compare_fails_on_worse_or_broken_gate(self):
        base = {"build": self.r("build", "gate", 0, 0), "check": self.r("check", "regression", 1, 6)}
        worse = {"build": self.r("build", "gate", 0, 0), "check": self.r("check", "regression", 1, 7)}
        self.assertFalse(chk.compare(base, worse)[0])
        broken = {"build": self.r("build", "gate", 1, None), "check": self.r("check", "regression", 1, 6)}
        self.assertFalse(chk.compare(base, broken)[0])


if __name__ == "__main__":
    unittest.main()
