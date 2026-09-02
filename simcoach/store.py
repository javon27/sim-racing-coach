from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from uuid import uuid4

import numpy as np

from simcoach.model_types import LapRecord

SCHEMA_VERSION = 5

# The coach conversation, persisted so a session's Q&A can be reviewed later and
# fed to cross-session memory. lap_id is the lap the question was asked about
# (NULL for pre-lap general questions); tool_calls is a JSON list of the on-demand
# tools the agentic coach invoked that turn (added v4). chat_id (added v5) groups
# messages into a chat that can span multiple race sessions (session_id stays the
# race-session scope). Defined separately so both fresh installs and the
# migration create the identical table.
_COACH_CHAT_SCHEMA = """
CREATE TABLE IF NOT EXISTS coach_chat (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL,
    chat_id TEXT,
    session_id TEXT,
    track TEXT,
    car TEXT,
    lap_id INTEGER,
    question TEXT,
    answer TEXT,
    tool_calls TEXT
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


def _add_column(conn: sqlite3.Connection, table: str, column_def: str) -> None:
    """ALTER TABLE ... ADD COLUMN, tolerating a race where another process (or a
    partially-applied prior migration) already added it."""
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column_def}")
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e).lower():
            raise


def _decode(tc):
    """Decode a tool_calls JSON column, returning the raw string (not None) if
    a row is corrupted so the corruption stays visible instead of vanishing."""
    if tc is None:
        return None
    try:
        return json.loads(tc)
    except (TypeError, ValueError):
        return tc


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
        self.conn.execute("PRAGMA busy_timeout=5000")
        self._lock = threading.RLock()
        current_version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if current_version == 0:
            self.conn.executescript(_SCHEMA)
        else:
            # Cumulative migrations: an old db steps through each version it's behind.
            if current_version < 2:
                # v1 -> v2: add nullable session/phase metadata columns in place.
                _add_column(self.conn, "laps", "phase TEXT")
                _add_column(self.conn, "laps", "session_id TEXT")
            if current_version < 3:
                # v2 -> v3: add the coach chat log.
                self.conn.executescript(_COACH_CHAT_SCHEMA)
            if current_version < 4:
                # v3 -> v4: log the agentic coach's tool calls per turn. Guard the
                # ADD COLUMN: a <3 db just created coach_chat with tool_calls already.
                cols = [r[1] for r in self.conn.execute("PRAGMA table_info(coach_chat)")]
                if "tool_calls" not in cols:
                    _add_column(self.conn, "coach_chat", "tool_calls TEXT")
            if current_version < 5:
                # v4 -> v5: decouple chat from the race session via chat_id. Guard
                # the ADD COLUMN (a <3 db just created coach_chat with chat_id
                # already); the backfill runs unconditionally so a half-applied
                # migration (crash between ALTER and UPDATE) still completes.
                cols = [r[1] for r in self.conn.execute("PRAGMA table_info(coach_chat)")]
                if "chat_id" not in cols:
                    _add_column(self.conn, "coach_chat", "chat_id TEXT")
                self.conn.execute("UPDATE coach_chat SET chat_id='legacy' WHERE chat_id IS NULL")
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
                 phase: str | None = None, session_id: str | None = None) -> LapRecord | None:
        with self._lock:
            query = "SELECT id FROM laps WHERE track = ? AND car = ? AND valid = 1"
            params: list = [track, car]
            if exclude_lap_id is not None:
                query += " AND id != ?"
                params.append(exclude_lap_id)
            if phase is not None:
                query += " AND phase = ?"
                params.append(phase)
            if session_id is not None:
                query += " AND session_id = ?"
                params.append(session_id)
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

    def recent_laps(self, track: str, car: str, limit: int = 10,
                    session_id: str | None = None) -> list[dict]:
        """Recent laps for a track/car (newest first) as lightweight rows — no
        telemetry loaded — so a tool can list laps for the model to choose from.
        Pass session_id to scope to the current stint."""
        with self._lock:
            query = "SELECT id, lap_time, valid, phase FROM laps WHERE track = ? AND car = ?"
            params: list = [track, car]
            if session_id is not None:
                query += " AND session_id = ?"
                params.append(session_id)
            query += " ORDER BY id DESC LIMIT ?"
            params.append(limit)
            rows = self.conn.execute(query, params).fetchall()
            keys = ("lap_id", "lap_time", "valid", "phase")
            return [dict(zip(keys, r)) for r in rows]

    def session_lap_ids(self, session_id: str) -> list[int]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT id FROM laps WHERE session_id = ? ORDER BY id ASC",
                (session_id,),
            ).fetchall()
            return [r[0] for r in rows]

    def create_chat(self) -> str:
        """Mint a new chat id (a chat can span multiple race sessions). No row is
        created — a chat is implied by the messages saved under its id."""
        return f"chat-{time.strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:6]}"

    def save_chat(self, chat_id: str, session_id: str, track: str | None, car: str | None,
                  lap_id: int | None, question: str, answer: str,
                  tool_calls: list | None = None, ts: float | None = None) -> None:
        """Append one coach Q&A exchange (with the tools it invoked) to the log."""
        if chat_id is None:
            raise ValueError("chat_id is required")
        tc_json = json.dumps(tool_calls) if tool_calls else None
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO coach_chat (ts, chat_id, session_id, track, car, lap_id, "
                "question, answer, tool_calls) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ts if ts is not None else time.time(), chat_id, session_id, track, car,
                 lap_id, question, answer, tc_json),
            )

    def list_chats(self) -> list[dict]:
        """One entry per chat_id, newest last-activity first."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT chat_id, MIN(ts), MAX(ts), COUNT(*), "
                "GROUP_CONCAT(DISTINCT track) FROM coach_chat "
                "GROUP BY chat_id ORDER BY MAX(ts) DESC"
            ).fetchall()
            keys = ("chat_id", "started", "last", "message_count", "tracks")
            return [dict(zip(keys, r)) for r in rows]

    def chat_messages(self, chat_id: str) -> list[dict]:
        """Every Q&A exchange in a chat, oldest first."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT ts, session_id, track, car, lap_id, question, answer, tool_calls "
                "FROM coach_chat WHERE chat_id = ? ORDER BY ts, id",
                (chat_id,),
            ).fetchall()
            keys = ("ts", "session_id", "track", "car", "lap_id", "question", "answer", "tool_calls")
            out = []
            for r in rows:
                d = dict(zip(keys, r))
                d["tool_calls"] = _decode(d["tool_calls"])
                out.append(d)
            return out

    def session_chat(self, session_id: str) -> list[dict]:
        """Every Q&A exchange in a session, oldest first (for review / memory)."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT ts, track, car, lap_id, question, answer, tool_calls FROM coach_chat "
                "WHERE session_id = ? ORDER BY id ASC",
                (session_id,),
            ).fetchall()
            keys = ("ts", "track", "car", "lap_id", "question", "answer", "tool_calls")
            out = []
            for r in rows:
                d = dict(zip(keys, r))
                d["tool_calls"] = _decode(d["tool_calls"]) if d["tool_calls"] else []
                out.append(d)
            return out

    def recent_session_ids(self, limit: int = 10) -> list[str]:
        """Chat session ids, most recent first."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT session_id, MAX(ts) t FROM coach_chat GROUP BY session_id "
                "ORDER BY t DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [r[0] for r in rows]
