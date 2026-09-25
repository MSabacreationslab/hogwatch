"""Daily email report: every slowdown and dropout since the last report, with reasons.

Mail goes out through the user's own email account over SMTP (TLS only). For
Gmail that means an App Password -- a 16-character password that can only
send mail and can be revoked on its own. It's stored encrypted with Windows
DPAPI (see secret.py) and never returned by the dashboard API.
"""

from __future__ import annotations

import base64
import datetime as dt
import html
import json
import logging
import re
import smtplib
import ssl
import threading
import time
from collections import Counter, defaultdict
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

from . import secret
from .incidents import fmt_mbps

log = logging.getLogger(__name__)

DEFAULTS = {
    "to": "",              # one or more addresses, comma-separated
    "from": "",            # the account that sends it (e.g. your Gmail address)
    "username": "",        # SMTP login; empty = same as "from"
    "smtp_host": "smtp.gmail.com",
    "smtp_port": 587,      # 587 = STARTTLS, 465 = TLS from the start
    "daily": True,
    "send_hour": 8,        # if HogWatch is already running, the daily report goes out at this hour
}
_EMAIL_RE = re.compile(r"^[^@\s,;<>\"]+@[^@\s,;<>\"]+\.[^@\s,;<>\"]+$")
MAX_REPORT_DAYS = 7


class ReportError(Exception):
    """A problem the user can fix (bad address, rejected login, unreachable server)."""


def parse_recipients(value: str) -> list[str]:
    """Split 'a@b.com, c@d.com' into addresses; raise ReportError on anything malformed."""
    parts = [p.strip() for p in re.split(r"[,;\s]+", value or "") if p.strip()]
    bad = [p for p in parts if not _EMAIL_RE.match(p)]
    if bad:
        raise ReportError(f"That doesn't look like an email address: {bad[0]}")
    return parts


# ------------------------------------------------------------------ settings


