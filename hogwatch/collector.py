"""Runs all the measuring in background threads and keeps a live snapshot for the dashboard.

Threads:
  ping   every 1s: this PC's eero, the main eero, ISP gateway (hop 2), internet
         -> slowdown detector + lag-spike (dropout) tracker
  pc     every 10s: network card totals + per-program bytes -> DB
  eero   every 30s (10s during a slowdown): per-device rates from the eero cloud
  house  hourly: delete old history
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import psutil

from . import ping
from .config import DATA_DIR
from .db import DB
from .eero import Eero, EeroError, LoginNeeded, parse_device, parse_unit, speed_from_network, uplink_chain
from .incidents import Detector, HiccupTracker, Incident, where_text
from .pcnet import GAMES, HostnameResolver, PCMonitor

log = logging.getLogger(__name__)

EERO_SESSION = DATA_DIR / "eero_session.json"
BUCKET_S = 10
BIG_FLOW_BYTES = 256 * 1024


def _my_macs() -> set[str]:
    """This PC's MAC addresses in eero's format (aa:bb:cc:dd:ee:ff), to spot 'this PC' in eero's list."""
    out = set()
    for addrs in psutil.net_if_addrs().values():
        for a in addrs:
            if a.family == psutil.AF_LINK and a.address:
                out.add(a.address.replace("-", ":").lower())
    return out


