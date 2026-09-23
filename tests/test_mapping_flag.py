# #154: a mapping lap keeps its REAL phase; mapping is a separate flag (laps.mapping, v23).
# Stamping phase='mapping' over the lap is how a stuck mapping mode erased a whole
# qualifying + race: no race result, the race called "a mapping run", no race tools.
import sqlite3
import types

import numpy as np

from simcoach.coach_tools import CoachContext
from simcoach.model_types import LapRecord
from simcoach.server.app import _capture_loop
from simcoach.store import SCHEMA_VERSION, LapStore

SID = "maptrack--gt3--online"


def _lap(phase, lap_time=90.0, ts=0.0):
    return LapRecord("lmu", "MapTrack", "GT3", lap_time, True, ts,
                     {"pos_x": np.array([0.0, 50.0, 100.0]),
                      "pos_z": np.array([5.0] * 3),
                      "lap_distance": np.array([0.0, 50.0, 100.0])},
                     phase=phase, session_id=SID)


class _Src:
    def __init__(self, laps):
        self._laps = laps

    def laps(self):
        return iter(self._laps)


def _store(tmp_path):
    return LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))


def test_mapping_lap_keeps_its_real_phase_and_is_flagged(tmp_path):
    store = _store(tmp_path)
    state = types.SimpleNamespace(mapping=True, map_buffer=[], latest=None)
    _capture_loop(_Src([_lap("practice")]), store, state)
    (lap_id,) = store.list_lap_ids("MapTrack", "GT3")
    rec = store.load(lap_id)
    assert rec.phase == "practice"
    assert rec.mapping is True
    assert state.map_buffer                                   # still buffered


def test_mapping_lap_still_excluded_from_best(tmp_path):
    store = _store(tmp_path)
    state = types.SimpleNamespace(mapping=True, map_buffer=[], latest=None)
    _capture_loop(_Src([_lap("practice", 80.0, ts=1.0)]), store, state)   # FASTER, mapping
    store.save(_lap("practice", 95.0, ts=2.0))
    sid = store.load(store.list_lap_ids("MapTrack", "GT3")[0]).session_id
    assert store.best_lap("MapTrack", "GT3").lap_time == 95.0
    assert store.best_lap("MapTrack", "GT3", phase="practice").lap_time == 95.0
    assert store.best_lap("MapTrack", "GT3", session_id=sid).lap_time == 95.0
    assert [r["mapping"] for r in store.session_laps(sid)] == [True, False]


def test_v22_mapping_rows_migrate_to_the_flag(tmp_path):
    db = str(tmp_path / "c.sqlite")
    s = LapStore(db, str(tmp_path / "data"))
    cols = [r[1] for r in s.conn.execute("PRAGMA table_info(laps)")]
    if "mapping" in cols:
        s.conn.execute("ALTER TABLE laps DROP COLUMN mapping")
    s.conn.execute("INSERT INTO laps (ts, track, car, phase, valid) "
                   "VALUES (1, 'T', 'C', 'mapping', 1), (2, 'T', 'C', 'race', 1)")
    s.conn.execute("PRAGMA user_version = 22")
    s.conn.commit()
    s.conn.close()
    s2 = LapStore(db, str(tmp_path / "data"))
    assert s2.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    rows = s2.conn.execute("SELECT phase, mapping FROM laps ORDER BY ts").fetchall()
    # Old rows' real phase is unknown: the phase stays 'mapping' (the repair script's
    # selector), and the flag now carries the exclusion.
    assert rows == [("mapping", 1), ("race", 0)]


def test_fresh_db_has_the_mapping_column(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "c.sqlite"))
    conn.close()
    s = _store(tmp_path)
    assert "mapping" in [r[1] for r in s.conn.execute("PRAGMA table_info(laps)")]


def test_race_mapping_lap_reads_as_a_race_lap(tmp_path):
    # Mapping is a capture detail, not a session type: the coach must call it a race lap.
    store = _store(tmp_path)
    state = types.SimpleNamespace(mapping=True, map_buffer=[], latest=None)
    _capture_loop(_Src([_lap("race", ts=100.0)]), store, state)
    lap_id = state.latest["lap_id"]
    loaded = store.load(lap_id)
    ctx = CoachContext(store, record=loaded, lap_id=lap_id, session_id=loaded.session_id)
    line = ctx._lap_line(loaded, lap_id)
    assert "race lap 1" in line, line
    assert "mapping" not in line.lower()
    listing = ctx.list_recent_laps()
    assert "race" in listing and "mapping" not in listing.lower(), listing
