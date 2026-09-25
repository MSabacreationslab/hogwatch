# HogWatch

Finds out **who** is slowing the home internet to a crawl, **when**, and **what
they're probably doing**, and shows it on a dashboard at <http://127.0.0.1:8765/>.

It watches three things at once:

| What | How | Needs |
|---|---|---|
| **Is the internet slow right now, and where?** | Pings your eero, the main eero, the AT&T gateway and the internet every second | nothing |
| **Which program on this PC is using the internet** | Windows' built-in network event feed (the same one Resource Monitor uses) | running as admin |
| **Which device on the network is using the internet** (roommates' PCs, DirecTV boxes, phones…) | Live per-device speeds from the eero cloud, the same numbers the eero app shows | an eero admin login |

When ping jumps well above normal for more than a few seconds, HogWatch records
a **slowdown**. It also writes a plain-English explanation, for example:

> Internet crawled for 6 min. Ping was 302 ms, peaking at 443 ms (normal is 16 ms).
> Biggest user: JAKE-DESKTOP (Jake), uploading 19 Mbps (95% of your upload speed).

It only names someone when they were actually using a big share of the line. If
nobody was, it says so and points at AT&T or the eero instead.

## Everyday use

| Double-click | To |
|---|---|
| `start-hogwatch.cmd` | Start monitoring. Click **Yes** on the Windows prompt. |
| `open-dashboard.cmd` | Open the dashboard |
| `stop-hogwatch.cmd` | Stop monitoring |
| `install-autostart.cmd` | Start HogWatch automatically at every login (recommended) |
| `uninstall-autostart.cmd` | Undo that |
| `eero-login.cmd` | Connect to the eero (one time, see below) |
| `eero-check.cmd` | See what the eero reports for every device right now |

## Connecting the eero

The eero has no local connection; everything goes through eero's cloud, like the app does.

1. The eero owner adds you as an admin: eero app › **Settings › Network settings › Admins › Add an admin**.
2. Accept the invite in your own eero app.
3. Double-click `eero-login.cmd`. Enter your eero email or phone number (with `+1`),
   then the code eero sends you.

A running HogWatch notices the login within 15 seconds. The login token is saved in
`data\eero_session.json`. Treat it like a password.

> This uses eero's unofficial app API (the same one the Home Assistant eero
> integrations use). If eero changes it, `eero-check.cmd` will show the problem and
> saves the raw response to `data\eero_devices_raw.json` for troubleshooting.

## Lag spikes (short dropouts)

Games disconnect on freezes of just 2–5 seconds, which is too short to count as a
slowdown. HogWatch pings every hop once a second:

    this PC → (cable) → eero → (wireless, 5 GHz) → eero → AT&T → internet

The first hop that goes bad is where the problem is. Each lag spike is listed with
where it happened, which game was running, and which devices were busy. Devices
that **share your wireless link** are marked. The same card lists any eero that
lost its connection and reconnected, which shows drops even when they happen
between pings.

## Daily email report

The dashboard's **Daily email report** card emails you every slowdown and dropout
since the last report, with its reason, the game you were playing, and who was busy
on the network. It's useful for tracking a problem over days, or for showing it to
whoever owns the eero, or to AT&T.

- **When:** once a day when HogWatch starts, or at 8 AM if it's already running,
  plus **Send report now** anytime. **Preview report** shows it without sending.
- **Sending account:** mail goes out through your own email account. For Gmail,
  create an [App Password](https://myaccount.google.com/apppasswords) (needs 2-Step
  Verification). It's a separate password that can only send mail and can be revoked
  on its own. HogWatch stores it encrypted with Windows DPAPI in `data\email.json`,
  so only your Windows account can read it, and the dashboard never sends it back.
- **Other providers:** open "Not using Gmail?" and enter your provider's server and port.
  HogWatch only sends over an encrypted connection.
- **From a terminal:** `.venv\Scripts\python.exe -m hogwatch send-report` sends one now;
  add `--preview` to save it as `data\report_preview.html` instead.

## Reading the results

- **"Internet crawled"**: the line was full. Look at "Biggest user". An **upload**
  (cloud backup, live-streaming, torrent seeding) hurts the most, because upload
  speed is usually much smaller than download.
- **"Home network slow"**: the eero itself was slow to answer. That's an
  eero/Wi-Fi problem (overloaded, or a weak link between eeros), not AT&T.
- **"Internet dropped out"**: the eero was fine but nothing past AT&T's gateway answered.

The dashboard's bottom section explains the fixes. The biggest one is eero app ›
Settings › eero Labs › **Optimize for Conferencing and Gaming**.

## Settings

`data\config.json` (created on first run):

| key | default | meaning |
|---|---|---|
| `slow_extra_ms` | 80 | a slowdown is ping this far above normal… |
| `slow_min_ms` | 100 | …and at least this high in total |
| `plan_down_mbps`, `plan_up_mbps` | null | your internet plan; null = use the eero's last speed test |
| `eero_poll_s` | 30 | how often to ask the eero (10s during a slowdown) |
| `retention_days` | 14 | how much history to keep |
| `port` | 8765 | dashboard port (only reachable from this PC) |

## Under the hood

- Python 3.10+, one dependency (`psutil`). Everything is stored in `data\hogwatch.db` (SQLite).
- The AT&T gateway ignores direct pings, so it's timed as "hop 2" (a ping with a hop limit of 2 that it answers on the way).
- Per-program data comes from the `Microsoft-Windows-Kernel-Network` ETW provider (TCP + UDP, IPv4 + IPv6).
  Only internet traffic is counted per program; traffic to devices at home is left out.
- The dashboard has no external scripts, so it keeps working while the internet is down.
- The dashboard only answers requests addressed to `127.0.0.1` or `localhost` (blocks DNS
  rebinding), and every change needs a custom header that other websites can't send.
- Command line: `.venv\Scripts\python.exe -m hogwatch run | eero-login | eero-check | selftest | send-report`
- Tests: `.venv\Scripts\python.exe -m unittest discover -s tests`
