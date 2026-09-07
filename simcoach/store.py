from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from uuid import uuid4

import numpy as np

from simcoach.car_lookup import resolve_car, resolve_car_class
from simcoach.model_types import LapRecord

# Class display order for the sessions filter (fastest/top class first); anything
# not listed sorts after these, alphabetically.
_CLASS_ORDER = ["Hyper", "LMP2", "LMP2_ELMS", "LMP3", "GT3", "GTE"]

SCHEMA_VERSION = 10

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
    session_id TEXT,
    source TEXT DEFAULT 'live',
    s1 REAL,
    s2 REAL,
    s3 REAL,
    position INTEGER,
    sector_bounds TEXT
);
CREATE TABLE IF NOT EXISTS ingested_files (
    path TEXT PRIMARY KEY,
    mtime REAL
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
CREATE TABLE IF NOT EXISTS race_sessions (
    session_id TEXT PRIMARY KEY,
    position INTEGER,
    class_position INTEGER,
    finish_status TEXT,
    incidents TEXT,
    penalties TEXT
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
            if current_version < 6:
                # v5 -> v6: tag each lap's origin (live capture vs backfilled
                # result-log import) and track which result files are already
                # imported, for dedup. Existing rows default to 'live'. Guard
                # the ADD COLUMN on the table existing at all -- some tests
                # simulate a bare pre-v6 db with only the tables relevant to
                # their own migration step.
                cols = [r[1] for r in self.conn.execute("PRAGMA table_info(laps)")]
                if cols and "source" not in cols:
                    _add_column(self.conn, "laps", "source TEXT DEFAULT 'live'")
                self.conn.execute(
                    "CREATE TABLE IF NOT EXISTS ingested_files (path TEXT PRIMARY KEY, mtime REAL)"
                )
            if current_version < 7:
                # v6 -> v7: per-lap sector times (s1/s2/s3), nullable. Backfilled
                # for result-log imports (which already parse sectors) and, later,
                # live capture. Existing rows stay NULL.
                cols = [r[1] for r in self.conn.execute("PRAGMA table_info(laps)")]
                if cols:
                    for c in ("s1", "s2", "s3"):
                        if c not in cols:
                            _add_column(self.conn, "laps", f"{c} REAL")
            if current_version < 8:
                # v7 -> v8: persist race outcome/racecraft context (position,
                # incidents, penalties) parsed from result logs but previously
                # dropped. Idempotent create (incidents/penalties are JSON).
                self.conn.execute(
                    "CREATE TABLE IF NOT EXISTS race_sessions ("
                    "session_id TEXT PRIMARY KEY, position INTEGER, class_position INTEGER, "
                    "finish_status TEXT, incidents TEXT, penalties TEXT)"
                )
            if current_version < 9:
                # v8 -> v9: per-lap live race position (mPlace), nullable. Existing
                # rows stay NULL. Guard the ADD COLUMN like the s1/s2/s3 one.
                cols = [r[1] for r in self.conn.execute("PRAGMA table_info(laps)")]
                if cols and "position" not in cols:
                    _add_column(self.conn, "laps", "position INTEGER")
            if current_version < 10:
                # v9 -> v10: per-lap sector-boundary distances (JSON [d1, d2] of
                # lap_distance), nullable. Existing rows stay NULL.
                cols = [r[1] for r in self.conn.execute("PRAGMA table_info(laps)")]
                if cols and "sector_bounds" not in cols:
                    _add_column(self.conn, "laps", "sector_bounds TEXT")
        if current_version != SCHEMA_VERSION:
            self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.conn.commit()

    def save(self, record: LapRecord, metrics: list | None = None) -> int:
        telemetry_path = None
        try:
            with self._lock, self.conn:
                def _sec(i):
                    return record.sectors[i] if record.sectors and len(record.sectors) > i else None
                bounds = json.dumps(record.sector_bounds) if record.sector_bounds else None
                cur = self.conn.execute(
                    "INSERT INTO laps (ts, sim, track, car, lap_time, valid, telemetry_path, "
                    "phase, session_id, s1, s2, s3, position, sector_bounds) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (record.timestamp, record.sim, record.track, record.car,
                     record.lap_time, int(record.valid), None,
                     record.phase, record.session_id, _sec(0), _sec(1), _sec(2),
                     record.position, bounds),
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
            if telemetry_path is None:
                raise FileNotFoundError(f"lap {lap_id} has no telemetry (imported from a result log)")
            with np.load(telemetry_path) as npz:
                channels = {k: npz[k] for k in npz.files}
            return LapRecord(sim, track, car, lap_time, bool(valid), ts, channels, phase, session_id)

    def best_lap(self, track: str, car: str, exclude_lap_id: int | None = None,
                 phase: str | None = None, session_id: str | None = None) -> LapRecord | None:
        with self._lock:
            # Exclude mapping-capture laps (slow weave/boundary laps) so a fresh
            # track with only mapping laps doesn't pick one as the reference best.
            query = ("SELECT id FROM laps WHERE track = ? AND car = ? AND valid = 1 "
                     "AND telemetry_path IS NOT NULL "
                     "AND (phase IS NULL OR phase != 'mapping')")
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

    def sector_bounds(self, track: str, car: str) -> list[float] | None:
        """The game's real S1/S2 sector-boundary lap_distances for a track/car,
        from the most recent lap that captured them (the boundaries are fixed per
        track, so any lap's are the track's). None if no lap has them yet — used to
        seed the map's sector splits at the real boundaries instead of naive thirds."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT sector_bounds FROM laps WHERE track = ? AND car = ? "
                "AND sector_bounds IS NOT NULL ORDER BY id DESC LIMIT 1",
                (track, car),
            ).fetchall()
        for (raw,) in rows:
            try:
                vals = [float(v) for v in json.loads(raw)]
            except (TypeError, ValueError):
                continue
            if vals:
                return vals
        return None

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

    def chats_for_session(self, session_id: str) -> list[dict]:
        """Chats that touched a race session — same shape as list_chats(), but
        only chat_ids with ≥1 message stamped with this session_id. Newest
        last-activity first, so callers can take [0] as the session's chat."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT chat_id, MIN(ts), MAX(ts), COUNT(*), "
                "GROUP_CONCAT(DISTINCT track) FROM coach_chat "
                "WHERE session_id = ? GROUP BY chat_id ORDER BY MAX(ts) DESC",
                (session_id,),
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

    # Filtered, paginated race sessions from the laps table (not coach_chat,
    # which would miss capture-only sessions with no coach questions asked).
    def _distinct_db_cars(self) -> list[str]:
        """Distinct raw entry/team names (the DB `car` key) across all sessions."""
        with self._lock:
            return [r[0] for r in self.conn.execute(
                "SELECT DISTINCT car FROM laps WHERE session_id IS NOT NULL "
                "AND car IS NOT NULL").fetchall()]

    def _session_where(self, track: str | None, car_model: str | None,
                       car_class: str | None = None) -> tuple[str, list]:
        """WHERE clause for the session filters. `car_model` filters by resolved car
        MODEL, `car_class` by resolved CLASS -- each expanded to every raw entry name
        that resolves to it (several team entries share a model/class). No match ->
        a clause that selects nothing. car_model wins if both are given."""
        clauses = ["session_id IS NOT NULL"]
        params: list = []
        if track:
            clauses.append("track = ?"); params.append(track)
        teams: list[str] | None = None
        if car_model:
            teams = [c for c in self._distinct_db_cars() if resolve_car(c) == car_model]
        elif car_class:
            teams = [c for c in self._distinct_db_cars() if resolve_car_class(c) == car_class]
        if teams is not None:
            clauses.append(f"car IN ({','.join('?' * len(teams)) or 'NULL'})")
            params += teams
        return " AND ".join(clauses), params

    def session_facets(self) -> dict:
        """Filter-dropdown facets over ALL sessions (independent of page/filter):
        distinct tracks, a flat list of car MODELS, and car_groups -- models grouped
        by resolved class, class-ordered -- so the dropdown can offer each class as a
        selectable filter with its models nested under it."""
        with self._lock:
            tracks = [r[0] for r in self.conn.execute(
                "SELECT DISTINCT track FROM laps WHERE session_id IS NOT NULL "
                "AND track IS NOT NULL ORDER BY track").fetchall()]
        by_class: dict[str, set[str]] = {}
        for c in self._distinct_db_cars():
            by_class.setdefault(resolve_car_class(c) or "Other", set()).add(resolve_car(c))
        order = lambda cls: (_CLASS_ORDER.index(cls) if cls in _CLASS_ORDER else len(_CLASS_ORDER), cls)
        car_groups = [{"car_class": cls, "cars": sorted(by_class[cls])}
                      for cls in sorted(by_class, key=order)]
        cars = sorted({m for models in by_class.values() for m in models})
        return {"tracks": tracks, "cars": cars, "car_groups": car_groups}

    def race_sessions_page(self, track: str | None = None, car: str | None = None,
                           offset: int = 0, limit: int = 30,
                           car_class: str | None = None) -> dict:
        """One page of sessions (newest first) matching the filters, plus the
        total count so the client can render page controls."""
        where, params = self._session_where(track, car, car_class)
        with self._lock:
            total = self.conn.execute(
                f"SELECT COUNT(*) FROM (SELECT session_id FROM laps WHERE {where} "
                f"GROUP BY session_id)", params).fetchone()[0]
            rows = self.conn.execute(
                "SELECT session_id, track, car, COUNT(*), "
                "MIN(CASE WHEN valid THEN lap_time END), MIN(ts), MAX(ts) "
                f"FROM laps WHERE {where} GROUP BY session_id "
                "ORDER BY MAX(ts) DESC LIMIT ? OFFSET ?",
                params + [limit, offset]).fetchall()
        keys = ("session_id", "track", "car", "laps", "best", "started", "last")
        out = []
        for r in rows:
            d = dict(zip(keys, r))
            d["car_model"] = resolve_car(d["car"]) if d["car"] else None
            out.append(d)
        return {"rows": out, "total": total}

    def recent_race_sessions(self, limit: int = 2000) -> list[dict]:
        """All sessions (newest first) as a flat list — convenience over
        race_sessions_page for callers/tests that don't need paging."""
        return self.race_sessions_page(offset=0, limit=limit)["rows"]

    def session_summary(self, track: str | None = None, car: str | None = None,
                        car_class: str | None = None) -> dict:
        """Aggregate stats + best-lap trend over the FULL filtered set (not one
        page), so the Sessions screen's cards/chart stay accurate under paging.
        The per-session trend series is only built when a track is selected
        (the chart is hidden otherwise), keeping the all-tracks case to pure
        aggregates."""
        where, params = self._session_where(track, car, car_class)
        per = (f"SELECT session_id, car, MIN(CASE WHEN valid THEN lap_time END) AS best, "
               f"MIN(ts) AS started FROM laps WHERE {where} GROUP BY session_id")
        with self._lock:
            count = self.conn.execute(f"SELECT COUNT(*) FROM ({per})", params).fetchone()[0]
            n_best, cars, best_min, best_max = self.conn.execute(
                f"SELECT COUNT(*), COUNT(DISTINCT car), MIN(best), MAX(best) "
                f"FROM ({per}) WHERE best IS NOT NULL", params).fetchone()
            first = self.conn.execute(
                f"SELECT best FROM ({per}) WHERE best IS NOT NULL ORDER BY started ASC LIMIT 1",
                params).fetchone()
            last = self.conn.execute(
                f"SELECT best FROM ({per}) WHERE best IS NOT NULL ORDER BY started DESC LIMIT 1",
                params).fetchone()
            series: list[dict] = []
            if track:
                rows = self.conn.execute(
                    f"SELECT car, started, best FROM ({per}) WHERE best IS NOT NULL "
                    f"ORDER BY started ASC", params).fetchall()
                by_car: dict[str, list] = {}
                for c, started, best in rows:
                    by_car.setdefault(resolve_car(c), []).append({"x": started, "value": best})
                series = [{"car": c, "points": pts} for c, pts in by_car.items()]
        # improvement = earliest best - latest best, only when a single car is in
        # the set (mirrors the old client rule); positive means faster over time.
        improvement = (first[0] - last[0]) if (cars == 1 and n_best >= 2 and first and last) else None
        consistency = ((best_max - best_min) / 2) if (n_best >= 2 and best_min is not None) else None
        return {
            "count": count,
            "cars": cars,
            "personal_best": best_min,
            "consistency": consistency,
            "improvement": improvement,
            "series": series,
        }

    def session_laps(self, session_id: str) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, lap_time, valid, ts, s1, s2, s3, phase, track, car FROM laps "
                "WHERE session_id = ? ORDER BY ts",
                (session_id,),
            ).fetchall()
            keys = ("id", "lap_time", "valid", "ts", "s1", "s2", "s3", "phase", "track", "car")
            out = []
            for r in rows:
                d = dict(zip(keys, r))
                d["car_model"] = resolve_car(d["car"]) if d["car"] else None
                out.append(d)
            return out

    def session_position_arc(self, session_id: str) -> dict | None:
        """The session's live per-lap race-position arc, or None if no lap carried
        a position. Lower place number = better, so best = min. Ordered by ts so
        start/last are the first/last lap actually driven."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT position FROM laps WHERE session_id = ? AND position IS NOT NULL "
                "ORDER BY ts",
                (session_id,),
            ).fetchall()
        places = [r[0] for r in rows]
        if not places:
            return None
        return {
            "start": places[0],
            "best": min(places),
            "worst": max(places),
            "last": places[-1],
            "laps": len(places),
        }

    def clear_all(self) -> None:
        """Wipe all persisted data: laps, setups, corner metrics, coach chat,
        and the raw per-lap telemetry files. Used by Settings > reset data."""
        with self._lock:
            for t in ("corner_metrics", "coach_chat", "laps", "setups"):
                self.conn.execute(f"DELETE FROM {t}")
            self.conn.commit()
        for f in Path(self.data_dir).glob("*.npz"):
            f.unlink(missing_ok=True)

    def save_result_session(self, rs, session_id: str) -> None:
        """Backfill a parsed LMU result-log session's laps into the laps
        table (source='result', no telemetry -- summary only)."""
        def _sec(sectors, i):
            return sectors[i] if sectors is not None and len(sectors) > i else None

        with self._lock, self.conn:
            self.conn.executemany(
                "INSERT INTO laps (ts, sim, track, car, lap_time, valid, telemetry_path, "
                "phase, session_id, source, s1, s2, s3) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'result', ?, ?, ?)",
                [
                    (rs.date_ts, rs.sim, rs.track, rs.car, lap.lap_time, int(lap.valid),
                     None, rs.session_type, session_id,
                     _sec(lap.sectors, 0), _sec(lap.sectors, 1), _sec(lap.sectors, 2))
                    for lap in rs.laps
                ],
            )
            self.conn.execute(
                "INSERT OR REPLACE INTO race_sessions "
                "(session_id, position, class_position, finish_status, incidents, penalties) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, rs.position, rs.class_position, rs.finish_status,
                 json.dumps(rs.incidents or []), json.dumps(rs.penalties or [])),
            )

    def race_session(self, session_id: str) -> dict | None:
        """The persisted race outcome (position, incidents, penalties) for a
        session, or None if no race row exists (practice/qualy, or not imported)."""
        with self._lock:
            row = self.conn.execute(
                "SELECT position, class_position, finish_status, incidents, penalties "
                "FROM race_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        pos, cls_pos, finish, incidents, penalties = row

        def _load(s):
            try:
                return json.loads(s) if s else []
            except (TypeError, ValueError):
                return []

        return {
            "position": pos,
            "class_position": cls_pos,
            "finish_status": finish,
            "incidents": _load(incidents),
            "penalties": _load(penalties),
        }

    def is_ingested(self, path: str, mtime: float) -> bool:
        with self._lock:
            row = self.conn.execute(
                "SELECT 1 FROM ingested_files WHERE path = ? AND mtime = ?", (path, mtime)
            ).fetchone()
            return row is not None

    def mark_ingested(self, path: str, mtime: float) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO ingested_files (path, mtime) VALUES (?, ?) "
                "ON CONFLICT(path) DO UPDATE SET mtime = excluded.mtime",
                (path, mtime),
            )

    def recent_session_ids(self, limit: int = 10) -> list[str]:
        """Chat session ids, most recent first."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT session_id, MAX(ts) t FROM coach_chat GROUP BY session_id "
                "ORDER BY t DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [r[0] for r in rows]
