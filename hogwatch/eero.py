"""Read live per-device usage from the eero cloud -- the same numbers the eero app shows.

eero has no local API; the app talks to https://api-user.e2ro.com. This is
the unofficial but long-stable API used by the Home Assistant eero
integrations (schmittx/home-assistant-eero, 343max/eero-client):

    POST /2.2/login          {"login": "<email or phone>"}  -> data.user_token
    POST /2.2/login/verify   {"code": "<code texted/emailed>"}  (cookie s=<token>)
    POST /2.2/login/refresh  -> new data.user_token (when the session expires)
    GET  /2.2/account        -> data.networks.data[] = {url, name}
    GET  <network url>       -> data.speed.{down,up}.{value,units} (last speed test)
    GET  <network url>/devices -> data[] with usage.{down_mbps,up_mbps}, nickname, ...

Your eero account must be an admin on the network (the owner can add you in
the eero app: Settings > Network settings > Admins).
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

API = "https://api-user.e2ro.com"


class EeroError(Exception):
    """Any failure talking to eero."""


class LoginNeeded(EeroError):
    """No session, or the session can no longer be refreshed."""


class Eero:
    """Minimal eero cloud client. The session token is saved in data/eero_session.json."""

    def __init__(self, session_path: Path):
        """Load a saved session if there is one."""
        self.path = session_path
        self.token: str | None = None
        self.network_url: str | None = None
        self.network_name: str | None = None
        if session_path.exists():
            try:
                s = json.loads(session_path.read_text(encoding="utf-8"))
                self.token = s.get("token")
                self.network_url = s.get("network_url")
                self.network_name = s.get("network_name")
            except (OSError, ValueError):
                pass

    @property
    def ready(self) -> bool:
        """True once logged in and a network has been chosen."""
        return bool(self.token and self.network_url)

    def save(self) -> None:
        """Persist the session. The token is a login credential -- the file stays local."""
        self.path.write_text(json.dumps({
            "token": self.token, "network_url": self.network_url, "network_name": self.network_name,
        }, indent=2), encoding="utf-8")

    def _call(self, method: str, path: str, body: dict | None = None, retry: bool = True):
        """Make one API call and return its `data` field, refreshing the session once if needed."""
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                   "User-Agent": "HogWatch/1.0 (home network monitor)"}
        if self.token:
            headers["Cookie"] = f"s={self.token}"
        data = json.dumps(body).encode() if body is not None else (b"" if method == "POST" else None)
        req = urllib.request.Request(API + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                payload = json.load(resp)
        except urllib.error.HTTPError as e:
            try:
                err = (json.load(e).get("meta") or {}).get("error", "")
            except ValueError:
                err = ""
            if e.code == 401 and retry and path != "/2.2/login/refresh" and self.token:
                self.refresh()
                return self._call(method, path, body, retry=False)
            if e.code == 401:
                raise LoginNeeded(err or "eero session expired") from e
            raise EeroError(f"eero API {e.code} {err}".strip()) from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise EeroError(f"can't reach eero cloud: {e}") from e
        return payload.get("data")

    # --- login flow (interactive, see `python -m hogwatch eero-login`) ---

    def start_login(self, login: str) -> None:
        """Ask eero to send a verification code to this email/phone."""
        self.token = None
        data = self._call("POST", "/2.2/login", {"login": login}, retry=False)
        self.token = data["user_token"]

    def verify(self, code: str) -> None:
        """Complete login with the code the user received."""
        self._call("POST", "/2.2/login/verify", {"code": code.strip()}, retry=False)

    def refresh(self) -> None:
        """Swap an expired session token for a fresh one."""
        try:
            data = self._call("POST", "/2.2/login/refresh", retry=False)
        except EeroError as e:
            raise LoginNeeded("eero session expired -- run eero-login again") from e
        self.token = data["user_token"]
        self.save()

    def networks(self) -> list[dict]:
        """Networks this account can manage: [{url, name}]."""
        acct = self._call("GET", "/2.2/account")
        return [{"url": n.get("url"), "name": n.get("name")} for n in (acct.get("networks") or {}).get("data", [])]

    # --- data ---

    def network(self) -> dict:
        """Network details, including the last speed test."""
        return self._call("GET", self.network_url) or {}

    def devices(self) -> list[dict]:
        """Raw device list for the chosen network."""
        return self._call("GET", f"{self.network_url}/devices") or []

    def units(self) -> list[dict]:
        """Raw list of the eero units themselves (the mesh nodes)."""
        return self._call("GET", f"{self.network_url}/eeros") or []


def parse_unit(u: dict, now: float) -> dict:
    """Normalize one eero unit: where it is, how it links upstream, when it last reconnected.

    `wireless_upstream_node` names the eero it talks to over the air (e.g. the
    Upstairs eero -> Main over 5GHz). `since_cloud_connection_s` resets when a
    unit loses and regains its connection, which exposes drops between pings.
    """
    up = u.get("uptime") or {}
    parent = u.get("wireless_upstream_node") or {}
    cloud_s = up.get("since_cloud_connection_s")
    return {
        "name": u.get("location") or u.get("serial") or "eero",
        "ip": u.get("ip_address"),
        "gateway": bool(u.get("gateway")),
        "wired": bool(u.get("wired")) or u.get("connection_type") == "WIRED",
        "upstream": parent.get("name"),
        "radio": (parent.get("primary_mesh_radio") or "").replace("GHz", " GHz") or None,
        "model": u.get("model"),
        "reconnected_ts": int(now - cloud_s) if isinstance(cloud_s, (int, float)) else None,
    }


def uplink_chain(units: list[dict], start: str | None) -> list[dict]:
    """The eeros from `start` up to the main one, e.g. [Upstairs, Main] or [Den, Upstairs, Main]."""
    by_name = {u["name"]: u for u in units}
    gateway = next((u for u in units if u["gateway"]), None)
    chain: list[dict] = []
    cur = by_name.get(start) if start else None
    while cur is not None and cur not in chain and len(chain) < 6:
        chain.append(cur)
        if cur["gateway"]:
            break
        # A wired satellite has no wireless parent; it reaches the main eero over the cable.
        cur = by_name.get(cur["upstream"]) if cur["upstream"] else gateway
    return chain


def speed_from_network(net: dict) -> dict | None:
    """Pull {'down': Mbps, 'up': Mbps, 'date': ...} out of the eero's last speed test."""
    speed = net.get("speed") or {}

    def mbps(part):
        v = (speed.get(part) or {}).get("value")
        units = ((speed.get(part) or {}).get("units") or "Mbps").lower()
        if v is None:
            return None
        return float(v) * {"kbps": 0.001, "mbps": 1, "gbps": 1000}.get(units, 1)

    down, up = mbps("down"), mbps("up")
    if down is None and up is None:
        return None
    return {"down": down, "up": up, "date": speed.get("date")}


