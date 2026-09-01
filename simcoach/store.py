from __future__ import annotations

import os
import sqlite3
import threading
import time

import numpy as np

from simcoach.model_types import LapRecord

SCHEMA_VERSION = 3

# The coach conversation, persisted so a session's Q&A can be reviewed later and
# fed to cross-session memory. lap_id is the lap the question was asked about
# (NULL for pre-lap general questions). Defined separately so both fresh installs
# and the v2->v3 migration create the identical table.
_COACH_CHAT_SCHEMA = """
CREATE TABLE IF NOT EXISTS coach_chat (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL,
    session_id TEXT,
    track TEXT,
    car TEXT,
    lap_id INTEGER,
    question TEXT,
    answer TEXT
);
"""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS laps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL,
    sim TEXT,
    track TEXT,
    car TEXT,
    lap_time REAL,
    valid INTEGER,
    telemetry_path TEXT,
    phase TEXT,
    session_id TEXT
);
CREATE TABLE IF NOT EXISTS setups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL,
    track TEXT,
    car TEXT,
    params_json TEXT,
    source TEXT
);
CREATE TABLE IF NOT EXISTS corner_metrics (
    lap_id INTEGER,
    corner_no INTEGER,
    entry_spd REAL,
    apex_spd REAL,
    min_spd REAL,
    exit_spd REAL,
    brake_point REAL,
    throttle_point REAL,
    trailbrake_overlap REAL
);
""" + _COACH_CHAT_SCHEMA


class LapStore:
    def __init__(self, db_path: str, data_dir: str):
        self.data_dir = data_dir
        os.makedirs(self.data_dir, exist_ok=True)
        # check_same_thread=False + self._lock: the background capture thread
        # and foreground REPL thread share this connection; self._lock (RLock,
        # reentrant because best_lap() calls self.load() internally) serializes
        # access since sqlite3 itself does not guard against interleaved reads
        # mid-write (e.g. reading a laps row while telemetry_path is still NULL).
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.RLock()
        current_version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if current_version == 0:
            self.conn.executescript(_SCHEMA)
        else:
            # Cumulative migrations: an old db steps through each version it's behind.
            if current_version < 2:
                # v1 -> v2: add nullable session/phase metadata columns in place.
                self.conn.execute("ALTER TABLE laps ADD COLUMN phase TEXT")
                self.conn.execute("ALTER TABLE laps ADD COLUMN session_id TEXT")
            if current_version < 3:
                # v2 -> v3: add the coach chat log.
                self.conn.executescript(_COACH_CHAT_SCHEMA)
        if current_version != SCHEMA_VERSION:
            self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.conn.commit()

    def save(self, record: LapRecord, metrics: list | None = None) -> int:
        telemetry_path = None
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    "INSERT INTO laps (ts, sim, track, car, lap_time, valid, telemetry_path, "
                    "phase, session_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (record.timestamp, record.sim, record.track, record.car,
                     record.lap_time, int(record.valid), None,
                     record.phase, record.session_id),
                )
                lap_id = cur.lastrowid
                telemetry_path = os.path.join(self.data_dir, f"{lap_id}.npz")
                np.savez_compressed(telemetry_path, **record.channels)
                self.conn.execute(
                    "UPDATE laps SET telemetry_path = ? WHERE id = ?",
                    (telemetry_path, lap_id),
                )
                if metrics:
                    self.conn.executemany(
                        "INSERT INTO corner_metrics (lap_id, corner_no, entry_spd, apex_spd, "
                        "min_spd, exit_spd, brake_point, throttle_point, trailbrake_overlap) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [
                            (lap_id, m.corner_no, m.entry_spd, m.apex_spd, m.min_spd,
                             m.exit_spd, m.brake_point, m.throttle_point, m.trailbrake_overlap)
                            for m in metrics
                        ],
                    )
            return lap_id
        except Exception:
            if telemetry_path and os.path.exists(telemetry_path):
                os.remove(telemetry_path)
            raise

    def load(self, lap_id: int) -> LapRecord:
        with self._lock:
            row = self.conn.execute(
                "SELECT ts, sim, track, car, lap_time, valid, telemetry_path, phase, session_id "
                "FROM laps WHERE id = ?",
                (lap_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"no lap with id {lap_id}")
            ts, sim, track, car, lap_time, valid, telemetry_path, phase, session_id = row
            with np.load(telemetry_path) as npz:
                channels = {k: npz[k] for k in npz.files}
            return LapRecord(sim, track, car, lap_time, bool(valid), ts, channels, phase, session_id)

    def best_lap(self, track: str, car: str, exclude_lap_id: int | None = None,
                 phase: str | None = None) -> LapRecord | None:
        with self._lock:
            query = "SELECT id FROM laps WHERE track = ? AND car = ? AND valid = 1"
            params: list = [track, car]
            if exclude_lap_id is not None:
                query += " AND id != ?"
                params.append(exclude_lap_id)
            if phase is not None:
                query += " AND phase = ?"
                params.append(phase)
            query += " ORDER BY lap_time ASC LIMIT 1"
            row = self.conn.execute(query, params).fetchone()
            if row is None:
                return None
            return self.load(row[0])

    def list_lap_ids(self, track: str, car: str) -> list[int]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT id FROM laps WHERE track = ? AND car = ?",
                (track, car),
            ).fetchall()
            return [r[0] for r in rows]

    def session_lap_ids(self, session_id: str) -> list[int]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT id FROM laps WHERE session_id = ? ORDER BY id ASC",
                (session_id,),
            ).fetchall()
            return [r[0] for r in rows]

    def save_chat(self, session_id: str, track: str | None, car: str | None,
                  lap_id: int | None, question: str, answer: str,
                  ts: float | None = None) -> None:
        """Append one coach Q&A exchange to the persistent chat log."""
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO coach_chat (ts, session_id, track, car, lap_id, question, answer) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ts if ts is not None else time.time(), session_id, track, car,
                 lap_id, question, answer),
            )

    def session_chat(self, session_id: str) -> list[dict]:
        """Every Q&A exchange in a session, oldest first (for review / memory)."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT ts, track, car, lap_id, question, answer FROM coach_chat "
                "WHERE session_id = ? ORDER BY id ASC",
                (session_id,),
            ).fetchall()
            keys = ("ts", "track", "car", "lap_id", "question", "answer")
            return [dict(zip(keys, r)) for r in rows]

    def recent_session_ids(self, limit: int = 10) -> list[str]:
        """Chat session ids, most recent first."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT session_id, MAX(ts) t FROM coach_chat GROUP BY session_id "
                "ORDER BY t DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [r[0] for r in rows]
