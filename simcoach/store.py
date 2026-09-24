from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import closing, suppress
from pathlib import Path
from uuid import uuid4

import numpy as np

from simcoach.battle import CarLap
from simcoach.car_lookup import resolve_car, resolve_car_class
from simcoach.model_types import LapRecord

# Class display order for the sessions filter (fastest/top class first); anything
# not listed sorts after these, alphabetically.
_CLASS_ORDER = ["Hyper", "LMP2", "LMP2_ELMS", "LMP3", "GT3", "GTE"]

# The deterministic (track, car, online|offline) session id merges every stint of
# that combo forever — so a NEW race weekend at the same track/car collapses into
# the previous one. Split by a time gap: within this window a lap continues the
# current weekend (survives a sidecar restart), beyond it starts a new instance.
_SESSION_GAP_S = 6 * 3600  # 6 hours

# Lap-time match tolerance when deciding whether a result-log lap is one already
# stored. NOT exact equality: LMU's result XML rounds lap times to 4 decimals
# ('70.4068') while live capture keeps the sim's full float ('70.40680694580078'),
# so the same lap differs by up to 5e-5 s. 1 ms is far above that rounding and far
# below any real gap between two different laps.
_LAP_TIME_TOL_S = 0.001

# How far a session's race laps may run past the moment a race's result log was written
# before that log is treated as describing an EARLIER race. A race's log does not exist
# while the race is being driven -- LMU writes it 153-229 s after the driver's finish
# (measured over the real logs) -- so during race 2 of a sitting the newest log on disk
# is race 1's, and using it would assert a grid slot and a "Finished" status for a race
# still in progress.
#
# ponytail: a calibrated margin, not a derived bound. It only has to separate a post-race
# cooldown lap (one lap, ~60-90 s, still stamped phase='race') from the next race in the
# sitting (>= ~40 min later in the real data; the tightest genuine last-lap-to-mtime gap
# measured is -46.8 s, leaving 646.8 s of headroom). Sim-clock fields cannot do this job:
# FinishTime runs from the green flag while DateTime runs from session start, so
# ts + FinishTime undershoots the real end by +122 to +2243 s. Narrow it if a cooldown
# stint is ever seen crossing it.
#
# Trusting an mtime has two failure directions, and they are not symmetric:
#   - SAFE: an mtime rewritten BACKWARDS, or a clock that jumps forward, only makes the
#     guard refuse more readily, which costs the grid/finish line and claims nothing.
#   - CONFIDENT-WRONG: an mtime rewritten FORWARDS (a backup restore, a sync tool, any
#     touch) or a clock stepped backwards disarms the guard and reinstates the stale-row
#     bug verbatim. A forward mtime is also self-reinforcing: it fails is_ingested(), so
#     the file re-imports and re-stamps logged_ts permanently.
# Deliberately NOT guarded. The only bound that does not depend on the file is a sim-clock
# one, and the FinishTime spread above is exactly why that cannot work -- a clamp would
# reintroduce the error it was measured to have. A NULL logged_ts has the same shape and
# also goes unguarded: it disables the check entirely, and is only reachable through a
# save_result_session caller that omits the argument (real data is 1399/1399 stamped).
_RACE_LOG_LAG_S = 600

# How long a session's race laps may pause before the next race lap is treated as a
# NEW race (see `race_windows`). A session instance deliberately spans 6 h, so one id
# covers every race of an evening; the boundary is what tells race 3 from race 1.
#
# The primary split is a non-race lap (leaving for the garage, a practice or qualy
# stint in between), which is what really separates two races in the captured data.
# This gap is only the backstop for two races with nothing but race laps between them.
#
# ponytail: a calibrated margin, not a derived bound. The longest gap measured INSIDE a
# single race is 924 s (pit stops, incidents, garage time -- see unlogged_race_since on
# why a 300 s rule shreds one race into six), so 1200 s clears real within-race data by
# ~4.6 min, while a genuine between-races gap in the reports is >= ~40 min. Widen it if
# a long safety-car or red-flag stint is ever seen splitting one race in two.
_RACE_WINDOW_GAP_S = 20 * 60


def _is_deterministic_sid(sid: str) -> bool:
    return sid.endswith(("--online", "--offline"))


# Instance separator: '~' is URL-unreserved (RFC 3986), so a suffixed id stays safe
# in the /sessions/{sid} path. NEVER use '#' — the browser would treat it as a URL
# fragment and drop the suffix, breaking session loads.
_SESSION_SEP = "~"


def _session_base(sid: str) -> str:
    """The (track, car, mode) base of a session id, dropping any ~<instance> suffix."""
    return sid.split(_SESSION_SEP, 1)[0]

SCHEMA_VERSION = 24

# The coach conversation, persisted so a session's Q&A can be reviewed later and
# fed to cross-session memory. lap_id is the lap the question was asked about
# (NULL for pre-lap general questions); tool_calls is a JSON list of the on-demand
# tools the agentic coach invoked that turn (added v4); since #159 each entry also
# carries the tool's `result` text, capped at TOOL_RESULT_CAP chars, so any answer
# can be audited against what the coach actually read (no schema change: the column
# was always JSON, older rows just lack the key). chat_id (added v5) groups
# messages into a chat that can span multiple race sessions (session_id stays the
# race-session scope). The four *_tokens columns and `model` (added v12) record what
# each turn actually cost to bill; `model` is per ROW because Settings can change the
# model mid-chat, so a chat total is only priceable row by row. All five are nullable
# -- pre-v12 rows stay NULL and sum as 0 via COALESCE. Defined separately so both
# fresh installs and the migration create the identical table.
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
    tool_calls TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_write_tokens INTEGER,
    model TEXT,
    spoken TEXT
);
"""

# (coach_chat column, TokenUsage/wire key) for the v12 usage buckets, in the order
# chat_usage() reports them. One list so the migration, the INSERT and the SUM
# can never drift apart.
# #159: one lap summary runs a few KB; a cap keeps a long chat's rows bounded.
TOOL_RESULT_CAP = 4000

_USAGE_COLUMNS = (("input_tokens", "input"), ("output_tokens", "output"),
                  ("cache_read_tokens", "cache_read"), ("cache_write_tokens", "cache_write"))

# The race situation at each lap's commit (#152), as (laps column, SQL type), in
# RaceSample field order. One list so the migration, the fresh schema check and the
# INSERT can never drift apart.
_RACE_COLUMNS = (("class_position", "INTEGER"), ("class_ahead", "TEXT"),
                 ("class_gap_ahead", "REAL"), ("class_behind", "TEXT"),
                 ("class_gap_behind", "REAL"), ("time_left", "REAL"),
                 ("max_laps", "INTEGER"))

# Longitudinal driver profile: reviewed "memories" (driver_learnings), the
# concise always-on summary the coach prompt carries (driver_summary, a single
# row id=1), and a per-session setup snapshot (session_setups). Defined
# separately so both fresh installs and the migration create identical tables.
_PROFILE_SCHEMA = """
CREATE TABLE IF NOT EXISTS driver_learnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    status TEXT NOT NULL,
    track TEXT,
    car TEXT,
    source TEXT,
    session_id TEXT,
    created_ts REAL NOT NULL,
    updated_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS driver_summary (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    text TEXT NOT NULL,
    status TEXT NOT NULL,
    updated_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS session_setups (
    session_id TEXT PRIMARY KEY,
    track TEXT NOT NULL,
    car TEXT NOT NULL,
    setup_json TEXT NOT NULL,
    updated_ts REAL NOT NULL
);
"""

# #152 get_battle: one row per (driver's lap, same-class car -- the driver included) with
# that car's corner crossing times and apex speeds on the lap, as JSON. SQLite, not the
# .npz: a few dozen numbers per car, cross-session, not a full-rate channel.
_BATTLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS battle_laps (
    lap_id INTEGER,
    car_id INTEGER,
    driver TEXT,
    car TEXT,
    model TEXT,
    class_position INTEGER,
    is_player INTEGER,
    crossings TEXT,
    UNIQUE (lap_id, car_id)
);
"""

# #160: the lap IN PROGRESS, one row per corner as the car leaves it (livelap.py):
# that corner's metrics + incidents as JSON, plus one label '_lap' row holding the
# lap-wide incident scan. Keyed to the deterministic base session id, the game's lap
# number and the wall time the lap began. PROVISIONAL: the stored lap's analysis is
# authoritative, so saving the lap deletes exactly its rows (LapRecord.live_meta
# carries the key). A lap that never commits (a crash, a retirement) keeps its rows
# until the worker ages them out a day later.
_LIVE_CORNERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS live_corners (
    session_id TEXT,
    phase TEXT,
    lap_no INTEGER,
    start_ts REAL,
    label TEXT,
    corner_no INTEGER,
    ts REAL,
    facts TEXT,
    UNIQUE (session_id, lap_no, start_ts, label)
);
CREATE INDEX IF NOT EXISTS live_corners_newest ON live_corners (session_id, start_ts);
CREATE INDEX IF NOT EXISTS live_corners_age ON live_corners (start_ts);
"""
_LIVE_LAP_ROW = "_lap"

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
    sector_bounds TEXT,
    lap_no INTEGER,
    pit INTEGER,
    partial TEXT,
    class_position INTEGER,
    class_ahead TEXT,
    class_gap_ahead REAL,
    class_behind TEXT,
    class_gap_behind REAL,
    time_left REAL,
    max_laps INTEGER,
    mapping INTEGER DEFAULT 0
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
    penalties TEXT,
    grid_position INTEGER,
    class_grid_position INTEGER,
    track TEXT,
    car TEXT,
    phase TEXT,
    ts REAL,
    mode TEXT,
    logged_ts REAL
);
""" + _COACH_CHAT_SCHEMA + _PROFILE_SCHEMA + _BATTLE_SCHEMA + _LIVE_CORNERS_SCHEMA


def _add_column(conn: sqlite3.Connection, table: str, column_def: str) -> None:
    """ALTER TABLE ... ADD COLUMN, tolerating a race where another process (or a
    partially-applied prior migration) already added it."""
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column_def}")
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e).lower():
            raise


