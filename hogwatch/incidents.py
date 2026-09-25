"""Notice slowdowns and explain them in plain English.

How a slowdown is recognised: every second we ping the internet. "Normal"
is the low end of the last 30 minutes of good pings. If 3 of the last 5
pings are far above normal (or lost), a slowdown starts; it ends after 10
good pings in a row. While it lasts we collect what every device and every
program on this PC was doing, then name the likely culprit.

Shorter blips (the 2-5 second freezes that disconnect games) are tracked
separately by HiccupTracker, which also pinpoints which hop they were on.

Why a single heavy user slows everyone: when the line is full, packets wait
in a queue at the modem ("bufferbloat"). Every web page, game and call waits
behind the big transfer, so ping jumps from ~15 ms to hundreds.
"""

from __future__ import annotations

import statistics
from collections import defaultdict, deque


def _percentile(values, pct: float) -> float:
    """Simple percentile without numpy."""
    s = sorted(values)
    return s[min(len(s) - 1, max(0, int(round(pct / 100 * (len(s) - 1)))))]


class Detector:
    """Decides, one ping round at a time, whether a slowdown has started or ended."""

    START_SLOW_OF_LAST = (3, 5)  # 3 of the last 5 rounds slow -> slowdown starts
    END_AFTER_GOOD = 10          # 10 good rounds in a row -> slowdown over

    def __init__(self, slow_extra_ms: float, slow_min_ms: float):
        """Thresholds come from config (see config.py for what they mean)."""
        self.slow_extra_ms = slow_extra_ms
        self.slow_min_ms = slow_min_ms
        self.good = deque(maxlen=1800)  # 30 minutes of good internet pings at 1 per second
        self.recent = deque(maxlen=self.START_SLOW_OF_LAST[1])
        self.recent_rtts = deque(maxlen=self.START_SLOW_OF_LAST[1])
        self.good_streak = 0
        self.active = False

    def normal_ms(self) -> float:
        """Typical unloaded ping. 20th percentile so it doesn't creep up on busy days."""
        if not self.good:
            return 20.0
        return _percentile(self.good, 20)

    def threshold_ms(self) -> float:
        """Pings above this count as slow."""
        return max(self.slow_min_ms, self.normal_ms() + self.slow_extra_ms)

    def feed(self, internet_ms: float | None) -> str | None:
        """Feed one round's internet ping (None = lost). Returns 'start', 'end' or None."""
        slow = internet_ms is None or internet_ms > self.threshold_ms()
        self.recent.append(slow)
        self.recent_rtts.append(internet_ms)
        if not slow and not self.active:
            self.good.append(internet_ms)
        if not self.active:
            if sum(self.recent) >= self.START_SLOW_OF_LAST[0]:
                self.active = True
                self.good_streak = 0
                return "start"
            return None
        self.good_streak = 0 if slow else self.good_streak + 1
        if self.good_streak >= self.END_AFTER_GOOD:
            self.active = False
            self.recent.clear()
            return "end"
        return None


def fmt_duration(secs: float) -> str:
    """90 -> '1 min 30 sec', 4000 -> '1 hr 7 min'."""
    secs = int(round(secs))
    if secs < 60:
        return f"{secs} sec"
    m, s = divmod(secs, 60)
    if m < 60:
        return f"{m} min" + (f" {s} sec" if s and m < 5 else "")
    h, m = divmod(m, 60)
    return f"{h} hr" + (f" {m} min" if m else "")


def fmt_mbps(x: float | None) -> str:
    """Readable rate: 0.4, 7.5, 180."""
    if x is None:
        return "?"
    return f"{x:.0f}" if x >= 10 else f"{x:.1f}"


def guess_activity(kind: str, down: float, up: float) -> str:
    """What a device is probably doing, from its type and traffic shape."""
    if kind == "directv":
        return ("DirecTV streaming. One HD stream is about 8 Mbps and 4K about 25 Mbps, "
                "so several TVs or recordings add up fast")
    if kind == "tv":
        return "TV / video streaming"
    if kind == "console":
        return "a game download or update, or game streaming"
    if up >= 5 and up >= down:
        return ("a big UPLOAD, usually live-streaming (Twitch/Discord/OBS), cloud backup or photo "
                "sync, uploading videos, or torrent seeding. Uploads clog the line for everyone")
    if down >= 50:
        return "a big download: a game or software update (Steam, Windows Update), torrents, or large files"
    if down >= 8:
        return "video streaming or downloading"
    return "light use"


