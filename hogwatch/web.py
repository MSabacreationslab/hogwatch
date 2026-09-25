"""Local dashboard server (http://127.0.0.1:8765) and its JSON API.

Bound to 127.0.0.1 only: the dashboard lists every device and program, so
it shouldn't be reachable from other machines on the network.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .report import ReportError, build_report

log = logging.getLogger(__name__)

STATIC = Path(__file__).resolve().parent / "static"
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}


def _bucket(hours: float) -> int:
    """Chart resolution: about 360 points across the window, never finer than 10s."""
    return max(10, int(hours * 3600 / 360) // 10 * 10)


def timeline(db, hours: float) -> dict:
    """Latency, this PC's traffic, and per-device eero traffic over the window, bucketed for charts."""
    now = int(time.time())
    start = now - int(hours * 3600)
    b = _bucket(hours)

    latency: dict[str, list] = {"internet": [], "lan": [], "isp": []}
    for r in db.query(
            "SELECT (ts / ?) * ? AS t, role, SUM(sent) sent, SUM(lost) lost, "
            "SUM(avg_ms * (sent - lost)) / NULLIF(SUM(sent - lost), 0) AS avg_ms, MAX(max_ms) AS max_ms "
            "FROM latency WHERE ts >= ? GROUP BY t, role ORDER BY t", (b, b, start)):
        loss = 100.0 * r["lost"] / r["sent"] if r["sent"] else 0.0
        latency.setdefault(r["role"], []).append([r["t"], r["avg_ms"], r["max_ms"], round(loss, 1)])

    pc = [[r["t"], r["rx"] * 8 / r["secs"] / 1e6, r["tx"] * 8 / r["secs"] / 1e6]
          for r in db.query("SELECT (ts / ?) * ? AS t, SUM(rx_bytes) rx, SUM(tx_bytes) tx, SUM(secs) secs "
                            "FROM pc_total WHERE ts >= ? GROUP BY t HAVING secs > 0 ORDER BY t", (b, b, start))]

    # The eero is polled every 30s, so its buckets can't be finer than that
    # or the device lines would be mostly gaps.
    eb = max(b, 30)
    top = db.query(
        "SELECT s.mac, d.name, d.owner, d.kind, SUM(down_mbps * secs) vd, SUM(up_mbps * secs) vu "
        "FROM eero_sample s LEFT JOIN eero_device d USING(mac) WHERE ts >= ? "
        "GROUP BY s.mac ORDER BY vd + vu DESC LIMIT 6", (start,))
    top = [t for t in top if (t["vd"] or 0) + (t["vu"] or 0) > 0]
    series: dict[str, list] = {t["mac"]: [] for t in top}
    if top:
        marks = ",".join("?" * len(top))
        # Each poll is a snapshot of rates, so average the polls that fall in a bucket.
        for r in db.query(
                f"SELECT (ts / ?) * ? AS t, mac, SUM(down_mbps) sd, SUM(up_mbps) su, COUNT(*) n FROM eero_sample "
                f"WHERE ts >= ? AND mac IN ({marks}) GROUP BY t, mac ORDER BY t",
                (eb, eb, start, *[t["mac"] for t in top])):
            series[r["mac"]].append([r["t"], r["sd"], r["su"], r["n"]])
    polls = {r["t"]: r["polls"] for r in db.query(
        "SELECT (ts / ?) * ? AS t, COUNT(DISTINCT ts) polls FROM eero_sample WHERE ts >= ? GROUP BY t",
        (eb, eb, start))}
    total = [[r["t"], r["sd"] / polls[r["t"]], r["su"] / polls[r["t"]]] for r in db.query(
        "SELECT (ts / ?) * ? AS t, SUM(down_mbps) sd, SUM(up_mbps) su FROM eero_sample WHERE ts >= ? "
        "GROUP BY t ORDER BY t", (eb, eb, start)) if polls.get(r["t"])]
    # Divide by polls-in-bucket (not the device's own sample count) so a device that
    # was only on for part of the bucket isn't overstated.
    for mac, rows in series.items():
        series[mac] = [[t, sd / polls.get(t, n), su / polls.get(t, n)] for t, sd, su, n in rows]

    incidents = db.query(
        "SELECT id, start_ts, end_ts, kind, headline FROM incident WHERE COALESCE(end_ts, ?) >= ? ORDER BY start_ts",
        (now, start))
    return {
        "start": start, "end": now, "bucket": b, "latency": latency, "pc": pc,
        "eero": {"devices": [{k: t[k] for k in ("mac", "name", "owner", "kind")} for t in top],
                 "series": series, "total": total, "bucket": eb},
        "incidents": incidents,
    }


