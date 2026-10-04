"""Settings, stored as plain JSON in the data folder's config.json so they can be hand-edited.

The file is created with defaults on first run. Unknown keys are ignored and
missing keys fall back to the defaults, so old config files keep working.

Where the data folder is:
  - running from source: `data\\` next to the code
  - the packaged HogWatch.exe: `%LOCALAPPDATA%\\HogWatch` (an exe has no folder of its
    own to write to; when packed into one file its code lives in a temp folder)
  - either can be overridden with `--data-dir` or the HOGWATCH_DATA_DIR variable
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _cli_option(name: str) -> str | None:
    """Read `--name value` straight from the command line.

    Needed before argparse runs: the data folder decides where config itself lives,
    and modules capture DATA_DIR when they're imported.
    """
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return None


FROZEN = bool(getattr(sys, "frozen", False))  # True inside the packaged HogWatch.exe
ROOT = Path(__file__).resolve().parent.parent


def _default_data_dir() -> Path:
    if FROZEN:
        return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "HogWatch"
    return ROOT / "data"


DATA_DIR = Path(_cli_option("--data-dir") or os.environ.get("HOGWATCH_DATA_DIR") or _default_data_dir())
CONFIG_PATH = DATA_DIR / "config.json"
# Lets a second copy run beside the first (for testing) without editing its config.
PORT_OVERRIDE = _cli_option("--port") or os.environ.get("HOGWATCH_PORT")

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
    merged = dict(DEFAULTS)
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULTS, indent=2) + "\n", encoding="utf-8")
    else:
        try:
            user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            merged.update({k: v for k, v in user.items() if k in DEFAULTS})
        except (OSError, ValueError):
            pass  # a typo in the file shouldn't stop monitoring; run on defaults instead
    if PORT_OVERRIDE:
        merged["port"] = int(PORT_OVERRIDE)
    return merged
