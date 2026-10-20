"""SQLite storage for time-series metrics, process snapshots, alerts, actions and incident history."""
import json
import sqlite3
import threading
import time
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS metrics (
    ts REAL PRIMARY KEY,
    cpu_percent REAL, load1 REAL, iowait REAL,
    mem_percent REAL, mem_used_mb REAL, mem_available_mb REAL, mem_total_mb REAL,
    swap_percent REAL, swap_used_mb REAL,
    swap_in_sec REAL, swap_out_sec REAL, major_faults_sec REAL, minor_faults_sec REAL,
    disk_percent REAL, disk_free_gb REAL, disk_read_mb_sec REAL, disk_write_mb_sec REAL,
    disk_busy_percent REAL, ctx_switches_sec REAL,
    proc_total INTEGER, proc_running INTEGER, proc_sleeping INTEGER, proc_zombie INTEGER,
    thread_total INTEGER, page_faults_sec REAL, disk_latency_ms REAL
);
CREATE TABLE IF NOT EXISTS processes (
    ts REAL, pid INTEGER, ppid INTEGER, name TEXT, username TEXT, status TEXT,
    cpu_percent REAL, rss_mb REAL, mem_percent REAL, num_threads INTEGER,
    nice INTEGER, open_files INTEGER, create_time REAL, cmdline TEXT, io_mb_sec REAL
);
CREATE INDEX IF NOT EXISTS idx_proc_ts ON processes(ts);
CREATE INDEX IF NOT EXISTS idx_proc_pid ON processes(pid, ts);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, issue_type TEXT, severity TEXT, pid INTEGER, process TEXT,
    title TEXT, details TEXT, evidence TEXT, status TEXT DEFAULT 'open', resolved_ts REAL
);
CREATE TABLE IF NOT EXISTS predictions (
    ts REAL, resource TEXT, current REAL, slope_per_min REAL, minutes_to_limit REAL, r2 REAL,
    status TEXT, n_samples INTEGER, span_min REAL, message TEXT
);
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, alert_id INTEGER, action TEXT, pid INTEGER, process TEXT,
    risk TEXT, decision TEXT, result TEXT, message TEXT,
    issue_type TEXT, approved INTEGER, before TEXT, after TEXT, resolved INTEGER, verification TEXT
);
CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, alert_id INTEGER, issue_type TEXT, process TEXT, cause TEXT,
    actions TEXT, outcome TEXT, fixed INTEGER, before TEXT, after TEXT
);
"""


class Database:
    """Thread-safe SQLite wrapper (one connection per call, WAL mode)."""

    def __init__(self, path=None):
        self.path = str(path or config.DB_PATH)
        self._lock = threading.Lock()
        with self.conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)
            self._migrate(c)

    # Columns added after the first release. Existing databases are upgraded in place (no data lost).
    MIGRATIONS = {
        "metrics": {"page_faults_sec": "REAL", "disk_latency_ms": "REAL"},
        "processes": {"io_mb_sec": "REAL"},
        "predictions": {"status": "TEXT", "n_samples": "INTEGER", "span_min": "REAL", "message": "TEXT"},
        "actions": {"issue_type": "TEXT", "approved": "INTEGER", "before": "TEXT", "after": "TEXT",
                    "resolved": "INTEGER", "verification": "TEXT"},
    }

    def _migrate(self, c):
        for table, cols in self.MIGRATIONS.items():
            have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
            for col, typ in cols.items():
                if col not in have:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    # ---------- writes ----------
    def insert_metrics(self, m: dict):
        cols = ",".join(m.keys())
        qs = ",".join("?" for _ in m)
        with self._lock, self.conn() as c:
            c.execute(f"INSERT OR REPLACE INTO metrics ({cols}) VALUES ({qs})", list(m.values()))

    def insert_processes(self, ts, procs):
        rows = [(ts, p["pid"], p["ppid"], p["name"], p["username"], p["status"], p["cpu_percent"],
                 p["rss_mb"], p["mem_percent"], p["num_threads"], p["nice"], p["open_files"],
                 p["create_time"], p["cmdline"], p.get("io_mb_sec")) for p in procs]
        with self._lock, self.conn() as c:
            c.executemany("INSERT INTO processes (ts, pid, ppid, name, username, status, cpu_percent, rss_mb,"
                          " mem_percent, num_threads, nice, open_files, create_time, cmdline, io_mb_sec)"
                          " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)

    def add_alert(self, issue):
        with self._lock, self.conn() as c:
            cur = c.execute(
                "INSERT INTO alerts (ts, issue_type, severity, pid, process, title, details, evidence)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (time.time(), issue.issue_type, issue.severity, issue.pid, issue.process,
                 issue.title, issue.details, json.dumps(issue.evidence, default=str)))
            return cur.lastrowid

    def set_alert_status(self, alert_id, status):
        with self._lock, self.conn() as c:
            c.execute("UPDATE alerts SET status=?, resolved_ts=? WHERE id=?",
                      (status, time.time() if status == "resolved" else None, alert_id))

    def add_prediction(self, p):
        with self._lock, self.conn() as c:
            c.execute("INSERT INTO predictions (ts, resource, current, slope_per_min, minutes_to_limit, r2,"
                      " status, n_samples, span_min, message) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (time.time(), p.resource, p.current, p.slope_per_min, p.minutes_to_limit, p.r2,
                       p.status, p.n_samples, p.span_min, p.message))

    def log_action(self, alert_id, action, pid, process, risk, decision, result, message,
                   issue_type=None, approved=None):
        with self._lock, self.conn() as c:
            cur = c.execute(
                "INSERT INTO actions (ts, alert_id, action, pid, process, risk, decision, result, message,"
                " issue_type, approved) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (time.time(), alert_id, action, pid, process, risk, decision, result, message, issue_type,
                 None if approved is None else int(bool(approved))))
            return cur.lastrowid

    def record_verification(self, action_id, before, after, resolved, verification):
        """Attach the fix-verification result (before/after metrics) to an executed action."""
        with self._lock, self.conn() as c:
            c.execute("UPDATE actions SET before=?, after=?, resolved=?, verification=? WHERE id=?",
                      (json.dumps(before, default=str), json.dumps(after, default=str), int(bool(resolved)),
                       verification, action_id))

    def add_incident(self, alert_id, issue_type, process, cause, actions, outcome, fixed, before, after):
        with self._lock, self.conn() as c:
            c.execute(
                "INSERT INTO incidents (ts, alert_id, issue_type, process, cause, actions, outcome, fixed, before, after)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (time.time(), alert_id, issue_type, process, cause, json.dumps(actions), outcome,
                 int(bool(fixed)), json.dumps(before, default=str), json.dumps(after, default=str)))

    def prune(self, hours=None):
        cutoff = time.time() - 3600 * (hours or config.RETENTION_HOURS)
        with self._lock, self.conn() as c:
            c.execute("DELETE FROM metrics WHERE ts < ?", (cutoff,))
            c.execute("DELETE FROM processes WHERE ts < ?", (cutoff,))
            c.execute("DELETE FROM predictions WHERE ts < ?", (cutoff,))

    # ---------- reads ----------
    def query(self, sql, params=()):
        with self.conn() as c:
            return [dict(r) for r in c.execute(sql, params).fetchall()]

    def recent_metrics(self, n=300):
        rows = self.query("SELECT * FROM metrics ORDER BY ts DESC LIMIT ?", (n,))
        return list(reversed(rows))

    def latest_processes(self):
        row = self.query("SELECT MAX(ts) AS ts FROM processes")
        if not row or row[0]["ts"] is None:
            return []
        return self.query("SELECT * FROM processes WHERE ts=? ORDER BY cpu_percent DESC", (row[0]["ts"],))

    def process_history(self, pid, n=120):
        rows = self.query("SELECT * FROM processes WHERE pid=? ORDER BY ts DESC LIMIT ?", (pid, n))
        return list(reversed(rows))

    def process_history_since(self, since_ts):
        return self.query("SELECT ts, pid, name, rss_mb, cpu_percent, create_time FROM processes"
                          " WHERE ts >= ? ORDER BY ts", (since_ts,))

    def open_alerts(self):
        return self.query("SELECT * FROM alerts WHERE status IN ('open','in_progress') ORDER BY ts DESC")

    def all_alerts(self, n=200):
        return self.query("SELECT * FROM alerts ORDER BY ts DESC LIMIT ?", (n,))

    def get_alert(self, alert_id):
        r = self.query("SELECT * FROM alerts WHERE id=?", (alert_id,))
        return r[0] if r else None

    def last_alert_of(self, issue_type, pid):
        r = self.query("SELECT * FROM alerts WHERE issue_type=? AND IFNULL(pid,-1)=IFNULL(?,-1)"
                       " ORDER BY ts DESC LIMIT 1", (issue_type, pid))
        return r[0] if r else None

    def recent_actions(self, n=100):
        return self.query("SELECT * FROM actions ORDER BY ts DESC LIMIT ?", (n,))

    def incidents(self, n=100):
        return self.query("SELECT * FROM incidents ORDER BY ts DESC LIMIT ?", (n,))

    def latest_predictions(self, max_age_sec=120):
        """Most recent prediction per resource, ignoring stale rows from a previous run."""
        return self.query("SELECT p.* FROM predictions p JOIN (SELECT resource, MAX(ts) mts FROM predictions"
                          " GROUP BY resource) q ON p.resource=q.resource AND p.ts=q.mts WHERE p.ts >= ?",
                          (time.time() - max_age_sec,))
