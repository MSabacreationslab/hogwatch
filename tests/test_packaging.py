"""Tests for what the packaged HogWatch.exe relies on: where data lives, the encrypted eero
session, start-at-login, signing in to eero from the dashboard, and the double-click launcher.

Run with:  .venv\\Scripts\\python.exe -m unittest discover -s tests
"""

import argparse
import http.client
import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from hogwatch import __main__ as cli
from hogwatch import autostart, config
from hogwatch.db import DB
from hogwatch.eero import Eero, EeroError
from hogwatch.web import make_server


class DataFolderTests(unittest.TestCase):
    def test_packaged_exe_uses_local_app_data(self):
        with mock.patch.object(config, "FROZEN", True), mock.patch.dict("os.environ", {"LOCALAPPDATA": r"C:\Users\x\AppData\Local"}):
            self.assertEqual(config._default_data_dir(), Path(r"C:\Users\x\AppData\Local\HogWatch"))

    def test_source_checkout_uses_data_next_to_code(self):
        with mock.patch.object(config, "FROZEN", False):
            self.assertEqual(config._default_data_dir(), config.ROOT / "data")

    def test_command_line_options_are_read_early(self):
        with mock.patch("sys.argv", ["hogwatch", "--data-dir", r"D:\hw", "--port", "8799", "run"]):
            self.assertEqual(config._cli_option("--data-dir"), r"D:\hw")
            self.assertEqual(config._cli_option("--port"), "8799")
            self.assertIsNone(config._cli_option("--missing"))
            self.assertEqual(cli._global_options(), ["--data-dir", r"D:\hw", "--port", "8799"])

    def test_port_override_applies(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(config, "DATA_DIR", Path(tmp)), \
                mock.patch.object(config, "CONFIG_PATH", Path(tmp) / "config.json"), \
                mock.patch.object(config, "PORT_OVERRIDE", "8799"):
            self.assertEqual(config.load()["port"], 8799)
            # The saved file keeps the default: the override is for this run only.
            self.assertEqual(json.loads((Path(tmp) / "config.json").read_text())["port"], 8765)


class EeroSessionTests(unittest.TestCase):
    def test_token_is_encrypted_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "eero_session.json"
            e = Eero(p)
            e.token, e.network_url, e.network_name = "secret-session-token", "/2.2/networks/1", "Home"
            e.save()
            self.assertNotIn("secret-session-token", p.read_text())
            again = Eero(p)
            self.assertEqual((again.token, again.network_url, again.ready), ("secret-session-token", "/2.2/networks/1", True))

    def test_plain_token_from_older_version_is_upgraded(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "eero_session.json"
            p.write_text(json.dumps({"token": "old-plain-token", "network_url": "/2.2/networks/1", "network_name": "Home"}))
            self.assertEqual(Eero(p).token, "old-plain-token")
            self.assertNotIn("old-plain-token", p.read_text())  # rewritten encrypted
            self.assertEqual(Eero(p).token, "old-plain-token")

    def test_unreadable_session_means_sign_in_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "eero_session.json"
            p.write_text(json.dumps({"token_dpapi": "bm90IGEgcmVhbCBibG9i", "network_url": "/2.2/networks/1"}))
            self.assertFalse(Eero(p).ready)


class AutostartTests(unittest.TestCase):
    def test_source_checkout_command(self):
        with mock.patch.object(autostart, "FROZEN", False):
            exe, args, cwd = autostart.command()
        self.assertTrue(exe.lower().endswith(("pythonw.exe", "python.exe")))
        self.assertEqual((args, cwd), ("-m hogwatch run --no-browser", str(config.ROOT)))

    def test_packaged_command_points_at_the_exe(self):
        with mock.patch.object(autostart, "FROZEN", True), mock.patch("sys.executable", r"C:\Tools\Bob's apps\HogWatch.exe"):
            self.assertEqual(autostart.command(), (r"C:\Tools\Bob's apps\HogWatch.exe", "run --no-browser", r"C:\Tools\Bob's apps"))
            script = autostart.install_script()
        self.assertIn(r"-Execute 'C:\Tools\Bob''s apps\HogWatch.exe'", script)  # the quote in the path is escaped
        self.assertIn("-RunLevel Highest", script)
        self.assertIn("-AtLogOn", script)

    def test_failure_is_explained(self):
        failed = mock.Mock(returncode=1, stdout="", stderr="Access is denied")
        with mock.patch.object(autostart, "_ps", return_value=failed):
            with self.assertRaisesRegex(autostart.AutostartError, "administrator"):
                autostart.install()
            with self.assertRaisesRegex(autostart.AutostartError, "administrator"):
                autostart.uninstall()


class FakeEero:
    """Stands in for the eero cloud: one account, code 123456."""
    networks_list = [{"url": "/2.2/networks/1", "name": "Home"}]
    saved = []

    def __init__(self, path):
        self.path, self.token, self.network_url, self.network_name = path, None, None, None

    def start_login(self, login):
        if "@" not in login:
            raise EeroError("eero API 404")
        self.token = "pending"

    def verify(self, code):
        if code != "123456":
            raise EeroError("eero API 401")

    def networks(self):
        return list(self.networks_list)

    def save(self):
        FakeEero.saved.append((self.network_url, self.network_name))


class DashboardApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name) / "t.db")
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        FakeEero.saved = []
        FakeEero.networks_list = [{"url": "/2.2/networks/1", "name": "Home"}]
        self.patches = [mock.patch("hogwatch.web.Eero", FakeEero)]
        for p in self.patches:
            p.start()
        self.server = make_server(self.port, {}, {"db": self.db}, on_shutdown=lambda: None,
                                  eero_session=Path(self.tmp.name) / "eero_session.json")
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        for p in self.patches:
            p.stop()
        self.db.conn.close()
        self.tmp.cleanup()

    def post(self, path, body, header=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Host": f"127.0.0.1:{self.port}", "Content-Type": "application/json"}
        if header:
            headers["X-HogWatch"] = "1"
        conn.request("POST", path, body=json.dumps(body), headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, (json.loads(data) if data.startswith(b"{") else data)

    def test_sign_in_with_one_network(self):
        self.assertEqual(self.post("/api/eero/login", {"login": "me@example.com"}), (200, {"ok": True}))
        status, data = self.post("/api/eero/verify", {"code": "123456"})
        self.assertEqual((status, data), (200, {"ok": True, "connected": "Home"}))
        self.assertEqual(FakeEero.saved, [("/2.2/networks/1", "Home")])

    def test_sign_in_with_a_choice_of_networks(self):
        FakeEero.networks_list = [{"url": "/2.2/networks/1", "name": "Home"}, {"url": "/2.2/networks/2", "name": "Cabin"}]
        self.post("/api/eero/login", {"login": "me@example.com"})
        status, data = self.post("/api/eero/verify", {"code": "123456"})
        self.assertEqual([n["name"] for n in data["networks"]], ["Home", "Cabin"])
        self.assertEqual(FakeEero.saved, [])  # nothing saved until a network is picked
        self.assertEqual(self.post("/api/eero/network", {"url": "/2.2/networks/9"})[0], 400)
        self.assertEqual(self.post("/api/eero/network", {"url": "/2.2/networks/2"}), (200, {"ok": True, "connected": "Cabin"}))
        self.assertEqual(FakeEero.saved, [("/2.2/networks/2", "Cabin")])

    def test_sign_in_errors_are_plain(self):
        self.assertIn("Enter the email", self.post("/api/eero/login", {"login": " "})[1]["error"])
        self.assertIn("didn't recognise", self.post("/api/eero/login", {"login": "nonsense"})[1]["error"])
        self.assertIn("Start again", self.post("/api/eero/verify", {"code": "123456"})[1]["error"])
        self.post("/api/eero/login", {"login": "me@example.com"})
        status, data = self.post("/api/eero/verify", {"code": "000000"})
        self.assertEqual(status, 400)
        self.assertIn("code didn't work", data["error"])
        self.assertEqual(FakeEero.saved, [])

    def test_account_without_a_network(self):
        FakeEero.networks_list = []
        self.post("/api/eero/login", {"login": "me@example.com"})
        self.assertIn("isn't an admin on any network", self.post("/api/eero/verify", {"code": "123456"})[1]["error"])

    def test_sign_in_needs_the_dashboard_header(self):
        self.assertEqual(self.post("/api/eero/login", {"login": "me@example.com"}, header=False)[0], 403)

    def test_autostart_switch(self):
        with mock.patch.object(autostart, "install") as install, mock.patch.object(autostart, "uninstall") as uninstall, \
                mock.patch.object(autostart, "installed", return_value=True):
            self.assertEqual(self.post("/api/autostart", {"enabled": True}), (200, {"ok": True, "autostart": True}))
            install.assert_called_once()
            self.post("/api/autostart", {"enabled": False})
            uninstall.assert_called_once()
        with mock.patch.object(autostart, "install", side_effect=autostart.AutostartError("needs administrator")):
            status, data = self.post("/api/autostart", {"enabled": True})
            self.assertEqual((status, data["ok"]), (400, False))


class LauncherTests(unittest.TestCase):
    """Double-clicking the exe: open the dashboard if already running; otherwise start an
    elevated background copy; if the user refuses the admin prompt, run without it."""

    def launch(self, *, up, admin, elevated_ok=True):
        args = argparse.Namespace(cmd="launch", no_browser=False)
        with mock.patch.object(cli, "server_up", side_effect=up) as server_up, \
                mock.patch.object(cli, "_is_admin", return_value=admin), \
                mock.patch.object(cli, "run_elevated", return_value=elevated_ok) as run_elevated, \
                mock.patch.object(cli, "open_dashboard") as open_dashboard, \
                mock.patch.object(cli, "cmd_run", return_value=0) as cmd_run, \
                mock.patch.object(cli.time, "sleep"), \
                mock.patch.object(cli.config, "load", return_value=dict(config.DEFAULTS)):
            cli.cmd_launch(args)
        return server_up, run_elevated, open_dashboard, cmd_run

    def test_already_running_just_opens_the_dashboard(self):
        _, run_elevated, open_dashboard, cmd_run = self.launch(up=[True], admin=False)
        open_dashboard.assert_called_once_with("http://127.0.0.1:8765/")
        run_elevated.assert_not_called()
        cmd_run.assert_not_called()

    def test_starts_elevated_copy_then_opens_dashboard(self):
        _, run_elevated, open_dashboard, cmd_run = self.launch(up=[False, False, True], admin=False)
        self.assertIn("run --no-browser", run_elevated.call_args[0][1])
        open_dashboard.assert_called_once()
        cmd_run.assert_not_called()

    def test_refused_admin_prompt_runs_without_it(self):
        _, run_elevated, _, cmd_run = self.launch(up=[False], admin=False, elevated_ok=False)
        run_elevated.assert_called_once()
        cmd_run.assert_called_once()
        self.assertFalse(cmd_run.call_args[0][0].no_browser)

    def test_already_admin_runs_directly(self):
        _, run_elevated, _, cmd_run = self.launch(up=[False], admin=True)
        run_elevated.assert_not_called()
        cmd_run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
