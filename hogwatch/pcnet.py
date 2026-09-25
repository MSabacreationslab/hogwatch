"""What THIS PC is doing on the network: totals from the network card, and
(with admin) a per-program breakdown built from the ETW feed in etw.py.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
import subprocess
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import psutil

from .etw import NetworkEventTrace

log = logging.getLogger(__name__)

# Friendly names for programs that commonly eat bandwidth. Keys are lowercase exe names.
FRIENDLY_EXE = {
    "steam.exe": "Steam", "steamwebhelper.exe": "Steam", "steamservice.exe": "Steam",
    "epicgameslauncher.exe": "Epic Games", "epicwebhelper.exe": "Epic Games",
    "battle.net.exe": "Battle.net", "eadesktop.exe": "EA app", "eabackgroundservice.exe": "EA app",
    "upc.exe": "Ubisoft Connect", "ubisoftconnect.exe": "Ubisoft Connect", "galaxyclient.exe": "GOG Galaxy",
    "riotclientservices.exe": "Riot Client", "xboxpcapp.exe": "Xbox app",
    "gamingservices.exe": "Xbox / Microsoft Store downloads",
    "onedrive.exe": "OneDrive (cloud sync)", "dropbox.exe": "Dropbox (cloud sync)",
    "googledrivefs.exe": "Google Drive (cloud sync)", "bztransmit.exe": "Backblaze backup",
    "bztransmit64.exe": "Backblaze backup", "icloudservices.exe": "iCloud",
    "qbittorrent.exe": "qBittorrent (torrents)", "utorrent.exe": "uTorrent (torrents)",
    "bittorrent.exe": "BitTorrent (torrents)", "transmission-qt.exe": "Transmission (torrents)",
    "deluge.exe": "Deluge (torrents)", "tixati.exe": "Tixati (torrents)",
    "chrome.exe": "Chrome", "msedge.exe": "Edge", "firefox.exe": "Firefox", "brave.exe": "Brave",
    "opera.exe": "Opera", "msedgewebview2.exe": "Edge WebView (in-app web content)",
    "discord.exe": "Discord", "zoom.exe": "Zoom", "ms-teams.exe": "Teams", "teams.exe": "Teams",
    "slack.exe": "Slack", "obs64.exe": "OBS (streaming/recording)", "spotify.exe": "Spotify",
    "msmpeng.exe": "Windows Defender", "mpdefendercoreservice.exe": "Windows Defender",
    "virtualbox.exe": "VirtualBox", "virtualboxvm.exe": "VirtualBox VM", "vboxheadless.exe": "VirtualBox VM",
    "nvcontainer.exe": "NVIDIA app", "nvidia app.exe": "NVIDIA app", "nvidia share.exe": "NVIDIA overlay",
    "code.exe": "VS Code", "claude.exe": "Claude", "git-remote-https.exe": "Git",
    "system": "Windows (system)", "registry": "Windows (system)",
    # Games: named so lag spikes can say "while you were playing X".
    "league of legends.exe": "League of Legends", "leagueclient.exe": "League client",
    "leagueclientux.exe": "League client", "leagueclientuxrender.exe": "League client",
    "riotclientux.exe": "Riot Client", "valorant-win64-shipping.exe": "Valorant",
    "r5apex.exe": "Apex Legends", "r5apex_dx12.exe": "Apex Legends", "cs2.exe": "Counter-Strike 2",
    "fortniteclient-win64-shipping.exe": "Fortnite", "overwatch.exe": "Overwatch 2",
    "rocketleague.exe": "Rocket League", "cod.exe": "Call of Duty", "destiny2.exe": "Destiny 2",
    "dota2.exe": "Dota 2", "marvel-win64-shipping.exe": "Marvel Rivals",
}

# In-match game traffic (not launchers/clients), for "were you playing when it spiked?"
GAMES = {"League of Legends", "Valorant", "Apex Legends", "Counter-Strike 2", "Fortnite", "Overwatch 2",
         "Rocket League", "Call of Duty", "Destiny 2", "Dota 2", "Marvel Rivals"}

# svchost.exe hosts many Windows services; name the one(s) inside instead.
FRIENDLY_SERVICE = {
    "DoSvc": "Windows Update P2P sharing (Delivery Optimization)",
    "wuauserv": "Windows Update", "UsoSvc": "Windows Update", "BITS": "Background downloads (BITS)",
    "WaaSMedicSvc": "Windows Update", "InstallService": "Microsoft Store installs",
    "Dnscache": "Windows DNS", "CryptSvc": "Windows certificates", "LanmanWorkstation": "Windows file sharing",
    "iphlpsvc": "Windows IP helper", "WpnService": "Windows notifications",
}


class AppNamer:
    """Turns a PID into a friendly program name, remembering names after the process exits."""

    def __init__(self):
        """Start with empty caches; services are re-read every few minutes."""
        self._names: dict[int, str] = {}
        self._svc_by_pid: dict[int, list[str]] = {}
        self._svc_loaded = 0.0

    def _load_services(self) -> None:
        """Map svchost PIDs to the services they host (needs admin for most of them)."""
        m: dict[int, list[str]] = defaultdict(list)
        try:
            for s in psutil.win_service_iter():
                try:
                    pid = s.pid()
                    if pid:
                        m[pid].append(s.name())
                except (psutil.Error, OSError):
                    continue
        except (psutil.Error, OSError):
            pass
        self._svc_by_pid = dict(m)
        self._svc_loaded = time.time()

    def name(self, pid: int) -> str:
        """Friendly name for `pid`."""
        if pid in self._names:
            return self._names[pid]
        if pid == 0:
            return "Windows (kernel)"
        if pid == 4:
            return "Windows (system)"
        try:
            exe = psutil.Process(pid).name()
        except (psutil.Error, OSError):
            # Exited before we could look it up; don't cache so a reused PID gets a fresh look.
            return "Short-lived program (closed before it could be named)"
        label = FRIENDLY_EXE.get(exe.lower())
        if exe.lower() == "svchost.exe":
            if time.time() - self._svc_loaded > 300 or pid not in self._svc_by_pid:
                self._load_services()
            services = self._svc_by_pid.get(pid, [])
            known = [FRIENDLY_SERVICE[s] for s in services if s in FRIENDLY_SERVICE]
            if known:
                label = known[0]
            elif services:
                label = f"Windows service ({', '.join(services[:2])})"
            else:
                label = "Windows service (svchost)"
        if label is None:
            label = exe[:-4] if exe.lower().endswith(".exe") else exe
        self._names[pid] = label
        return label

    def forget_dead(self) -> list[int]:
        """Drop cached PIDs that no longer exist (Windows reuses PIDs). Returns the dropped ones."""
        alive = set(psutil.pids())
        # list() snapshot: the namer thread may be adding entries meanwhile.
        dead = [pid for pid in list(self._names) if pid not in alive]
        for pid in dead:
            self._names.pop(pid, None)
        return dead


def _local_addresses() -> set[bytes]:
    """All IP addresses of this PC, packed, for telling 'me' from 'them' in each event."""
    out: set[bytes] = set()
    for addrs in psutil.net_if_addrs().values():
        for a in addrs:
            try:
                if a.family == socket.AF_INET:
                    out.add(socket.inet_aton(a.address))
                elif a.family == socket.AF_INET6:
                    out.add(socket.inet_pton(socket.AF_INET6, a.address.split("%")[0]))
            except OSError:
                continue
    return out


def primary_interface() -> str | None:
    """Name of the network card used to reach the internet (e.g. 'Ethernet')."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("1.1.1.1", 53))  # UDP connect sends nothing; it just picks a route
            my_ip = s.getsockname()[0]
    except OSError:
        return None
    for nic, addrs in psutil.net_if_addrs().items():
        if any(a.address == my_ip for a in addrs):
            return nic
    return None