class EmailSettings:
    """data/email.json: where reports go and how to send them. The password is stored DPAPI-encrypted."""

    def __init__(self, path: Path):
        """`path` is the JSON file (data/email.json in normal use)."""
        self.path = path

    def _raw(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def load(self) -> dict:
        """Settings for display: never includes the password, just whether one is saved."""
        raw = self._raw()
        out = dict(DEFAULTS)
        out.update({k: v for k, v in raw.items() if k in DEFAULTS})
        out["password_set"] = bool(raw.get("password_dpapi"))
        return out

    def password(self) -> str | None:
        """The decrypted app password, or None if none is saved."""
        blob = self._raw().get("password_dpapi")
        if not blob:
            return None
        return secret.unprotect(base64.b64decode(blob))

    def save(self, updates: dict) -> dict:
        """Validate and store changes. An empty password leaves the saved one alone."""
        raw = self._raw()
        for key in ("to", "from", "username", "smtp_host"):
            if key in updates:
                raw[key] = str(updates[key] or "").strip()
        if "to" in updates and raw["to"]:
            raw["to"] = ", ".join(parse_recipients(raw["to"]))
        for key in ("from", "username"):
            if raw.get(key) and not _EMAIL_RE.match(raw[key]):
                raise ReportError(f"That doesn't look like an email address: {raw[key]}")
        if "smtp_port" in updates:
            try:
                port = int(updates["smtp_port"])
            except (TypeError, ValueError):
                raise ReportError("The port must be a number (587 for Gmail)") from None
            if not 1 <= port <= 65535:
                raise ReportError("The port must be between 1 and 65535")
            raw["smtp_port"] = port
        if "daily" in updates:
            raw["daily"] = bool(updates["daily"])
        if updates.get("clear_password"):
            raw.pop("password_dpapi", None)
        elif updates.get("password"):
            # Google shows app passwords with spaces ("abcd efgh ijkl mnop"); they work either way.
            pw = str(updates["password"]).strip()
            raw["password_dpapi"] = base64.b64encode(secret.protect(pw)).decode("ascii")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
        return self.load()


# ------------------------------------------------------------------ building the report


def _clock(ts: float) -> str:
    """'Thu Sep 24, 9:17 PM' (no leading zero; Windows strftime lacks %-I)."""
    d = dt.datetime.fromtimestamp(ts)
    return f"{d:%a %b} {d.day}, {d.hour % 12 or 12}:{d.minute:02d} {'AM' if d.hour < 12 else 'PM'}"


def _dur(secs: float) -> str:
    secs = max(1, int(round(secs)))
    if secs < 60:
        return f"{secs} sec"
    m, s = divmod(secs, 60)
    return f"{m} min" + (f" {s} sec" if s and m < 5 else "")


_KIND_LABEL = {
    "eero_link": "Link between eeros dropped", "congested": "Internet crawled", "outage": "Internet dropped out",
    "home": "Home network slow", "home_outage": "PC lost the eero",
}


def _advice(where: str, path: dict | None) -> str | None:
    """The same fix suggestion the dashboard shows for the most common dropout location."""
    wired = bool(path and path.get("wired"))
    return {
        "eero_link": ("Check that both ends of the cable between your eeros are plugged in, or try another cable."
                      if wired else
                      "Most dropouts are on the wireless link between your eeros, not your internet line. "
                      "An Ethernet cable between those two eeros (a wired backhaul) would fix them."),
        "pc_link": "Most dropouts are between this PC and its eero. Try a different cable and the eero's other port.",
        "eero": "Most dropouts are inside your main eero. Restart it and check the eero app for updates.",
        "att": "Most dropouts are at the AT&T gateway. Restart it; if they continue, give AT&T these times.",
        "internet": "Most dropouts happen past your eeros (AT&T or beyond). If frequent, give AT&T these times.",
    }.get(where)


def build_report(db, since: float, until: float) -> dict:
    """Everything that went wrong in [since, until): subject line, plain text, and HTML."""
    hiccups = db.query("SELECT * FROM hiccup WHERE start_ts >= ? AND start_ts < ? ORDER BY start_ts", (since, until))
    incidents = db.query("SELECT * FROM incident WHERE start_ts >= ? AND start_ts < ? AND end_ts IS NOT NULL "
                         "ORDER BY start_ts", (since, until))
    events = db.query("SELECT * FROM eero_event WHERE ts >= ? AND ts < ? ORDER BY ts", (since, until))
    raw_path = db.get_meta("eero_path")
    path = json.loads(raw_path) if raw_path else None

    where_counts = Counter(h["where_"] for h in hiccups)
    where_text: dict[str, str] = {}
    where_secs: dict[str, float] = defaultdict(float)
    for h in hiccups:
        where_text.setdefault(h["where_"], h["where_text"])
        where_secs[h["where_"]] += h["end_ts"] - h["start_ts"]
    in_game = sum(1 for h in hiccups if h["game"])
    down_secs = sum(h["end_ts"] - h["start_ts"] for h in hiccups)
    advice = None
    if hiccups:
        top, n = where_counts.most_common(1)[0]
        if len(hiccups) >= 3 and n / len(hiccups) >= 0.6:
            advice = _advice(top, path)

    period = f"{_clock(since)} to {_clock(until)}"
    nd, ns = len(hiccups), len(incidents)
    if nd or ns:
        bits = []
        if nd:
            bits.append(f"{nd} dropout{'s' if nd != 1 else ''}")
        if ns:
            bits.append(f"{ns} slowdown{'s' if ns != 1 else ''}")
        subject = f"HogWatch: {' and '.join(bits)} since {_clock(since)}"
    else:
        subject = f"HogWatch: all clear since {_clock(since)}"

    def busy_text(h) -> str:
        busy = json.loads(h["busy"]) if h["busy"] else []
        if not busy:
            return "nobody heavy"
        return ", ".join(f"{b['name']} ↓{fmt_mbps(b['down_mbps'])} ↑{fmt_mbps(b['up_mbps'])} Mbps"
                         + (" (shares your link)" if b.get("on_link") and not b.get("this_pc") else "")
                         for b in busy[:3])

    def how_bad(h) -> str:
        parts = []
        if h["lost"]:
            parts.append(f"{h['lost']} ping{'s' if h['lost'] != 1 else ''} lost")
        if h["worst_ms"]:
            parts.append(f"worst {h['worst_ms']:.0f} ms")
        return ", ".join(parts) or "slow"

    # ---- plain text
    lines = [f"HogWatch report for {period}", ""]
    if not (nd or ns):
        lines.append("No slowdowns or dropouts. Everything ran normally.")
    else:
        lines.append(f"Dropouts (lag spikes): {nd}, about {_dur(down_secs)} offline in total"
                     + (f", {in_game} during a game" if in_game else ""))
        for where, n in where_counts.most_common():
            lines.append(f"  {n} {where_text[where]} ({_dur(where_secs[where])})")
        lines.append(f"Slowdowns: {ns}")
        if advice:
            lines += ["", f"Suggested fix: {advice}"]
    if hiccups:
        lines += ["", "DROPOUTS"]
        for h in hiccups:
            lines.append(f"- {_clock(h['start_ts'])}, {_dur(h['end_ts'] - h['start_ts'])}, {how_bad(h)}: {h['where_text']}"
                         + (f". Game: {h['game']}" if h["game"] else "") + f". Busy: {busy_text(h)}")
    if incidents:
        lines += ["", "SLOWDOWNS"]
        for i in incidents:
            lines.append(f"- {_clock(i['start_ts'])} ({_KIND_LABEL.get(i['kind'], i['kind'])}): {i['headline']}")
    if events:
        lines += ["", "EEROS THAT LOST THEIR CONNECTION"]
        for e in events:
            lines.append(f"- {_clock(e['ts'])}: {e['unit']} eero reconnected")
    lines += ["", "Open the dashboard for charts: http://127.0.0.1:8765/ (on the PC running HogWatch)"]
    text = "\n".join(lines) + "\n"

    # ---- HTML (inline styles: most email apps strip <style> blocks). Everything
    # from the network -- device names, eero names -- is escaped.
    e = html.escape
    td = 'style="padding:6px 8px;border-bottom:1px solid #e1e0d9;vertical-align:top"'
    th = 'style="padding:6px 8px;border-bottom:1px solid #c3c2b7;text-align:left;color:#52514e;font-weight:600"'
    parts = [f'<div style="font-family:Segoe UI,system-ui,sans-serif;color:#0b0b0b;max-width:760px">',
             f'<h2 style="margin:0 0 4px">HogWatch report</h2><p style="margin:0 0 16px;color:#52514e">{e(period)}</p>']
    if not (nd or ns):
        parts.append('<p style="font-size:16px">&#10004; No slowdowns or dropouts. Everything ran normally.</p>')
    else:
        parts.append(f'<p style="font-size:16px;margin:0 0 6px"><b>{nd} dropout{"s" if nd != 1 else ""}</b> '
                     f'(about {e(_dur(down_secs))} offline{f", {in_game} during a game" if in_game else ""}) and '
                     f'<b>{ns} slowdown{"s" if ns != 1 else ""}</b>.</p><ul style="margin:0 0 12px">')
        for where, n in where_counts.most_common():
            parts.append(f"<li><b>{n}</b> {e(where_text[where])} ({e(_dur(where_secs[where]))})</li>")
        parts.append("</ul>")
        if advice:
            parts.append(f'<p style="border-left:4px solid #2a78d6;padding:8px 12px;background:#f3f2ee">'
                         f'<b>Suggested fix:</b> {e(advice)}</p>')
    if hiccups:
        parts.append(f'<h3 style="margin:20px 0 6px">Dropouts</h3><table style="border-collapse:collapse;font-size:14px;width:100%">'
                     f"<tr><th {th}>When</th><th {th}>Length</th><th {th}>How bad</th><th {th}>Where</th>"
                     f"<th {th}>Game</th><th {th}>Busy on the network</th></tr>")
        for h in hiccups:
            parts.append(f"<tr><td {td}>{e(_clock(h['start_ts']))}</td><td {td}>{e(_dur(h['end_ts'] - h['start_ts']))}</td>"
                         f"<td {td}>{e(how_bad(h))}</td><td {td}>{e(h['where_text'] or '')}</td>"
                         f"<td {td}>{e(h['game'] or '–')}</td><td {td}>{e(busy_text(h))}</td></tr>")
        parts.append("</table>")
    if incidents:
        parts.append(f'<h3 style="margin:20px 0 6px">Slowdowns</h3><table style="border-collapse:collapse;font-size:14px;width:100%">'
                     f"<tr><th {th}>When</th><th {th}>What happened and why</th></tr>")
        for i in incidents:
            parts.append(f"<tr><td {td}>{e(_clock(i['start_ts']))}<br><span style=\"color:#52514e\">"
                         f"{e(_KIND_LABEL.get(i['kind'], i['kind'] or ''))}</span></td><td {td}>{e(i['headline'] or '')}</td></tr>")
        parts.append("</table>")
    if events:
        parts.append('<h3 style="margin:20px 0 6px">eeros that lost their connection</h3><ul>')
        parts += [f"<li>{e(_clock(ev['ts']))}: {e(ev['unit'])} eero reconnected</li>" for ev in events]
        parts.append("</ul>")
    parts.append('<p style="color:#6f6d68;font-size:12px;margin-top:24px">Sent by HogWatch. Charts: '
                 'http://127.0.0.1:8765/ on the PC running it.</p></div>')
    return {"subject": subject, "text": text, "html": "".join(parts),
            "counts": {"dropouts": nd, "slowdowns": ns, "reconnects": len(events)}}


# ------------------------------------------------------------------ sending


def send_email(settings: dict, password: str, subject: str, text: str, html_body: str, timeout: float = 30) -> list[str]:
    """Send over SMTP with TLS (never in the clear). Returns the recipients."""
    to = parse_recipients(settings.get("to", ""))
    sender = settings.get("from") or ""
    if not to:
        raise ReportError("Enter at least one address to send reports to")
    if not sender:
        raise ReportError("Enter the email address to send from")
    if not password:
        raise ReportError("Enter the app password for the sending account")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="hogwatch.local")
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")

    host, port = settings.get("smtp_host") or "smtp.gmail.com", int(settings.get("smtp_port") or 587)
    ctx = ssl.create_default_context()
    try:
        if port == 465:
            smtp = smtplib.SMTP_SSL(host, port, timeout=timeout, context=ctx)
        else:
            smtp = smtplib.SMTP(host, port, timeout=timeout)
            smtp.ehlo()
            smtp.starttls(context=ctx)  # refuses to continue if the server can't do TLS
            smtp.ehlo()
        with smtp:
            smtp.login(settings.get("username") or sender, password.replace(" ", ""))
            smtp.send_message(msg)
    except smtplib.SMTPAuthenticationError as err:
        raise ReportError(f"{host} rejected the login. For Gmail, use an App Password (not your normal "
                          "password) and make sure 'Send from' is that Gmail address.") from err
    except smtplib.SMTPNotSupportedError as err:
        raise ReportError(f"{host} doesn't support a secure connection on port {port}.") from err
    except (smtplib.SMTPException, OSError) as err:
        raise ReportError(f"Couldn't send through {host}:{port} ({err}).") from err
    return to