def _pair_laps(stored: list[tuple],
               lap_times: list[float | None],
               lap_nos: list[int | None] | None = None) -> list[int | None]:
    """For each incoming lap time, the id of the stored lap that already holds it, or
    None. THE lap-identity rule, shared by result ingest (#68) and the v16 cleanup
    (#71) so the two directions cannot drift apart: a lap time matches within
    _LAP_TIME_TOL_S (not `==`: the XML rounds to 4 decimals, 70.4068 vs
    70.40680694580078), an untimed lap matches only an untimed one, and each stored
    lap absorbs at most one incoming lap, so two genuinely identical times keep two
    rows. `stored` is (id, lap_time[, lap_no]) in the order candidates should be
    preferred.

    An UNTIMED pair must also agree on the game's lap number when BOTH sides carry
    one (`lap_nos`, and a third element on `stored`). Until #77 that half of the rule
    was unreachable -- live capture could not write a NULL lap_time -- and "both
    untimed" was identity enough. Now that out/in/partial laps are stored, a result
    file lists several untimed laps and the stint may hold several partials, so the
    lap number is the only thing that says WHICH untimed lap is which. Either side
    missing it falls back to the old order-based match, so legacy rows and the v16
    cleanup (which passes 2-tuples and no numbers) behave exactly as before."""
    unclaimed = list(stored)
    twins: list[int | None] = []
    for i, lt in enumerate(lap_times):
        want_no = lap_nos[i] if lap_nos is not None else None
        if lt is None:
            at = next((k for k, r in enumerate(unclaimed)
                       if r[1] is None
                       and (want_no is None or len(r) < 3 or r[2] is None
                            or r[2] == want_no)), None)
        else:
            at = next((k for k, r in enumerate(unclaimed)
                       if r[1] is not None and abs(r[1] - lt) <= _LAP_TIME_TOL_S), None)
        twins.append(None if at is None else unclaimed.pop(at)[0])
    return twins


# A duplicate result lap's sectors, copied onto the LIVE twin kept in its place before
# the result copy is dropped. "The live row is strictly richer" is FALSE for sectors:
# live capture still writes sector-less laps when its split fails (31 of 173 live rows
# on the real store, newest 2026-09-08), and in 17 of the 120 duplicate pairs the result
# row held the only full set -- one race kept 11 of 12 laps' sectors ONLY on its result
# rows. A column-by-column audit of those pairs found no other column the result row
# alone holds (every other one agrees, is live-only, or differs by design: id/ts/source).
#
# ALL OR NOTHING, and the whole rule lives in this one statement so the migration and
# ingest cannot apply different ones:
# - the twin must be `source = 'live'` with all three sectors NULL. Live capture writes
#   three or none (lmu/smm.py), so a column-wise COALESCE would mint a shape it never
#   produces -- e.g. result (32.662, NULL, NULL) onto an all-NULL live twin.
# - the result must hold all three: NULL arithmetic makes the sum NULL and the row
#   unmatched. Partial result rows are common (345 real), because results.py drops a
#   missing sector and COMPACTS the list; every real gap is trailing (0 shifted of 1,402
#   XMLs), so the compaction is harmless today -- and this rule rejects partials anyway.
# - the three must sum to the live lap_time within _SECTOR_SUM_TOL_S. What that catches
#   is an INCONSISTENT set: sectors that do not add up to the lap they are copied onto
#   (every real row with three sectors does add up -- 0 exceptions). What it does NOT
#   catch is a wrong-stint match. A result row's sectors sum to its own lap time, and a
#   pair only exists within _LAP_TIME_TOL_S (1 ms), so every time-matched pair passes --
#   0 of 120 rejected on the real store -- and a cross-stint coincidence would copy that
#   other lap's split. Preventing that is the matcher's scope, not this check's; it needs
#   two stints within 1 ms, and the nearest real coincidence is 10.3 ms apart.
# A pair that fails still dedupes -- only the sector copy is skipped.
_SECTOR_SUM_TOL_S = 0.01
_BACKFILL_SECTORS_SQL = (
    "UPDATE laps SET s1 = :s1, s2 = :s2, s3 = :s3 WHERE id = :id AND source = 'live' "
    "AND s1 IS NULL AND s2 IS NULL AND s3 IS NULL "
    f"AND ABS(:s1 + :s2 + :s3 - lap_time) <= {_SECTOR_SUM_TOL_S}")


def _duplicate_result_lap_pairs(conn: sqlite3.Connection) -> list[tuple]:
    """The `source='result'` lap rows that duplicate a lap live capture already holds,
    as (result_id, live_id, s1, s2, s3) -- the row to delete, the twin kept in its
    place, and the sectors to merge into that twin first (_BACKFILL_SECTORS_SQL).

    Read-only: it identifies rows, it does not change them, so the v16 migration and a
    dry-run against a copy of the real store run the SAME code. Issue #71 -- the
    ingest-side skip (#68) stops NEW duplicates; these are the rows written before it.

    Each result row is matched by _pair_laps against the live rows of the same stint --
    same track, car and phase, within the session window of the result row's own ts. A
    live row absorbs at most one result row GLOBALLY (`claimed`), so two result files
    sharing a window cannot both delete against the same live lap: the rows this can
    remove are bounded by the live laps. Groups run in `ts` order so which of two such
    files claims a live lap is deterministic (the earlier one).

    Deliberate narrowings versus the ingest predicate, each so that nothing held only
    once is lost:

    - Only `source = 'live'` rows are candidates, not the ingest's
      `source = 'live' OR session_id = ?`. That second half lets a RE-import dedupe
      against its own earlier rows; here it would mean one result row deleting another
      with no live twin at all. THIS, together with the global `claimed` set, is what
      keeps a sibling session (P1/P2, or two races in one sitting) from being raided --
      the loss that ate 379 laps of 7,066 on a fresh import: a result row can only be
      deleted against a live lap, and a live lap can delete only one result row. The
      grouping below is NOT that protection; it only yields each file's scope (track,
      car, phase, ts) and the per-session row count the next bullet needs.
    - A row with a telemetry_path is never returned, however it matched. The `.npz`
      cannot be regenerated without re-driving (#66); result rows are inserted with
      telemetry_path NULL, so this only fires on a matching bug -- and it means no
      deletion here can orphan or break a point-cloud file. It is filtered BEFORE
      pairing, so a spared row never claims the live lap a real duplicate needs.
    - A result session is never EMPTIED unless its race outcome stays reachable. The
      Sessions list is built from lap rows, so a session with none drops out of it;
      the outcome is then only reachable through _linked_result_session, which needs a
      race_sessions row of phase 'race' carrying its identity (track, car, ts). Rows
      ingested before v15 have none -- all 1,399 on the real store -- so emptying such
      a session hid two real races. Such a session keeps ALL its rows (not "all but
      one", which would pick a survivor arbitrarily); a partial match still cleans.
      Its claims are not released, so a later file cannot delete against those laps.
    """
    pairs: list[tuple] = []
    claimed: set[int] = set()
    removed_by_sid: dict[str, int] = {}
    # The game's lap number pairs UNTIMED laps (#154 red-team 2): without it they pair in
    # id order, and a log's untimed out-lap was deleted against a live in-lap. NULL on a
    # db the v16 step reaches before v18 adds the column -- the old order-based match.
    lap_no = ("lap_no" if "lap_no" in {r[1] for r in conn.execute("PRAGMA table_info(laps)")}
              else "NULL")
    groups = conn.execute(
        "SELECT session_id, track, car, phase, ts FROM laps WHERE source = 'result' "
        "GROUP BY session_id, track, car, phase, ts ORDER BY ts").fetchall()
    for sid, track, car, phase, ts in groups:
        query = (f"SELECT id, lap_time, {lap_no} FROM laps WHERE source = 'live' "
                 "AND track IS ? AND car IS ? AND phase IS ?")
        params: list = [track, car, phase]
        if ts is not None:
            query += " AND ts BETWEEN ? AND ?"
            params += [ts - _SESSION_GAP_S, ts + _SESSION_GAP_S]
        query += " ORDER BY id"
        unclaimed = [r for r in conn.execute(query, params).fetchall() if r[0] not in claimed]
        # `telemetry_path IS NULL`: 0 of 7,066 real result rows carry one, so this never
        # changes HOW MANY rows are removed today. On a store where it did fire, it would
        # change WHICH of two same-time result rows goes -- the spared one never enters
        # pairing, so the live lap stays available to the other.
        rows = conn.execute(
            f"SELECT id, lap_time, s1, s2, s3, {lap_no} FROM laps WHERE source = 'result' "
            "AND session_id IS ? AND track IS ? AND car IS ? AND phase IS ? AND ts IS ? "
            "AND telemetry_path IS NULL ORDER BY id",
            (sid, track, car, phase, ts)).fetchall()
        twins = _pair_laps(unclaimed, [r[1] for r in rows], [r[5] for r in rows])
        found = [(r[0], twin, r[2], r[3], r[4]) for r, twin in zip(rows, twins)
                 if twin is not None]
        claimed.update(p[1] for p in found)
        removed = removed_by_sid.get(sid, 0) + len(found)
        session_rows = conn.execute(
            "SELECT COUNT(*) FROM laps WHERE session_id IS ?", (sid,)).fetchone()[0]
        if found and removed == session_rows and conn.execute(
                "SELECT 1 FROM race_sessions WHERE session_id IS ? AND phase = 'race' "
                "AND track IS NOT NULL AND car IS NOT NULL AND ts IS NOT NULL",
                (sid,)).fetchone() is None:
            continue
        removed_by_sid[sid] = removed
        pairs.extend(found)
    return pairs


def _drop_duplicate_result_laps(conn: sqlite3.Connection, pairs: list[tuple]) -> None:
    """Remove the result rows _duplicate_result_lap_pairs found, first offering each one's
    sectors to the live twin it paired with (_BACKFILL_SECTORS_SQL decides whether)."""
    conn.executemany(_BACKFILL_SECTORS_SQL, [
        {"id": live_id, "s1": s1, "s2": s2, "s3": s3} for _, live_id, s1, s2, s3 in pairs])
    conn.executemany("DELETE FROM laps WHERE id = ?", [(p[0],) for p in pairs])


