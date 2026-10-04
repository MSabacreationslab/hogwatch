"""Command line:

    python -m hogwatch run [--no-browser]   start monitoring + dashboard (default from source)
    python -m hogwatch launch               what double-clicking HogWatch.exe does: ask for admin
                                            rights, start in the background, open the dashboard
    python -m hogwatch stop                 stop a running HogWatch
    python -m hogwatch eero-login           connect to the eero (one time; also on the dashboard)
    python -m hogwatch eero-check           show what the eero reports right now
    python -m hogwatch selftest             15-second check that everything works
    python -m hogwatch send-report          email the last 24 hours now (--preview: save it as HTML instead)
    python -m hogwatch installer-report     technical report for the eero installer (HTML + PDF in the data folder)

Options before the command: --data-dir FOLDER, --port N (see config.py).
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from . import config, ping
from .config import DATA_DIR, FROZEN, ROOT
from .winutil import is_admin as _is_admin
from .winutil import open_dashboard, run_elevated, server_up


def _setup_logging() -> None:
    """Log to data/hogwatch.log (rotating), plus the console when there is one."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.handlers.RotatingFileHandler(
        DATA_DIR / "hogwatch.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")]
    if sys.stderr is not None:  # pythonw.exe has no console
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def cmd_run(args) -> int:
    """Start everything and serve the dashboard until stopped."""
    from .collector import Collector
    from .db import DB
    from .report import EmailSettings, Reporter
    from .web import make_server

    _setup_logging()
    log = logging.getLogger("hogwatch")
    cfg = config.load()
    url = f"http://127.0.0.1:{cfg['port']}/"
    done = threading.Event()
    collector_ref: dict = {}
    db_ref: dict = {}
    reporter_ref: dict = {}
    try:
        server = make_server(cfg["port"], collector_ref, db_ref, on_shutdown=done.set, reporter_ref=reporter_ref,
                             eero_session=DATA_DIR / "eero_session.json")
    except OSError:
        log.info("HogWatch is already running -- opening the dashboard")
        if not args.no_browser:
            open_dashboard(url)
        return 0

    db = DB(DATA_DIR / "hogwatch.db")
    db_ref["db"] = db
    threading.Thread(target=server.serve_forever, name="web", daemon=True).start()
    log.info("HogWatch %s running%s. Dashboard: %s",
             __import__("hogwatch").__version__, " as admin" if _is_admin() else " (not admin: PC totals only)", url)
    collector = Collector(cfg, db)
    collector.start()
    collector_ref["c"] = collector
    # Daily email report (does nothing until an address and app password are saved).
    reporter = Reporter(db, EmailSettings(DATA_DIR / "email.json"), stopping=done)
    reporter_ref["r"] = reporter
    threading.Thread(target=reporter.loop, name="report", daemon=True).start()
    if not args.no_browser:
        open_dashboard(url)
    try:
        while not done.wait(1):
            pass
    except KeyboardInterrupt:
        pass
    log.info("stopping")
    collector.stop()
    server.shutdown()
    return 0


def _global_options() -> list[str]:
    """--data-dir / --port as given on this command line, to pass on to a relaunched copy."""
    out = []
    for name in ("--data-dir", "--port"):
        if name in sys.argv and sys.argv.index(name) + 1 < len(sys.argv):
            out += [name, sys.argv[sys.argv.index(name) + 1]]
    return out


def cmd_launch(args) -> int:
    """Double-click behaviour: make sure HogWatch is running in the background (as admin if
    the user allows it), open the dashboard, and exit.

    The background copy is started as administrator because Windows only shares
    per-program network data with admin tools. This copy stays un-elevated so the
    browser it opens isn't running as administrator.
    """
    cfg = config.load()
    port = int(cfg["port"])
    url = f"http://127.0.0.1:{port}/"
    if server_up(port):
        open_dashboard(url)
        return 0
    if not _is_admin():
        params = _global_options() + ["run", "--no-browser"]
        if FROZEN:
            exe, argv, cwd = sys.executable, params, str(Path(sys.executable).parent)
        else:
            pyw = Path(sys.executable).with_name("pythonw.exe")
            exe, argv, cwd = str(pyw if pyw.exists() else sys.executable), ["-m", "hogwatch", *params], str(ROOT)
        if run_elevated(exe, subprocess.list2cmdline(argv), cwd):
            for _ in range(60):  # the packaged exe unpacks itself first; allow it 30 seconds
                if server_up(port, timeout=0.5):
                    break
                time.sleep(0.5)
            open_dashboard(url)
            return 0
        # The user said No to the admin prompt: run here without per-program detail.
    args.no_browser = False
    return cmd_run(args)