# ------------------------------------------------------------------ scheduling


def is_due(now: dt.datetime, last_daily: str | None, startup: bool, send_hour: int) -> bool:
    """The daily report is due once per calendar day: at startup, or from `send_hour` if already running."""
    if last_daily == now.date().isoformat():
        return False
    return startup or now.hour >= send_hour


class Reporter:
    """Sends the daily report and on-demand reports; remembers what was sent in the DB's meta table."""

    STARTUP_DELAY_S = 90   # after boot, let the network come up before emailing
    CHECK_EVERY_S = 900
    RETRY_AFTER_S = 3600   # after a failure, don't hammer the mail server

    def __init__(self, db, settings: EmailSettings, stopping: threading.Event):
        """`stopping` is the app's shutdown event."""
        self.db = db
        self.settings = settings
        self.stopping = stopping
        self.lock = threading.Lock()
        self.last_attempt = 0.0

    def status(self) -> dict:
        """Last success/failure, for the dashboard."""
        raw = self.db.get_meta("report_status")
        return json.loads(raw) if raw else {}

    def _record(self, **fields) -> None:
        s = self.status()
        s.update(fields)
        self.db.set_meta("report_status", json.dumps(s))

    def send(self, daily: bool, hours: float = 24) -> dict:
        """Build and send one report. Daily reports cover the time since the last daily one;
        on-demand ones cover the last `hours`."""
        with self.lock:
            now = time.time()
            since = now - min(hours, MAX_REPORT_DAYS * 24) * 3600
            if daily:
                last_ts = self.status().get("last_daily_ts")
                if last_ts:
                    since = max(float(last_ts), now - MAX_REPORT_DAYS * 86400)
            try:
                cfg = self.settings.load()
                rep = build_report(self.db, since, now)
                to = send_email(cfg, self.settings.password() or "", rep["subject"], rep["text"], rep["html"])
            except (ReportError, OSError) as err:
                self._record(last_error=str(err), last_error_ts=int(now))
                log.warning("email report failed: %s", err)
                return {"ok": False, "error": str(err)}
            fields = {"last_sent_ts": int(now), "last_to": ", ".join(to), "last_subject": rep["subject"],
                      "last_error": None}
            if daily:
                fields.update(last_daily=dt.date.today().isoformat(), last_daily_ts=int(now))
            self._record(**fields)
            log.info("emailed report to %s: %s", ", ".join(to), rep["subject"])
            return {"ok": True, "to": to, "subject": rep["subject"], "counts": rep["counts"]}

    def loop(self) -> None:
        """Send the daily report at startup (if not sent today), then check every 15 minutes."""
        if self.stopping.wait(self.STARTUP_DELAY_S):
            return
        startup = True
        while True:
            cfg = self.settings.load()
            ready = cfg["daily"] and cfg["to"] and cfg["from"] and cfg["password_set"]
            if ready and time.time() - self.last_attempt > self.RETRY_AFTER_S and \
                    is_due(dt.datetime.now(), self.status().get("last_daily"), startup, int(cfg["send_hour"])):
                self.last_attempt = time.time()
                self.send(daily=True)
            startup = False
            if self.stopping.wait(self.CHECK_EVERY_S):
                return