class PCMonitor:
    """Samples the network card and (optionally) the ETW per-program feed."""

    def __init__(self):
        """Pick the network card and try to start per-program tracing."""
        self.nic = primary_interface()
        self._last_nic = self._nic_counters()
        self._last_t = time.time()
        self.namer = AppNamer()
        self._local = _local_addresses()
        self._local_t = time.time()
        self.trace: NetworkEventTrace | None = None
        self.trace_error: str | None = None
        self._naming = threading.Event()
        try:
            t = NetworkEventTrace()
            t.start()
            self.trace = t
            threading.Thread(target=self._name_new_pids, name="namer", daemon=True).start()
        except PermissionError as e:
            self.trace_error = str(e)
            log.warning("%s -- showing PC totals only", e)
        except OSError as e:
            self.trace_error = f"Per-program tracking unavailable: {e}"
            log.warning(self.trace_error)

    def _name_new_pids(self) -> None:
        """Twice a second, name programs that just started using the network, while they still exist."""
        while not self._naming.wait(0.5):
            for pid in self.trace.take_new_pids():
                self.namer.name(pid)

    def _nic_counters(self):
        """(bytes received, bytes sent) for the primary card, or None."""
        if not self.nic:
            return None
        c = psutil.net_io_counters(pernic=True).get(self.nic)
        return (c.bytes_recv, c.bytes_sent) if c else None

    def sample(self) -> dict:
        """Everything since the last call: card totals, per-app internet bytes, big flows.

        Returns {"secs", "rx", "tx", "apps": {app: [rx, tx]}, "remotes": {(app, ip): [rx, tx]}}.
        """
        now = time.time()
        secs = max(now - self._last_t, 0.001)
        cur = self._nic_counters()
        rx = tx = 0
        if cur and self._last_nic:
            # Counters can reset if the adapter restarts; never report negatives.
            rx = max(cur[0] - self._last_nic[0], 0)
            tx = max(cur[1] - self._last_nic[1], 0)
        self._last_nic, self._last_t = cur, now

        apps: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        remotes: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
        if self.trace:
            housekeeping = now - self._local_t > 60
            if housekeeping:
                self._local, self._local_t = _local_addresses(), now
            for (pid, is_send, a, b), size in self.trace.drain().items():
                remote = b if a in self._local else a
                if remote in self._local:
                    continue  # traffic to ourselves (loopback, VM host-only adapter)
                ip = ipaddress.ip_address(remote)
                if not ip.is_global:
                    continue  # LAN traffic (printer, NAS, casting) doesn't use the internet line
                app = self.namer.name(pid)
                idx = 1 if is_send else 0
                apps[app][idx] += size
                remotes[(app, str(ip))][idx] += size
            if housekeeping:
                # Only after this batch is named, or programs that exited in the
                # last 10 seconds would lose their names.
                self.trace.forget_pids(self.namer.forget_dead())
        return {"secs": secs, "rx": rx, "tx": tx, "apps": dict(apps), "remotes": dict(remotes)}

    def stop(self) -> None:
        """Shut down the ETW session so it doesn't linger after we exit."""
        self._naming.set()
        if self.trace:
            self.trace.stop()