def usage(db, hours: float) -> dict:
    """Totals over the window: per eero device and per program on this PC."""
    start = int(time.time()) - int(hours * 3600)
    devices = db.query(
        "SELECT s.mac, d.name, d.owner, d.kind, d.connection, d.node, d.ip, d.manufacturer, "
        "SUM(down_mbps * secs) / 8.0 AS down_mb, SUM(up_mbps * secs) / 8.0 AS up_mb, "
        "MAX(down_mbps) AS peak_down, MAX(up_mbps) AS peak_up "
        "FROM eero_sample s LEFT JOIN eero_device d USING(mac) WHERE ts >= ? "
        "GROUP BY s.mac ORDER BY down_mb + up_mb DESC", (start,))
    apps = db.query(
        "SELECT app, SUM(rx_bytes) / 1e6 AS down_mb, SUM(tx_bytes) / 1e6 AS up_mb, "
        "MAX(rx_bytes) AS max_rx, MAX(tx_bytes) AS max_tx "
        "FROM pc_app WHERE ts >= ? GROUP BY app ORDER BY down_mb + up_mb DESC LIMIT 25", (start,))
    for a in apps:
        # Rows are 10-second buckets, so the biggest row approximates the peak rate.
        a["peak_down"] = a.pop("max_rx") * 8 / 10 / 1e6
        a["peak_up"] = a.pop("max_tx") * 8 / 10 / 1e6
    hosts: dict[str, list] = {}
    for r in db.query(
            "SELECT r.app, r.ip, h.name, SUM(r.rx_bytes + r.tx_bytes) / 1e6 AS mb FROM pc_remote r "
            "LEFT JOIN hostnames h USING(ip) WHERE r.ts >= ? GROUP BY r.app, r.ip ORDER BY mb DESC", (start,)):
        lst = hosts.setdefault(r["app"], [])
        if len(lst) < 3:
            lst.append({"ip": r["ip"], "name": r["name"], "mb": r["mb"]})
    for a in apps:
        a["top_hosts"] = hosts.get(a["app"], [])
    pc_total = db.query("SELECT SUM(rx_bytes) / 1e6 AS down_mb, SUM(tx_bytes) / 1e6 AS up_mb "
                        "FROM pc_total WHERE ts >= ?", (start,))[0]
    return {"hours": hours, "devices": devices, "apps": apps, "pc_total": pc_total}


def hiccups(db, hours: float) -> dict:
    """Short lag spikes in the window: counts by where they happened, the list, eero reconnects, and the mesh path."""
    start = time.time() - hours * 3600
    items = db.query("SELECT * FROM hiccup WHERE start_ts >= ? ORDER BY start_ts DESC LIMIT 300", (start,))
    for h in items:
        h["busy"] = json.loads(h["busy"]) if h["busy"] else []
    summary = db.query(
        # Grouped by the text too, so spikes from before and after a change (e.g. the
        # wireless link being replaced by a cable) are counted separately.
        "SELECT where_, where_text, COUNT(*) AS n, SUM(end_ts - start_ts) AS secs, "
        "SUM(CASE WHEN game IS NOT NULL THEN 1 ELSE 0 END) AS in_game "
        "FROM hiccup WHERE start_ts >= ? GROUP BY where_, where_text ORDER BY n DESC", (start,))
    events = db.query("SELECT ts, unit, kind, detail FROM eero_event WHERE ts >= ? ORDER BY ts DESC", (int(start),))
    for e in events:
        e["detail"] = json.loads(e["detail"]) if e["detail"] else {}
    raw_path = db.get_meta("eero_path")
    return {"hours": hours, "summary": summary, "items": items, "eero_events": events,
            "path": json.loads(raw_path) if raw_path else None}


def incidents(db, days: float, limit: int) -> list[dict]:
    """Most recent slowdowns first, with their stored explanations."""
    rows = db.query("SELECT * FROM incident WHERE start_ts >= ? ORDER BY start_ts DESC LIMIT ?",
                    (int(time.time() - days * 86400), limit))
    for r in rows:
        r["details"] = json.loads(r["details"]) if r["details"] else None
    return rows