# Every `laps` column _duplicate_result_lap_pairs reads. A migration step that runs it
# is guarded on all of them: v7 only adds s1-s3 when `laps` exists, and some tests build
# a bare `laps` of two columns -- nothing to clean in such a db anyway.
_CLEANUP_COLUMNS = frozenset({"id", "source", "telemetry_path", "track", "car", "phase", "ts",
                              "lap_time", "session_id", "s1", "s2", "s3"})


def _capped_result(call):
    """A tool-call entry with its `result` bounded (#159); the caller's dict is untouched."""
    r = call.get("result") if isinstance(call, dict) else None
    if not isinstance(r, str) or len(r) <= TOOL_RESULT_CAP:
        return call
    return {**call, "result": r[:TOOL_RESULT_CAP] + " ...[truncated]"}


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
        # #160: provisional-lap keys already superseded by their commit, so a worker
        # write that lands after the commit is dropped (see save_live_corner).
        self._live_superseded: set[tuple] = set()
        current_version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        # A FRESH db is version 0 AND empty. Version 0 with tables already present is a
        # db that lost its header -- `sqlite3 .dump` + restore does exactly this, since
        # the dump carries no `PRAGMA user_version`. Treating that as fresh would run
        # _SCHEMA, whose every statement is CREATE TABLE IF NOT EXISTS and so no-ops,
        # skip the whole migration chain, and stamp it current: the columns added since
        # would be missing and every read of race_sessions would raise. So fall through
        # to the cumulative migrations, which are all idempotent and handle it correctly.
        has_tables = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' LIMIT 1").fetchone() is not None
        stamp = SCHEMA_VERSION   # lowered only by a step that must retry next open (v16+)
        if current_version == 0 and not has_tables:
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
            if current_version < 11:
                # v10 -> v11: driver profile memories + maintained summary + per-session
                # setup snapshots. Idempotent creates.
                self.conn.executescript(_PROFILE_SCHEMA)
            if current_version < 12:
                # v11 -> v12: per-turn billed token usage + the model that billed it,
                # so the chat's cost is priceable row by row (the model can change
                # mid-chat via Settings). Guard the ADD COLUMNs like the v4/v5 steps:
                # a <3 db just created coach_chat with these columns already, and a
                # half-applied migration must be able to finish. `if cols` also guards
                # the table existing at all -- some tests simulate a bare pre-v12 db
                # holding only the tables their own migration step cares about.
                cols = [r[1] for r in self.conn.execute("PRAGMA table_info(coach_chat)")]
                if cols:
                    for col, _ in _USAGE_COLUMNS:
                        if col not in cols:
                            _add_column(self.conn, "coach_chat", f"{col} INTEGER")
                    if "model" not in cols:
                        _add_column(self.conn, "coach_chat", "model TEXT")
            if current_version < 15:
                # v12 -> v15: give race_sessions the GRID position the race started from
                # (parsed from the result log but previously dropped -- without it the
                # coach read its first captured lap as the start, issue #67) AND its own
                # session identity. The identity columns matter because the row is keyed
                # by the result-log id: with no track/car/phase/ts/mode of its own, the
                # only way to find it from a live session was to join through `laps`,
                # which result-lap dedupe and clear_all both empty. Guard the ADD COLUMNs
                # on the table existing, like the v6/v9 steps.
                #
                # Numbered 15 rather than reusing 13/14: two interim unreleased builds
                # stamped those after adding only some of these columns, and a gate at the
                # older number would skip such a db entirely and then crash on every read
                # of race_sessions ("no such column: track") rather than degrade. The
                # ADD COLUMN loop is idempotent, so re-running it costs nothing -- every
                # future column added here should bump the number the same way.
                cols = [r[1] for r in self.conn.execute("PRAGMA table_info(race_sessions)")]
                if cols:
                    for c, t in (("grid_position", "INTEGER"), ("class_grid_position", "INTEGER"),
                                 ("track", "TEXT"), ("car", "TEXT"), ("phase", "TEXT"),
                                 ("ts", "REAL"), ("mode", "TEXT"), ("logged_ts", "REAL")):
                        if c not in cols:
                            _add_column(self.conn, "race_sessions", f"{c} {t}")
                # Forget which result files are imported, so the next ingest re-reads them
                # and backfills the grid positions the old rows never stored. Ingest is
                # idempotent on (path, mtime), so without this those races stay blank
                # forever. Safe to re-read now that save_result_session skips laps it
                # already has -- before that, this would have duplicated every lap.
                self.conn.execute(
                    "CREATE TABLE IF NOT EXISTS ingested_files (path TEXT PRIMARY KEY, mtime REAL)"
                )
                self.conn.execute("DELETE FROM ingested_files")
            if current_version < 16:
                # v15 -> v16: remove the result-log lap rows that duplicate a lap live
                # capture already holds (issue #71). v15's fix stops NEW duplicates on
                # ingest; the rows written before it are still there -- 19 rows for a
                # 10-lap race -- so anything that COUNTS is wrong while anything that
                # averages still looks right. _duplicate_result_lap_pairs says what is
                # never removed; _BACKFILL_SECTORS_SQL merges the one field a result copy
                # can hold alone onto its live twin before the copy goes.
                #
                # DELETE, not a `source='duplicate'` mark: session_laps, session_lap_ids
                # and the Sessions list do not filter on source, so a staged row would
                # still be listed and counted -- the #71 symptom itself.
                #
                #
                # The cleanup itself now runs inside the v17 step below -- ONCE, after v17
                # renames the qualifying phase. Running it here and again there gave a db
                # opened from v15 two passes, and a pass has no memory of which live laps an
                # earlier one used: a live lap whose twin the first pass removed is free
                # again, so a second file's same-time row paired with it and went too --
                # the only copy of that lap (the two-races test caught exactly this).
                pass
            if current_version < 17:
                # v16 -> v17: one spelling for the qualifying phase (#88). Live capture
                # stores "qualifying"; result-log import stored the XML tag, "qualify".
                # The lap matcher compares phase exactly, so no qualifying result lap ever
                # paired with its live twin: ingest inserted each one as a duplicate, and
                # v16 could not see them (11 on the real store). The parser now says
                # "qualifying"; this renames the rows stored before it, in `laps` and
                # `race_sessions`, then re-runs v16's cleanup, which can now pair them.
                #
                # The rename is also what makes RE-importing a pre-fix log harmless.
                # Ingest dedupes a file against its own earlier rows by phase as well, so
                # without it every such log would be inserted again (4 -> 8 rows for one
                # real log, 1,287 across the results dir): result<->result copies that no
                # cleanup removes, since it only pairs a result row with a live one.
                #
                # Backed up before ANY change -- the rename included -- and only when
                # there is one to make; the rename, the cleanup and the version bump then
                # commit together. The re-run also picks up duplicates v16 spared as
                # unreachable whose race identity a later ingest has since filled in.
                #
                # This is also v16's cleanup for a db older than v16 (see above), so such a
                # db gets ONE pass. A db already at v16 gets a second, which cannot know the
                # live laps v16 used: a live lap whose twin v16 removed is free again, and a
                # different file's row within 1 ms of it would now pair with it and go. That
                # needs two files of one track/car/phase within 6 h holding laps within 1 ms
                # of each other. Measured on the real store against its pre-v16 backup: no
                # v17 pair reuses a live lap v16 paired (v16 102 pairs, v17 29, overlap 0).
                cols = {r[1] for r in self.conn.execute("PRAGMA table_info(laps)")}
                rs_cols = {r[1] for r in self.conn.execute("PRAGMA table_info(race_sessions)")}
                if _CLEANUP_COLUMNS <= cols and "phase" in rs_cols:
                    stray = self.conn.execute(
                        "SELECT EXISTS (SELECT 1 FROM laps WHERE phase = 'qualify') "
                        "OR EXISTS (SELECT 1 FROM race_sessions WHERE phase = 'qualify')"
                    ).fetchone()[0]
                    if stray or _duplicate_result_lap_pairs(self.conn):
                        if self._back_up_before_cleanup(db_path, 17):
                            for table in ("laps", "race_sessions"):
                                self.conn.execute(f"UPDATE {table} SET phase = 'qualifying' "
                                                  "WHERE phase = 'qualify'")
                            _drop_duplicate_result_laps(
                                self.conn, _duplicate_result_lap_pairs(self.conn))
                        else:
                            stamp = current_version   # nothing renamed or removed; retry
            if current_version < 18:
                # v17 -> v18: the game's own lap number (scoring mTotalLaps, read at the
                # same commit as mLastLapTime) and whether the car was ever in the pits
                # during the lap (scoring mInPits) -- issue #77. Existing rows stay NULL;
                # lap numbering falls back to today's per-phase counting when any lap in
                # a run lacks lap_no (CoachContext._lap_numbering, LapList.svelte).
                cols = [r[1] for r in self.conn.execute("PRAGMA table_info(laps)")]
                if cols:
                    for c in ("lap_no", "pit"):
                        if c not in cols:
                            _add_column(self.conn, "laps", f"{c} INTEGER")
            if current_version < 19:
                # v18 -> v19: persist the PTT spoken cue (the short line read aloud)
                # alongside the written answer, so a later review can compare what
                # was heard against what was written -- issue #108. Existing rows
                # stay NULL. Guard the ADD COLUMN like the v4/v5/v12 steps.
                cols = [r[1] for r in self.conn.execute("PRAGMA table_info(coach_chat)")]
                if cols and "spoken" not in cols:
                    _add_column(self.conn, "coach_chat", "spoken TEXT")
            if current_version < 20:
                # v19 -> v20: the kind of UNFINISHED lap a row is -- 'out', 'in',
                # 'partial' -- or NULL for a normal timed lap (issue #77). Existing
                # rows stay NULL, which is exactly what they are; a partial row also
                # carries lap_time NULL and valid 0, so `best_lap`, `_stint_analyses`,
                # consistency and tire trend keep ignoring it with no change. Guarded
                # like the v18/v19 steps.
                cols = [r[1] for r in self.conn.execute("PRAGMA table_info(laps)")]
                if cols and "partial" not in cols:
                    _add_column(self.conn, "laps", "partial TEXT")
            if current_version < 21:
                # v20 -> v21: the race situation at each lap's commit (#152) -- class
                # position, the in-class car ahead/behind and their gaps, time left and
                # a lap-count race's length -- so the gap trend has a stored history.
                # Existing rows stay NULL. Guarded like the v18-v20 steps.
                cols = [r[1] for r in self.conn.execute("PRAGMA table_info(laps)")]
                if cols:
                    for c, typ in _RACE_COLUMNS:
                        if c not in cols:
                            _add_column(self.conn, "laps", f"{c} {typ}")
            if current_version < 22:
                # v21 -> v22: per-lap battle rows for get_battle (#152). A new table, so
                # nothing to backfill: laps stored before it have no battle rows.
                self.conn.executescript(_BATTLE_SCHEMA)
            if current_version < 23:
                # v22 -> v23: mapping is a FLAG, not a phase (#154). Capture used to stamp
                # phase='mapping' over a lap's real phase, so a stuck mapping mode erased a
                # whole qualifying + race. Old rows' real phase is unknown, so they KEEP
                # phase='mapping' (honest: "a mapping lap, phase not recorded", and the
                # selector scripts/repair_mapping_phase.py recovers known ones by) and gain
                # the flag, which is what every exclusion now reads. Guarded like v18-v21.
                cols = [r[1] for r in self.conn.execute("PRAGMA table_info(laps)")]
                if cols:
                    if "mapping" not in cols:
                        _add_column(self.conn, "laps", "mapping INTEGER DEFAULT 0")
                    self.conn.execute("UPDATE laps SET mapping = 1 WHERE phase = 'mapping'")
            if current_version < 24:
                # v23 -> v24: provisional per-corner rows for the lap in progress (#160).
                # A new table, nothing to backfill.
                self.conn.executescript(_LIVE_CORNERS_SCHEMA)
        if current_version != stamp:
            self.conn.execute(f"PRAGMA user_version = {stamp}")
        self.conn.commit()

    def _back_up_before_cleanup(self, db_path: str, version: int) -> bool:
        """Copy the store to `<db>.pre-v<version>.bak` before a migration step changes rows
        it cannot give back. True when a valid backup now exists; False -- logged -- when
        none could be written, in which case the step must change nothing.

        Only called when there IS something to change: this runs unprompted on app launch.
        backup() against an open WRITE transaction on its own connection retries
        SQLITE_LOCKED forever (measured: still hung after 10 s), and an older db arrives
        here with earlier steps' writes uncommitted, so those are committed first. Safe:
        each earlier step is idempotent and user_version is not bumped yet, so a crash after
        this commit only re-runs them. The step's own changes and the version bump still
        commit together, after this returns.

        Written to a fresh temp name and MOVED into place. Backing up straight onto the
        `.bak` failed on every launch forever when a leftover there was unusable (corrupt,
        truncated, read-only): each open stayed on the old version, silently. A leftover
        os.replace cannot overwrite (read-only, on Windows) keeps this run's backup at its
        temp name instead -- a valid backup still exists before anything changes.

        Known and pre-existing, not specific to this: if another process holds the db at
        launch, backup() or the step's writes fail with "database is locked"; nothing past
        the old version is stamped, and the next open retries."""
        if self.conn.in_transaction:
            self.conn.commit()
        bak = f"{db_path}.pre-v{version}.bak"
        tmp = f"{bak}.{uuid4().hex}.tmp"
        log = logging.getLogger(__name__)
        try:
            with closing(sqlite3.connect(tmp)) as dst:
                self.conn.backup(dst)
        except sqlite3.Error:
            with suppress(OSError):
                os.remove(tmp)
            log.warning("v%d migration skipped: could not write the backup %s; will retry "
                        "on the next open", version, tmp, exc_info=True)
            return False
        try:
            os.replace(tmp, bak)
        except OSError:
            log.warning("could not replace %s; this run's pre-v%d backup is kept at %s",
                        bak, version, tmp, exc_info=True)
        return True

    def _resolve_session_instance(self, base: str, ts: float) -> str:
        """Map a deterministic base id to the right weekend instance: reuse the
        current one if the last lap was within _SESSION_GAP_S, else start a new
        instance (`base#<date>`). The first weekend keeps the clean base id.
        Non-deterministic ids (e.g. the REPL's timestamp) pass through. Must be
        called under self._lock (reads the laps table)."""
        if not base or not _is_deterministic_sid(base):
            return base
        base = _session_base(base)
        # Newest ACTIVITY in any instance of this base (exact base, or base + '~...').
        # Activity is laps OR coach chats: a pre-lap "garage" chat stamped with a fresh
        # instance must be joined by the laps that follow (within the gap), so the chat
        # stays linked to the session and resumable — else the first lap would mint a
        # second instance minutes later and orphan the chat.
        match = ("substr(session_id, 1, ?) = ? "
                 "AND (length(session_id) = ? OR substr(session_id, ?, 1) = ?)")
        args = (len(base), base, len(base), len(base) + 1, _SESSION_SEP)
        row = self.conn.execute(
            f"SELECT session_id, ts FROM ("
            f"  SELECT session_id, ts FROM laps WHERE {match} "
            f"  UNION ALL "
            f"  SELECT session_id, ts FROM coach_chat WHERE session_id IS NOT NULL AND {match} "
            f") ORDER BY ts DESC LIMIT 1",
            args + args,
        ).fetchone()
        if row is None:
            return base  # first ever for this combo
        if abs(ts - row[1]) <= _SESSION_GAP_S:
            return row[0]  # same weekend (or a sidecar restart) -> continue it
        return f"{base}{_SESSION_SEP}{time.strftime('%Y%m%dT%H%M', time.localtime(ts))}"

    def resolve_current_instance(self, base: str, ts: float | None = None) -> str:
        """The current session-instance id for a deterministic base
        (`track--car--online|offline`), for stamping activity that isn't a lap —
        e.g. a coach chat started in the garage before any lap is captured. Reuses a
        recent instance (lap or chat within the gap) so the upcoming laps share it,
        else mints a fresh one. Non-deterministic bases pass through unchanged."""
        with self._lock:
            return self._resolve_session_instance(base, ts if ts is not None else time.time())

    def save(self, record: LapRecord, metrics: list | None = None) -> int:
        telemetry_path = None
        try:
            with self._lock, self.conn:
                sid = self._resolve_session_instance(record.session_id, record.timestamp) \
                    if record.session_id else None

                def _sec(i):
                    return record.sectors[i] if record.sectors and len(record.sectors) > i else None
                bounds = json.dumps(record.sector_bounds) if record.sector_bounds else None
                pit = None if record.pit is None else int(record.pit)
                race = [getattr(record.race, c) if record.race else None
                        for c, _ in _RACE_COLUMNS]
                cur = self.conn.execute(
                    "INSERT INTO laps (ts, sim, track, car, lap_time, valid, telemetry_path, "
                    "phase, session_id, s1, s2, s3, position, sector_bounds, lap_no, pit, "
                    "partial, mapping, " + ", ".join(c for c, _ in _RACE_COLUMNS) + ") "
                    "VALUES (" + ", ".join(["?"] * (18 + len(_RACE_COLUMNS))) + ")",
                    (record.timestamp, record.sim, record.track, record.car,
                     record.lap_time, int(record.valid), None,
                     record.phase, sid, _sec(0), _sec(1), _sec(2),
                     record.position, bounds, record.lap_no, pit, record.partial,
                     int(record.mapping), *race),
                )
                lap_id = cur.lastrowid
                if record.live_meta:
                    self._supersede_live(record.live_meta)
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
            # Reflect the resolved instance back onto the record so live consumers
            # (/status, lap list, coach auto-anchor) that read it point at THIS
            # session, not the deterministic base (which is the oldest weekend).
            record.session_id = sid
            return lap_id
        except Exception:
            if telemetry_path and os.path.exists(telemetry_path):
                os.remove(telemetry_path)
            raise

    def lap_track_car(self, lap_id: int) -> tuple[str, str] | None:
        """(track, car) for a lap id, or None if it doesn't exist. Unlike load(),
        this never requires telemetry -- a result-log-imported lap (telemetry_path
        NULL) still has a track/car, which is all per-track map resolution needs."""
        with self._lock:
            row = self.conn.execute(
                "SELECT track, car FROM laps WHERE id = ?", (lap_id,),
            ).fetchone()
        return (row[0], row[1]) if row is not None else None

    def latest_lap_row(self, session_id: str | None = None) -> dict | None:
        """The newest live-captured lap (by ts), optionally scoped to a session --
        the store-backed fallback for `/coach` and `/status` when there's no live
        SMM and no in-memory `app.state.latest` (#137: a sidecar restart with LMU
        sitting in the lobby). Only `source = 'live'` rows are candidates -- a
        result-log import carries no live session to resume into."""
        with self._lock:
            query = "SELECT id, track, car, session_id, ts FROM laps WHERE source = 'live'"
            params: list = []
            if session_id is not None:
                query += " AND session_id = ?"
                params.append(session_id)
            query += " ORDER BY ts DESC LIMIT 1"
            row = self.conn.execute(query, params).fetchone()
        if row is None:
            return None
        return dict(zip(("id", "track", "car", "session_id", "ts"), row))

    def save_battle(self, lap_id: int, laps: list[CarLap]) -> None:
        """#152: every same-class car's corner timing for one of the driver's laps."""
        rows = [(lap_id, c.car_id, c.driver, c.car, c.model, c.class_position,
                 int(c.is_player),
                 json.dumps({"entry": c.entry, "exit": c.exit, "apex_kph": c.apex_kph,
                             "lap": c.lap, "pit": c.pit}))
                for c in laps]
        with self._lock, self.conn:
            # A lap's battle is replaced whole: a re-run commit never doubles its rows.
            self.conn.execute("DELETE FROM battle_laps WHERE lap_id = ?", (lap_id,))
            self.conn.executemany(
                "INSERT INTO battle_laps (lap_id, car_id, driver, car, model, "
                "class_position, is_player, crossings) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)

    def battle_laps(self, lap_id: int) -> list[CarLap]:
        """The CarLaps save_battle stored for a lap (JSON's string keys back to corner
        numbers), ordered by car id."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT car_id, driver, car, model, class_position, is_player, crossings "
                "FROM battle_laps WHERE lap_id = ? ORDER BY car_id", (lap_id,)).fetchall()
        out = []
        for car_id, driver, car, model, pos, is_player, crossings in rows:
            x = json.loads(crossings)
            timing = {k: {int(n): v for n, v in x.get(k, {}).items()}
                      for k in ("entry", "exit", "apex_kph", "lap")}
            out.append(CarLap(car_id, driver, car, pos, bool(is_player), **timing,
                              model=model or "",
                              pit=[tuple(span) for span in x.get("pit", [])]))
        return out

    @staticmethod
    def _live_key(meta: dict) -> tuple:
        return (meta["session_id"], meta["lap_no"], meta["start_ts"])

    def _supersede_live(self, meta: dict) -> None:
        """Delete one provisional lap's rows by its exact key (an index lookup). Caller
        holds the lock and the transaction."""
        key = self._live_key(meta)
        self._live_superseded.add(key)
        self.conn.execute("DELETE FROM live_corners WHERE session_id = ? AND lap_no = ? "
                          "AND start_ts = ?", key)

    def supersede_live_lap(self, meta: dict) -> None:
        """#160: the lap `meta` names has been stored -- its provisional rows go."""
        with self._lock, self.conn:
            self._supersede_live(meta)

    def age_out_live_corners(self, before_ts: float) -> None:
        """#160: drop provisional laps that began before `before_ts` (indexed)."""
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM live_corners WHERE start_ts < ?", (before_ts,))

    def save_live_corner(self, meta: dict, label: str, corner_no: int | None,
                         facts: dict) -> None:
        """#160: one corner of the lap in progress (livelap.py), or its lap-wide
        incident scan (label '_lap'), replacing any earlier write of the same row. A
        lap already stored is never written again."""
        with self._lock, self.conn:
            if self._live_key(meta) in self._live_superseded:
                return
            self.conn.execute(
                "INSERT OR REPLACE INTO live_corners (session_id, phase, lap_no, start_ts, "
                "label, corner_no, ts, facts) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (meta["session_id"], meta["phase"], meta["lap_no"], meta["start_ts"],
                 label, corner_no, time.time(), json.dumps(facts)))

    def live_lap_corners(self, session_id: str, lap_no: int | None = None,
                         start_ts: float | None = None) -> tuple[dict | None, list[dict]]:
        """#160: (lap meta, corner rows in along-lap order) for one provisional lap of
        this session -- the named one, else the newest. meta["incidents"] is the lap-wide
        scan (or None). A stored lap's rows are already gone (save supersedes them).
        `session_id` may be an instance id; rows are keyed to its base."""
        base = _session_base(session_id or "")
        with self._lock:
            if lap_no is None:
                row = self.conn.execute(
                    "SELECT lap_no, start_ts FROM live_corners WHERE session_id = ? "
                    "ORDER BY start_ts DESC LIMIT 1", (base,)).fetchone()
                if row is None:
                    return None, []
                lap_no, start_ts = row
            rows = self.conn.execute(
                "SELECT phase, label, corner_no, ts, facts FROM live_corners "
                "WHERE session_id = ? AND lap_no = ? AND start_ts = ? ORDER BY corner_no",
                (base, lap_no, start_ts)).fetchall()
        if not rows:
            return None, []
        lap_rows = [json.loads(r[4]) for r in rows if r[1] == _LIVE_LAP_ROW]
        meta = {"session_id": base, "phase": rows[0][0], "lap_no": lap_no,
                "start_ts": start_ts, "incidents": lap_rows[0] if lap_rows else None}
        return meta, [{"label": r[1], "corner_no": r[2], "ts": r[3],
                       "facts": json.loads(r[4])} for r in rows if r[1] != _LIVE_LAP_ROW]

    def load(self, lap_id: int) -> LapRecord:
        with self._lock:
            row = self.conn.execute(
                "SELECT ts, sim, track, car, lap_time, valid, telemetry_path, phase, session_id, "
                "lap_no, pit, partial, mapping FROM laps WHERE id = ?",
                (lap_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"no lap with id {lap_id}")
            (ts, sim, track, car, lap_time, valid, telemetry_path, phase, session_id,
             lap_no, pit, partial, mapping) = row
            if telemetry_path is None:
                raise FileNotFoundError(f"lap {lap_id} has no telemetry (imported from a result log)")
            with np.load(telemetry_path) as npz:
                channels = {k: npz[k] for k in npz.files}
            return LapRecord(sim, track, car, lap_time, bool(valid), ts, channels, phase, session_id,
                              lap_no=lap_no, pit=(None if pit is None else bool(pit)),
                              partial=partial, mapping=bool(mapping))

    def best_lap(self, track: str, car: str, exclude_lap_id: int | None = None,
                 phase: str | None = None, session_id: str | None = None) -> LapRecord | None:
        with self._lock:
            # Exclude mapping-capture laps (slow weave/boundary laps) so a fresh
            # track with only mapping laps doesn't pick one as the reference best. The
            # flag, not the phase (#154): a mapping lap keeps its real phase.
            # session_id alone scopes the stint; when given, don't also match on car
            # (the caller may pass a resolved display model while laps store the raw
            # entry name — see recent_laps).
            if session_id is not None:
                query = ("SELECT id FROM laps WHERE session_id = ? AND valid = 1 "
                         "AND telemetry_path IS NOT NULL "
                         "AND NOT COALESCE(mapping, 0)")
                params: list = [session_id]
            else:
                # `car` may be a resolved display model while laps store the raw entry
                # name (see recent_laps) -- expand to every raw entry that resolves to
                # it, same as _session_where. Falls back to the literal value when
                # nothing resolves to it (an exact raw-name match, as before).
                teams = [c for c in self._distinct_db_cars() if resolve_car(c) == car] or [car]
                query = (f"SELECT id FROM laps WHERE track = ? "
                         f"AND car IN ({','.join('?' * len(teams))}) AND valid = 1 "
                         "AND telemetry_path IS NOT NULL "
                         "AND NOT COALESCE(mapping, 0)")
                params = [track, *teams]
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

    def list_lap_ids(self, track: str, car: str, complete_only: bool = False) -> list[int]:
        """Every lap id for a track/car. complete_only drops out/in/partial laps (#77)
        -- map building, the Tracks badge and "how much data do we have" counts want
        only whole laps. The default stays inclusive because lap REFERENCE resolution
        (`_resolve_lap`) must be able to reach a partial lap by its number."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT id FROM laps WHERE track = ? AND car = ?"
                + (" AND partial IS NULL" if complete_only else ""),
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
                    session_id: str | None = None,
                    complete_only: bool = False) -> list[dict]:
        """Recent laps for a track/car (newest first) as lightweight rows — no
        telemetry loaded — so a tool can list laps for the model to choose from.
        Pass session_id to scope to the current stint.

        complete_only drops out/in/partial laps (#77). The lap LIST wants them (they
        are laps, and their numbers are the point); anything that treats "the most
        recent lap" as the driver's subject or as data — the coach's `latest`, the
        grip envelope, the wear baseline — wants the most recent COMPLETE one, or a
        garage return makes the in-lap the default subject."""
        with self._lock:
            # session_id uniquely scopes a stint (track+car+online), so when it's
            # given, filter by it ALONE — the caller's `car` may be a resolved display
            # model ("BMW M4 LMGT3") while laps are stored under the raw entry name, so
            # matching on car would wrongly return nothing (matches the UI, which
            # scopes by session_id).
            cols = ("SELECT id, lap_time, valid, phase, partial, position, pit, mapping "
                    "FROM laps WHERE ")
            if session_id is not None:
                query = cols + "session_id = ?"
                params: list = [session_id]
            else:
                query = cols + "track = ? AND car = ?"
                params = [track, car]
            if complete_only:
                query += " AND partial IS NULL"
            query += " ORDER BY id DESC LIMIT ?"
            params.append(limit)
            rows = self.conn.execute(query, params).fetchall()
            keys = ("lap_id", "lap_time", "valid", "phase", "partial", "position", "pit",
                    "mapping")
            out = [dict(zip(keys, r)) for r in rows]
            for d in out:
                d["pit"] = None if d["pit"] is None else bool(d["pit"])
                d["mapping"] = bool(d["mapping"])
            return out

    def split_merged_sessions(self) -> dict:
        """One-time migration: split deterministic sessions that merged multiple
        weekends (same track/car/mode across a long gap) into per-weekend instances
        by _SESSION_GAP_S. The oldest weekend keeps the clean base id; later ones
        become `base#<date>`. A race result sitting on the base is moved to the newest
        instance (results carry no timestamp; the stored one is the last ingested).
        Idempotent — re-running reproduces the same ids and changes nothing."""
        with self._lock, self.conn:
            rows = self.conn.execute(
                "SELECT id, ts, session_id FROM laps WHERE session_id IS NOT NULL ORDER BY ts"
            ).fetchall()
            by_base: dict[str, list] = {}
            for lap_id, ts, sid in rows:
                base = _session_base(sid)
                if _is_deterministic_sid(base):
                    by_base.setdefault(base, []).append((lap_id, ts))

            split = 0
            reassigned = 0
            for base, laps in by_base.items():
                instances: list[tuple[str, list[int]]] = []
                cur_sid, cur_ids, prev_ts = base, [], None
                for lap_id, ts in laps:  # ts-ordered
                    if prev_ts is not None and (ts - prev_ts) > _SESSION_GAP_S:
                        instances.append((cur_sid, cur_ids))
                        cur_ids = []
                        cur_sid = f"{base}{_SESSION_SEP}{time.strftime('%Y%m%dT%H%M', time.localtime(ts))}"
                    cur_ids.append(lap_id)
                    prev_ts = ts
                instances.append((cur_sid, cur_ids))
                if len(instances) <= 1:
                    continue  # single weekend — nothing merged
                split += 1
                for sid, ids in instances:
                    if sid == base:
                        continue  # oldest weekend keeps the clean base id
                    self.conn.executemany(
                        "UPDATE laps SET session_id = ? WHERE id = ?",
                        [(sid, lap_id) for lap_id in ids],
                    )
                    reassigned += len(ids)
                # Move a base race result to the newest instance, unless that instance
                # already has its own result row (avoid a PK clash).
                newest = instances[-1][0]
                if newest != base:
                    exists = self.conn.execute(
                        "SELECT 1 FROM race_sessions WHERE session_id = ?", (newest,)
                    ).fetchone()
                    if not exists:
                        self.conn.execute(
                            "UPDATE race_sessions SET session_id = ? WHERE session_id = ?",
                            (newest, base),
                        )
            self.conn.commit()
        return {"split_sessions": split, "reassigned_laps": reassigned}

    def session_lap_ids(self, session_id: str, complete_only: bool = False) -> list[int]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT id FROM laps WHERE session_id = ?"
                + (" AND partial IS NULL" if complete_only else "")
                + " ORDER BY id ASC",
                (session_id,),
            ).fetchall()
            return [r[0] for r in rows]

    def create_chat(self) -> str:
        """Mint a new chat id (a chat can span multiple race sessions). No row is
        created — a chat is implied by the messages saved under its id."""
        return f"chat-{time.strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:6]}"

    def save_chat(self, chat_id: str, session_id: str, track: str | None, car: str | None,
                  lap_id: int | None, question: str, answer: str,
                  tool_calls: list | None = None, ts: float | None = None,
                  usage: dict | None = None, model: str | None = None,
                  spoken: str | None = None) -> None:
        """Append one coach Q&A exchange (with the tools it invoked) to the log.

        `usage` is a TokenUsage.as_dict() of what this turn actually billed, summed
        over every tool-loop call; `model` is the model that billed it. Both optional
        — a turn with no usage stores NULLs and sums as zero. `spoken` (added v19) is
        the PTT cue actually read aloud, so a later review can compare what was heard
        against what was written; NULL for a non-voice or non-PTT turn."""
        if chat_id is None:
            raise ValueError("chat_id is required")
        tc_json = json.dumps([_capped_result(c) for c in tool_calls]) if tool_calls else None
        u = usage or {}
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO coach_chat (ts, chat_id, session_id, track, car, lap_id, "
                "question, answer, tool_calls, input_tokens, output_tokens, "
                "cache_read_tokens, cache_write_tokens, model, spoken) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ts if ts is not None else time.time(), chat_id, session_id, track, car,
                 lap_id, question, answer, tc_json,
                 *(u.get(key) for _, key in _USAGE_COLUMNS), model, spoken),
            )

    def chat_usage(self, chat_id: str) -> dict:
        """Billed tokens for a whole chat: the four buckets + `total` + the message
        count, plus a `by_model` breakdown.

        `by_model` exists because Settings can change the model mid-chat, so the chat's
        cost is only priceable per model — pricing the total against one id would be
        wrong. Rows written before v12 (and any turn whose usage went missing) carry
        NULL and count as 0 in the sums; an unknown chat is all zeroes, never an error.
        Unknown / NULL model rows key under "" so the caller can see them.

        A NULL-usage row summed as 0 is indistinguishable from a turn that genuinely
        billed zero tokens once COALESCEd — so `null_usage_rows` reports that count
        separately (keyed on `input_tokens IS NULL`) rather than folding it into the
        buckets. The caller (server `_usage_view`) uses it to flag the chat's cost as
        incomplete even when every bucket happens to sum to zero."""
        sums = ", ".join(f"SUM(COALESCE({col}, 0))" for col, _ in _USAGE_COLUMNS)
        with self._lock:
            rows = self.conn.execute(
                f"SELECT model, {sums}, COUNT(*) FROM coach_chat "
                "WHERE chat_id = ? GROUP BY model",
                (chat_id,),
            ).fetchall()
            null_usage_rows = self.conn.execute(
                "SELECT COUNT(*) FROM coach_chat WHERE chat_id = ? AND input_tokens IS NULL",
                (chat_id,),
            ).fetchone()[0]
        out = {key: 0 for _, key in _USAGE_COLUMNS}
        out["messages"] = 0
        by_model: dict[str, dict] = {}
        for row in rows:
            model, counts, n = row[0], row[1:-1], row[-1]
            per = {key: counts[i] for i, (_, key) in enumerate(_USAGE_COLUMNS)}
            for key, val in per.items():
                out[key] += val
            out["messages"] += n
            by_model[model or ""] = per
        out["total"] = sum(out[key] for _, key in _USAGE_COLUMNS)
        out["by_model"] = by_model
        out["null_usage_rows"] = null_usage_rows
        return out

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
                "SELECT ts, session_id, track, car, lap_id, question, answer, tool_calls, spoken "
                "FROM coach_chat WHERE chat_id = ? ORDER BY ts, id",
                (chat_id,),
            ).fetchall()
            keys = ("ts", "session_id", "track", "car", "lap_id", "question", "answer", "tool_calls",
                     "spoken")
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
                "SELECT ts, track, car, lap_id, question, answer, tool_calls, spoken FROM coach_chat "
                "WHERE session_id = ? ORDER BY id ASC",
                (session_id,),
            ).fetchall()
            keys = ("ts", "track", "car", "lap_id", "question", "answer", "tool_calls", "spoken")
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
                # COUNT of COMPLETE laps: out/in/partial rows are stored now (#77) but
                # this card's number is "laps you ran here", and counting them would
                # silently inflate every historical session's total the day they
                # started being stored.
                "SELECT session_id, track, car, SUM(partial IS NULL), "
                # A mapping lap (#154) is never a best, as in best_lap.
                "MIN(CASE WHEN valid AND NOT COALESCE(mapping, 0) THEN lap_time END), "
                "MIN(ts), MAX(ts) "
                f"FROM laps WHERE {where} GROUP BY session_id "
                "ORDER BY MAX(ts) DESC LIMIT ? OFFSET ?",
                params + [limit, offset]).fetchall()
            # Which of this page's sessions have a saved chat -- one query over just
            # this page's ids (page size is small; the alternative is a correlated
            # EXISTS per row in the query above, which needs an index to stay fast).
            ids = [r[0] for r in rows]
            with_chat = {r[0] for r in self.conn.execute(
                "SELECT DISTINCT session_id FROM coach_chat WHERE session_id IN "
                f"({','.join('?' * len(ids))})", ids).fetchall()} if ids else set()
        keys = ("session_id", "track", "car", "laps", "best", "started", "last")
        out = []
        for r in rows:
            d = dict(zip(keys, r))
            d["car_model"] = resolve_car(d["car"]) if d["car"] else None
            d["has_chat"] = d["session_id"] in with_chat
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
        per = ("SELECT session_id, car, "
               "MIN(CASE WHEN valid AND NOT COALESCE(mapping, 0) THEN lap_time END) AS best, "
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
                "SELECT id, lap_time, valid, ts, s1, s2, s3, phase, track, car, lap_no, pit, "
                "partial, position, mapping FROM laps WHERE session_id = ? ORDER BY ts",
                (session_id,),
            ).fetchall()
            keys = ("id", "lap_time", "valid", "ts", "s1", "s2", "s3", "phase", "track", "car",
                    "lap_no", "pit", "partial", "position", "mapping")
            out = []
            for r in rows:
                d = dict(zip(keys, r))
                d["car_model"] = resolve_car(d["car"]) if d["car"] else None
                d["pit"] = None if d["pit"] is None else bool(d["pit"])
                d["mapping"] = bool(d["mapping"])
                out.append(d)
        # race_index (#142): 1-based, null for a non-race lap. Derived here -- the only
        # place a race boundary is computed -- so the lap list can restart its numbering
        # per race without reimplementing _race_windows_from_laps' boundary rule (that's
        # the note-key bug's shape: the same grammar built twice, and the two drift).
        win_of_lap = {lid: w["index"] for w in self._race_windows_from_laps(out)
                      for lid in w["lap_ids"]}
        for d in out:
            d["race_index"] = win_of_lap.get(d["id"])
        return out

    @staticmethod
    def _race_windows_from_laps(laps: list[dict]) -> list[dict]:
        """The windowing rule race_windows() applies, over an already-fetched,
        ts-ordered lap list. Pulled out so session_laps can derive race_index from
        the same rows it already has, without a second query or a second boundary
        rule (see race_windows's docstring for the rule itself and the shape)."""
        windows: list[dict] = []
        current: dict | None = None
        for lap in laps:
            if lap.get("phase") != "race":
                current = None      # any other phase ends the race run
                continue
            if current is None or (lap["ts"] - current["end_ts"]) > _RACE_WINDOW_GAP_S:
                current = {"index": len(windows) + 1, "start_ts": lap["ts"],
                           "end_ts": lap["ts"], "lap_ids": [], "laps": 0}
                windows.append(current)
            current["end_ts"] = lap["ts"]
            current["lap_ids"].append(lap["id"])
            if not lap.get("partial"):
                current["laps"] += 1
        return windows

    def race_windows(self, session_id: str) -> list[dict]:
        """The RACES this session instance holds, oldest first (issue #136).

        One session id spans a whole evening (track + car + online|offline, split only
        after _SESSION_GAP_S = 6 h), so three races in a row share it -- and a chat that
        spans them had nothing to say which race an earlier turn was about. A race window
        is a maximal run of phase='race' laps, ended by any non-race lap or by a pause
        longer than _RACE_WINDOW_GAP_S.

        Derived from `laps` on every call: no schema change, and it stays right for laps
        imported later from a result log. Each window is
        {"index" (1-based), "start_ts", "end_ts", "lap_ids" (every stored race lap, ts
        order), "laps" (COMPLETE laps only)}. `laps` is the figure to show a driver --
        it matches get_session_status's complete-only per-phase count, so the two can
        never disagree about how long the race was; `lap_ids` still carries the out/in/
        partial rows so a window's membership test is complete.

        Empty list for a session with no race laps (practice/qualy only).
        """
        return self._race_windows_from_laps(self.session_laps(session_id))

    def session_position_arc(self, session_id: str, phase: str | None = None,
                             since: float | None = None,
                             until: float | None = None) -> dict | None:
        """The session's live per-lap race-position arc, or None if no lap carried
        a position. Lower place number = better, so best = min. Ordered by ts so
        start/last are the first/last lap actually driven. Pass phase='race' to scope
        to the race stint — a session_id spans practice/qualy/race, and positions from
        those earlier phases would otherwise pollute the start/worst/finish figures.

        since/until bound the arc in time, for a sitting that ran more than one race:
        phase='race' alone spans every race the session covers, which would describe
        two different races in one arc."""
        query = "SELECT position FROM laps WHERE session_id = ? AND position IS NOT NULL"
        params: list = [session_id]
        if phase is not None:
            query += " AND phase = ?"
            params.append(phase)
        if since is not None:
            query += " AND ts >= ?"
            params.append(since)
        if until is not None:
            query += " AND ts < ?"
            params.append(until)
        query += " ORDER BY ts"
        with self._lock:
            rows = self.conn.execute(query, params).fetchall()
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
        """Wipe all persisted data: laps, setups, corner metrics, coach chat, race
        outcomes, the record of which result files were imported, the raw per-lap
        telemetry files, AND the raw mapping point clouds under data_dir/captures/
        (#17 -- these used to survive a reset the docstring described as wiping the
        raw telemetry files). Used by Settings > reset data.

        race_sessions and ingested_files are cleared too: leaving them behind left race
        outcomes with no laps to belong to, and told the next ingest every result file
        was already imported -- so a reset could never be undone by re-importing."""
        with self._lock:
            # Skip a table this db doesn't have, matching the `if cols` guard the v15
            # migration step uses two functions away: a reset must not raise on a db
            # that predates (or has lost) one of these tables.
            present = {r[0] for r in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
            for t in ("corner_metrics", "coach_chat", "laps", "setups",
                      "race_sessions", "ingested_files", "battle_laps", "live_corners"):
                if t in present:
                    self.conn.execute(f"DELETE FROM {t}")
            self.conn.commit()
        for f in Path(self.data_dir).rglob("*.npz"):
            f.unlink(missing_ok=True)

    def _already_stored_laps(self, rs, session_id: str) -> list[tuple[int, float | None]]:
        """(id, lap_time) of the laps already stored for THIS result session -- the live
        capture of the same stint, plus this result file's own earlier import -- in id
        order, for _pair_laps. Includes NULL times (a result lap with no time) so those
        dedupe too. The id is what lets a skipped lap's sectors reach the exact stored
        twin it matched (_BACKFILL_SECTORS_SQL). Must be called under self._lock.

        The `source = 'live' OR session_id = ?` scope is load-bearing: LMU writes P1/P2,
        and repeated races, as separate files with the same track/car/phase in one
        sitting, so a plain (track, car, phase, window) scope let session 2's laps
        dedupe against session 1's rows and silently dropped real laps (379 of 7066 on
        a fresh import of 1402 real logs). Only live rows and this same file's rows can
        legitimately already hold a lap this file lists.

        Known limit: the `source = 'live'` half is still scoped to the WINDOW, not to one
        live session, so a live lap from a neighbouring stint can absorb this file's lap.
        A timed match still needs a sub-millisecond coincidence across stints. The
        untimed half is no longer unreachable -- #77 made capture store out/in/partial
        laps with a NULL lap_time -- so `_pair_laps` is handed each row's `lap_no` and an
        untimed pair must agree on it. Two stints in one window whose untimed laps share
        a lap number would still cross-match; scoping this half to the live session the
        result's window overlaps is the fix if that is ever seen."""
        query = ("SELECT id, lap_time, lap_no FROM laps WHERE track IS ? AND car IS ? "
                 "AND phase IS ? AND (source = 'live' OR session_id = ?)")
        params: list = [rs.track, rs.car, rs.session_type, session_id]
        if rs.date_ts is not None:
            query += " AND ts BETWEEN ? AND ?"
            params += [rs.date_ts - _SESSION_GAP_S, rs.date_ts + _SESSION_GAP_S]
        return self.conn.execute(query + " ORDER BY id", params).fetchall()

    def save_result_session(self, rs, session_id: str, logged_ts: float | None = None) -> None:
        """Backfill a parsed LMU result-log session's laps into the laps
        table (source='result', no telemetry -- summary only).

        Laps already stored for this stint are SKIPPED. A session driven with the
        sidecar running is both captured live and written to a result log, so every
        lap arrived twice (issue #68: 19 rows for a 10-lap race). The already-stored
        row wins: a live one carries the telemetry `.npz`, per-lap position and sector
        bounds the result copy lacks, and skipping makes a re-import of the same file a
        no-op. The one thing a skipped lap may hold alone is its SECTORS -- live capture
        writes none when its split fails -- so those are merged onto a live twin that
        lacks them (_BACKFILL_SECTORS_SQL, the same rule the v16 cleanup applies).
        Scoped to this session's own rows (see _already_stored_laps) so a sibling
        session is never raided. Matching is _pair_laps: a lap-time match within
        _LAP_TIME_TOL_S (not `==`: see that constant), each stored lap absorbing at most
        one result lap, so two genuinely identical times still keep two rows. A result
        lap with no time ('--.----') -- the lap live capture tends to miss -- is kept on
        a first import and matched against an equally timeless stored lap after that,
        so re-importing it does not pile up copies.

        `logged_ts` is when the result file was WRITTEN (its mtime), which bounds which
        laps it can possibly describe -- see _RACE_LOG_LAG_S. NULL when unknown, which
        keeps the bound from being applied at all."""
        def _sec(sectors, i):
            return sectors[i] if sectors is not None and len(sectors) > i else None

        with self._lock, self.conn:
            twins = _pair_laps(self._already_stored_laps(rs, session_id),
                               [lap.lap_time for lap in rs.laps],
                               [getattr(lap, "lap_no", None) for lap in rs.laps])
            rows, backfill = [], []
            for lap, twin in zip(rs.laps, twins):
                s1, s2, s3 = (_sec(lap.sectors, i) for i in range(3))
                if twin is not None:
                    # Skip the copy; offer its sectors to the exact twin it matched. The
                    # statement's own WHERE refuses anything but a live, sector-less twin
                    # whose lap time the three add up to.
                    backfill.append({"id": twin, "s1": s1, "s2": s2, "s3": s3})
                    continue
                rows.append(
                    (rs.date_ts, rs.sim, rs.track, rs.car, lap.lap_time, int(lap.valid),
                     None, rs.session_type, session_id, s1, s2, s3, lap.lap_no))
            self.conn.executemany(_BACKFILL_SECTORS_SQL, backfill)
            self.conn.executemany(
                "INSERT INTO laps (ts, sim, track, car, lap_time, valid, telemetry_path, "
                "phase, session_id, source, s1, s2, s3, lap_no) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'result', ?, ?, ?, ?)",
                rows,
            )
            self.conn.execute(
                "INSERT OR REPLACE INTO race_sessions "
                "(session_id, position, class_position, finish_status, incidents, penalties, "
                "grid_position, class_grid_position, track, car, phase, ts, mode, logged_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (session_id, rs.position, rs.class_position, rs.finish_status,
                 json.dumps(rs.incidents or []), json.dumps(rs.penalties or []),
                 rs.grid_position, rs.class_grid_position,
                 rs.track, rs.car, rs.session_type, rs.date_ts,
                 None if rs.online is None else ("online" if rs.online else "offline"),
                 logged_ts),
            )

    def _linked_result_session(self, session_id: str) -> str | None:
        """The result-log session id (XML filename stem) holding the race outcome for
        a LIVE session id, or None.

        Live capture keys laps by `track--car--online|offline`; result ingest keys them
        by the XML stem, so `race_sessions` rows can never be found from a live id by
        session_id alone -- that join failure is why the coach reported a race backwards
        (issue #67). Match instead on the identity race_sessions now carries itself:
        same track and car (both sides agree on the strings), a race phase, the same
        online/offline mode, and a timestamp inside the live session's RACE window.

        Resolving against race_sessions rather than joining through `laps` is what keeps
        this working: result laps are skipped when live capture already holds them, so a
        cleanly captured race leaves zero result lap rows and a laps-join finds nothing
        -- restoring #67's symptom on exactly the races that were captured best.

        Scope rules, each guarding a way to attach the wrong race:
        - Only a live session with RACE laps resolves; a practice-only session must not
          inherit the sitting's race result.
        - NEAREST result to the LAST captured race lap. Anchoring on the first would
          always answer with race 1 of a sitting: a session instance deliberately spans
          a 6 h gap, so both races share one session_id and `ABS(ts - first)` can never
          reach race 2. The coach's session is always the current one, so the most
          recent race is the right default -- and `race_ts`/`next_race_ts` then bound
          the arc to that race rather than to the sitting.
        - Mode must match. One online race in the window would otherwise answer for both
          `t--c--online` and `t--c--offline`, so an offline session would report a grid
          slot and a finish it never drove. Rows ingested before mode existed carry NULL
          and are still allowed, but always lose to an exact-mode match.
        - Ties are broken by newest ts, then session_id, so two equidistant results
          resolve by contract instead of by scan order.
        - A row whose log was WRITTEN before these laps were driven describes an earlier
          race, and is refused. A race's log does not exist while it is being driven, so
          mid-race-2 the newest log on disk is race 1's -- using it asserted a grid slot
          and a "Finished" status for a race still in progress. See _RACE_LOG_LAG_S.

        Returns (result session id, arc-floor ts). The second value is set only on that
        refusal: it is when the stale log was written, which is the point after which the
        session's race laps belong to the race that has no log yet -- so the caller can
        still scope an honest per-lap arc to the current race instead of spanning both."""
        row = self.conn.execute(
            "SELECT track, car, MIN(ts), MAX(ts) FROM laps "
            "WHERE session_id = ? AND source = 'live' AND phase = 'race'",
            (session_id,),
        ).fetchone()
        if row is None or row[2] is None:
            return None, None
        track, car, first, last = row
        base = _session_base(session_id)
        mode = ("online" if base.endswith("--online")
                else "offline" if base.endswith("--offline") else None)
        query = ("SELECT session_id, logged_ts FROM race_sessions WHERE phase = 'race' "
                 "AND track IS ? AND car IS ? AND ts IS NOT NULL AND ts BETWEEN ? AND ?")
        params: list = [track, car, first - _SESSION_GAP_S, last + _SESSION_GAP_S]
        order = "ORDER BY ABS(ts - ?), ts DESC, session_id LIMIT 1"
        if mode is not None:
            query += " AND (mode IS NULL OR mode = ?)"
            params.append(mode)
            order = "ORDER BY (mode IS NULL), ABS(ts - ?), ts DESC, session_id LIMIT 1"
        hit = self.conn.execute(f"{query} {order}", params + [last]).fetchone()
        if hit is None:
            return None, None
        sid, logged_ts = hit
        if logged_ts is not None and last > logged_ts + _RACE_LOG_LAG_S:
            return None, logged_ts
        return sid, None

    def unlogged_race_since(self, session_id: str) -> float | None:
        """When the last result-logged race of this sitting was written, if the session
        has race laps driven AFTER it -- i.e. a race is in progress (or finished but not
        imported) and has no result row yet. None otherwise.

        Used to floor the per-lap arc on that race alone: with no result row there is no
        `race_ts` to bound it, so the arc would otherwise span every race in the sitting
        and report an earlier race's worst position as this one's.

        Known limit: the floor reaches back to the last LOGGED race, not the last race, so
        two consecutive unlogged races would merge into one arc. Measured unreachable on
        the real data: of 112 consecutive race pairs across 233 sittings, 110 have the
        earlier race's log already written when the next race STARTS (median 967 s early),
        and the two exceptions are -1 s and -0 s against the next race's start stamp --
        while a race's first lap cannot complete for at least 67 s after that stamp (min
        over 333 races, median 132 s). So the previous log always exists >= 66 s before the
        next race's first lap row does, and this never merges. It matches the observed
        behaviour that LMU writes the log when you leave the session: starting another race
        requires leaving, which writes it.

        Do NOT try to derive the boundary from lap-timestamp discontinuities instead. That
        was measured and it does not separate: in the one real session spanning two races
        (11 laps, ONE real boundary) a >300 s gap rule fires 6 times, because pit stops,
        incidents and garage time produce 316-924 s gaps inside a single race while genuine
        within-race gaps reach 2.1x the lap time. It would shred one race into many."""
        with self._lock:
            return self._linked_result_session(session_id)[1]

    def race_session(self, session_id: str) -> dict | None:
        """The persisted race outcome (grid/finish position, incidents, penalties) for
        a session, or None if no race row exists (practice/qualy, or not imported).

        Falls back to the result-log row linked to a LIVE session id (see
        _linked_result_session) -- the row is keyed by the XML stem, so a live session
        has no row of its own. None while a race is still being driven: its log is not
        written yet, and the newest one on disk belongs to an earlier race."""
        # phase = 'race' OR NULL: a practice/qualifying result row must not resolve
        # (issue #67 residual). NULL is grandfathered for rows migrated from a
        # pre-v15 db that never had a phase column -- a re-ingest backfills it.
        cols = ("SELECT position, class_position, finish_status, incidents, penalties, "
                "grid_position, class_grid_position, track, car, ts, mode "
                "FROM race_sessions WHERE session_id = ? AND (phase = 'race' OR phase IS NULL)")
        with self._lock:
            row = self.conn.execute(cols, (session_id,)).fetchone()
            if row is None:
                linked, _ = self._linked_result_session(session_id)
                row = self.conn.execute(cols, (linked,)).fetchone() if linked else None
            if row is None:
                return None
            pos, cls_pos, finish, incidents, penalties, grid, cls_grid, track, car, ts, mode = row
            # When this race is one of several in a sitting, the live session spans them
            # all -- so hand the caller the window this race occupies (its own start, up
            # to the next race's) or the per-lap arc would mix two races into one answer.
            next_ts = self.conn.execute(
                "SELECT MIN(ts) FROM race_sessions WHERE phase = 'race' AND track IS ? "
                "AND car IS ? AND ts > ? AND (mode IS NULL OR mode IS ?)",
                (track, car, ts, mode),
            ).fetchone()[0] if ts is not None else None

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
            "grid_position": grid,
            "class_grid_position": cls_grid,
            "race_ts": ts,
            "next_race_ts": next_ts,
        }

    def is_ingested(self, path: str, mtime: float) -> bool:
        with self._lock:
            row = self.conn.execute(
                "SELECT 1 FROM ingested_files WHERE path = ? AND mtime = ?", (path, mtime)
            ).fetchone()
            return row is not None

    def ingested_files(self) -> set[tuple[str, float]]:
        """Every imported (path, mtime), in one query. The result-log poller (#81) checks
        ~1,400 files every 30 s: measured 1 ms this way, 92 ms as an is_ingested() per file."""
        with self._lock:
            return set(self.conn.execute("SELECT path, mtime FROM ingested_files"))

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

    _LEARNING_KEYS = ("id", "text", "status", "track", "car", "source", "session_id",
                       "created_ts", "updated_ts")

    def add_learning(self, text: str, *, status: str, track: str | None = None,
                      car: str | None = None, source: str,
                      session_id: str | None = None) -> int:
        """Record a driver-profile memory (a distilled observation about the
        driver). track/car NULL means it's a general learning, not tied to
        a specific track/car combo."""
        now = time.time()
        with self._lock, self.conn:
            cur = self.conn.execute(
                "INSERT INTO driver_learnings (text, status, track, car, source, "
                "session_id, created_ts, updated_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (text, status, track, car, source, session_id, now, now),
            )
            return cur.lastrowid

    def list_learnings(self, status: str | None = None) -> list[dict]:
        with self._lock:
            if status is not None:
                rows = self.conn.execute(
                    "SELECT id, text, status, track, car, source, session_id, "
                    "created_ts, updated_ts FROM driver_learnings WHERE status = ? "
                    "ORDER BY updated_ts DESC",
                    (status,),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT id, text, status, track, car, source, session_id, "
                    "created_ts, updated_ts FROM driver_learnings ORDER BY updated_ts DESC"
                ).fetchall()
            return [dict(zip(self._LEARNING_KEYS, r)) for r in rows]

    def set_learning_status(self, learning_id: int, status: str) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE driver_learnings SET status = ?, updated_ts = ? WHERE id = ?",
                (status, time.time(), learning_id),
            )

    def update_learning(self, learning_id: int, text: str) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE driver_learnings SET text = ?, updated_ts = ? WHERE id = ?",
                (text, time.time(), learning_id),
            )

    def get_learning(self, learning_id: int) -> dict | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT id, text, status, track, car, source, session_id, "
                "created_ts, updated_ts FROM driver_learnings WHERE id = ?",
                (learning_id,),
            ).fetchone()
            return dict(zip(self._LEARNING_KEYS, row)) if row else None

    def active_learnings(self, track: str | None = None, car: str | None = None,
                          limit: int = 50) -> list[dict]:
        """Active learnings for the coach prompt: general ones (track/car both
        NULL) plus any tagged to match the given track/car, newest first."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, text, status, track, car, source, session_id, "
                "created_ts, updated_ts FROM driver_learnings "
                "WHERE status = 'active' AND (track IS NULL OR track = ?) "
                "AND (car IS NULL OR car = ?) ORDER BY updated_ts DESC LIMIT ?",
                (track, car, limit),
            ).fetchall()
            return [dict(zip(self._LEARNING_KEYS, r)) for r in rows]

    def get_driver_summary(self) -> dict:
        with self._lock:
            row = self.conn.execute(
                "SELECT text, status FROM driver_summary WHERE id = 1"
            ).fetchone()
        return {"text": row[0], "status": row[1]} if row else {"text": "", "status": "active"}

    _SUMMARY_WORD_CAP = 60

    def set_driver_summary(self, text: str, status: str) -> None:
        # The always-on summary rides in every coach prompt -- cap it here (the one
        # place both the manual-edit and approve paths go through) so it can't grow
        # past the spec's ~60-word budget.
        words = text.split()
        if len(words) > self._SUMMARY_WORD_CAP:
            text = " ".join(words[: self._SUMMARY_WORD_CAP])
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO driver_summary (id, text, status, updated_ts) "
                "VALUES (1, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                "text = excluded.text, status = excluded.status, updated_ts = excluded.updated_ts",
                (text, status, time.time()),
            )

    def save_session_setup(self, session_id: str, track: str, car: str, setup: dict) -> None:
        """Persist the current setup for a live session (latest-wins upsert on
        session_id) so the driver profile can recall "what did we run here
        last time?" even if the session ends abruptly."""
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO session_setups (session_id, track, car, setup_json, updated_ts) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET "
                "track = excluded.track, car = excluded.car, setup_json = excluded.setup_json, "
                "updated_ts = excluded.updated_ts",
                (session_id, track, car, json.dumps(setup), time.time()),
            )

    def last_setup(self, track: str, car: str,
                   exclude_session_id: str | None = None) -> dict | None:
        """Most recent session_setups row for this track/car, or None.

        `exclude_session_id` drops the current live session so a "what did we run
        here LAST time?" query returns a prior session's setup, not the one the
        current session already autosaved."""
        sql = ("SELECT session_id, track, car, setup_json, updated_ts FROM session_setups "
               "WHERE track = ? AND car = ?")
        params: list = [track, car]
        if exclude_session_id is not None:
            sql += " AND session_id != ?"
            params.append(exclude_session_id)
        sql += " ORDER BY updated_ts DESC LIMIT 1"
        with self._lock:
            row = self.conn.execute(sql, params).fetchone()
        if not row:
            return None
        session_id, track, car, setup_json, updated_ts = row
        return {
            "session_id": session_id,
            "track": track,
            "car": car,
            "setup": json.loads(setup_json),
            "updated_ts": updated_ts,
        }
