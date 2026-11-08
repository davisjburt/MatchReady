"""Run history in SQLite under %APPDATA%\\MatchReady."""
import json
import os
import sqlite3
import threading
import time
from pathlib import Path


def db_path():
    base = Path(os.environ.get("APPDATA", Path.home())) / "MatchReady"
    base.mkdir(parents=True, exist_ok=True)
    return base / "history.db"


class History:
    def __init__(self, path=None):
        self.conn = sqlite3.connect(str(path or db_path()), check_same_thread=False)
        self.conn.execute("""CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY, ts REAL NOT NULL, kind TEXT NOT NULL,
            rl INTEGER, ow INTEGER, network TEXT, metrics TEXT)""")
        # Older versions stored two per-game scores (rl, ow); now there's one score. Old rows keep the worse
        # of their two, which is what decided whether you could play.
        if "score" not in {r[1] for r in self.conn.execute("PRAGMA table_info(runs)")}:
            self.conn.execute("ALTER TABLE runs ADD COLUMN score INTEGER")
            self.conn.execute("UPDATE runs SET score = MIN(rl, ow) WHERE rl IS NOT NULL AND ow IS NOT NULL")
        # Day-long monitor: one row per session, one row per minute-sample (flattened by monitor.sample_from).
        self.conn.execute("""CREATE TABLE IF NOT EXISTS monitor_sessions (
            id INTEGER PRIMARY KEY, started REAL NOT NULL, planned_end REAL NOT NULL, ended REAL)""")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS monitor_samples (
            session INTEGER NOT NULL, ts REAL NOT NULL, data TEXT NOT NULL)""")
        self.conn.execute("CREATE INDEX IF NOT EXISTS monitor_samples_session ON monitor_samples (session, ts)")
        self.conn.commit()
        self.lock = threading.Lock()  # the monitor thread and check threads both write

    def _q(self, sql, args=()):
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    # ---------- monitor ----------
    def start_monitor(self, seconds, now=None):
        now = now or time.time()
        with self.lock:
            cur = self.conn.execute("INSERT INTO monitor_sessions (started, planned_end) VALUES (?, ?)",
                                    (now, now + seconds))
            self.conn.commit()
        return cur.lastrowid

    def end_monitor(self, session, now=None):
        with self.lock:
            self.conn.execute("UPDATE monitor_sessions SET ended = ? WHERE id = ?", (now or time.time(), session))
            self.conn.commit()

    def add_monitor_sample(self, session, sample):
        with self.lock:
            self.conn.execute("INSERT INTO monitor_samples (session, ts, data) VALUES (?, ?, ?)",
                              (session, sample["ts"], json.dumps(sample, default=str)))
            self.conn.commit()

    def monitor_samples(self, session):
        rows = self._q("SELECT data FROM monitor_samples WHERE session = ? ORDER BY ts", (session,))
        return [json.loads(r[0]) for r in rows]

    def monitor_sessions(self):
        """[(id, started, planned_end, ended, sample_count)] newest first."""
        return self._q(
            """SELECT s.id, s.started, s.planned_end, s.ended, COUNT(m.ts) FROM monitor_sessions s
               LEFT JOIN monitor_samples m ON m.session = s.id GROUP BY s.id ORDER BY s.started DESC""")

    def unfinished_monitor(self):
        """The newest session that never ended (app closed or PC restarted mid-run), or None."""
        rows = self._q("SELECT id, started, planned_end FROM monitor_sessions WHERE ended IS NULL "
                       "ORDER BY started DESC LIMIT 1")
        return rows[0] if rows else None

    def add(self, kind, score, network=None, metrics=None, ts=None):
        with self.lock:
            self.conn.execute("INSERT INTO runs (ts, kind, score, network, metrics) VALUES (?,?,?,?,?)",
                              (ts or time.time(), kind, score, network,
                               json.dumps(metrics, default=str) if metrics else None))
            self.conn.commit()

    def rows(self, since=0.0, network=None):
        """[(ts, kind, score)] oldest first. network=None means all networks."""
        q, args = "SELECT ts, kind, score FROM runs WHERE ts >= ?", [since]
        if network:
            q += " AND network = ?"
            args.append(network)
        return self._q(q + " ORDER BY ts", args)

    def last_full_metrics(self):
        rows = self._q("SELECT metrics FROM runs WHERE kind='full' AND metrics IS NOT NULL ORDER BY ts DESC LIMIT 1")
        row = rows[0] if rows else None
        return json.loads(row[0]) if row else None