# Words that identify a device type from its name/maker. Checked in order.
_KIND_RULES = [
    ("directv", ("directv", "genie", "c61k", "c71k", "hr54", "hr44", "hs17", "osprey", "at&t tv", "att tv")),
    ("tv", ("roku", "fire tv", "firetv", "aft", "chromecast", "google tv", "apple tv", "appletv",
            "shield", "bravia", "vizio", "tcl", "hisense", "samsung tv", "lg tv", "webos", "tizen", "smart tv")),
    ("console", ("xbox", "playstation", "ps4", "ps5", "nintendo", "switch")),
    ("phone", ("iphone", "pixel", "galaxy", "android", "oneplus", "motorola", "phone")),
    ("tablet", ("ipad", "tablet", "kindle")),
    ("computer", ("desktop", "laptop", "pc", "macbook", "imac", "windows", "surface", "thinkpad")),
    ("speaker", ("echo", "alexa", "sonos", "homepod", "google home", "nest mini", "nest audio")),
    ("camera", ("camera", "ring", "wyze", "arlo", "blink", "nest cam", "doorbell")),
]
_EERO_TYPE_MAP = {
    "computer": "computer", "laptop": "computer", "desktop": "computer", "pc": "computer",
    "phone": "phone", "mobile": "phone", "tablet": "tablet", "tv": "tv", "media_player": "tv",
    "streaming": "tv", "game_console": "console", "gaming": "console", "speaker": "speaker",
    "camera": "camera",
}


def _mentions(text: str, words) -> bool:
    """True if any word appears at the start of a word in `text` ("ring" matches "Ring-Doorbell", not "spring")."""
    return any(re.search(r"(?<![a-z0-9])" + re.escape(w), text) for w in words)


def classify(name: str, hostname: str, manufacturer: str, device_type: str) -> str:
    """Best-guess device kind; DirecTV boxes are singled out since they're suspects."""
    text = " ".join(x for x in (name, hostname, manufacturer) if x).lower()
    for kind, words in _KIND_RULES[:1]:  # DirecTV beats everything, even eero's own label
        if _mentions(text, words):
            return kind
    if device_type and device_type.lower() in _EERO_TYPE_MAP:
        return _EERO_TYPE_MAP[device_type.lower()]
    for kind, words in _KIND_RULES[1:]:
        if _mentions(text, words):
            return kind
    return "other"


def parse_device(d: dict) -> dict:
    """Normalize one raw eero device record into the fields HogWatch uses."""
    mac = (d.get("mac") or "").lower()
    hostname = d.get("hostname") or ""
    manufacturer = d.get("manufacturer") or ""
    name = d.get("nickname") or d.get("display_name") or hostname or (
        f"{manufacturer or 'Unknown device'} ({mac[-5:]})")
    usage = d.get("usage") or {}
    profile = d.get("profile") or {}
    if isinstance(profile, dict) and (profile.get("name") or "").strip().lower() in ("", "unassigned"):
        profile = {}  # eero's placeholder for "no profile", not a person
    wired = d.get("connection_type") == "wired" or d.get("wireless") is False
    return {
        "mac": mac,
        "name": name,
        "owner": profile.get("name") if isinstance(profile, dict) else None,
        "ip": d.get("ip") or ((d.get("ips") or [None])[0]),
        "manufacturer": manufacturer,
        "kind": classify(name, hostname, manufacturer, d.get("device_type") or ""),
        "connection": "wired" if wired else "wifi",
        "node": (d.get("source") or {}).get("location"),
        "connected": bool(d.get("connected")),
        "down_mbps": usage.get("down_mbps"),
        "up_mbps": usage.get("up_mbps"),
    }
