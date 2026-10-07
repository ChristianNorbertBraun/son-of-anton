import tempfile
import unittest
from pathlib import Path

from anton import tripwire


def diff(path, *added, removed=()):
    body = "".join(f"-{r}\n" for r in removed) + "".join(f"+{a}\n" for a in added)
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n{body}"


class TripwireTests(unittest.TestCase):
    def conf(self, **files):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        for name, text in files.items():
            (Path(d.name) / name).write_text(text)
        return Path(d.name)

    def test_exact_values_from_the_config_directory_are_found(self):
        values = tripwire.secret_values(self.conf(**{"claude-token": "tok-9f8e7d6c5b4a3210\n", "telegram-chat": "123456789012345"}))
        self.assertEqual(values, ["tok-9f8e7d6c5b4a3210"])  # the chat id is not a secret
        self.assertEqual(tripwire.scan(diff("a.py", 'T = "tok-9f8e7d6c5b4a3210"'), values), ["a stored secret in a.py"])

    def test_the_finding_never_contains_the_secret(self):
        values = ["tok-9f8e7d6c5b4a3210"]
        self.assertNotIn("9f8e7d6c", " ".join(tripwire.scan(diff("a.py", "x = tok-9f8e7d6c5b4a3210"), values)))

    def test_each_line_of_a_key_file_counts_but_not_the_pem_markers(self):
        pem = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n-----END PRIVATE KEY-----\n"
        values = tripwire.secret_values(self.conf(**{"app-key.pem": pem}))
        self.assertIn("MIIEvQIBADANBgkqhkiG9w0BAQEFAASC", values)
        self.assertTrue(all(not v.startswith("-----") for v in values))

    def test_toml_and_backup_files_are_not_secret_values(self):
        values = tripwire.secret_values(self.conf(**{"repos.toml": "installation_id = 168520611\n", "x.bak-1": "somelongvalue123456"}))
        self.assertEqual(values, [])

    def test_token_and_key_shapes(self):
        cases = {
            "Claude token": "sk-ant-oat01-" + "A" * 30,
            "GitHub token": "ghp_" + "a1B2" * 10,
            "private key": "-----BEGIN RSA PRIVATE KEY-----",
            "Telegram bot token": "123456789:" + "AbC-" * 8 + "xyz",
        }
        for kind, text in cases.items():
            with self.subTest(kind=kind):
                self.assertEqual(tripwire.scan(diff("f.txt", f"x = {text}"), []), [f"{kind} in f.txt"])

    def test_only_added_lines_count(self):
        leaked = "sk-ant-oat01-" + "A" * 30
        self.assertEqual(tripwire.scan(diff("a.py", "fine", removed=[leaked]), []), [])  # removing a leak is fine
        self.assertEqual(tripwire.scan(diff("a.py", "sk-ant-", "ghp_short"), []), [])  # fake placeholders are fine
        self.assertEqual(tripwire.scan("", []), [])

    def test_the_example_config_and_placeholders_pass(self):
        example = Path(__file__).parent.parent / "examples/repos.toml"
        self.assertEqual(tripwire.scan(diff("examples/repos.toml", *example.read_text().splitlines()), []), [])
