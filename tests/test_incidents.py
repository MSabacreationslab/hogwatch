"""Tests for slowdown detection and the plain-English explanations.

Run with:  .venv\\Scripts\\python.exe -m unittest discover -s tests
"""

import unittest

from hogwatch.eero import classify, parse_device, parse_unit, uplink_chain
from hogwatch.incidents import Detector, HiccupTracker, Incident, fmt_duration, where_text


def feed(det, values):
    """Feed a sequence of pings and return the events that fired."""
    return [e for e in (det.feed(v) for v in values) if e]


class DetectorTests(unittest.TestCase):
    def test_quiet_line_never_triggers(self):
        det = Detector(slow_extra_ms=80, slow_min_ms=100)
        self.assertEqual(feed(det, [15, 16, 14, 18, 15] * 20), [])
        self.assertAlmostEqual(det.normal_ms(), 15, delta=1)

    def test_single_spike_is_ignored(self):
        det = Detector(80, 100)
        feed(det, [15] * 50)
        self.assertEqual(feed(det, [15, 400, 15, 15, 15]), [])

    def test_sustained_bufferbloat_starts_and_ends(self):
        det = Detector(80, 100)
        feed(det, [15] * 50)
        self.assertEqual(feed(det, [300, 350, 320]), ["start"])
        self.assertEqual(feed(det, [300] * 20), [])  # still going
        self.assertEqual(feed(det, [15] * 9), [])    # not over yet
        self.assertEqual(feed(det, [15]), ["end"])   # 10 good in a row

    def test_packet_loss_counts_as_slow(self):
        det = Detector(80, 100)
        feed(det, [15] * 50)
        self.assertEqual(feed(det, [None, None, None]), ["start"])

    def test_small_wobble_is_not_a_slowdown(self):
        # 15 -> 60 ms is noticeable but not "crawling"; slow_min_ms guards this.
        det = Detector(80, 100)
        feed(det, [15] * 50)
        self.assertEqual(feed(det, [60] * 20), [])

    def test_normal_does_not_creep_up_during_slowdown(self):
        det = Detector(80, 100)
        feed(det, [15] * 50)
        feed(det, [500] * 100)
        self.assertAlmostEqual(det.normal_ms(), 15, delta=1)


def dev(mac, name, down, up, kind="computer", owner=None):
    """A parsed eero device as the collector would produce it."""
    return {"mac": mac, "name": name, "owner": owner, "kind": kind, "down_mbps": down, "up_mbps": up}


