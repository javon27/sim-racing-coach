# #154: a mapping lap keeps its REAL phase; mapping is a separate flag (laps.mapping, v23).
# Stamping phase='mapping' over the lap is how a stuck mapping mode erased a whole
# qualifying + race: no race result, the race called "a mapping run", no race tools.
import sqlite3
import types

import numpy as np
import pytest

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


# --- mapping ends by itself when the live session changes (#154 safety net) ---------

def _app(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from simcoach.coach import Coach
    from simcoach.lmu import smm
    from simcoach.model.fake import FakeModel
    from simcoach.server.app import create_app
    monkeypatch.setattr(smm, "read_session_track_car", lambda: None)   # no live sim
    store = _store(tmp_path)
    app = create_app(store, Coach(FakeModel()), tracks_dir=str(tmp_path / "tracks"))
    return app, store, TestClient(app, base_url="http://127.0.0.1")


def _changed(field, value, ts):
    lap = _lap("practice", ts=ts)
    setattr(lap, field, value)
    return lap


def test_mapping_ends_when_the_phase_changes(tmp_path, monkeypatch):
    app, store, c = _app(tmp_path, monkeypatch)
    _capture_loop(_Src([_lap("practice", ts=1.0)]), store, app.state)   # driving practice
    assert c.post("/mapcapture/start", json={}).status_code == 200
    _capture_loop(_Src([_lap("practice", ts=2.0)]), store, app.state)   # a mapping pass
    assert app.state.mapping is True and len(app.state.map_buffer) == 1
    _capture_loop(_Src([_lap("qualifying", ts=3.0)]), store, app.state)
    assert app.state.mapping is False                                   # ended itself
    assert len(app.state.map_buffer) == 1                              # not buffered
    q = store.load(app.state.latest["lap_id"])
    assert q.phase == "qualifying" and q.mapping is False
    assert c.get("/mapcapture/status").json()["active"] is False


def test_mapping_started_before_any_lap_ends_on_the_next_change(tmp_path, monkeypatch):
    # A sidecar restart: no lap yet this run, so the first mapping lap pins the session.
    app, store, c = _app(tmp_path, monkeypatch)
    c.post("/mapcapture/start", json={})
    _capture_loop(_Src([_lap("practice", ts=1.0), _lap("practice", ts=2.0),
                        _lap("race", ts=3.0)]), store, app.state)
    assert app.state.mapping is False
    assert [r["mapping"] for r in store.session_laps(store.load(
        app.state.latest["lap_id"]).session_id)] == [True, True, False]



@pytest.mark.parametrize("field,value", [
    ("track", "OtherTrack"),
    ("car", "LMP2"),
])
def test_mapping_ends_on_a_track_car_or_session_change(tmp_path, monkeypatch, field, value):
    app, store, c = _app(tmp_path, monkeypatch)
    c.post("/mapcapture/start", json={})
    _capture_loop(_Src([_lap("practice", ts=1.0), _changed(field, value, 2.0)]),
                  store, app.state)
    assert app.state.mapping is False
    assert store.load(app.state.latest["lap_id"]).mapping is False


def _at(track, phase, ts, mode="offline"):
    lap = _lap(phase, ts=ts)
    lap.track, lap.session_id = track, f"{track.lower()}--gt3--{mode}"
    return lap


def test_generate_on_a_new_track_is_not_ended_by_the_last_track_driven(tmp_path, monkeypatch):
    # Red-team #154: start pinned the session from `latest` -- Spa, driven earlier this
    # sidecar run -- so the driver's first Fuji mapping lap ended mapping at once.
    app, store, c = _app(tmp_path, monkeypatch)
    _capture_loop(_Src([_at("Spa", "race", 1.0)]), store, app.state)
    assert c.post("/mapcapture/start", json={}).status_code == 200
    _capture_loop(_Src([_at("Fuji", "practice", 5000.0), _at("Fuji", "practice", 5100.0)]),
                  store, app.state)
    assert app.state.mapping is True and len(app.state.map_buffer) == 2


def test_generate_in_a_new_practice_after_a_race_is_not_ended(tmp_path, monkeypatch):
    app, store, c = _app(tmp_path, monkeypatch)
    _capture_loop(_Src([_at("Fuji", "race", 1.0)]), store, app.state)
    c.post("/mapcapture/start", json={})
    _capture_loop(_Src([_at("Fuji", "practice", 5000.0)]), store, app.state)
    assert app.state.mapping is True and len(app.state.map_buffer) == 1


def test_online_offline_flip_does_not_end_mapping(tmp_path, monkeypatch):
    # _is_online flips when a human joins the server: same session, mapping stays on.
    app, store, c = _app(tmp_path, monkeypatch)
    c.post("/mapcapture/start", json={})
    _capture_loop(_Src([_at("Fuji", "practice", 1.0, "offline"),
                        _at("Fuji", "practice", 100.0, "online")]), store, app.state)
    assert app.state.mapping is True
