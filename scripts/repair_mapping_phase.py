"""One-off repair (#154): recover the real phase of laps a stuck mapping mode stamped
phase='mapping'.

Before #154 the capture loop OVERWROTE a mapping lap's phase with 'mapping', and on
2026-09-23 mapping stayed on for 40 minutes -- every qualifying and race lap lost its
phase. The phase is recoverable from the ingested result logs: each race_sessions row
is one session with its start (`ts`, the log's DateTime -- session start) and when its
log was written (`logged_ts`, the file mtime -- after the session ended). A lap belongs
to the session of the same track and car that had STARTED by the lap's ts and whose log
(+ store._RACE_LOG_LAG_S) was not written before it; sessions run one after another, so
when the lag window lets two match, the latest-started one is the lap's. No match, or
two different-phase sessions starting together, is AMBIGUOUS and the lap is left as is.

Each recovered lap gets phase=<recovered>. Its mapping flag (1 on every phase='mapping'
row since the v23 migration) becomes 0 for a qualifying or race lap -- mapping is a
Tracks-screen practice activity, so those were stamped only by the stuck mode and must
count in bests and stint stats again -- and stays 1 for a practice lap, which may have
been a real mapping pass. Then the result-log duplicates the wrong phase let ingest
store (source='result' copies of the live laps) are dropped by the store's own cleanup.

Dry run by default: it prints the per-lap changes and the duplicate count, from the same
writes run on an in-memory copy. --apply writes, in one transaction, after backing the
db up to <db>.pre-repair-<timestamp>.bak; it refuses a busy db (the app running).

    python scripts/repair_mapping_phase.py [--db PATH] [--apply]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path

from simcoach import paths
from simcoach.store import (
    _RACE_LOG_LAG_S,
    _drop_duplicate_result_laps,
    _duplicate_result_lap_pairs,
)


def _flag(phase: str) -> int:
    return 1 if phase == "practice" else 0


def plan(conn: sqlite3.Connection) -> list[tuple[int, float, str | None, str | None]]:
    """[(lap id, lap ts, recovered phase or None, race_sessions id or reason)]."""
    out = []
    laps = conn.execute("SELECT id, ts, track, car FROM laps WHERE phase = 'mapping' "
                        "ORDER BY ts").fetchall()
    for lap_id, ts, track, car in laps:
        hits = conn.execute(
            "SELECT session_id, phase, ts FROM race_sessions WHERE track IS ? AND car IS ? "
            "AND phase IS NOT NULL AND phase != 'mapping' AND ts IS NOT NULL "
            "AND logged_ts IS NOT NULL AND ts <= ? AND ? <= logged_ts + ? "
            "ORDER BY ts DESC", (track, car, ts, ts, _RACE_LOG_LAG_S)).fetchall()
        latest = [h for h in hits if h[2] == hits[0][2]]   # the latest-started
        phases = {h[1] for h in latest}
        if len(phases) == 1:
            out.append((lap_id, ts, phases.pop(), latest[0][0]))
        else:
            out.append((lap_id, ts, None, "no covering session" if not latest
                        else "ambiguous: " + ", ".join(sorted(h[0] for h in latest))))
    return out


def _repair(conn: sqlite3.Connection, fixed: list) -> int:
    """Write the recovered phases, then drop the result-log duplicates they expose.
    Returns how many duplicate result laps were dropped. The caller owns the
    transaction (and, for a dry run, hands in a throwaway in-memory copy).

    Result ingest dedupes against live laps of the SAME phase, so a log ingested while
    its live laps said 'mapping' found no twins and stored every lap again as
    source='result'. Once the phases are right, the store's own cleanup
    (_duplicate_result_lap_pairs, with its sibling-session and never-empty-a-race
    safeguards) finds them -- the same code the v16 migration runs."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(laps)")]
    if "mapping" not in cols:
        # A v22 db (not yet opened by a v23 app): the v23 migration step is guarded,
        # so adding the column here is what it would do anyway.
        conn.execute("ALTER TABLE laps ADD COLUMN mapping INTEGER DEFAULT 0")
        conn.execute("UPDATE laps SET mapping = 1 WHERE phase = 'mapping'")
    conn.executemany("UPDATE laps SET phase = ?, mapping = ? "
                     "WHERE id = ? AND phase = 'mapping'",
                     [(phase, _flag(phase), lap_id) for lap_id, _ts, phase, _why in fixed])
    pairs = _duplicate_result_lap_pairs(conn)
    _drop_duplicate_result_laps(conn, pairs)
    return len(pairs)


_BUSY = ("The database is busy (is SimCoach running?). Close the app and re-run; "
         "nothing was written.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", default=str(paths.db_path()), help="lap store (default: app data)")
    ap.add_argument("--apply", action="store_true", help="write the changes (after a backup)")
    args = ap.parse_args(argv)

    # Read-only unless applying: a dry run must not even create or migrate the file.
    # Autocommit (isolation_level=None): this script owns every transaction explicitly.
    uri = f"{Path(args.db).resolve().as_uri()}?mode={'rw' if args.apply else 'ro'}"
    with closing(sqlite3.connect(uri, uri=True, timeout=2, isolation_level=None)) as conn:
        try:
            rows = plan(conn)
        except sqlite3.OperationalError as e:
            if "locked" in str(e) or "busy" in str(e):
                print(_BUSY)
                return 2
            raise
        fixed = [r for r in rows if r[2] is not None]
        for lap_id, ts, phase, why in rows:
            when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "?"
            print(f"lap {lap_id} {when}: mapping -> {phase}, mapping flag 1 -> {_flag(phase)} "
                  f"(race_sessions {why})" if phase
                  else f"lap {lap_id} {when}: untouched ({why})")
        print(f"{len(fixed)} of {len(rows)} phase='mapping' laps recoverable.")
        if not args.apply:
            # The same writes on an in-memory copy, so the count is the real one.
            with closing(sqlite3.connect(":memory:", isolation_level=None)) as mem:
                conn.backup(mem)
                mem.execute("BEGIN")
                dropped = _repair(mem, fixed)
            print(f"duplicate result laps: would drop {dropped}")
            print("Dry run: nothing written. Re-run with --apply to write.")
            return 0
        if not fixed:
            return 0
        # Refuse a busy db up front (#154 red-team): the app holding a write lock would
        # otherwise fail the backup or the writes part-way. Probe, back up, then take
        # the write lock for real -- backup() cannot run under our own write transaction.
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("ROLLBACK")
            bak = f"{args.db}.pre-repair-{time.strftime('%Y%m%dT%H%M%S')}.bak"
            with closing(sqlite3.connect(bak)) as dst:
                conn.backup(dst)
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as e:
            if "locked" in str(e) or "busy" in str(e):
                print(_BUSY)
                return 2
            raise
        print(f"Backed up to {bak}")
        try:
            dropped = _repair(conn, fixed)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        print(f"Repaired {len(fixed)} laps; dropped {dropped} duplicate result laps.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
