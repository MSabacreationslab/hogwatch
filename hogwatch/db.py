"""SQLite storage. One file (data/hogwatch.db), shared by all collector threads.

Everything is stored as small time-bucketed rows (ts = unix seconds) so a
couple of weeks of history stays in the tens of MB.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
-- Ping results, one row per 10-second bucket per role (node / lan / isp / internet /
-- unit:<eero name>). jitter_ms = average change between consecutive pings.
CREATE TABLE IF NOT EXISTS latency (
    ts INTEGER NOT NULL, role TEXT NOT NULL,
    sent INTEGER NOT NULL, lost INTEGER NOT NULL,
    avg_ms REAL, max_ms REAL
);
CREATE INDEX IF NOT EXISTS latency_ts ON latency(ts);

-- Everything through this PC's network card (includes LAN traffic).
CREATE TABLE IF NOT EXISTS pc_total (
    ts INTEGER NOT NULL, secs REAL NOT NULL, rx_bytes INTEGER, tx_bytes INTEGER
);
CREATE INDEX IF NOT EXISTS pc_total_ts ON pc_total(ts);

-- Internet traffic per program on this PC (needs admin).
CREATE TABLE IF NOT EXISTS pc_app (
    ts INTEGER NOT NULL, app TEXT NOT NULL, rx_bytes INTEGER, tx_bytes INTEGER
);
CREATE INDEX IF NOT EXISTS pc_app_ts ON pc_app(ts);

-- Which servers each program talked to (only the big flows, >= 256 KB per 10s).
CREATE TABLE IF NOT EXISTS pc_remote (
    ts INTEGER NOT NULL, app TEXT NOT NULL, ip TEXT NOT NULL, rx_bytes INTEGER, tx_bytes INTEGER
);
CREATE INDEX IF NOT EXISTS pc_remote_ts ON pc_remote(ts);

CREATE TABLE IF NOT EXISTS hostnames (ip TEXT PRIMARY KEY, name TEXT, ts INTEGER);

-- Live per-device rates as reported by the eero, one row per device per poll.
-- secs = time since the previous poll, so rate * secs estimates the volume.
CREATE TABLE IF NOT EXISTS eero_sample (
    ts INTEGER NOT NULL, mac TEXT NOT NULL, down_mbps REAL, up_mbps REAL, secs REAL
);
CREATE INDEX IF NOT EXISTS eero_sample_ts ON eero_sample(ts);

CREATE TABLE IF NOT EXISTS eero_device (
    mac TEXT PRIMARY KEY, name TEXT, owner TEXT, ip TEXT, manufacturer TEXT,
    kind TEXT, connection TEXT, node TEXT, last_seen INTEGER
);

CREATE TABLE IF NOT EXISTS incident (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_ts INTEGER NOT NULL, end_ts INTEGER,
    kind TEXT, peak_ms REAL, normal_ms REAL, loss_pct REAL,
    headline TEXT, details TEXT
);
CREATE INDEX IF NOT EXISTS incident_start ON incident(start_ts);

-- Short lag spikes / dropouts (seconds), with the hop they happened on.
CREATE TABLE IF NOT EXISTS hiccup (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_ts REAL NOT NULL, end_ts REAL NOT NULL,
    where_ TEXT, where_text TEXT, rounds INTEGER, lost INTEGER,
    worst_ms REAL, worst_lan_ms REAL, worst_node_ms REAL,
    game TEXT, busy TEXT
);
CREATE INDEX IF NOT EXISTS hiccup_start ON hiccup(start_ts);

-- An eero unit reconnecting to eero's servers: its link dropped at some point,
-- even if that happened between our pings.
CREATE TABLE IF NOT EXISTS eero_event (ts INTEGER NOT NULL, unit TEXT NOT NULL, kind TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS eero_event_ts ON eero_event(ts);

-- Each eero's health every 2 minutes: how it links to the others, its radios and
-- Ethernet ports. Evidence for an installer (e.g. a wall cable that never links).
CREATE TABLE IF NOT EXISTS unit_sample (
    ts INTEGER NOT NULL, unit TEXT NOT NULL, gateway INTEGER, wired INTEGER,
    upstream TEXT, radio TEXT, bars INTEGER, status TEXT, firmware TEXT,
    wired_clients INTEGER, wireless_clients INTEGER,
    bands TEXT,  -- JSON {"5 GHz": {"channel", "width", "utilization", "clients", "tx_power"}, ...}
    ports TEXT   -- JSON [{"port", "carrier", "speed", "wan"}, ...]
);
CREATE INDEX IF NOT EXISTS unit_sample_ts ON unit_sample(ts);

-- This PC's own network card going down or up (rules the PC's cable in or out).
CREATE TABLE IF NOT EXISTS link_event (ts INTEGER NOT NULL, what TEXT NOT NULL, detail TEXT);
CREATE INDEX IF NOT EXISTS link_event_ts ON link_event(ts);

-- The eero's own speed tests (shows whether the internet line itself is healthy).
CREATE TABLE IF NOT EXISTS speedtest (date TEXT PRIMARY KEY, ts INTEGER, down REAL, up REAL);

CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""

# A dropout's end, capped by how many bad pings it had (at most 2 s apart). Dropouts
# recorded before sleep handling existed could stretch across hours of the PC sleeping.
HICCUP_END = "(start_ts + MIN(end_ts - start_ts, rounds * 2 + 4))"

# Columns added after the first release; older databases get them on startup.
MIGRATIONS = [("latency", "jitter_ms", "REAL")]


class DB:
    """Thread-safe wrapper: a single connection guarded by a lock.

    Write volume is tiny (a few dozen rows every 10s), so one lock is simpler
    and plenty fast compared with a connection per thread.
    """

    def __init__(self, path: Path):
        """Open (or create) the database and apply the schema."""
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            # WAL lets the dashboard read while the collector writes.
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.executescript(SCHEMA)
            for table, column, kind in MIGRATIONS:
                cols = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
                if column not in cols:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")

    def execute(self, sql: str, params=()) -> int | None:
        """Run one statement; returns lastrowid (useful after INSERT)."""
        with self.lock:
            return self.conn.execute(sql, params).lastrowid

    def executemany(self, sql: str, rows) -> None:
        """Insert many rows in one transaction."""
        rows = list(rows)
        if not rows:
            return
        with self.lock:
            self.conn.execute("BEGIN")
            try:
                self.conn.executemany(sql, rows)
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def query(self, sql: str, params=()) -> list[dict]:
        """Run a SELECT and return rows as plain dicts (JSON-ready)."""
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def rows(self, sql: str, params=()) -> list:
        """Like query(), but plain rows instead of dicts: lighter for big scans (reports)."""
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        """Read a small persisted value (e.g. the eero speed test result)."""
        rows = self.query("SELECT v FROM meta WHERE k = ?", (key,))
        return rows[0]["v"] if rows else default

    def set_meta(self, key: str, value: str) -> None:
        """Persist a small value."""
        self.execute("INSERT INTO meta(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v", (key, value))

    def prune(self, retention_days: int) -> None:
        """Delete history older than the retention window."""
        cutoff = int(time.time()) - retention_days * 86400
        for table in ("latency", "pc_total", "pc_app", "pc_remote", "eero_sample", "eero_event", "unit_sample",
                      "link_event", "speedtest"):
            self.execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
        self.execute("DELETE FROM incident WHERE start_ts < ?", (cutoff,))
        self.execute("DELETE FROM hiccup WHERE start_ts < ?", (cutoff,))
        self.execute("DELETE FROM hostnames WHERE ts < ?", (cutoff,))