class ExplainTests(unittest.TestCase):
    def make(self, pings=(350,) * 60, lan=3.0):
        inc = Incident(start_ts=1000, normal_ms=15)
        for p in pings:
            inc.add_ping(p, lan, 4.0, True)
        return inc

    def test_roommate_upload_is_named_with_share_of_plan(self):
        inc = self.make()
        for _ in range(3):
            inc.add_eero([dev("aa:01", "JAKE-PC", 2.0, 19.0, owner="Jake"),
                          dev("aa:02", "DIRECTV-LIVING", 9.0, 0.2, kind="directv"),
                          dev("aa:03", "Mike-PC", 0.3, 0.1)])
        s = inc.summarize(1240, {"down": 300, "up": 20}, eero_connected=True,
                          my_macs={"aa:03"}, per_app_available=True)
        self.assertEqual(s["kind"], "congested")
        top = s["details"]["suspects"][0]
        self.assertEqual(top["name"], "JAKE-PC")
        self.assertIn("uploading 19 Mbps", s["headline"])
        self.assertIn("95% of your upload", s["headline"])
        self.assertIn("4 min", s["headline"])
        self.assertIn("UPLOAD", top["guess"])
        self.assertTrue(any(x["kind"] == "directv" for x in s["details"]["suspects"]))
        # Mike-PC used < 1 Mbps, so it isn't listed as a suspect.
        self.assertFalse(any(x["this_pc"] for x in s["details"]["suspects"]))

    def test_light_user_is_not_blamed(self):
        inc = self.make()
        inc.add_eero([dev("aa:01", "JAKE-PC", 1.0, 0.4, owner="Jake")])
        s = inc.summarize(1100, {"down": 300, "up": 20}, eero_connected=True, my_macs=set(), per_app_available=True)
        self.assertNotIn("Biggest user", s["headline"])
        self.assertIn("AT&T", s["headline"])
        self.assertFalse(s["details"]["suspects"][0]["heavy"])

    def test_without_eero_clears_this_pc(self):
        inc = self.make()
        inc.add_pc({"secs": 10, "rx": 50_000, "tx": 20_000, "apps": {"Chrome": [50_000, 20_000]}})
        s = inc.summarize(1100, None, eero_connected=False, my_macs=set(), per_app_available=True)
        self.assertIn("barely using the internet", s["headline"])
        self.assertTrue(any("eero" in n for n in s["details"]["notes"]))

    def test_this_pc_download_is_attributed_to_program(self):
        inc = self.make()
        inc.add_pc({"secs": 10, "rx": 250_000_000, "tx": 1_000_000,
                    "apps": {"Steam": [240_000_000, 500_000], "Chrome": [10_000_000, 500_000]}})
        s = inc.summarize(1100, None, eero_connected=False, my_macs=set(), per_app_available=True)
        self.assertIn("Steam", s["headline"])

    def test_eero_link_dropout_is_not_blamed_on_att(self):
        # What the real log showed: 5 s total dropout, Upstairs eero fine, Main eero silent.
        # The median ping stays normal, which used to make this read as "AT&T's side".
        inc = Incident(start_ts=1000, normal_ms=15)
        for _ in range(5):
            inc.add_ping(None, None, None, True, node_ms=0.5, node_probed=True)
        for _ in range(10):
            inc.add_ping(16.0, 5.0, 6.0, True, node_ms=0.5, node_probed=True)
        path = {"node": "Upstairs", "gateway": "Main", "chain": ["Upstairs", "Main"], "radio": "5 GHz"}
        s = inc.summarize(1015, {"down": 968, "up": 630}, eero_connected=True, my_macs=set(),
                          per_app_available=True, path=path)
        self.assertEqual(s["kind"], "eero_link")
        self.assertIn("wireless link between the Upstairs and Main eeros", s["headline"])
        self.assertIn("5 pings lost", s["headline"])
        self.assertNotIn("AT&T's side", s["headline"])
        self.assertTrue(any("Ethernet cable" in n for n in s["details"]["notes"]))

    def test_whole_house_dropout_without_node_is_home(self):
        inc = Incident(start_ts=1000, normal_ms=15)
        for _ in range(4):
            inc.add_ping(None, None, None, True)
        for _ in range(10):
            inc.add_ping(16.0, 5.0, 6.0, True)
        s = inc.summarize(1014, None, eero_connected=True, my_macs=set(), per_app_available=True)
        self.assertEqual(s["kind"], "home")
        self.assertIn("inside the house", s["headline"])

    def test_eero_slow_means_home_network(self):
        inc = self.make(lan=120.0)
        s = inc.summarize(1100, None, eero_connected=False, my_macs=set(), per_app_available=True)
        self.assertEqual(s["kind"], "home")

    def test_total_loss_is_outage(self):
        inc = self.make(pings=(None,) * 30)
        s = inc.summarize(1060, None, eero_connected=False, my_macs=set(), per_app_available=True)
        self.assertEqual(s["kind"], "outage")
        self.assertIn("dropped out", s["headline"])


class DeviceTests(unittest.TestCase):
    def test_directv_detected_from_name_or_maker(self):
        self.assertEqual(classify("Den", "", "DIRECTV", "tv"), "directv")
        self.assertEqual(classify("C71KW-400", "", "", ""), "directv")
        self.assertEqual(classify("Genie HR54", "", "", ""), "directv")

    def test_word_start_matching(self):
        self.assertEqual(classify("Ring Doorbell", "", "", ""), "camera")
        self.assertNotEqual(classify("Springfield laptop", "", "", ""), "camera")

    def test_parse_device_prefers_nickname_and_reads_usage(self):
        d = parse_device({"mac": "AA:BB:CC:DD:EE:FF", "nickname": "Jake's PC", "hostname": "DESKTOP-1",
                          "connected": True, "connection_type": "wired", "usage": {"down_mbps": 5.5, "up_mbps": 1.25},
                          "profile": {"name": "Jake"}, "source": {"location": "Main"}})
        self.assertEqual(d["name"], "Jake's PC")
        self.assertEqual(d["owner"], "Jake")
        self.assertEqual(d["connection"], "wired")
        self.assertEqual((d["down_mbps"], d["up_mbps"]), (5.5, 1.25))
        self.assertEqual(d["mac"], "aa:bb:cc:dd:ee:ff")

    def test_parse_device_without_usage(self):
        d = parse_device({"mac": "aa:bb:cc:dd:ee:01", "manufacturer": "Roku", "connected": True, "wireless": True})
        self.assertIsNone(d["down_mbps"])
        self.assertEqual(d["kind"], "tv")
        self.assertIn("Roku", d["name"])


