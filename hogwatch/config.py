"""Settings, stored as plain JSON in data/config.json so they can be hand-edited.

The file is created with defaults on first run. Unknown keys are ignored and
missing keys fall back to the defaults, so old config files keep working.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CONFIG_PATH = DATA_DIR / "config.json"

DEFAULTS: dict = {
    # Dashboard port. Only bound to 127.0.0.1, so nobody else on the network can see it.
    "port": 8765,
    # How often to ping. Every second, so a 2-5 second game dropout shows up as
    # several lost pings and can be pinned to a hop. (4-5 tiny pings per second.)
    "ping_interval_s": 1.0,
    "ping_timeout_ms": 1000,
    # Two well-run anycast servers; we take the faster of the two each round so
    # one server having a hiccup never looks like *your* line being slow.
    "internet_targets": ["1.1.1.1", "8.8.8.8"],
    # The eero's address. null = auto-detect (it's the first hop out of this PC).
    # The ISP gateway is always timed as "hop 2" because AT&T gateways ignore
    # pings sent straight to them but do answer hop-limited probes.
    "lan_target": None,
    # eero cloud polling. Faster during a slowdown so we catch the culprit in the act.
    "eero_poll_s": 30,
    "eero_poll_during_incident_s": 10,
    # A slowdown = internet ping more than `slow_extra_ms` above normal AND at
    # least `slow_min_ms` total (so a 15 -> 40 ms wobble doesn't count).
    "slow_extra_ms": 80,
    "slow_min_ms": 100,
    # Your internet plan speeds in Mbps. null = use the eero's last speed test.
    "plan_down_mbps": None,
    "plan_up_mbps": None,
    # How long to keep history.
    "retention_days": 14,
}


def load() -> dict:
    """Return the merged config (defaults + data/config.json), creating the file if missing."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULTS, indent=2) + "\n", encoding="utf-8")
        return dict(DEFAULTS)
    try:
        user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # A typo in the file shouldn't stop monitoring; run on defaults instead.
        return dict(DEFAULTS)
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in user.items() if k in DEFAULTS})
    return merged
