"""A technical report for whoever installed or services the eero system.

It is built only from measurements of the network itself: ping timing to each eero
and each network hop, the eeros' own status (how they link to each other, their
radios, Ethernet ports, reconnects) and their speed tests. It deliberately reads no
device names, people, programs or games, so it can be sent to an outside company.
"""

from __future__ import annotations

import html
import ipaddress
import json
import shutil
import statistics
import subprocess
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from .db import HICCUP_END
from .report import _clock, _dur

# 5 GHz channels 52-144 must vacate for radar (DFS); a wide channel touching any of
# them can be forced to move or pause without warning.
DFS_CHANNELS = set(range(52, 145, 4))

E = html.escape


def channel_block(channel: int, width: int) -> list[int]:
    """The 20 MHz channels a 5 GHz channel of `width` MHz (primary `channel`) occupies."""
    if not channel or not width or width <= 20:
        return [channel] if channel else []
    n = width // 20
    base = 36 if channel < 100 else (100 if channel < 149 else 149)
    start = base + ((channel - base) // (4 * n)) * 4 * n
    return [start + 4 * i for i in range(n)]


def touches_dfs(band: str, channel: int, width: int) -> bool:
    """True if a 5 GHz channel's span includes radar-shared (DFS) channels."""
    return band.startswith("5") and any(c in DFS_CHANNELS for c in channel_block(channel, width))


def _pct(sorted_vals: list[float], p: float) -> float | None:
    if not sorted_vals:
        return None
    return sorted_vals[min(len(sorted_vals) - 1, int(round(p / 100 * (len(sorted_vals) - 1))))]


def hop_stats(db, since: float) -> dict[str, dict]:
    """Per ping target: typical and 95th-percentile delay (of 10-second averages), jitter, loss."""
    by = defaultdict(lambda: {"avgs": [], "sent": 0, "lost": 0, "jit": []})
    for role, avg, sent, lost, jit in db.rows(
            "SELECT role, avg_ms, sent, lost, jitter_ms FROM latency WHERE ts >= ?", (since,)):
        d = by[role]
        d["sent"] += sent
        d["lost"] += lost
        if avg is not None:
            d["avgs"].append(avg)
        if jit is not None:
            d["jit"].append(jit)
    out = {}
    for role, d in by.items():
        avgs = sorted(d["avgs"])
        out[role] = {"median": statistics.median(avgs) if avgs else None, "p95": _pct(avgs, 95),
                     "jitter": statistics.fmean(d["jit"]) if d["jit"] else None,
                     "loss_pct": 100.0 * d["lost"] / d["sent"] if d["sent"] else None, "sent": d["sent"]}
    return out


def _bar_chart(labels: list[str], values: list[float], title: str, fmt=lambda v: f"{v:g}") -> str:
    """A small single-series bar chart as inline SVG (prints cleanly in a PDF)."""
    w, h, left, bottom, top = 700, 190, 40, 30, 12
    vmax = max(values) if values and max(values) > 0 else 1
    step = next(s for s in (1, 2, 5, 10, 20, 25, 50, 100, 200, 500, 1000, 1e9) if vmax / s <= 5)
    top_tick = step * (int(vmax / step) + (1 if vmax % step else 0))
    plot_w, plot_h = w - left - 8, h - top - bottom
    slot = plot_w / max(len(values), 1)
    bar = min(24, slot * 0.7)
    y = lambda v: top + plot_h - (v / top_tick) * plot_h  # noqa: E731
    parts = [f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="{E(title)}" '
             f'style="max-width:{w}px;font:11px Segoe UI,system-ui,sans-serif">']
    t = 0.0
    while t <= top_tick + 1e-9:
        parts.append(f'<line x1="{left}" x2="{w - 8}" y1="{y(t):.1f}" y2="{y(t):.1f}" stroke="{"#c3c2b7" if t == 0 else "#e1e0d9"}"/>'
                     f'<text x="{left - 6}" y="{y(t) + 4:.1f}" text-anchor="end" fill="#6f6d68">{fmt(t)}</text>')
        t += step
    every = max(1, int(len(labels) / 14 + 0.999))
    for i, (lab, v) in enumerate(zip(labels, values)):
        x = left + i * slot + (slot - bar) / 2
        if v > 0:
            parts.append(f'<rect x="{x:.1f}" y="{y(v):.1f}" width="{bar:.1f}" height="{y(0) - y(v):.1f}" fill="#2a78d6">'
                         f'<title>{E(lab)}: {E(fmt(v))}</title></rect>')
        if i % every == 0:
            parts.append(f'<text x="{x + bar / 2:.1f}" y="{h - 10}" text-anchor="middle" fill="#6f6d68">{E(lab)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _route_label(where_text: str) -> str:
    """'...eeros, 5 GHz' -> 'direct over 5 GHz'; '...(Upstairs → Den → Main)' -> 'via Upstairs → Den → Main'."""
    if "(" in where_text and where_text.endswith(")"):
        return "via " + where_text[where_text.index("(") + 1:-1]
    if "cable" in where_text:
        return "direct by cable"
    if where_text.rsplit(", ", 1)[-1].endswith("GHz"):
        return "direct over " + where_text.rsplit(", ", 1)[-1]
    return "direct"


def _ms(v) -> str:
    return "–" if v is None else ("<1 ms" if v < 1 else f"{v:.0f} ms")


def build_installer_report(db, days: float = 7, note: str = "", now: float | None = None) -> str:
    """The full report as one self-contained HTML page (print it, or see save_pdf)."""
    now = now or time.time()
    since = now - days * 86400
    path = json.loads(db.get_meta("eero_path") or "null") or {}
    targets = json.loads(db.get_meta("targets") or "null") or {}
    node, gw = path.get("node"), path.get("gateway") or "main"

    # Coverage: the PC isn't always on, so rates are per monitored hour, not per calendar hour.
    covered = [r[0] for r in db.rows(
        "SELECT ts FROM latency WHERE role = 'internet' AND ts >= ? ORDER BY ts", (since,))]
    monitored_h = len(covered) * 10 / 3600
    first_ts = covered[0] if covered else since
    hours_by_day: Counter = Counter()
    hours_by_hod: Counter = Counter()
    for ts in covered:
        d = datetime.fromtimestamp(ts)
        hours_by_day[d.date()] += 10 / 3600
        hours_by_hod[d.hour] += 10 / 3600

    hics = db.query(f"SELECT start_ts, {HICCUP_END} AS end_ts, where_, where_text, lost, worst_ms FROM hiccup "
                    "WHERE start_ts >= ? ORDER BY start_ts", (since,))
    # A dropout can only be placed on the eero link if the main eero was being pinged at
    # the time; if it wasn't (e.g. HogWatch started before the network was up), say so
    # rather than letting it read as an internet-side problem.
    lan_buckets = {r[0] for r in db.rows("SELECT ts FROM latency WHERE role = 'lan' AND ts >= ?", (since - 20,))}
    for h in hics:
        b = int(h["start_ts"]) // 10 * 10
        if h["where_"] != "eero_link" and not ({b, b - 10, b + 10} & lan_buckets):
            h["where_"], h["where_text"] = "unknown", "location not measured (the main eero wasn't being pinged then)"
    link = [h for h in hics if h["where_"] == "eero_link"]
    beyond = [h for h in hics if h["where_"] in ("internet", "att")]
    events = db.query("SELECT ts, unit FROM eero_event WHERE ts >= ? ORDER BY ts", (int(since),))
    samples = db.query("SELECT * FROM unit_sample WHERE ts >= ? ORDER BY ts", (int(since),))
    links_pc = db.query("SELECT ts, what, detail FROM link_event WHERE ts >= ? ORDER BY ts", (int(since),))
    tests = db.query("SELECT date, ts, down, up FROM speedtest WHERE ts >= ? ORDER BY ts", (int(since),))
    if not tests and db.get_meta("eero_speed"):
        s = json.loads(db.get_meta("eero_speed"))
        tests = [{"date": s.get("date"), "ts": None, "down": s.get("down"), "up": s.get("up")}]
    hops = hop_stats(db, since)

    latest: dict[str, dict] = {}
    for s in samples:
        latest[s["unit"]] = s
    units_info = {u["name"]: u for u in path.get("units") or []}

    # ---- evidence derived from the eero snapshots
    port_hist: dict[tuple, dict] = {}
    radio_hist: dict[tuple, dict] = {}
    channel_changes, backhaul_changes = [], []
    prev_chan: dict[tuple, tuple] = {}
    prev_bh: dict[str, tuple] = {}
    for s in samples:
        for p in json.loads(s["ports"] or "[]"):
            k = (s["unit"], p["port"])
            ph = port_hist.setdefault(k, {"checks": 0, "linked": 0, "wan": p.get("wan"), "speeds": Counter()})
            ph["checks"] += 1
            if p.get("carrier"):
                ph["linked"] += 1
                ph["speeds"][p.get("speed")] += 1
        for band, b in json.loads(s["bands"] or "{}").items():
            k = (s["unit"], band)
            rh = radio_hist.setdefault(k, {"util": [], "clients": []})
            if b.get("utilization") is not None:
                rh["util"].append(b["utilization"])
            if b.get("clients") is not None:
                rh["clients"].append(b["clients"])
            cw = (b.get("channel"), b.get("width"))
            if k in prev_chan and prev_chan[k] != cw:
                channel_changes.append((s["ts"], s["unit"], band, prev_chan[k], cw))
            prev_chan[k] = cw
        bh = (bool(s["wired"]), s["radio"], s["upstream"])
        if s["unit"] in prev_bh and prev_bh[s["unit"]] != bh:
            backhaul_changes.append((s["ts"], s["unit"], prev_bh[s["unit"]], bh))
        prev_bh[s["unit"]] = bh

    reconnects = Counter(e["unit"] for e in events)
    relays = [n for n, u in units_info.items() if u.get("upstream") == node] if node else []
    node_now = latest.get(node) if node else None
    node_bands = json.loads(node_now["bands"]) if node_now else {}
    bh_band = (path.get("radio") or "").replace(" ", "")  # e.g. "5GHz"
    bh_stats = next((b for name, b in node_bands.items() if name.replace(" ", "").startswith(bh_band)), None) if bh_band else None
    isp_ip = targets.get("isp") or db.get_meta("isp_seen")
    double_nat = bool(isp_ip) and ipaddress.ip_address(isp_ip).is_private
    watch_since = db.get_meta("link_watch_since")
    jitter_since = (db.rows("SELECT MIN(ts) FROM latency WHERE jitter_ms IS NOT NULL AND ts >= ?", (since,)) or [[None]])[0][0]
    dead_ports = [(u, p, ph) for (u, p), ph in port_hist.items()
                  if u in (node, gw) and not ph["wan"] and ph["linked"] == 0 and ph["checks"] >= 3]

    # ---- findings (plain sentences, numbers first)
    findings: list[str] = []
    span_days = max(monitored_h / 24, 1e-9)
    if node and not path.get("wired") and link:
        longest = max(h["end_ts"] - h["start_ts"] for h in link)
        mins = sum(h["end_ts"] - h["start_ts"] for h in link) / 60
        findings.append(
            f"<b>The wireless link between the {E(node)} and {E(gw)} eeros keeps dropping.</b> "
            f"{len(link)} dropouts in {monitored_h:.0f} monitored hours (about {len(link) / span_days:.0f} per day), "
            f"{mins:.1f} minutes offline in total, the longest {_dur(longest)}. During every one, the PC's own "
            f"{E(node)} eero kept answering over its cable while the {E(gw)} eero and everything beyond it went "
            f"silent, so the failure is on that eero-to-eero link, not the PC, the cabling at the PC, or AT&T.")
    # The route each link dropout was on (recorded at the time): a mesh that keeps
    # re-routing its satellite is a sign the direct link is marginal.
    routes = Counter(_route_label(h["where_text"] or "") for h in link)
    if len(routes) > 1:
        listed = ", ".join(f"{E(r)} ({n})" for r, n in routes.most_common())
        findings.append(f"<b>The {E(node)} eero's route to the {E(gw)} eero kept changing</b>, so eero itself is "
                        f"struggling to find a stable link. Routes in use when dropouts happened: {listed}. A healthy "
                        "mesh keeps one stable route; switching like this happens when the direct link is marginal.")
    if reconnects:
        parts = ", ".join(f"{E(u)} {n}×" for u, n in reconnects.most_common())
        findings.append(f"<b>eero units repeatedly lost their connection and had to reconnect</b> ({parts} in this period). "
                        + (f"The {E(', '.join(relays))} eero{'s' if len(relays) != 1 else ''} relay"
                           f"{'' if len(relays) != 1 else 's'} through the {E(node)} eero, so {'they drop' if len(relays) != 1 else 'it drops'} with it."
                           if relays else ""))
    if dead_ports:
        lines = "; ".join(f"{E(u)} eero port {E(p)}: no link in {ph['checks']} of {ph['checks']} checks" for u, p, ph in dead_ports)
        findings.append(f"<b>The Ethernet backhaul between the eeros is not working.</b> {lines}. A cable plugged in at "
                        "both ends that never shows a link means the run is open or damaged, wired to the wrong "
                        "pins, or is a different cable than assumed. With a working cable, eero would switch the "
                        f"{E(node or 'satellite')} eero to a wired link automatically.")
    if node and not path.get("wired"):
        radio = path.get("radio") or "wireless"
        txt = f"<b>The {E(node)}–{E(gw)} hop runs on {E(radio)}"
        if bh_stats:
            txt += f", channel {bh_stats.get('channel')} at {bh_stats.get('width')} MHz"
        txt += ".</b> "
        if "5" in radio and any(b.startswith("6") for b in node_bands):
            txt += ("Both eeros have 6 GHz radios, but eero isn't using 6 GHz for this hop, which usually means "
                    "the distance or the floors between them are too much for 6 GHz. ")
        if bh_stats and touches_dfs("5 GHz", bh_stats.get("channel"), bh_stats.get("width")):
            blk = channel_block(bh_stats["channel"], bh_stats["width"])
            txt += (f"That {bh_stats['width']} MHz channel spans channels {blk[0]}–{blk[-1]}, which include radar-shared "
                    "(DFS) channels: when radar is detected there, the radio has to stop and move, which drops the "
                    "link for several seconds or more. ")
        if relays:
            txt += f"The {E(', '.join(relays))} eero also relays through this link, adding its traffic to it."
        findings.append(txt)
    nh, lh = hops.get("node"), hops.get("lan")
    if nh and lh and nh["sent"]:
        findings.append(f"<b>Measured from the wired PC:</b> the {E(node)} eero answered {100 - (nh['loss_pct'] or 0):.2f}% "
                        f"of pings (typical {_ms(nh['median'])}, jitter {_ms(nh['jitter'])}); the {E(gw)} eero, one "
                        f"hop further, answered {100 - (lh['loss_pct'] or 0):.2f}% (typical {_ms(lh['median'])}, "
                        f"jitter {_ms(lh['jitter'])}).")
    if tests:
        t = tests[-1]
        findings.append(f"<b>The internet line itself is healthy:</b> the eero's speed test measured "
                        f"{t['down']:.0f} Mbps down / {t['up']:.0f} Mbps up. {len(beyond)} of {len(hics)} dropouts "
                        "happened past the eeros, on the AT&T side.")
    if double_nat:
        findings.append(f"<b>Two routers in a row (double NAT).</b> The first hop past the eeros is {E(isp_ip)}, a private "
                        "address: the AT&T gateway is still routing in front of the eero. AT&T gateways are normally set to "
                        "IP Passthrough for an eero system; this also removes a second place for connections to stall.")
    if watch_since and not links_pc:
        findings.append("<b>The PC's own connection was solid:</b> its network cable to the "
                        f"{E(node or gw)} eero hasn't gone down since {E(_clock(max(float(watch_since), first_ts)))}.")

    work = []
    if node and not path.get("wired"):
        work.append(f"<b>Repair the Ethernet run between the {E(gw)} and the {E(node)}.</b> Test it end to end with a cable "
                    f"tester or certifier; confirm the cable at the {E(gw)} eero is the one that reaches the {E(node)} "
                    "jack; re-terminate both ends to the same standard (T568B) or replace the run. Connect it to a free port on each "
                    f"eero. Done when the eero app shows the {E(node)} eero as <i>Wired</i>.")
        work.append(f"<b>If the run can't be repaired:</b> move the main eero to a more central floor so the {E(node)} "
                    "hop is shorter (and can use 6 GHz), or provide a wired path another way (MoCA over coax, checking "
                    "compatibility with any satellite TV on that coax, or powerline adapters).")
        work.append("<b>Review the wireless channel</b> if any wireless hop remains: avoid a 160 MHz channel that overlaps "
                    "radar-shared (DFS) channels on a link that must not drop.")
    if double_nat:
        work.append("<b>Set the AT&T gateway to IP Passthrough</b> for the eero, so there is only one router.")
    if relays:
        work.append(f"<b>Re-check the mesh layout afterwards:</b> the {E(', '.join(relays))} eero should link directly to a "
                    f"wired eero rather than relaying through the {E(node)} eero.")

    # ---- tables
    def row(*cells, head=False) -> str:
        tag = "th" if head else "td"
        return "<tr>" + "".join(f"<{tag}>{c}</{tag}>" for c in cells) + "</tr>"

    hop_rows = []
    labels = [("node", f"{E(node)} eero (PC's cable)" if node else None), ("lan", f"{E(gw)} eero (main)")]
    labels += [(f"unit:{n}", f"{E(n)} eero") for n in sorted(units_info) if f"unit:{n}" in hops]
    labels += [("isp", f"AT&T gateway{f' ({E(isp_ip)})' if isp_ip else ''}"), ("internet", "Internet (1.1.1.1 / 8.8.8.8)")]
    for role, lab in labels:
        st = hops.get(role)
        if not lab or not st or not st["sent"]:
            continue
        loss = "n/a (replies rate-limited)" if role == "isp" else f"{st['loss_pct']:.2f}%"
        hop_rows.append(row(lab, _ms(st["median"]), _ms(st["p95"]), _ms(st["jitter"]), loss, f"{st['sent']:,}"))

    unit_rows = []
    is_gateway = lambda n: bool((latest.get(n) or units_info.get(n) or {}).get("gateway"))  # noqa: E731
    for name in sorted(set(latest) | set(units_info), key=lambda n: (not is_gateway(n), n)):
        s = latest.get(name)
        u = units_info.get(name, {})
        if s:
            if s["gateway"]:
                how = "Main eero, cable to AT&T"
            elif s["wired"]:
                how = "Cable"
            else:
                how = f"Wireless{f' ({E(s['radio'])})' if s['radio'] else ''} to {E(s['upstream'] or gw)}"
            unit_rows.append(row(E(name), E(u.get("model") or ""), how,
                                 f"{s['bars'] if s['bars'] is not None else '–'}/5",
                                 f"{s['wired_clients'] or 0} wired, {s['wireless_clients'] or 0} wireless",
                                 E(s["firmware"] or "–"), E(u.get("ip") or "–"), f"{reconnects.get(name, 0)}"))
        else:
            unit_rows.append(row(E(name), E(u.get("model") or ""), "Wireless" if not u.get("wired") else "Cable",
                                 "–", "–", "–", E(u.get("ip") or "–"), f"{reconnects.get(name, 0)}"))

    port_rows = [row(E(u), E(p), "AT&T (internet)" if ph["wan"] else "LAN",
                     f"{ph['linked']} of {ph['checks']}" + (" — <b>never linked</b>" if ph["linked"] == 0 and not ph["wan"] else ""),
                     ", ".join(f"{s} Mbps" for s in ph["speeds"] if s) or "–")
                 for (u, p), ph in sorted(port_hist.items())]

    radio_rows = []
    for name, s in sorted(latest.items()):
        for band, b in json.loads(s["bands"] or "{}").items():
            rh = radio_hist.get((name, band), {"util": [], "clients": []})
            dfs = " (includes DFS)" if touches_dfs(band, b.get("channel"), b.get("width")) else ""
            radio_rows.append(row(E(name), E(band), f"{b.get('channel')}", f"{b.get('width')} MHz{dfs}",
                                  f"{statistics.fmean(rh['util']):.0f}% / {max(rh['util'])}%" if rh["util"] else "–",
                                  f"{b.get('tx_power') if b.get('tx_power') is not None else '–'}"))

    # ---- charts: dropouts on the eero link per day and by hour of day (per monitored hour)
    days_sorted = sorted(hours_by_day)
    per_day = Counter(datetime.fromtimestamp(h["start_ts"]).date() for h in link)
    day_chart = _bar_chart([d.strftime("%a %d") for d in days_sorted], [per_day.get(d, 0) for d in days_sorted],
                           "Dropouts on the eero link per day")
    per_hod = Counter(datetime.fromtimestamp(h["start_ts"]).hour for h in link)
    hod_vals = [per_hod.get(h, 0) / hours_by_hod[h] if hours_by_hod.get(h, 0) >= 0.5 else 0 for h in range(24)]
    hod_chart = _bar_chart([f"{(h % 12) or 12}{'a' if h < 12 else 'p'}" for h in range(24)], hod_vals,
                           "Eero-link dropouts per monitored hour, by time of day", fmt=lambda v: f"{v:.1f}")
    day_rows = [row(d.strftime("%a %b %d"), f"{hours_by_day[d]:.1f} h", f"{per_day.get(d, 0)}",
                    f"{sum(1 for h in beyond if datetime.fromtimestamp(h['start_ts']).date() == d)}") for d in days_sorted]
    where_counts = Counter(h["where_text"] for h in hics)
    longest_rows = [row(E(_clock(h["start_ts"])), E(_dur(h["end_ts"] - h["start_ts"])), f"{h['lost']}", E(h["where_text"] or ""))
                    for h in sorted(hics, key=lambda h: h["end_ts"] - h["start_ts"], reverse=True)[:10]]
    all_rows = [row(E(_clock(h["start_ts"])), E(_dur(h["end_ts"] - h["start_ts"])), f"{h['lost']}",
                    _ms(h["worst_ms"]) if h["worst_ms"] else "–", E(h["where_text"] or "")) for h in hics]

    chain = " → ".join(["PC", *[f"{n} eero" for n in path.get("chain") or []], "AT&T gateway", "internet"])
    period = f"{_clock(first_ts)} to {_clock(now)}"
    note_html = E(note).replace("\n", "<br>") if note else ""

    def table(head: list[str], rows: list[str]) -> str:
        return f'<table>{row(*head, head=True)}{"".join(rows)}</table>' if rows else '<p class="muted">No data yet.</p>'

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Network diagnostic report: eero mesh</title>
<style>
  @page {{ margin: 14mm; }}
  body {{ font: 13.5px/1.5 "Segoe UI", system-ui, sans-serif; color: #0b0b0b; background: #fff; margin: 0 auto; max-width: 900px; padding: 24px 16px; }}
  h1 {{ font-size: 24px; margin: 0 0 4px; }} h2 {{ font-size: 17px; margin: 28px 0 8px; border-bottom: 1px solid #c3c2b7; padding-bottom: 4px; }}
  h3 {{ font-size: 14px; margin: 16px 0 6px; }}
  .muted {{ color: #52514e; }} .meta {{ color: #52514e; margin: 0 0 16px; }}
  ul, ol {{ padding-left: 22px; }} li {{ margin: 6px 0; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 12.5px; margin: 6px 0 10px; }}
  th, td {{ text-align: left; padding: 5px 7px; border-bottom: 1px solid #e1e0d9; vertical-align: top; }}
  th {{ color: #52514e; font-weight: 600; border-bottom-color: #c3c2b7; }}
  .path {{ font-weight: 600; background: #f3f2ee; padding: 8px 12px; border-radius: 6px; display: inline-block; }}
  .notes {{ border: 1px solid #c3c2b7; border-radius: 6px; padding: 10px 12px; min-height: 2.5em; white-space: pre-wrap; }}
  .notes:empty::before {{ content: attr(data-placeholder); color: #898781; }}
  .toolbar {{ display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin-bottom: 16px; }}
  .toolbar button, .toolbar a {{ font: inherit; padding: 6px 12px; border-radius: 6px; border: 1px solid #c3c2b7; background: #f3f2ee; color: #0b0b0b; text-decoration: none; cursor: pointer; }}
  .toolbar button {{ background: #0b0b0b; color: #fff; border-color: #0b0b0b; }}
  .appendix {{ break-before: page; }}
  @media print {{ .toolbar, .notes-section:has(.notes:empty) {{ display: none; }} body {{ padding: 0; }} h2 {{ break-after: avoid; }} tr {{ break-inside: avoid; }} }}
</style></head><body>
<div class="toolbar"><button type="button" onclick="window.print()">Print or save as PDF</button>
  <span class="muted">Period:</span><a href="?days=3">3 days</a><a href="?days=7">7 days</a><a href="?days=14">14 days</a></div>
<h1>Network diagnostic report: eero mesh</h1>
<p class="meta">{E(period)} · {monitored_h:.0f} hours of monitoring · generated {E(_clock(now))} by HogWatch</p>

<h2>Summary</h2>
<ul>{"".join(f"<li>{f}</li>" for f in findings) or "<li>No problems recorded in this period.</li>"}</ul>

<section class="notes-section"><h2>Notes from the homeowner</h2>
<div class="notes" contenteditable="true" data-placeholder="Click here to add notes before printing (for example: both ends of the cable between the eeros are plugged in).">{note_html}</div></section>

<h2>Requested work</h2>
{"<ol>" + "".join(f"<li>{w}</li>" for w in work) + "</ol>" if work else '<p class="muted">None: no wiring or layout problems were found.</p>'}

<h2>Network layout</h2>
<p class="path">{E(chain)}</p>
{table(["eero", "Model", "Link to the network", "Signal", "Devices now", "Firmware", "IP", "Reconnects"], unit_rows)}

<h2>Measurements from the wired PC</h2>
<p class="muted">One small ping per second to each point on the path. Typical and 95th percentile are taken over 10-second
averages; jitter is the average change between consecutive pings (games and voice calls need it low){
f", recorded since {E(_clock(jitter_since))}" if jitter_since and jitter_since > first_ts + 3600 else ""}.</p>
{table(["Ping target", "Typical", "95th pct", "Jitter", "Lost", "Pings"], hop_rows)}

<h2>Dropouts</h2>
<p class="muted">A dropout is any stretch where the internet stops answering or slows sharply. Its location is the first hop
on the path that stopped answering.</p>
{table(["Where", "Count"], [row(E(w), f"{n}") for w, n in where_counts.most_common()])}
<h3>Dropouts on the eero link, per day</h3>{day_chart}
{table(["Day", "Monitored", "On the eero link", "Past the eeros"], day_rows)}
<h3>By time of day (dropouts per monitored hour)</h3>{hod_chart}
<h3>Longest dropouts</h3>
{table(["When", "Length", "Pings lost", "Where"], longest_rows)}

<h2>eero reconnects</h2>
<p class="muted">Each eero reports when it last (re)connected to eero's servers; a new time means its link dropped, even if
that happened between pings.</p>
{table(["When", "eero"], [row(E(_clock(e["ts"])), E(e["unit"])) for e in events[-40:]])}
{f'<p class="muted">Showing the latest 40 of {len(events)}.</p>' if len(events) > 40 else ""}

<h2>Ethernet ports</h2>
<p class="muted">Each eero's ports, checked every 2 minutes{f" since {E(_clock(samples[0]['ts']))}" if samples else ""}.</p>
{table(["eero", "Port", "Use", "Checks with a link", "Link speed"], port_rows)}

<h2>Radios and channels</h2>
{table(["eero", "Band", "Channel", "Width", "Busy (avg / max)", "Tx power"], radio_rows)}
{"<h3>Channel changes</h3>" + table(["When", "eero", "Band", "From", "To"], [row(E(_clock(t)), E(u), E(b), f"ch {a[0]} / {a[1]} MHz", f"ch {c[0]} / {c[1]} MHz") for t, u, b, a, c in channel_changes]) if channel_changes else ""}
{"<h3>Backhaul changes</h3>" + table(["When", "eero", "From", "To"], [row(E(_clock(t)), E(u), "cable" if a[0] else f"{E(a[1] or 'wireless')} to {E(a[2] or '?')}", "cable" if c[0] else f"{E(c[1] or 'wireless')} to {E(c[2] or '?')}") for t, u, a, c in backhaul_changes]) if backhaul_changes else ""}

<h2>Internet line</h2>
{table(["eero speed test", "Download", "Upload"], [row(E(t["date"] or "–"), f"{t['down']:.0f} Mbps", f"{t['up']:.0f} Mbps") for t in tests[-8:]])}
{"" if not links_pc else "<h3>PC network-card events</h3>" + table(["When", "Event"], [row(E(_clock(l["ts"])), E(l["what"])) for l in links_pc])}

<h2>How this was measured</h2>
<p class="muted">HogWatch runs on a PC wired to the {E(node or gw)} eero. It sends one small ping per second to each eero,
the AT&T gateway and two internet servers, and reads each eero's own status from eero's cloud service (the same data the
eero app shows) every 2 minutes. It records timing and link status only: no traffic contents, browsing or personal
information are included in this report.</p>

<section class="appendix"><h2>Appendix: every dropout in this period ({len(hics)})</h2>
{table(["When", "Length", "Pings lost", "Worst ping", "Where"], all_rows[-400:])}</section>
</body></html>
"""


def find_browser() -> str | None:
    """Edge or Chrome, for printing the report to PDF without opening a window."""
    for p in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"):
        if Path(p).exists():
            return p
    return shutil.which("msedge") or shutil.which("chrome")


def save_pdf(html_path: Path, pdf_path: Path, profile_dir: Path) -> bool:
    """Print the HTML report to PDF with a headless browser. Returns False if none is available."""
    browser = find_browser()
    if not browser:
        return False
    pdf_path.unlink(missing_ok=True)
    subprocess.run([browser, "--headless=new", "--disable-gpu", "--no-first-run", "--no-pdf-header-footer",
                    "--print-to-pdf-no-header", f"--user-data-dir={profile_dir}", f"--print-to-pdf={pdf_path}",
                    html_path.resolve().as_uri()], timeout=120, capture_output=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return pdf_path.exists() and pdf_path.stat().st_size > 0