class HiccupTests(unittest.TestCase):
    """Rounds are (node_ms, lan_ms, isp_ms, internet_ms); None = lost."""

    def run_rounds(self, rounds, node_probed=True, bad_ms=100):
        t = HiccupTracker(interval_s=1.0)
        out = []
        for i, (node, lan, isp, inet) in enumerate(rounds):
            r = t.feed(1000 + i, node, node_probed, lan, True, isp, inet, bad_ms)
            if r:
                out.append(r)
        return out

    GOOD = (1.0, 5.0, 6.0, 16.0)

    def test_wireless_link_dropout(self):
        # Upstairs eero answers, main eero and everything past it goes dark for 3 seconds.
        rounds = [self.GOOD] * 5 + [(1.0, None, None, None)] * 3 + [self.GOOD] * 5
        [h] = self.run_rounds(rounds)
        self.assertEqual(h["where"], "eero_link")
        self.assertEqual(h["lost"], 3)
        self.assertEqual(h["end_ts"] - h["start_ts"], 3)

    def test_cable_or_port_dropout(self):
        rounds = [self.GOOD] * 5 + [(None, None, None, None)] * 2 + [self.GOOD] * 5
        [h] = self.run_rounds(rounds)
        self.assertEqual(h["where"], "pc_link")

    def test_internet_side_spike(self):
        rounds = [self.GOOD] * 5 + [(1.0, 5.0, 6.0, 400.0)] * 2 + [self.GOOD] * 5
        [h] = self.run_rounds(rounds)
        self.assertEqual(h["where"], "internet")
        self.assertEqual(h["worst_ms"], 400.0)

    def test_pc_on_main_eero_blames_that_eero(self):
        rounds = [self.GOOD] * 5 + [(None, 250.0, None, None)] * 2 + [self.GOOD] * 5
        [h] = self.run_rounds(rounds, node_probed=False)
        self.assertEqual(h["where"], "eero")

    def test_single_slightly_slow_ping_is_noise(self):
        rounds = [self.GOOD] * 5 + [(1.0, 5.0, 6.0, 120.0)] + [self.GOOD] * 5
        self.assertEqual(self.run_rounds(rounds), [])

    def test_lost_eero_ping_alone_is_not_a_dropout(self):
        # eeros sometimes skip answering pings while traffic flows fine.
        rounds = [self.GOOD] * 5 + [(1.0, None, 6.0, 16.0)] * 2 + [self.GOOD] * 5
        self.assertEqual(self.run_rounds(rounds), [])

    def test_flapping_is_one_dropout(self):
        bad = (1.0, None, None, None)
        rounds = [self.GOOD] * 5 + [bad, bad, self.GOOD, bad, bad] + [self.GOOD] * 5
        self.assertEqual(len(self.run_rounds(rounds)), 1)


class MeshPathTests(unittest.TestCase):
    UNITS = [
        {"name": "Main", "gateway": True, "wired": True, "upstream": None, "radio": None, "ip": "192.168.4.1"},
        {"name": "Upstairs", "gateway": False, "wired": False, "upstream": "Main", "radio": "5 GHz", "ip": "192.168.4.2"},
        {"name": "Den", "gateway": False, "wired": False, "upstream": "Upstairs", "radio": "6 GHz", "ip": "192.168.4.3"},
        {"name": "Garage", "gateway": False, "wired": False, "upstream": "Main", "radio": "5 GHz", "ip": "192.168.4.4"},
    ]

    def test_chains(self):
        names = lambda start: [u["name"] for u in uplink_chain(self.UNITS, start)]  # noqa: E731
        self.assertEqual(names("Upstairs"), ["Upstairs", "Main"])
        self.assertEqual(names("Den"), ["Den", "Upstairs", "Main"])
        self.assertEqual(names("Main"), ["Main"])
        self.assertEqual(names(None), [])

    def test_where_text_uses_real_names(self):
        path = {"node": "Upstairs", "gateway": "Main", "chain": ["Upstairs", "Main"], "radio": "5 GHz"}
        self.assertEqual(where_text("eero_link", path), "on the wireless link between the Upstairs and Main eeros, 5 GHz")
        self.assertIn("Upstairs eero", where_text("pc_link", path))
        wired = dict(path, wired=True, radio=None)
        self.assertEqual(where_text("eero_link", wired), "on the cable between the Upstairs and Main eeros")

    def test_wired_satellite_chains_to_gateway(self):
        units = [dict(u) for u in self.UNITS]
        units[1].update(wired=True, upstream=None, radio=None)  # Upstairs after the cable is plugged in
        self.assertEqual([u["name"] for u in uplink_chain(units, "Upstairs")], ["Upstairs", "Main"])
        self.assertEqual([u["name"] for u in uplink_chain(units, "Den")], ["Den", "Upstairs", "Main"])

    def test_parse_unit(self):
        u = parse_unit({"location": "Upstairs", "gateway": False, "connection_type": "WIRELESS", "ip_address": "192.168.4.2",
                        "wireless_upstream_node": {"name": "Main", "primary_mesh_radio": "5GHz"},
                        "uptime": {"since_cloud_connection_s": 60}}, now=1000)
        self.assertEqual((u["upstream"], u["radio"], u["reconnected_ts"], u["wired"]), ("Main", "5 GHz", 940, False))


class FormatTests(unittest.TestCase):
    def test_durations(self):
        self.assertEqual(fmt_duration(42), "42 sec")
        self.assertEqual(fmt_duration(90), "1 min 30 sec")
        self.assertEqual(fmt_duration(600), "10 min")
        self.assertEqual(fmt_duration(4000), "1 hr 6 min")


if __name__ == "__main__":
    unittest.main()
