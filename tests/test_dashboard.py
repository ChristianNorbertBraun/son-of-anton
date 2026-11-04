import io
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from anton import cli, config, dashboard
from anton.flow import Event, Flow
from anton.flowdb import FlowDb


def flow(**kw):
    return replace(Flow("abc", "example/repo", "7", "standard", "A title\n\nThe body", "main", "c" * 40, "owner"), **kw)


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = FlowDb(Path(self.tmp.name) / "flows.db")

    def body(self, *flows):
        for f in flows:
            self.db.create(f)
            self.db.commit(f, f, [], Event("start"))
        code, _, body = dashboard.respond(self.db, "/t0ken", "127.0.0.1:8766", "t0ken")
        self.assertEqual(code, 200)
        return body.decode()

    def test_an_empty_page_works(self):
        self.assertIn("No flows yet", self.body())

    def test_a_flow_shows_its_task_state_and_link(self):
        text = self.body(flow(state="awaiting-gate:plan", stage="gate:plan", pr=3))
        for part in ("example/repo#7", "A title", "awaiting-gate:plan", "https://github.com/example/repo/pull/3"):
            self.assertIn(part, text)
        self.assertNotIn("The body", text)

    def test_a_rework_round_shows_where_implement_was(self):
        pips = dashboard.pip_states(flow(state="running:rework", stage="rework", reworks=2))
        self.assertEqual(pips[:3], [("plan", "done"), ("plan gate", "done"), ("rework 2/3", "now")])
        self.assertEqual(pips[-1], ("merge gate", "todo"))
        after = dashboard.pip_states(flow(state="awaiting-gate:merge", stage="gate:merge", reworks=1))
        self.assertIn(("rework 1/3", "done"), after)
        self.assertIn(("implement", "done"), dashboard.pip_states(flow(state="awaiting-gate:merge", stage="gate:merge")))

    def test_all_tracker_text_is_escaped(self):
        evil = '<script>alert(1)</script>"\'><img src=x>'
        text = self.body(flow(brief=evil, reason=evil, key="7<b>", state="blocked", stage="implement"))
        self.assertNotIn("<script>", text)
        self.assertNotIn("<img", text)
        self.assertNotIn("<b>", text)

    def test_the_policy_allows_no_script(self):
        _, headers, body = dashboard.respond(self.db, "/t0ken", "localhost", "t0ken")
        policy = headers["Content-Security-Policy"]
        self.assertIn("default-src 'none'", policy)
        self.assertNotIn("script-src", policy)
        self.assertNotIn("unsafe", policy)
        nonce = policy.split("'nonce-")[1].split("'")[0]
        self.assertIn(f'nonce="{nonce}"', body.decode())

    def test_each_response_has_its_own_nonce(self):
        first = dashboard.respond(self.db, "/t0ken", "localhost", "t0ken")[1]["Content-Security-Policy"]
        self.assertNotEqual(first, dashboard.respond(self.db, "/t0ken", "localhost", "t0ken")[1]["Content-Security-Policy"])

    def test_a_foreign_host_is_refused(self):
        for host in ("evil.example", "evil.example:8766", "", None):
            with self.subTest(host=host):
                self.assertEqual(dashboard.respond(self.db, "/t0ken", host, "t0ken")[0], 403)

    def test_the_bind_address_is_a_valid_host(self):
        self.assertEqual(dashboard.respond(self.db, "/t0ken", "127.0.0.2:8766", "t0ken", bind="127.0.0.2")[0], 200)

    def test_the_page_needs_the_token(self):
        for path in ("/", "/wrong", "/t0ken/", "/flows.db", "/t0ke"):
            with self.subTest(path=path):
                code, _, body = dashboard.respond(self.db, path, "localhost", "t0ken")
                self.assertEqual(code, 404)
                self.assertEqual(body, b"not found\n")
        self.assertEqual(dashboard.respond(self.db, "/t0ken?x=1", "localhost", "t0ken")[0], 200)

    def test_no_token_means_no_page(self):
        self.assertEqual(dashboard.respond(self.db, "/", "localhost", "")[0], 404)

    def test_the_token_is_private_and_stays_the_same(self):
        path = Path(self.tmp.name) / dashboard.TOKEN_FILE
        token = dashboard.load_token(path)
        self.assertGreater(len(token), 30)
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        self.assertEqual(dashboard.load_token(path), token)

    def test_pips_follow_the_flow(self):
        states = dict(dashboard.pip_states(flow(state="running:implement", stage="implement")))
        self.assertEqual((states["plan"], states["implement"], states["cleanup"]), ("done", "now", "todo"))

    def test_a_detour_shows_under_its_own_name(self):
        states = dict(dashboard.pip_states(flow(state="running:fix", stage="fix")))
        self.assertEqual(states["fix"], "now")
        self.assertNotIn("review", states)

    def test_a_blocked_flow_stops_at_its_step(self):
        states = dict(dashboard.pip_states(flow(state="blocked", stage="review")))
        self.assertEqual(states["review"], "stopped")

    def test_a_done_flow_is_done_everywhere(self):
        states = dashboard.pip_states(flow(state="done", stage="gate:merge"))
        self.assertEqual({state for _, state in states}, {"done"})

    def test_a_flow_that_has_not_started_has_only_steps_to_come(self):
        self.assertEqual({s for _, s in dashboard.pip_states(flow())}, {"todo"})

    def test_cost_and_time_are_summed(self):
        f = flow(state="running:review", stage="review")
        self.db.create(f)
        self.db.record_run(f.id, "plan", 1, "j1", "ok", {"cost_usd": 0.25}, 100, 160)
        self.db.record_run(f.id, "implement", 2, "j2", "ok", {"cost_usd": 0.5}, 200, 320)
        text = dashboard.respond(self.db, "/t0ken", "localhost", "t0ken")[2].decode()
        self.assertIn("$0.75", text)
        self.assertIn("3 min", text)


