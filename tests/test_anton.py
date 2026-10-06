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
        self.assertEqual(gitops.branch_name(7, "20261006-120000-abcdef", "x"), "anton/issue-7-abcdef")
        b = gitops.branch_name(None, "20261006-120000-abcdef", "Fix the Footer typo!")
        self.assertEqual(b, "anton/fix-the-footer-typo-abcdef")
        # two attempts at the same issue never collide on the remote
        self.assertNotEqual(gitops.branch_name(7, "20261006-120000-aaaaaa", "x"),
                            gitops.branch_name(7, "20261006-120001-bbbbbb", "x"))
        self.assertTrue(gitops.branch_name(None, "20261006-120000-abcdef", "!!!").startswith("anton/task-"))

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
