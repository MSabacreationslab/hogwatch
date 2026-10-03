"""Tests for the installer report and the extra monitoring behind it.

Run with:  .venv\\Scripts\\python.exe -m unittest discover -s tests
"""

import gzip
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from hogwatch.db import DB
from hogwatch.eero import _body, parse_unit
from hogwatch.incidents import HiccupTracker
from hogwatch.installer import _route_label, build_installer_report, channel_block, touches_dfs

NOW = time.time()
LINK5 = "on the wireless link between the Upstairs and Main eeros, 5 GHz"
LINK6 = "on the wireless link between the Upstairs and Main eeros, 6 GHz"
VIA = "on the links between the eeros (Upstairs → Den → Main)"


def unit_raw(name, gateway, wired, upstream=None, port2_carrier=False, ip="192.168.4.2"):
    """A raw /eeros record as the eero API returns it."""
    return {
        "location": name, "gateway": gateway, "wired": wired, "connection_type": "WIRED" if wired else "WIRELESS",
        "ip_address": ip, "model": "eero Pro 6E", "os_version": "v7.16.2-80", "mesh_quality_bars": 5,
        "status": "green", "connected_wired_clients_count": 1, "connected_wireless_clients_count": 6,
        "wireless_upstream_node": {"name": upstream, "primary_mesh_radio": "5GHz"} if upstream else None,
        "uptime": {"since_cloud_connection_s": 600, "since_last_reboot_s": 86400},
        "radio_channel_stats": {
            "band_2_4GHz": {"channel": 11, "channel_width": 20, "channel_utilization": 49, "client_count": 2, "tx_power": 29},
            "band_5GHz_full": {"channel": 36, "channel_width": 160, "channel_utilization": 30, "client_count": 4, "tx_power": 24},
            "band_6GHz": {"channel": 37, "channel_width": 160, "channel_utilization": 5, "client_count": 0, "tx_power": 18},
        },
        "ethernet_status": {"statuses": [
            {"port_name": 1, "hasCarrier": gateway, "speed": "P2500" if gateway else "P10", "isWanPort": gateway},
            {"port_name": 2, "hasCarrier": port2_carrier, "speed": "P1000" if port2_carrier else "P10", "isWanPort": False},
        ]},
    }


class ChannelTests(unittest.TestCase):
    def test_channel_blocks(self):
        self.assertEqual(channel_block(36, 160), [36, 40, 44, 48, 52, 56, 60, 64])
        self.assertEqual(channel_block(52, 80), [52, 56, 60, 64])
        self.assertEqual(channel_block(149, 80), [149, 153, 157, 161])
        self.assertEqual(channel_block(44, 40), [44, 48])
        self.assertEqual(channel_block(36, 20), [36])

    def test_dfs(self):
        self.assertTrue(touches_dfs("5 GHz", 36, 160))    # 36-64 includes 52-64
        self.assertFalse(touches_dfs("5 GHz", 36, 80))    # 36-48 only
        self.assertTrue(touches_dfs("5 GHz", 100, 80))
        self.assertFalse(touches_dfs("5 GHz", 149, 80))
        self.assertFalse(touches_dfs("6 GHz", 37, 160))   # DFS is a 5 GHz thing
        self.assertFalse(touches_dfs("2.4 GHz", 11, 20))

    def test_route_labels(self):
        self.assertEqual(_route_label(LINK5), "direct over 5 GHz")
        self.assertEqual(_route_label(VIA), "via Upstairs → Den → Main")
        self.assertEqual(_route_label("on the cable between the Upstairs and Main eeros"), "direct by cable")


class UnitParsingTests(unittest.TestCase):
    def test_health_fields(self):
        u = parse_unit(unit_raw("Upstairs", False, False, "Main"), now=1_000_000)
        self.assertEqual(u["bands"]["5 GHz"], {"channel": 36, "width": 160, "utilization": 30, "clients": 4, "tx_power": 24})
        self.assertEqual(u["ports"], [{"port": "1", "carrier": False, "speed": None, "wan": False},
                                      {"port": "2", "carrier": False, "speed": None, "wan": False}])
        self.assertEqual((u["bars"], u["firmware"], u["wireless_clients"]), (5, "v7.16.2-80", 6))
        self.assertEqual(u["rebooted_ts"], 1_000_000 - 86400)
        gw = parse_unit(unit_raw("Main", True, True), now=1_000_000)
        self.assertEqual(gw["ports"][0], {"port": "1", "carrier": True, "speed": 2500, "wan": True})

    def test_gzip_responses_are_decoded(self):
        class Resp:
            headers = {"Content-Encoding": "gzip"}

            def read(self):
                return gzip.compress(b'{"data": 1}')
        self.assertEqual(_body(Resp()), b'{"data": 1}')