class HostnameResolver:
    """Background lookups of 'which website is this IP', for the big flows only.

    First choice is Windows' own DNS cache (the name a program actually asked
    for, e.g. 'steamcdn-a.akamaihd.net'); reverse DNS is the fallback.
    """

    def __init__(self, db):
        """`db` is the shared DB; names are cached in its hostnames table."""
        self.db = db
        self._pending: set[str] = set()
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="rdns")
        self._dns_cache: dict[str, str] = {}
        self._dns_cache_t = 0.0

    def want(self, ips) -> None:
        """Queue IPs that don't have a cached name yet."""
        known = {r["ip"] for r in self.db.query(
            f"SELECT ip FROM hostnames WHERE ip IN ({','.join('?' * len(ips))})", list(ips))} if ips else set()
        with self._lock:
            new = [ip for ip in ips if ip not in known and ip not in self._pending]
            self._pending.update(new)
        if new:
            self._pool.submit(self._resolve_batch, new)

    def _refresh_dns_cache(self) -> None:
        """Parse `ipconfig /displaydns`: each block starts with the name that was looked up."""
        if time.time() - self._dns_cache_t < 60:
            return
        self._dns_cache_t = time.time()
        try:
            out = subprocess.run(["ipconfig", "/displaydns"], capture_output=True, text=True, timeout=15,
                                 creationflags=subprocess.CREATE_NO_WINDOW).stdout
        except (OSError, subprocess.SubprocessError):
            return
        lines = out.splitlines()
        header = None
        ip_re = re.compile(r":\s*([0-9a-fA-F:.]+)\s*$")
        for i, line in enumerate(lines):
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            if nxt.strip().startswith("----"):
                header = line.strip()
                continue
            m = ip_re.search(line)
            if header and m:
                try:
                    ipaddress.ip_address(m.group(1))
                except ValueError:
                    continue
                self._dns_cache[m.group(1)] = header

    def _resolve_batch(self, ips) -> None:
        """Resolve and store names; failures are stored as NULL so we don't retry forever."""
        self._refresh_dns_cache()
        rows = []
        for ip in ips:
            name = self._dns_cache.get(ip)
            if not name:
                try:
                    name = socket.gethostbyaddr(ip)[0]
                except OSError:
                    name = None
            rows.append((ip, name, int(time.time())))
        self.db.executemany("INSERT OR REPLACE INTO hostnames(ip, name, ts) VALUES(?, ?, ?)", rows)
        with self._lock:
            self._pending.difference_update(ips)