class ServerTests(unittest.TestCase):
    """No socket: the handler the server would use, with its input and output replaced."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = FlowDb(Path(self.tmp.name) / "flows.db")

    def get(self, path, host="localhost"):
        with mock.patch.object(dashboard, "ThreadingHTTPServer") as server:
            dashboard.serve(self.db, "127.0.0.1", 8766, "t0ken")
        handler_class = server.call_args.args[1]
        handler = handler_class.__new__(handler_class)
        handler.path, handler.headers, handler.wfile = path, {"Host": host}, io.BytesIO()
        sent = {}
        handler.send_response = lambda code: sent.update(code=code)
        handler.send_header = lambda name, value: None
        handler.end_headers = lambda: None
        handler.do_GET()
        return sent["code"]

    def test_the_handler_passes_on_the_real_path_and_host(self):
        self.assertEqual(self.get("/t0ken"), 200)
        self.assertEqual(self.get("/"), 404)
        self.assertEqual(self.get("/t0ken", host="evil.example"), 403)

    def test_it_listens_where_the_config_says(self):
        with mock.patch.object(dashboard, "ThreadingHTTPServer") as server:
            dashboard.serve(self.db, "127.0.0.2", 9000, "t0ken")
        self.assertEqual(server.call_args.args[0], ("127.0.0.2", 9000))

    def test_the_command_uses_the_checked_address_and_the_token_file(self):
        settings = SimpleNamespace(daemon=config.parse_daemon({"daemon": {"dashboard_port": 9000}}))
        with mock.patch.object(cli.config, "load_settings", return_value=settings), \
                mock.patch.object(cli.runner, "CONF_DIR", Path(self.tmp.name)), \
                mock.patch.object(cli, "FLOW_DB_PATH", Path(self.tmp.name) / "flows.db"), \
                mock.patch.object(dashboard, "serve") as serve, mock.patch("builtins.print") as printed:
            cli.cmd_dashboard(mock.Mock())
        _, bind, port, token = serve.call_args.args
        self.assertEqual((bind, port), ("127.0.0.1", 9000))
        self.assertEqual(token, (Path(self.tmp.name) / dashboard.TOKEN_FILE).read_text().strip())
        self.assertNotIn(token, str(printed.call_args_list))  # stdout goes to the journal


class ConfigTests(unittest.TestCase):
    def test_it_binds_to_loopback_unless_told_otherwise(self):
        self.assertEqual(config.parse_daemon({}).dashboard_bind, "127.0.0.1")

    def test_anything_but_loopback_is_refused(self):
        for bind in ("0.0.0.0", "10.0.0.5", "0.0.0.0; rm -rf", "127.0.0.1.evil.example", "1270.0.0.1", "127.example.org"):
            with self.subTest(bind=bind), self.assertRaises(config.ConfigError):
                config.parse_daemon({"daemon": {"dashboard_bind": bind}})


if __name__ == "__main__":
    unittest.main()
