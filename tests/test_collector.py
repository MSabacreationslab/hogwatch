"""End-to-end: fake pings go slow, the collector opens a slowdown, then closes it with an explanation.

Pings are faked so the test never loads the real connection.
"""

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from hogwatch import collector as collector_mod
from hogwatch import config
from hogwatch.db import DB


class CollectorFlowTests(unittest.TestCase):
    def test_slowdown_is_recorded_and_explained(self):
        phase = {"slow": False}

        def fake_ping(ip, timeout_ms=1000, ttl=128):
            if ttl == 2:
                return 4.0, 11013, "192.168.1.254"
            if ip == "192.168.0.1":
                return 2.0, 0, ip
            return (400.0 if phase["slow"] else 15.0), 0, ip

        cfg = dict(config.DEFAULTS, ping_interval_s=0.05, lan_target="192.168.0.1")
        with tempfile.TemporaryDirectory() as tmp:
            db = DB(Path(tmp) / "t.db")
            with mock.patch.object(collector_mod.ping, "ping", fake_ping), \
                    mock.patch.object(collector_mod.ping, "hop", lambda n, toward="1.1.1.1": "192.168.1.254"), \
                    mock.patch.object(collector_mod, "EERO_SESSION", Path(tmp) / "none.json"):
                c = collector_mod.Collector(cfg, db)
                c.start()
                try:
                    time.sleep(1.5)            # learn "normal"
                    phase["slow"] = True
                    time.sleep(1.0)            # slowdown starts
                    snap = c.snapshot()
                    self.assertEqual(snap["status"], "slow")
                    self.assertIsNotNone(snap["incident"])
                    phase["slow"] = False
                    time.sleep(1.5)            # 10 good rounds -> ends
                finally:
                    c.stop()
            rows = db.query("SELECT * FROM incident")
            self.assertEqual(len(rows), 1)
            inc = rows[0]
            self.assertIsNotNone(inc["end_ts"])
            self.assertEqual(inc["kind"], "congested")
            self.assertIn("Internet crawled", inc["headline"])
            self.assertAlmostEqual(inc["peak_ms"], 400.0)
            db.conn.close()


if __name__ == "__main__":
    unittest.main()