class HiccupTracker:
    """Catches short lag spikes and dropouts -- a few seconds, too brief to count as a
    slowdown but long enough to disconnect a game -- and finds which hop they were on.

    Each round pings every hop outward from this PC, so the first hop that went bad
    is where the problem is:
        this PC -> [cable] -> its eero -> [wireless mesh link] -> main eero -> AT&T -> internet
    """

    NODE_BAD_MS = 50   # this PC to the eero it's plugged into: normally 0-2 ms
    LAN_BAD_MS = 100   # this PC to the main eero across the mesh: normally under ~30 ms
    ISP_BAD_MS = 100
    END_AFTER_GOOD = 2  # rounds; stops one flapping dropout being split in two

    def __init__(self, interval_s: float):
        """`interval_s` is the ping round length, used to size each dropout."""
        self.interval_s = interval_s
        self.cur: dict | None = None
        self.good = 0

    def feed(self, ts: float, node_ms, node_probed: bool, lan_ms, lan_probed: bool, isp_ms,
             internet_ms, internet_bad_ms: float) -> dict | None:
        """Feed one ping round (None = lost). Returns a finished dropout, or None."""
        inet_bad = internet_ms is None or internet_ms > internet_bad_ms
        node_spike = node_ms is not None and node_ms > self.NODE_BAD_MS
        lan_spike = lan_ms is not None and lan_ms > self.LAN_BAD_MS
        # A lone lost ping to an eero while the internet is fine is the eero ignoring
        # ICMP, not a dropout -- only internet trouble or real delay starts one.
        if inet_bad or node_spike or lan_spike:
            if self.cur is None:
                self.cur = {"start": ts, "last": ts, "rounds": 0, "lost": 0, "worst": None, "worst_lan": None,
                            "worst_node": None, "node_bad": 0, "lan_bad": 0, "isp_bad": 0, "node_probed": False}
            c = self.cur
            self.good = 0
            c["last"] = ts
            c["rounds"] += 1
            if internet_ms is None:
                c["lost"] += 1
            else:
                c["worst"] = max(c["worst"] or 0, internet_ms)
            if node_probed:
                c["node_probed"] = True
                if node_ms is None or node_spike:
                    c["node_bad"] += 1
                if node_ms is not None:
                    c["worst_node"] = max(c["worst_node"] or 0, node_ms)
            if lan_probed:
                if lan_ms is None or lan_spike:
                    c["lan_bad"] += 1
                if lan_ms is not None:
                    c["worst_lan"] = max(c["worst_lan"] or 0, lan_ms)
            if isp_ms is not None and isp_ms > self.ISP_BAD_MS:
                c["isp_bad"] += 1
            return None
        if self.cur is None:
            return None
        self.good += 1
        if self.good < self.END_AFTER_GOOD:
            return None
        c, self.cur = self.cur, None
        # One slightly slow ping is noise; keep drops, repeats, and big spikes.
        if not (c["lost"] or c["rounds"] >= 2 or (c["worst"] or 0) >= 200 or (c["worst_lan"] or 0) >= 200):
            return None
        if c["node_bad"]:
            where = "pc_link"
        elif c["lan_bad"]:
            where = "eero_link" if c["node_probed"] else "eero"
        elif c["isp_bad"]:
            where = "att"
        else:
            where = "internet"
        return {"start_ts": c["start"], "end_ts": c["last"] + self.interval_s, "where": where,
                "rounds": c["rounds"], "lost": c["lost"], "worst_ms": c["worst"],
                "worst_lan_ms": c["worst_lan"], "worst_node_ms": c["worst_node"]}


def where_text(where: str, path: dict | None) -> str:
    """Plain-English location of a dropout, using the real eero names when known."""
    path = path or {}
    node, gw = path.get("node"), path.get("gateway") or "main"
    chain = path.get("chain") or []
    if where == "pc_link":
        return f"between this PC and the {node or 'nearest'} eero (the cable, its port, or that eero itself)"
    if where == "eero_link":
        if len(chain) > 2:
            return "on the links between the eeros (" + " → ".join(chain) + ")"
        if path.get("wired"):
            return f"on the cable between the {node} and {gw} eeros"
        radio = f", {path['radio']}" if path.get("radio") else ""
        return f"on the wireless link between the {node} and {gw} eeros{radio}"
    if where == "eero":
        return f"inside the {gw} eero (your main eero)"
    if where == "att":
        return "at the AT&T gateway"
    return "past your eeros (AT&T's line or the wider internet)"