class SleepGapTests(unittest.TestCase):
    def test_flush_ends_dropout_at_last_bad_ping(self):
        t = HiccupTracker(interval_s=1.0)
        for i in range(4):
            t.feed(1000 + i, 1.0, True, None, True, None, None, 100)
        h = t.flush()  # the PC went to sleep here
        self.assertEqual((h["start_ts"], h["end_ts"], h["where"]), (1000, 1004, "eero_link"))
        self.assertIsNone(t.flush())


class MigrationTests(unittest.TestCase):
    def test_old_database_gains_jitter_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "old.db"
            c = sqlite3.connect(p)
            c.execute("CREATE TABLE latency (ts INTEGER NOT NULL, role TEXT NOT NULL, sent INTEGER NOT NULL, "
                      "lost INTEGER NOT NULL, avg_ms REAL, max_ms REAL)")
            c.execute("INSERT INTO latency VALUES (1, 'lan', 10, 0, 5.0, 9.0)")
            c.commit()
            c.close()
            db = DB(p)
            cols = {r["name"] for r in db.query("PRAGMA table_info(latency)")}
            self.assertIn("jitter_ms", cols)
            self.assertEqual(db.query("SELECT COUNT(*) n FROM latency")[0]["n"], 1)  # data kept
            db.conn.close()


class InstallerReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db = self.db = DB(Path(self.tmp.name) / "t.db")
        units = [parse_unit(unit_raw("Main", True, True, ip="192.168.4.1"), NOW),
                 parse_unit(unit_raw("Upstairs", False, False, "Main"), NOW),
                 parse_unit(unit_raw("Den", False, False, "Upstairs", ip="192.168.4.3"), NOW)]
        path = {"node": "Upstairs", "gateway": "Main", "chain": ["Upstairs", "Main"], "radio": "5 GHz", "wired": False,
                "on_link_units": ["Upstairs", "Den"],
                "units": [{k: u[k] for k in ("name", "gateway", "wired", "upstream", "radio", "model", "ip")} for u in units]}
        db.set_meta("eero_path", json.dumps(path))
        db.set_meta("targets", json.dumps({"lan": "192.168.4.1", "isp": "192.168.1.254", "node": "192.168.4.2"}))
        db.set_meta("link_watch_since", str(int(NOW - 3 * 86400)))
        db.execute("INSERT INTO speedtest VALUES ('2026-10-01T09:00:00Z', ?, 968.0, 630.0)", (int(NOW - 86400),))
        for k in range(5):  # unit snapshots every 2 minutes
            db.executemany("INSERT INTO unit_sample(ts, unit, gateway, wired, upstream, radio, bars, status, firmware, "
                           "wired_clients, wireless_clients, bands, ports) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           [(int(NOW - 600 + k * 120), u["name"], int(u["gateway"]), int(u["wired"]), u["upstream"],
                             u["radio"], u["bars"], u["status"], u["firmware"], u["wired_clients"],
                             u["wireless_clients"], json.dumps(u["bands"]), json.dumps(u["ports"])) for u in units])
        # Two days of 10-second ping buckets for every hop.
        rows = []
        for i in range(0, 2 * 86400, 10):
            ts = int(NOW - 2 * 86400 + i)
            rows += [(ts, "internet", 10, 0, 16.0, 30.0, 2.0), (ts, "node", 10, 0, 0.4, 1.0, 0.1),
                     (ts, "lan", 10, 0, 6.0, 25.0, 4.0), (ts, "isp", 10, 3, 7.0, 12.0, 1.0)]
        db.executemany("INSERT INTO latency(ts, role, sent, lost, avg_ms, max_ms, jitter_ms) VALUES(?,?,?,?,?,?,?)", rows)
        # Dropouts on the eero link over three routes, one past the eeros, and one that
        # (before the sleep fix) stretched across 38 hours of the PC sleeping.
        hic = []
        for i, text in enumerate([LINK5] * 5 + [LINK6] * 3 + [VIA] * 2):
            s = NOW - 86400 + i * 3600
            hic.append((s, s + 8, "eero_link", text, 8, 8, None, None, 0.4, "League of Legends", '[{"name": "JAKE-DESKTOP"}]'))
        hic.append((NOW - 7200, NOW - 7198, "internet", "past your eeros (AT&T's line or the wider internet)", 2, 1, 300.0, None, 0.4, None, "[]"))
        hic.append((NOW - 40 * 3600, NOW - 40 * 3600 + 136_522, "pc_link", "between this PC and the Upstairs eero", 11, 11, None, None, None, None, "[]"))
        db.executemany("INSERT INTO hiccup(start_ts, end_ts, where_, where_text, rounds, lost, worst_ms, worst_lan_ms, "
                       "worst_node_ms, game, busy) VALUES(?,?,?,?,?,?,?,?,?,?,?)", hic)
        db.executemany("INSERT INTO eero_event(ts, unit, kind, detail) VALUES(?,?,'reconnected','{}')",
                       [(int(NOW - 3000 * k), "Upstairs") for k in range(1, 6)] + [(int(NOW - 5000), "Den")])
        # Personal data that must never reach the report.
        db.execute("INSERT INTO eero_device VALUES('aa:bb', 'Sam''s iPhone', 'Sam', '192.168.4.50', 'Apple', 'phone', 'wifi', 'Den', ?)", (int(NOW),))
        db.execute("INSERT INTO pc_app VALUES(?, 'Discord', 1000000, 1000000)", (int(NOW - 60),))
        db.execute("INSERT INTO incident(start_ts, end_ts, kind, headline, details) VALUES(?, ?, 'congested', "
                   "'Biggest user: JAKE-DESKTOP (Jake), uploading', '{}')", (NOW - 5000, NOW - 4900))
        self.html = build_installer_report(db, days=7, note="Both ends of the <b>cable</b> are plugged in.", now=NOW)

    def tearDown(self):
        self.db.conn.close()
        self.tmp.cleanup()

    def test_no_personal_information(self):
        for private in ("JAKE-DESKTOP", "Jake", "Sam", "iPhone", "Discord", "League of Legends", "Apple", "aa:bb"):
            self.assertNotIn(private, self.html)

    def test_findings(self):
        h = self.html
        self.assertIn("The wireless link between the Upstairs and Main eeros keeps dropping", h)
        self.assertIn("10 dropouts", h)
        self.assertIn("never linked", h)                     # port 2 on both eeros: 0 of 5 checks
        self.assertIn("radar-shared (DFS)", h)               # 36/160 backhaul
        self.assertIn("eero isn't using 6 GHz for this hop", h)
        self.assertIn("route to the Main eero kept changing", h)
        self.assertIn("direct over 5 GHz (5)", h)
        self.assertIn("via Upstairs → Den → Main (2)", h)
        self.assertIn("IP Passthrough", h)                   # 192.168.1.254 is private: double NAT
        self.assertIn("968 Mbps down", h)
        self.assertIn("Den eero relays through the Upstairs eero", h)
        self.assertIn("hasn't gone down since", h)

    def test_requested_work_and_note(self):
        self.assertIn("Repair the Ethernet run between the Main and the Upstairs", self.html)
        self.assertIn("Both ends of the &lt;b&gt;cable&lt;/b&gt; are plugged in.", self.html)  # escaped

    def test_sleep_stretched_dropout_is_capped(self):
        self.assertNotIn("2275 min", self.html)
        self.assertNotIn(" hr", self.html.split("Longest dropouts")[1].split("</table>")[0])

    def test_unmeasured_location_is_labelled(self):
        # Remove the main-eero pings around the internet-side dropout: it must not be blamed on AT&T.
        self.db.execute("DELETE FROM latency WHERE role = 'lan' AND ts BETWEEN ? AND ?", (NOW - 7300, NOW - 7100))
        html = build_installer_report(self.db, days=7, now=NOW)
        self.assertIn("location not measured", html)


if __name__ == "__main__":
    unittest.main()