class Collector:
    """Owns the measuring threads, the slowdown detector, and the live snapshot."""

    def __init__(self, cfg: dict, db: DB):
        """Set up state; nothing runs until start()."""
        self.cfg = cfg
        self.db = db
        self.stopping = threading.Event()
        self.lock = threading.Lock()
        self.detector = Detector(cfg["slow_extra_ms"], cfg["slow_min_ms"])
        self.incident: Incident | None = None
        self.incident_id: int | None = None
        self.targets: dict = {}
        self.rounds = deque(maxlen=60)  # last minute of ping rounds: (ts, internet, lan, isp, node)
        self.hiccups = HiccupTracker(float(cfg["ping_interval_s"]))
        # Mesh layout: which eero this PC plugs into and the links from it to the main eero.
        self.path: dict = {}
        self.unit_reconnects: dict[str, int] = {}
        self.pc: PCMonitor | None = None
        self.pc_live: dict = {"down_mbps": 0.0, "up_mbps": 0.0, "apps": [], "ts": None}
        self.last_pc_sample: dict | None = None
        self.eero = Eero(EERO_SESSION)
        self.eero_wakeup = threading.Event()
        self.eero_live: dict = {"status": "not_connected", "devices": [], "ts": None}
        self.last_eero_devices: list[dict] = []
        self.last_eero_ts = 0.0
        self.resolver = HostnameResolver(db)
        self.my_macs = _my_macs()
        self.started = time.time()

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        """Discover the network path, start per-program tracing, launch the threads."""
        self._close_stale_incidents()
        lan = self.cfg.get("lan_target") or ping.hop(1)
        isp = ping.hop(2, toward=self.cfg["internet_targets"][0])
        self.targets = {"lan": lan, "isp": isp, "internet": self.cfg["internet_targets"]}
        self.db.set_meta("targets", json.dumps(self.targets))
        log.info("targets: %s", self.targets)
        self.pc = PCMonitor()
        for fn in (self._ping_loop, self._pc_loop, self._eero_loop, self._housekeeping_loop):
            threading.Thread(target=self._guard(fn), name=fn.__name__.strip("_"), daemon=True).start()

    def _guard(self, fn):
        """Keep a thread alive through unexpected errors: log, pause, carry on."""
        def run():
            while not self.stopping.is_set():
                try:
                    fn()
                    return
                except Exception:
                    log.exception("%s crashed; restarting in 10s", fn.__name__)
                    self.stopping.wait(10)
        return run

    def stop(self) -> None:
        """Stop threads, close any open slowdown, and shut down the ETW session."""
        self.stopping.set()
        self.eero_wakeup.set()
        with self.lock:
            if self.incident:
                self._incident_end(time.time())
        if self.pc:
            self.pc.stop()

    def _close_stale_incidents(self) -> None:
        """A slowdown left open by a crash/shutdown gets closed at its last known point."""
        self.db.execute(
            "UPDATE incident SET end_ts = start_ts, headline = COALESCE(headline, '') || ' (monitor stopped)' "
            "WHERE end_ts IS NULL")

    # ------------------------------------------------------------------ ping

    def _ping_loop(self) -> None:
        """Ping every target each round, feed the detector, store 10-second buckets."""
        interval = float(self.cfg["ping_interval_s"])
        timeout = int(self.cfg["ping_timeout_ms"])
        inet = list(self.cfg["internet_targets"])
        pool = ThreadPoolExecutor(max_workers=len(inet) + 3, thread_name_prefix="icmp")
        buckets: dict[str, dict] = {}
        bucket_ts = int(time.time()) // BUCKET_S * BUCKET_S
        while not self.stopping.is_set():
            t0 = time.time()
            lan = self.targets.get("lan")
            node = self.targets.get("node")  # the eero this PC plugs into, once eero tells us
            futs = {("inet", n): pool.submit(ping.ping, ip, timeout) for n, ip in enumerate(inet)}
            if lan:
                futs["lan"] = pool.submit(ping.ping, lan, timeout)
            if node:
                futs["node"] = pool.submit(ping.ping, node, timeout)
            futs["isp"] = pool.submit(ping.ping, inet[0], timeout, 2)
            res = {k: f.result()[0] for k, f in futs.items()}
            internet = min((v for k, v in res.items() if isinstance(k, tuple) and v is not None), default=None)
            lan_ms, isp_ms, node_ms = res.get("lan"), res.get("isp"), res.get("node")

            for role, v, probed in (("internet", internet, True), ("lan", lan_ms, bool(lan)),
                                    ("isp", isp_ms, True), ("node", node_ms, bool(node))):
                if not probed:
                    continue
                b = buckets.setdefault(role, {"sent": 0, "lost": 0, "sum": 0.0, "n": 0, "max": None})
                b["sent"] += 1
                if v is None:
                    b["lost"] += 1
                else:
                    b["sum"] += v
                    b["n"] += 1
                    b["max"] = v if b["max"] is None else max(b["max"], v)

            with self.lock:
                self.rounds.append((t0, internet, lan_ms, isp_ms, node_ms))
                threshold = self.detector.threshold_ms()
                seed = list(self.detector.recent_rtts)
                event = self.detector.feed(internet)
                if event == "start":
                    self._incident_start(t0, seed)
                if self.incident:
                    self.incident.add_ping(internet, lan_ms, isp_ms, bool(lan), node_ms, bool(node))
                if event == "end":
                    self._incident_end(t0)
            hiccup = self.hiccups.feed(t0, node_ms, bool(node), lan_ms, bool(lan), isp_ms, internet, threshold)
            if hiccup:
                self._record_hiccup(hiccup)

            if t0 >= bucket_ts + BUCKET_S:
                self.db.executemany(
                    "INSERT INTO latency(ts, role, sent, lost, avg_ms, max_ms) VALUES(?,?,?,?,?,?)",
                    [(bucket_ts, role, b["sent"], b["lost"], (b["sum"] / b["n"]) if b["n"] else None, b["max"])
                     for role, b in buckets.items()])
                buckets = {}
                bucket_ts = int(t0) // BUCKET_S * BUCKET_S
            self.stopping.wait(max(0.0, interval - (time.time() - t0)))

    # ------------------------------------------------------------------ lag spikes

    def _record_hiccup(self, hic: dict) -> None:
        """Store a short dropout with what was going on: which game was running, who was busy."""
        with self.lock:
            sample = self.last_pc_sample or {}
            devices = self.last_eero_devices if time.time() - self.last_eero_ts < 90 else []
            path = dict(self.path)
        games = sorted({app for app, (rx, tx) in (sample.get("apps") or {}).items() if app in GAMES and rx + tx > 0})
        on_link = set(path.get("on_link_units") or [])
        busy = sorted(
            ({"name": d["name"], "owner": d.get("owner"), "node": d.get("node"), "this_pc": d.get("this_pc", False),
              # Shares this PC's wireless link to the main eero, so its traffic competes with ours.
              "on_link": d.get("node") in on_link,
              "down_mbps": round(d.get("down_mbps") or 0, 1), "up_mbps": round(d.get("up_mbps") or 0, 1)}
             for d in devices if (d.get("down_mbps") or 0) + (d.get("up_mbps") or 0) >= 1),
            key=lambda b: b["down_mbps"] + b["up_mbps"], reverse=True)[:5]
        text = where_text(hic["where"], path)
        self.db.execute(
            "INSERT INTO hiccup(start_ts, end_ts, where_, where_text, rounds, lost, worst_ms, worst_lan_ms, "
            "worst_node_ms, game, busy) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (hic["start_ts"], hic["end_ts"], hic["where"], text, hic["rounds"], hic["lost"], hic["worst_ms"],
             hic["worst_lan_ms"], hic["worst_node_ms"], ", ".join(games) or None, json.dumps(busy)))
        log.info("lag spike: %.0fs, %d lost, %s", hic["end_ts"] - hic["start_ts"], hic["lost"], text)

    def _refresh_units(self, connected: list[dict], now: float) -> None:
        """Learn the mesh layout (which eero this PC plugs into, and the links up to the main eero)
        and note any eero that reconnected since we last looked."""
        units = [parse_unit(u, now) for u in self.eero.units()]
        me = next((d for d in connected if d.get("this_pc")), None)
        chain = uplink_chain(units, me.get("node") if me else None)
        gateway = next((u for u in units if u["gateway"]), None)
        node = chain[0] if chain and not chain[0]["gateway"] else None
        # Every eero whose route to the main eero runs through ours shares our wireless link.
        on_link = [u["name"] for u in units
                   if node and node["name"] in [c["name"] for c in uplink_chain(units, u["name"])]]
        path = {"node": node["name"] if node else None, "node_ip": node["ip"] if node else None,
                "gateway": gateway["name"] if gateway else None, "chain": [c["name"] for c in chain],
                "radio": node["radio"] if node else None, "wired": node["wired"] if node else None,
                "on_link_units": on_link,
                "units": [{k: u[k] for k in ("name", "gateway", "wired", "upstream", "radio", "model")} for u in units]}
        with self.lock:
            self.path = path
            if node and node["ip"]:
                self.targets["node"] = node["ip"]
            else:
                self.targets.pop("node", None)
            targets = dict(self.targets)
        self.db.set_meta("eero_path", json.dumps(path))
        self.db.set_meta("targets", json.dumps(targets))
        for u in units:
            ts = u["reconnected_ts"]
            if not ts:
                continue
            prev = self.unit_reconnects.get(u["name"])
            self.unit_reconnects[u["name"]] = ts
            if prev is not None and ts <= prev + 60:
                continue  # same connection as last time (the computed time jitters by a second or two)
            if self.db.query("SELECT 1 FROM eero_event WHERE unit = ? AND ABS(ts - ?) < 120", (u["name"], ts)):
                continue
            self.db.execute("INSERT INTO eero_event(ts, unit, kind, detail) VALUES(?, ?, 'reconnected', ?)",
                            (ts, u["name"], json.dumps({"upstream": u["upstream"], "radio": u["radio"],
                                                        "gateway": u["gateway"], "wired": u["wired"]})))
            log.info("eero %s reconnected at %s", u["name"], time.strftime("%I:%M %p", time.localtime(ts)))

    # ------------------------------------------------------------------ incidents

    def capacity(self) -> dict | None:
        """Plan speeds from config, else the eero's last speed test."""
        down, up = self.cfg.get("plan_down_mbps"), self.cfg.get("plan_up_mbps")
        if down or up:
            return {"down": down, "up": up, "source": "config"}
        raw = self.db.get_meta("eero_speed")
        if raw:
            s = json.loads(raw)
            return {"down": s.get("down"), "up": s.get("up"), "source": "eero speed test", "date": s.get("date")}
        return None

    def _summary(self, inc: Incident, end_ts: float) -> dict:
        """Run the incident's explanation with the current context."""
        return inc.summarize(end_ts, self.capacity(), self.eero.ready, self.my_macs,
                             bool(self.pc and self.pc.trace), self.path)

    def _incident_start(self, ts: float, seed) -> None:
        """Open a slowdown; seed it with what was happening just before; poll eero right away. Caller holds lock."""
        inc = Incident(ts, self.detector.normal_ms(), seed)
        # The culprit usually started just *before* ping rose, so include the latest readings.
        if self.last_eero_devices and ts - self.last_eero_ts < 60:
            inc.add_eero(self.last_eero_devices)
        if self.last_pc_sample:
            inc.add_pc(self.last_pc_sample)
        self.incident = inc
        self.incident_id = self.db.execute(
            "INSERT INTO incident(start_ts, kind, normal_ms, headline) VALUES(?, 'ongoing', ?, ?)",
            (int(ts), inc.normal_ms, "Slowdown in progress..."))
        self.eero_wakeup.set()
        log.warning("slowdown started (normal %.0f ms)", inc.normal_ms)

    def _update_open_incident(self) -> None:
        """Refresh the in-progress row so the dashboard explains a slowdown while it's happening."""
        with self.lock:
            if not self.incident:
                return
            s = self._summary(self.incident, time.time())
            self.db.execute("UPDATE incident SET peak_ms=?, loss_pct=?, headline=?, details=? WHERE id=?",
                            (s["peak_ms"], s["loss_pct"], s["headline"], json.dumps(s["details"]), self.incident_id))

    def _incident_end(self, ts: float) -> None:
        """Close the slowdown and store its explanation. Caller holds lock."""
        inc, self.incident = self.incident, None
        if inc is None:
            return
        s = self._summary(inc, ts)
        self.db.execute(
            "UPDATE incident SET end_ts=?, kind=?, peak_ms=?, loss_pct=?, headline=?, details=? WHERE id=?",
            (int(ts), s["kind"], s["peak_ms"], s["loss_pct"], s["headline"], json.dumps(s["details"]), self.incident_id))
        log.warning("slowdown ended: %s", s["headline"])

    # ------------------------------------------------------------------ this PC

    def _pc_loop(self) -> None:
        """Every 10s: record card totals and per-program usage."""
        last_update = 0.0
        while not self.stopping.wait(BUCKET_S):
            s = self.pc.sample()
            ts = int(time.time())
            secs = s["secs"]
            self.db.execute("INSERT INTO pc_total(ts, secs, rx_bytes, tx_bytes) VALUES(?,?,?,?)",
                            (ts, secs, s["rx"], s["tx"]))
            self.db.executemany("INSERT INTO pc_app(ts, app, rx_bytes, tx_bytes) VALUES(?,?,?,?)",
                                [(ts, app, rx, tx) for app, (rx, tx) in s["apps"].items() if rx + tx > 0])
            big = [(ts, app, ip, rx, tx) for (app, ip), (rx, tx) in s["remotes"].items() if rx + tx >= BIG_FLOW_BYTES]
            self.db.executemany("INSERT INTO pc_remote(ts, app, ip, rx_bytes, tx_bytes) VALUES(?,?,?,?,?)", big)
            if big:
                self.resolver.want(list({row[2] for row in big}))

            def mbps(b):
                return round(b * 8 / secs / 1e6, 2)

            apps = sorted(({"app": a, "down_mbps": mbps(rx), "up_mbps": mbps(tx)} for a, (rx, tx) in s["apps"].items()),
                          key=lambda a: a["down_mbps"] + a["up_mbps"], reverse=True)[:8]
            with self.lock:
                self.pc_live = {"down_mbps": mbps(s["rx"]), "up_mbps": mbps(s["tx"]), "apps": apps, "ts": ts}
                self.last_pc_sample = s
                if self.incident:
                    self.incident.add_pc(s)
            if time.time() - last_update > 20:
                self._update_open_incident()
                last_update = time.time()

    # ------------------------------------------------------------------ eero

    def _eero_loop(self) -> None:
        """Poll the eero cloud; pick up a new login automatically when eero-login is run."""
        last_poll = 0.0
        last_speed = 0.0
        last_units = 0.0
        saved_raw = False
        session_mtime = None
        while not self.stopping.is_set():
            # eero-login runs as a separate program, so re-read its session file when it changes.
            mtime = EERO_SESSION.stat().st_mtime if EERO_SESSION.exists() else None
            if mtime != session_mtime:
                session_mtime = mtime
                self.eero = Eero(EERO_SESSION)
            if not self.eero.ready:
                with self.lock:
                    self.eero_live = {"status": "not_connected", "devices": [], "ts": None}
                self.eero_wakeup.wait(15)
                self.eero_wakeup.clear()
                continue
            try:
                raw = self.eero.devices()
                now = time.time()
                if not saved_raw:
                    # Kept for troubleshooting if eero ever changes its field names.
                    (DATA_DIR / "eero_devices_raw.json").write_text(json.dumps(raw, indent=2), encoding="utf-8")
                    saved_raw = True
                devices = [parse_device(d) for d in raw]
                connected = [d for d in devices if d["connected"] and d["mac"]]
                secs = min(now - last_poll, 120) if last_poll else float(self.cfg["eero_poll_s"])
                last_poll = now
                ts = int(now)
                self.db.executemany(
                    "INSERT INTO eero_device(mac, name, owner, ip, manufacturer, kind, connection, node, last_seen) "
                    "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(mac) DO UPDATE SET name=excluded.name, owner=excluded.owner, "
                    "ip=excluded.ip, manufacturer=excluded.manufacturer, kind=excluded.kind, "
                    "connection=excluded.connection, node=excluded.node, last_seen=excluded.last_seen",
                    [(d["mac"], d["name"], d["owner"], d["ip"], d["manufacturer"], d["kind"], d["connection"],
                      d["node"], ts) for d in connected])
                with_usage = [d for d in connected if d["down_mbps"] is not None or d["up_mbps"] is not None]
                self.db.executemany(
                    "INSERT INTO eero_sample(ts, mac, down_mbps, up_mbps, secs) VALUES(?,?,?,?,?)",
                    [(ts, d["mac"], d["down_mbps"] or 0.0, d["up_mbps"] or 0.0, secs) for d in with_usage])
                for d in connected:
                    d["this_pc"] = d["mac"] in self.my_macs
                ranked = sorted(connected, key=lambda d: (d["down_mbps"] or 0) + (d["up_mbps"] or 0), reverse=True)
                with self.lock:
                    self.last_eero_devices, self.last_eero_ts = connected, now
                    self.eero_live = {"status": "ok", "ts": ts, "devices": ranked,
                                      "usage_reported": bool(with_usage), "network": self.eero.network_name}
                    if self.incident:
                        self.incident.add_eero(connected)
                if now - last_units > 300:
                    last_units = now
                    self._refresh_units(connected, now)
                if now - last_speed > 1800:
                    last_speed = now
                    speed = speed_from_network(self.eero.network())
                    if speed:
                        self.db.set_meta("eero_speed", json.dumps(speed))
            except LoginNeeded as e:
                with self.lock:
                    self.eero_live = {"status": "login_needed", "message": str(e), "devices": [], "ts": None}
            except EeroError as e:
                log.warning("eero: %s", e)
                with self.lock:
                    self.eero_live = dict(self.eero_live, status="error", message=str(e))
            with self.lock:
                interval = self.cfg["eero_poll_during_incident_s"] if self.incident else self.cfg["eero_poll_s"]
            self.eero_wakeup.wait(interval)
            self.eero_wakeup.clear()

    # ------------------------------------------------------------------ housekeeping

    def _housekeeping_loop(self) -> None:
        """Trim old history once an hour."""
        while True:
            self.db.prune(int(self.cfg["retention_days"]))
            if self.stopping.wait(3600):
                return

    # ------------------------------------------------------------------ dashboard

    def snapshot(self) -> dict:
        """Everything the dashboard's 'right now' section needs."""
        with self.lock:
            rounds = list(self.rounds)[-5:]
            inet = [r[1] for r in rounds if r[1] is not None]
            lan = [r[2] for r in rounds if r[2] is not None]
            isp = [r[3] for r in rounds if r[3] is not None]
            node = [r[4] for r in rounds if r[4] is not None]
            loss = 100.0 * sum(1 for r in rounds if r[1] is None) / len(rounds) if rounds else 0.0
            internet_ms = sum(inet) / len(inet) if inet else None
            if not rounds:
                status = "starting"
            elif loss >= 80:
                status = "down"
            elif self.detector.active:
                status = "slow"
            else:
                status = "ok"
            incident = None
            if self.incident:
                s = self._summary(self.incident, time.time())
                incident = {"id": self.incident_id, "start_ts": int(self.incident.start_ts),
                            "headline": s["headline"], "details": s["details"], "kind": s["kind"]}
            return {
                "ts": int(time.time()),
                "uptime_s": int(time.time() - self.started),
                "status": status,
                "latency": {
                    "internet_ms": internet_ms, "lan_ms": sum(lan) / len(lan) if lan else None,
                    "isp_ms": sum(isp) / len(isp) if isp else None, "loss_pct": loss,
                    "node_ms": sum(node) / len(node) if node else None,
                    "normal_ms": self.detector.normal_ms(), "slow_above_ms": self.detector.threshold_ms(),
                },
                "targets": self.targets,
                "path": self.path,
                "incident": incident,
                "pc": dict(self.pc_live, per_app=bool(self.pc and self.pc.trace),
                           per_app_error=self.pc.trace_error if self.pc else None,
                           nic=self.pc.nic if self.pc else None),
                "eero": self.eero_live,
                "capacity": self.capacity(),
            }