def make_server(port: int, collector_ref: dict, db_ref: dict, on_shutdown,
                reporter_ref: dict | None = None) -> ThreadingHTTPServer:
    """Create (bind) the server. Binding first doubles as the 'already running?' check."""
    reporter_ref = reporter_ref if reporter_ref is not None else {}
    # Only answer requests addressed to this machine by name. A web page elsewhere can
    # point its own domain at 127.0.0.1 ("DNS rebinding") and then read this dashboard
    # as if it were its own site; checking the Host header stops that. It matters more
    # now that the dashboard holds email settings.
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    last_manual_send = [0.0]

    class Handler(BaseHTTPRequestHandler):
        def _host_ok(self) -> bool:
            return (self.headers.get("Host") or "").lower() in allowed_hosts

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 16384:
                raise ValueError("request too large")
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj, default=str).encode(), "application/json")

        def do_GET(self):  # noqa: N802 (http.server naming)
            """Static files and read-only API."""
            if not self._host_ok():
                return self._send(403, b"forbidden", "text/plain")
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            col, db, rep = collector_ref.get("c"), db_ref.get("db"), reporter_ref.get("r")
            try:
                if url.path == "/api/email":
                    # Settings never include the password itself, only whether one is saved.
                    return self._json({"settings": rep.settings.load(), "status": rep.status()} if rep
                                      else {"error": "starting"})
                if url.path == "/api/email/preview":
                    now = time.time()
                    body = build_report(db, now - min(float(q.get("hours", 24)), 24 * 7) * 3600, now)["html"]
                    page = f"<!doctype html><meta charset=utf-8><title>HogWatch report preview</title><body style=\"margin:24px\">{body}"
                    return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
                if url.path == "/api/now":
                    return self._json(col.snapshot() if col else {"status": "starting"})
                if url.path == "/api/timeline":
                    return self._json(timeline(db, min(float(q.get("hours", 1)), 24 * 14)))
                if url.path == "/api/usage":
                    return self._json(usage(db, min(float(q.get("hours", 24)), 24 * 14)))
                if url.path == "/api/incidents":
                    return self._json(incidents(db, float(q.get("days", 14)), int(q.get("limit", 200))))
                if url.path == "/api/hiccups":
                    return self._json(hiccups(db, min(float(q.get("hours", 24)), 24 * 14)))
            except Exception as e:  # keep the dashboard alive; report the problem
                log.exception("API error on %s", url.path)
                return self._json({"error": str(e)}, 500)
            name = "index.html" if url.path in ("/", "") else url.path.lstrip("/")
            path = (STATIC / name).resolve()
            if STATIC in path.parents and path.is_file():
                return self._send(200, path.read_bytes(), TYPES.get(path.suffix, "application/octet-stream"))
            self._send(404, b"not found", "text/plain")

        def do_POST(self):  # noqa: N802
            """Shutdown and email actions. Every POST needs the X-HogWatch header: browsers
            can't send a custom header cross-site without a CORS preflight (which we never
            approve), so another web page can't change settings or trigger emails."""
            path = urlparse(self.path).path
            if not self._host_ok() or self.headers.get("X-HogWatch") != "1":
                return self._send(403, b"forbidden", "text/plain")
            if path == "/api/shutdown":
                self._json({"ok": True})
                threading.Thread(target=on_shutdown, daemon=True).start()
                return
            rep = reporter_ref.get("r")
            if rep is None:
                return self._json({"ok": False, "error": "HogWatch is still starting"}, 503)
            try:
                if path == "/api/email":
                    return self._json({"ok": True, "settings": rep.settings.save(self._body())})
                if path == "/api/email/send":
                    if time.time() - last_manual_send[0] < 20:
                        return self._json({"ok": False, "error": "Please wait a few seconds between sends."}, 429)
                    last_manual_send[0] = time.time()
                    result = rep.send(daily=False)
                    return self._json(result, 200 if result["ok"] else 502)
            except ReportError as e:
                return self._json({"ok": False, "error": str(e)}, 400)
            except ValueError as e:
                return self._json({"ok": False, "error": f"Bad request: {e}"}, 400)
            self._send(404, b"not found", "text/plain")

        def log_message(self, fmt, *args):
            """Silence per-request logging; the dashboard polls every few seconds."""

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)