def cmd_stop(args) -> int:
    """Ask a running HogWatch to shut down cleanly."""
    port = int(config.load()["port"])
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/shutdown", data=b"{}", method="POST",
                                 headers={"X-HogWatch": "1", "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=5).close()
        print("HogWatch stopped.")
    except OSError:
        print("HogWatch wasn't running.")
    return 0


def cmd_eero_login(args) -> int:
    """Interactive one-time login. The user types their own login and code; we store only the session token."""
    from .eero import Eero, EeroError

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    e = Eero(DATA_DIR / "eero_session.json")
    print("Connect HogWatch to the eero\n")
    print("Your eero account needs to be an ADMIN on the network. The owner adds you in the")
    print("eero app: Settings > Network settings > Admins > Add an admin.\n")
    login = input("Your eero login (email, or phone number with country code like +15551234567): ").strip()
    try:
        e.start_login(login)
    except EeroError as err:
        print(f"\neero didn't accept that login: {err}")
        return 1
    code = input("eero just sent you a verification code. Enter it here: ").strip()
    try:
        e.verify(code)
        nets = e.networks()
    except EeroError as err:
        print(f"\nThat code didn't work: {err}")
        return 1
    if not nets:
        print("\nYou're logged in, but this account isn't an admin on any eero network yet.")
        print("Ask the owner to add you as an admin, then run this again.")
        return 1
    if len(nets) == 1:
        chosen = nets[0]
    else:
        for i, n in enumerate(nets, 1):
            print(f"  {i}. {n['name']}")
        chosen = nets[int(input("Which network? ").strip()) - 1]
    e.network_url, e.network_name = chosen["url"], chosen["name"]
    e.save()
    print(f"\nConnected to '{chosen['name']}'. The session is saved in data\\eero_session.json")
    print("(it's a login token: don't share that file).\n")
    return cmd_eero_check(args)


def cmd_eero_check(args) -> int:
    """Print the eero's current view of every device, to confirm live usage is reported."""
    from .eero import Eero, EeroError, parse_device, speed_from_network

    e = Eero(DATA_DIR / "eero_session.json")
    if not e.ready:
        print("Not connected to eero yet. Run eero-login first.")
        return 1
    try:
        raw = e.devices()
        speed = speed_from_network(e.network())
    except EeroError as err:
        print(f"eero error: {err}")
        return 1
    (DATA_DIR / "eero_devices_raw.json").write_text(json.dumps(raw, indent=2), encoding="utf-8")
    devs = sorted((parse_device(d) for d in raw if d.get("connected")),
                  key=lambda d: (d["down_mbps"] or 0) + (d["up_mbps"] or 0), reverse=True)
    print(f"Network: {e.network_name}")
    if speed:
        print(f"Last eero speed test: {speed['down']} Mbps down / {speed['up']} Mbps up ({speed.get('date')})")
    print(f"\n{'Device':34} {'Type':9} {'Link':6} {'Down Mbps':>10} {'Up Mbps':>9}")
    for d in devs:
        fmt = lambda v: "-" if v is None else f"{v:.1f}"  # noqa: E731
        print(f"{d['name'][:34]:34} {d['kind']:9} {d['connection']:6} {fmt(d['down_mbps']):>10} {fmt(d['up_mbps']):>9}")
    if devs and all(d["down_mbps"] is None for d in devs):
        print("\nNote: eero isn't reporting live per-device usage for this network. Send data\\eero_devices_raw.json")
        print("to whoever maintains HogWatch; the field may have a different name on your eero.")
    return 0


def cmd_selftest(args) -> int:
    """Quick end-to-end check; also written to data/selftest.txt (handy when run elevated in a new window)."""
    from .pcnet import PCMonitor

    lines: list[str] = []

    def out(s=""):
        lines.append(s)
        if sys.stdout:
            print(s, flush=True)

    out(f"admin: {_is_admin()}")
    lan, isp = ping.hop(1), ping.hop(2)
    out(f"eero (hop 1): {lan}   ISP gateway (hop 2): {isp}")
    for label, ip, ttl in (("eero", lan, 128), ("ISP gw", "1.1.1.1", 2), ("1.1.1.1", "1.1.1.1", 128),
                           ("8.8.8.8", "8.8.8.8", 128)):
        if ip:
            out(f"  ping {label:8} -> {ping.ping(ip, 1000, ttl)[0]} ms")
    pc = PCMonitor()
    out(f"network card: {pc.nic}")
    out(f"per-program tracking: {'ON' if pc.trace else 'OFF -- ' + str(pc.trace_error)}")
    pc.sample()
    secs = int(getattr(args, "seconds", 15))
    out(f"measuring for {secs}s...")
    time.sleep(secs)
    s = pc.sample()
    out(f"card total: down {s['rx'] * 8 / s['secs'] / 1e6:.2f} Mbps, up {s['tx'] * 8 / s['secs'] / 1e6:.2f} Mbps")
    if pc.trace:
        out(f"ETW events seen: {pc.trace.events_seen}")
        apps = sorted(s["apps"].items(), key=lambda kv: sum(kv[1]), reverse=True)[:10]
        att = sum(rx + tx for rx, tx in s["apps"].values())
        out(f"internet bytes attributed to programs: {att / 1e6:.2f} MB of {(s['rx'] + s['tx']) / 1e6:.2f} MB card total")
        for app, (rx, tx) in apps:
            out(f"  {app:45} down {rx / 1e6:8.2f} MB   up {tx / 1e6:8.2f} MB")
        remotes = sorted(s["remotes"].items(), key=lambda kv: sum(kv[1]), reverse=True)[:5]
        for (app, ip), (rx, tx) in remotes:
            out(f"  flow {app:30} {ip:40} {(rx + tx) / 1e6:.2f} MB")
    pc.stop()
    (DATA_DIR / "selftest.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0


def cmd_send_report(args) -> int:
    """Email a report of the last N hours now, or (--preview) save it as an HTML file without sending."""
    from .db import DB
    from .report import EmailSettings, Reporter, build_report

    db = DB(DATA_DIR / "hogwatch.db")
    if args.preview:
        now = time.time()
        rep = build_report(db, now - args.hours * 3600, now)
        out = DATA_DIR / "report_preview.html"
        out.write_text(f"<!doctype html><meta charset=utf-8><title>{rep['subject']}</title>{rep['html']}",
                       encoding="utf-8")
        print(f"Subject: {rep['subject']}\n\n{rep['text']}\nHTML version saved to {out}")
        return 0
    result = Reporter(db, EmailSettings(DATA_DIR / "email.json"), threading.Event()).send(daily=False, hours=args.hours)
    if result["ok"]:
        print(f"Sent to {', '.join(result['to'])}: {result['subject']}")
        return 0
    print(f"Not sent: {result['error']}")
    return 1


def cmd_installer_report(args) -> int:
    """Write the installer report as HTML and (with Edge or Chrome installed) PDF in data\\."""
    from .db import DB
    from .installer import build_installer_report, save_pdf

    db = DB(DATA_DIR / "hogwatch.db")
    page = build_installer_report(db, days=args.days, note=args.note or "")
    html_path = DATA_DIR / "installer_report.html"
    html_path.write_text(page, encoding="utf-8")
    print(f"HTML report: {html_path}")
    pdf_path = DATA_DIR / "installer_report.pdf"
    if save_pdf(html_path, pdf_path, DATA_DIR / "pdf-browser-profile"):
        print(f"PDF report:  {pdf_path}")
    else:
        print("No Edge or Chrome found for the PDF: open the HTML report and use Print > Save as PDF.")
    return 0


def main() -> int:
    """Parse the command and run it."""
    p = argparse.ArgumentParser(prog="hogwatch", description="Find out who is slowing the internet down.")
    # Read early by config.py (they decide where config lives); declared here so argparse accepts them.
    p.add_argument("--data-dir", help="folder for history, settings and logs")
    p.add_argument("--port", type=int, help="dashboard port (default 8765)")
    sub = p.add_subparsers(dest="cmd")
    r = sub.add_parser("run", help="start monitoring and the dashboard")
    r.add_argument("--no-browser", action="store_true", help="don't open the dashboard")
    sub.add_parser("launch", help="start in the background as administrator and open the dashboard")
    sub.add_parser("stop", help="stop a running HogWatch")
    sub.add_parser("eero-login", help="connect to the eero (one time)")
    sub.add_parser("eero-check", help="show what the eero reports right now")
    st = sub.add_parser("selftest", help="15-second check that everything works")
    st.add_argument("--seconds", type=int, default=15)
    sr = sub.add_parser("send-report", help="email the recent slowdowns and dropouts now")
    sr.add_argument("--hours", type=float, default=24, help="how far back to cover (default 24)")
    sr.add_argument("--preview", action="store_true", help="save the report as HTML instead of sending")
    ir = sub.add_parser("installer-report", help="technical report for whoever services the eeros (HTML + PDF)")
    ir.add_argument("--days", type=float, default=7, help="how far back to cover (1-14, default 7)")
    ir.add_argument("--note", help="notes from the homeowner to include")
    args = p.parse_args()
    if args.cmd is None:
        # Double-clicking the exe gives no command: do the friendly thing. From source,
        # plain `python -m hogwatch` keeps its original meaning (run in this window).
        args.cmd = "launch" if FROZEN else "run"
        args.no_browser = False
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return {"run": cmd_run, "launch": cmd_launch, "stop": cmd_stop, "eero-login": cmd_eero_login,
            "eero-check": cmd_eero_check, "selftest": cmd_selftest, "send-report": cmd_send_report,
            "installer-report": cmd_installer_report}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
