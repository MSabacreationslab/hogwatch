"""Tests for the daily email report: settings storage, report content, sending, scheduling,
and the dashboard API's protections around it. No real email is sent (smtplib is mocked).

Run with:  .venv\\Scripts\\python.exe -m unittest discover -s tests
"""

import datetime as dt
import http.client
import json
import smtplib
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from hogwatch.db import DB
from hogwatch.report import (EmailSettings, Reporter, ReportError, build_report, is_due, parse_recipients,
                             send_email)
from hogwatch.web import make_server

PATH = {"node": "Upstairs", "gateway": "Main", "chain": ["Upstairs", "Main"], "radio": "5 GHz", "wired": False}
LINK = "on the wireless link between the Upstairs and Main eeros, 5 GHz"


def seed(db, t0=1_000_000.0):
    """Four dropouts on the eero link (one during a game), one past the eeros, and one slowdown."""
    db.set_meta("eero_path", json.dumps(PATH))
    busy = json.dumps([{"name": "<script>alert(1)</script>", "owner": None, "node": "Upstairs", "on_link": True,
                        "this_pc": False, "down_mbps": 12.0, "up_mbps": 0.1}])
    rows = [(t0 + i * 600, t0 + i * 600 + 5, "eero_link", LINK, 5, 5, None, None, 0.4,
             "League of Legends" if i == 0 else None, busy) for i in range(4)]
    rows.append((t0 + 3000, t0 + 3002, "internet", "past your eeros (AT&T's line or the wider internet)",
                 2, 0, 260.0, None, 0.4, None, "[]"))
    db.executemany("INSERT INTO hiccup(start_ts, end_ts, where_, where_text, rounds, lost, worst_ms, worst_lan_ms, "
                   "worst_node_ms, game, busy) VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)
    db.execute("INSERT INTO incident(start_ts, end_ts, kind, peak_ms, normal_ms, loss_pct, headline, details) "
               "VALUES(?,?,?,?,?,?,?,?)", (t0 + 100, t0 + 112, "eero_link", 30.0, 15.0, 40.0,
                                           "The connection dropped out between your eeros.", "{}"))
    db.execute("INSERT INTO eero_event(ts, unit, kind, detail) VALUES(?,?,?,?)", (int(t0 + 200), "Upstairs", "reconnected", "{}"))


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db = DB(self.dir / "t.db")

    def tearDown(self):
        self.db.conn.close()
        self.tmp.cleanup()


class RecipientTests(unittest.TestCase):
    def test_parses_lists(self):
        self.assertEqual(parse_recipients("a@b.com, c@d.org;e@f.net"), ["a@b.com", "c@d.org", "e@f.net"])
        self.assertEqual(parse_recipients("  "), [])

    def test_rejects_junk(self):
        for bad in ("nope", "a@b", "a@b.com, <x@y.com>", "a b@c.com@d"):
            with self.assertRaises(ReportError):
                parse_recipients(bad)


class SettingsTests(TempDirTest):
    def test_password_is_encrypted_and_never_returned(self):
        s = EmailSettings(self.dir / "email.json")
        loaded = s.save({"to": "me@example.com", "from": "me@gmail.com", "password": "abcd efgh ijkl mnop"})
        self.assertTrue(loaded["password_set"])
        self.assertNotIn("password", loaded)
        self.assertNotIn("password_dpapi", loaded)
        self.assertNotIn("abcd", (self.dir / "email.json").read_text())
        self.assertEqual(s.password(), "abcd efgh ijkl mnop")

    def test_blank_password_keeps_saved_one_and_clear_removes_it(self):
        s = EmailSettings(self.dir / "email.json")
        s.save({"password": "first"})
        s.save({"to": "x@y.com", "password": ""})
        self.assertEqual(s.password(), "first")
        s.save({"clear_password": True})
        self.assertIsNone(s.password())

    def test_bad_values_rejected(self):
        s = EmailSettings(self.dir / "email.json")
        with self.assertRaises(ReportError):
            s.save({"to": "not-an-email"})
        with self.assertRaises(ReportError):
            s.save({"smtp_port": "abc"})
        with self.assertRaises(ReportError):
            s.save({"from": "me at gmail"})


class ReportContentTests(TempDirTest):
    def test_counts_reasons_and_advice(self):
        seed(self.db)
        r = build_report(self.db, 999_000, 1_010_000)
        self.assertEqual(r["counts"], {"dropouts": 5, "slowdowns": 1, "reconnects": 1})
        self.assertTrue(r["subject"].startswith("HogWatch: 5 dropouts and 1 slowdown since"))
        self.assertIn(f"4 {LINK}", r["text"])
        self.assertIn("1 during a game", r["text"])
        self.assertIn("Game: League of Legends", r["text"])
        self.assertIn("The connection dropped out between your eeros.", r["text"])
        self.assertIn("Ethernet cable between those two eeros", r["text"])  # 4 of 5 on one link -> advice
        self.assertIn("Upstairs eero reconnected", r["text"])

    def test_device_names_are_escaped_in_html(self):
        seed(self.db)
        r = build_report(self.db, 999_000, 1_010_000)
        self.assertNotIn("<script>", r["html"])
        self.assertIn("&lt;script&gt;", r["html"])

    def test_wired_link_gets_cable_advice(self):
        seed(self.db)
        self.db.set_meta("eero_path", json.dumps(dict(PATH, wired=True, radio=None)))
        self.assertIn("both ends of the cable", build_report(self.db, 999_000, 1_010_000)["text"])

    def test_all_clear(self):
        r = build_report(self.db, 0, 100)
        self.assertTrue(r["subject"].startswith("HogWatch: all clear"))
        self.assertIn("Everything ran normally", r["text"])


class SendTests(unittest.TestCase):
    SETTINGS = {"to": "me@example.com, you@example.com", "from": "me@gmail.com", "username": "",
                "smtp_host": "smtp.gmail.com", "smtp_port": 587}

    def test_starttls_login_and_send(self):
        with mock.patch("hogwatch.report.smtplib.SMTP") as smtp_cls:
            smtp = smtp_cls.return_value
            smtp.__enter__.return_value = smtp
            to = send_email(self.SETTINGS, "abcd efgh ijkl mnop", "Subj", "text", "<p>html</p>")
        self.assertEqual(to, ["me@example.com", "you@example.com"])
        smtp_cls.assert_called_once_with("smtp.gmail.com", 587, timeout=30)
        smtp.starttls.assert_called_once()
        smtp.login.assert_called_once_with("me@gmail.com", "abcdefghijklmnop")  # spaces from Google's display removed
        msg = smtp.send_message.call_args[0][0]
        self.assertEqual(msg["To"], "me@example.com, you@example.com")
        self.assertEqual(msg["Subject"], "Subj")
        self.assertTrue(msg.is_multipart())

    def test_port_465_uses_tls_from_the_start(self):
        with mock.patch("hogwatch.report.smtplib.SMTP_SSL") as ssl_cls, mock.patch("hogwatch.report.smtplib.SMTP") as plain:
            ssl_cls.return_value.__enter__.return_value = ssl_cls.return_value
            send_email(dict(self.SETTINGS, smtp_port=465), "pw", "s", "t", "h")
        ssl_cls.assert_called_once()
        plain.assert_not_called()

    def test_rejected_login_explains_app_passwords(self):
        with mock.patch("hogwatch.report.smtplib.SMTP") as smtp_cls:
            smtp = smtp_cls.return_value
            smtp.__enter__.return_value = smtp
            smtp.login.side_effect = smtplib.SMTPAuthenticationError(535, b"bad credentials")
            with self.assertRaisesRegex(ReportError, "App Password"):
                send_email(self.SETTINGS, "wrong", "s", "t", "h")

    def test_missing_pieces(self):
        with self.assertRaisesRegex(ReportError, "app password"):
            send_email(self.SETTINGS, "", "s", "t", "h")
        with self.assertRaisesRegex(ReportError, "send reports to"):
            send_email(dict(self.SETTINGS, to=""), "pw", "s", "t", "h")


class ScheduleTests(unittest.TestCase):
    def test_once_a_day(self):
        morning, night = dt.datetime(2026, 9, 25, 9, 0), dt.datetime(2026, 9, 25, 2, 0)
        self.assertTrue(is_due(morning, "2026-09-24", startup=False, send_hour=8))
        self.assertFalse(is_due(morning, "2026-09-25", startup=True, send_hour=8))   # already sent today
        self.assertFalse(is_due(night, "2026-09-24", startup=False, send_hour=8))    # running overnight: wait for 8 AM
        self.assertTrue(is_due(night, "2026-09-24", startup=True, send_hour=8))      # just started: send now
        self.assertTrue(is_due(night, None, startup=True, send_hour=8))

    def test_daily_send_records_status_and_covers_since_last(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = DB(Path(tmp) / "t.db")
            settings = EmailSettings(Path(tmp) / "email.json")
            settings.save({"to": "me@example.com", "from": "me@gmail.com", "password": "pw"})
            rep = Reporter(db, settings, threading.Event())
            with mock.patch("hogwatch.report.send_email", return_value=["me@example.com"]) as send:
                result = rep.send(daily=True)
            self.assertTrue(result["ok"])
            self.assertEqual(send.call_args[0][1], "pw")
            st = rep.status()
            self.assertEqual(st["last_daily"], dt.date.today().isoformat())
            self.assertIsNone(st["last_error"])
            with mock.patch("hogwatch.report.send_email", side_effect=ReportError("server down")):
                self.assertFalse(rep.send(daily=False)["ok"])
            self.assertEqual(rep.status()["last_error"], "server down")
            self.assertEqual(rep.status()["last_daily"], dt.date.today().isoformat())  # failure doesn't erase success
            db.conn.close()


class ApiProtectionTests(TempDirTest):
    """The dashboard now holds email settings, so it must refuse other sites' requests."""

    def setUp(self):
        super().setUp()
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.settings = EmailSettings(self.dir / "email.json")
        self.reporter = Reporter(self.db, self.settings, threading.Event())
        self.server = make_server(self.port, {}, {"db": self.db}, on_shutdown=lambda: None,
                                  reporter_ref={"r": self.reporter})
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def request(self, method, path, host=None, headers=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Host": host or f"127.0.0.1:{self.port}"}
        h.update(headers or {})
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, data

    def test_foreign_host_is_refused(self):
        # DNS rebinding: another site's domain pointed at 127.0.0.1.
        self.assertEqual(self.request("GET", "/api/email", host=f"evil.example:{self.port}")[0], 403)
        self.assertEqual(self.request("GET", "/api/email")[0], 200)
        self.assertEqual(self.request("GET", "/api/email", host=f"localhost:{self.port}")[0], 200)

    def test_post_needs_header(self):
        body = {"to": "attacker@example.com"}
        self.assertEqual(self.request("POST", "/api/email", body=body)[0], 403)
        self.assertEqual(self.settings.load()["to"], "")
        status, data = self.request("POST", "/api/email", headers={"X-HogWatch": "1", "Content-Type": "application/json"},
                                    body={"to": "me@example.com", "password": "secret-pw"})
        self.assertEqual(status, 200)
        self.assertNotIn(b"secret-pw", data)
        self.assertEqual(self.settings.load()["to"], "me@example.com")

    def test_settings_response_never_contains_password(self):
        self.settings.save({"to": "me@example.com", "password": "secret-pw"})
        status, data = self.request("GET", "/api/email")
        self.assertEqual(status, 200)
        self.assertNotIn(b"secret-pw", data)
        self.assertTrue(json.loads(data)["settings"]["password_set"])

    def test_bad_input_is_a_clear_error(self):
        status, data = self.request("POST", "/api/email", headers={"X-HogWatch": "1"}, body={"to": "nope"})
        self.assertEqual(status, 400)
        self.assertIn("email address", json.loads(data)["error"])

    def test_preview_renders(self):
        seed(self.db, t0=__import__("time").time() - 3600)
        status, data = self.request("GET", "/api/email/preview")
        self.assertEqual(status, 200)
        self.assertIn(b"HogWatch report", data)


if __name__ == "__main__":
    unittest.main()