class Incident:
    """Everything observed while one slowdown lasts."""

    def __init__(self, start_ts: float, normal_ms: float, seed_rtts=()):
        """`seed_rtts` are the few pings that triggered the slowdown, so they're counted too."""
        self.start_ts = start_ts
        self.normal_ms = normal_ms
        self.internet: list[float] = []
        self.rounds = 0
        self.lost = 0
        self.lan: list[float] = []
        self.lan_rounds = 0
        self.lan_lost = 0
        # Internet pings lost in rounds we have hop data for, and how many of those the
        # main eero also missed (break inside the house) while this PC's own eero still
        # answered (break on the link between the eeros).
        self.lost_tracked = 0
        self.co_lost = 0
        self.co_node_ok = 0
        self.co_node_lost = 0
        self.isp: list[float] = []
        # mac -> {"info": latest parsed device, "down": [...], "up": [...]}
        self.devices: dict[str, dict] = {}
        self.eero_polls = 0
        self.pc_secs = 0.0
        self.pc_rx = 0
        self.pc_tx = 0
        self.apps: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for r in seed_rtts:
            self._add_internet(r)

    def _add_internet(self, ms):
        self.rounds += 1
        if ms is None:
            self.lost += 1
        else:
            self.internet.append(ms)

    def add_ping(self, internet_ms, lan_ms, isp_ms, lan_probed: bool, node_ms=None, node_probed: bool = False) -> None:
        """Record one ping round (None = lost)."""
        self._add_internet(internet_ms)
        if internet_ms is None:
            self.lost_tracked += 1
            if lan_probed and lan_ms is None:
                self.co_lost += 1
                if node_probed:
                    if node_ms is None:
                        self.co_node_lost += 1
                    else:
                        self.co_node_ok += 1
        if lan_probed:
            self.lan_rounds += 1
            if lan_ms is None:
                self.lan_lost += 1
            else:
                self.lan.append(lan_ms)
        if isp_ms is not None:  # the ISP gateway rate-limits replies, so its losses mean nothing
            self.isp.append(isp_ms)

    def add_eero(self, devices: list[dict]) -> None:
        """Record one eero poll (parsed, connected devices)."""
        self.eero_polls += 1
        for d in devices:
            if d.get("down_mbps") is None and d.get("up_mbps") is None:
                continue
            rec = self.devices.setdefault(d["mac"], {"info": d, "down": [], "up": []})
            rec["info"] = d
            rec["down"].append(d.get("down_mbps") or 0.0)
            rec["up"].append(d.get("up_mbps") or 0.0)

    def add_pc(self, sample: dict) -> None:
        """Record one 10-second sample from this PC."""
        self.pc_secs += sample["secs"]
        self.pc_rx += sample["rx"]
        self.pc_tx += sample["tx"]
        for app, (rx, tx) in sample["apps"].items():
            self.apps[app][0] += rx
            self.apps[app][1] += tx

    # ---------------------------------------------------------------- summary

    def summarize(self, end_ts: float, capacity: dict | None, eero_connected: bool,
                  my_macs: set[str], per_app_available: bool, path: dict | None = None) -> dict:
        """Work out what kind of slowdown this was, who was hogging, and say it plainly.

        `path` is the mesh layout from the collector (eero names), used for wording.
        """
        path = path or {}
        gw = path.get("gateway") or "main"
        duration = max(end_ts - self.start_ts, 1)
        loss_pct = 100.0 * self.lost / self.rounds if self.rounds else 0.0
        med = statistics.median(self.internet) if self.internet else None
        peak = max(self.internet) if self.internet else None
        lan_med = statistics.median(self.lan) if self.lan else None
        lan_peak = max(self.lan) if self.lan else None
        lan_loss = 100.0 * self.lan_lost / self.lan_rounds if self.lan_rounds else 0.0
        isp_med = statistics.median(self.isp) if self.isp else None

        # Where did it break? If the main eero also stopped answering while the internet
        # pings were lost, the break was inside the house -- and if this PC's own eero
        # kept answering, it was the link between the eeros. Averages can't show this:
        # a 5-second total dropout barely moves the median ping.
        home_loss = self.lost_tracked > 0 and self.co_lost >= 0.6 * self.lost_tracked
        link_loss = home_loss and self.co_node_ok > 0 and self.co_node_ok >= self.co_node_lost
        if link_loss:
            kind = "eero_link"
        elif loss_pct >= 80:
            kind = "home_outage" if lan_loss >= 80 else "outage"
        elif home_loss or (lan_med is not None and lan_med > 40) or lan_loss >= 30:
            kind = "home"
        else:
            kind = "congested"

        suspects = self._suspects(capacity, my_macs)
        pc = self._pc_summary(per_app_available)

        dur = fmt_duration(duration)
        pings = f"{self.lost} ping{'' if self.lost == 1 else 's'} lost"
        if kind == "eero_link":
            head = (f"The connection dropped out {where_text('eero_link', path) if path.get('node') else 'between your eeros'} "
                    f"({pings}, {dur} in all). This PC's own eero kept answering but the {gw} eero didn't, "
                    "so the break was inside the house, not at AT&T")
        elif kind == "outage":
            head = f"Internet dropped out for {dur}. The eero was fine, so the AT&T line or gateway lost its connection"
        elif kind == "home_outage":
            head = f"This PC lost contact with the eero for {dur}"
        elif kind == "home" and (home_loss or lan_loss >= 30):
            head = (f"The home network dropped out ({self.lan_lost} pings to the {gw} eero lost, {dur} in all), "
                    "so the problem was inside the house, not at AT&T")
        elif kind == "home":
            head = (f"The home network bogged down for {dur}. The eero itself took up to "
                    f"{lan_peak:.0f} ms to answer (normally a few ms)" if lan_peak is not None else
                    f"The home network bogged down for {dur}")
        else:
            head = (f"Internet crawled for {dur}. Ping was {med:.0f} ms, peaking at {peak:.0f} ms "
                    f"(normal is {self.normal_ms:.0f} ms)" if med is not None else f"Internet crawled for {dur}")

        notes: list[str] = []
        heavy = [s for s in suspects if s["heavy"]]
        if heavy:
            top = heavy[0]
            head += f". Biggest user: {top['label']}, {top['doing']}"
            if top.get("share_pct"):
                head += f" ({top['share_pct']:.0f}% of your {top['share_dir']} speed)"
            if top["this_pc"] and pc["apps"]:
                head += f", mostly {pc['apps'][0]['app']}"
            head += "."
        elif eero_connected and self.eero_polls:
            # Don't pin it on whoever happened to be busiest if they weren't actually heavy.
            busiest = f" (busiest: {suspects[0]['label']} at {fmt_mbps(suspects[0]['down_mbps'] + suspects[0]['up_mbps'])} Mbps)" if suspects else ""
            if kind == "eero_link":
                head += f". Nobody was using much bandwidth{busiest}, so the link itself is dropping out, not someone hogging it."
            elif kind == "home":
                head += f". Nobody was using much bandwidth{busiest}, so the eero or Wi-Fi itself was struggling."
            elif kind in ("outage", "home_outage"):
                head += "."
            else:
                head += (f". Nobody at home was using much bandwidth{busiest}, so the slowdown most likely "
                         "came from AT&T's side.")
        else:
            pc_total = pc["down_mbps"] + pc["up_mbps"]
            if pc_total < 2:
                head += (f". This PC was barely using the internet ({fmt_mbps(pc_total)} Mbps), "
                         "so another device caused it.")
            elif pc["apps"]:
                a = pc["apps"][0]
                head += f". On this PC, {a['app']} was using {fmt_mbps(a['down_mbps'] + a['up_mbps'])} Mbps."
            else:
                head += f". This PC was using {fmt_mbps(pc_total)} Mbps."
            if not eero_connected:
                notes.append("Other devices can't be seen until HogWatch is connected to the eero (see Setup).")

        if kind == "eero_link":
            notes.append("If this keeps happening, check that both ends of the cable between the eeros are plugged in."
                         if path.get("wired") else
                         "The fix is an Ethernet cable between the two eeros (a wired backhaul), or moving them closer together.")
        if kind == "home":
            notes.append("The eero itself was slow to answer. That usually means the eero was overloaded "
                         "or the wireless link between eeros was congested, rather than the AT&T line.")
        if kind == "congested" and isp_med is not None and isp_med > self.normal_ms + 50:
            notes.append(f"The AT&T gateway was slow too (ping {isp_med:.0f} ms), so the jam was at "
                         "the gateway or earlier.")

        return {
            "kind": kind,
            "peak_ms": peak,
            "loss_pct": round(loss_pct, 1),
            "headline": head,
            "details": {
                "duration_s": round(duration),
                "normal_ms": round(self.normal_ms, 1),
                "median_ms": med, "peak_ms": peak, "loss_pct": round(loss_pct, 1),
                "lan_median_ms": lan_med, "lan_peak_ms": lan_peak, "lan_loss_pct": round(lan_loss, 1),
                "isp_median_ms": isp_med,
                "suspects": suspects, "pc": pc,
                "eero_connected": eero_connected, "eero_polls": self.eero_polls,
                "capacity": capacity, "notes": notes,
            },
        }

    def _suspects(self, capacity: dict | None, my_macs: set[str]) -> list[dict]:
        """eero devices ranked by how much of the line they used during the slowdown."""
        cap_down = (capacity or {}).get("down")
        cap_up = (capacity or {}).get("up")
        out = []
        for mac, rec in self.devices.items():
            down = statistics.fmean(rec["down"]) if rec["down"] else 0.0
            up = statistics.fmean(rec["up"]) if rec["up"] else 0.0
            if down + up < 1.0:
                continue
            info = rec["info"]
            share_down = down / cap_down if cap_down else None
            share_up = up / cap_up if cap_up else None
            if share_down is not None or share_up is not None:
                impact = max(share_down or 0, share_up or 0)
            else:
                impact = (down + up) / 1000  # no plan speed known: rank by raw volume
            upload_led = (share_up or 0) > (share_down or 0) if (share_up or share_down) else up > down
            doing = f"uploading {fmt_mbps(up)} Mbps" if upload_led else f"downloading {fmt_mbps(down)} Mbps"
            # "Heavy" = enough to plausibly fill the line. With a known plan speed that's
            # 40%+ of it (readings are 10-30s snapshots, so the true peak was likely higher).
            if cap_down or cap_up:
                heavy = impact >= 0.4
            else:
                heavy = down >= 25 or up >= 5
            label = info["name"] + (f" ({info['owner']})" if info.get("owner") else "")
            if mac in my_macs:
                label += " (this PC)"
            out.append({
                "mac": mac, "label": label, "name": info["name"], "owner": info.get("owner"),
                "kind": info.get("kind"), "this_pc": mac in my_macs,
                "down_mbps": round(down, 2), "up_mbps": round(up, 2),
                "peak_down_mbps": round(max(rec["down"]), 2), "peak_up_mbps": round(max(rec["up"]), 2),
                "doing": doing, "guess": guess_activity(info.get("kind") or "", down, up),
                "share_pct": round(100 * impact, 0) if (cap_down or cap_up) else None,
                "share_dir": "upload" if upload_led else "download",
                "heavy": heavy,
                "_impact": impact,
            })
        out.sort(key=lambda s: s["_impact"], reverse=True)
        for s in out:
            del s["_impact"]
        return out[:6]

    def _pc_summary(self, per_app_available: bool) -> dict:
        """This PC's average rates during the slowdown, and its top programs."""
        secs = self.pc_secs or 1

        def mbps(b):
            return round(b * 8 / secs / 1e6, 2)

        apps = sorted(
            ({"app": a, "down_mbps": mbps(rx), "up_mbps": mbps(tx)} for a, (rx, tx) in self.apps.items()),
            key=lambda x: x["down_mbps"] + x["up_mbps"], reverse=True)
        apps = [a for a in apps if a["down_mbps"] + a["up_mbps"] >= 0.2][:6]
        return {"down_mbps": mbps(self.pc_rx), "up_mbps": mbps(self.pc_tx), "apps": apps,
                "per_app_available": per_app_available, "measured": self.pc_secs > 0}
