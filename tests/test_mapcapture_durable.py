# tests/test_mapcapture_durable.py
# #150: a mapping lap reaches disk AS IT COMPLETES (captures/pending/<slug>/), so a
# crash / restart / app close between driving and Save loses nothing, no lap is
# silently dropped by a buffer cap, and Save never writes a map without its cloud.
import numpy as np
import pytest
from fastapi.testclient import TestClient

from simcoach.coach import Coach
from simcoach.lmu import smm
from simcoach.model.fake import FakeModel
from simcoach.model_types import LapRecord
from simcoach.server import app as app_mod
from simcoach.server.app import _capture_loop, create_app
from simcoach.store import LapStore


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    # No live SMM: _live_track falls back to the latest lap, never a sim running here.
    monkeypatch.setattr(smm, "read_session_track_car", lambda: None)


def _app(tmp_path):
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    return create_app(store, Coach(FakeModel()), tracks_dir=str(tmp_path / "tracks")), store


def _tc(app):
    return TestClient(app, base_url="http://127.0.0.1")


def _edge(zsign):
    return LapRecord("lmu", "MapTrack", "GT3", 90.0, True, 0.0,
                     {"pos_x": np.array([0.0, 50.0, 100.0]),
                      "pos_y": np.array([0.0, 2.0, 4.0]),
                      "pos_z": np.array([zsign * 5.0] * 3),
                      "lap_distance": np.array([0.0, 50.0, 100.0])})


class _Src:
    def __init__(self, laps):
        self._laps = laps

    def laps(self):
        return iter(self._laps)


def _drive(app, store, n):
    app.state.mapping = True
    _capture_loop(_Src([_edge(1 if i % 2 else -1) for i in range(n)]), store, app.state)


PENDING = ("data", "captures", "pending", "maptrack")
SAVED = ("data", "captures", "maptrack.npz")


def test_lap_is_on_disk_before_any_save(tmp_path):
    app, store = _app(tmp_path)
    _drive(app, store, 2)
    files = sorted(tmp_path.joinpath(*PENDING).glob("*.npz"))
    assert len(files) == 2                       # durable before Save was pressed
    with np.load(str(files[0])) as z:
        assert {k.split("_", 1)[1] for k in z.files} == {"pos_x", "pos_y", "pos_z",
                                                          "lap_distance"}
    assert not tmp_path.joinpath(*SAVED).exists()  # not promoted yet


def test_every_lap_is_kept_past_eight(tmp_path):
    app, store = _app(tmp_path)
    _drive(app, store, 12)
    assert len(list(tmp_path.joinpath(*PENDING).glob("*.npz"))) == 12
    assert len(app.state.map_buffer) == 12


def test_restart_resumes_the_capture_and_save_promotes_it(tmp_path):
    app, store = _app(tmp_path)
    _drive(app, store, 3)
    # "Crash": a NEW app on the same data dir; nothing carried over in RAM.
    app2, _ = _app(tmp_path)
    app2.state.latest = {"record": _edge(1), "lap_id": 0}   # live track = MapTrack
    c = _tc(app2)
    assert c.post("/mapcapture/start", json={}).status_code == 200
    assert len(c.get("/mapcapture/status").json()["laps"]) == 3   # recovered
    assert c.post("/mapcapture/build", json={}).status_code == 200
    assert c.post("/mapcapture/save", json={}).status_code == 200
    saved = tmp_path.joinpath(*SAVED)
    with np.load(str(saved)) as z:
        assert {k.split("_", 1)[0] for k in z.files} == {"lap0", "lap1", "lap2"}
    assert not tmp_path.joinpath(*PENDING).exists()   # promoted, not duplicated
    assert app2.state.mapping is False and app2.state.map_buffer == []


def test_restart_rebuild_sees_pending_capture_and_save_promotes_it(tmp_path):
    app, store = _app(tmp_path)
    _drive(app, store, 2)
    app2, _ = _app(tmp_path)
    c = _tc(app2)
    r = c.post("/mapcapture/rebuild", json={"slug": "maptrack"})
    assert r.status_code == 200, r.text
    assert r.json()["lap_count"] == 2
    assert c.post("/mapcapture/save", json={}).status_code == 200
    assert tmp_path.joinpath(*SAVED).exists()
    assert tmp_path.joinpath("tracks", "maptrack.json").exists()
    assert not tmp_path.joinpath(*PENDING).exists()


def test_save_cloud_after_restart_promotes_pending(tmp_path):
    app, store = _app(tmp_path)
    _drive(app, store, 2)
    app2, _ = _app(tmp_path)
    app2.state.latest = {"record": _edge(1), "lap_id": 0}
    c = _tc(app2)
    c.post("/mapcapture/start", json={})
    assert c.post("/mapcapture/save-cloud", json={}).status_code == 200
    assert tmp_path.joinpath(*SAVED).exists()
    assert not tmp_path.joinpath(*PENDING).exists()


def test_cancel_keeps_the_laps_on_disk(tmp_path):
    # The editor calls cancel on unmount too -- that must not be a loss path.
    app, store = _app(tmp_path)
    _drive(app, store, 2)
    assert _tc(app).post("/mapcapture/cancel", json={}).status_code == 200
    assert len(list(tmp_path.joinpath(*PENDING).glob("*.npz"))) == 2


def test_failed_cloud_write_leaves_no_map_without_capture(tmp_path, monkeypatch):
    # Loss path 3: the map used to be written FIRST, so a failed cloud write left a
    # track map with no .npz and "Rebuild from saved capture" failing later.
    app, store = _app(tmp_path)
    _drive(app, store, 2)
    c = _tc(app)
    assert c.post("/mapcapture/build", json={}).status_code == 200

    def boom(path, laps):
        raise OSError("disk full")
    monkeypatch.setattr(app_mod, "_save_cloud", boom)
    with pytest.raises(OSError):
        c.post("/mapcapture/save", json={})
    assert not tmp_path.joinpath("tracks", "maptrack.json").exists()


def test_capture_built_ribbon_whose_cloud_is_gone_is_refused(tmp_path):
    app, _ = _app(tmp_path)
    app.state.last_ribbon = {"world": {"left": [], "right": [], "center": [], "width": [],
                                       "bank": [], "dist": []},
                             "corners": [], "sectors": [], "track": "MapTrack",
                             "laps": None, "built_from": "capture"}
    assert _tc(app).post("/mapcapture/save", json={}).status_code == 422
    assert not tmp_path.joinpath("tracks", "maptrack.json").exists()


def test_failing_disk_write_never_stops_capture(tmp_path, monkeypatch, caplog):
    app, store = _app(tmp_path)

    def boom(data_dir, lap):
        raise OSError("disk full")
    monkeypatch.setattr(app_mod, "_append_pending_lap", boom)
    _drive(app, store, 3)
    assert len(store.list_lap_ids("MapTrack", "GT3")) == 3   # every lap still stored
    assert len(app.state.map_buffer) == 3
    assert "pending capture" in caplog.text                  # and the failure is logged


def test_discard_drops_the_pending_capture(tmp_path):
    # The explicit "Discard capture": bad laps must not resurrect on every start, nor
    # win _rebuild_clouds over the track's live laps.
    app, store = _app(tmp_path)
    app.state.latest = {"record": _edge(1), "lap_id": 0}
    c = _tc(app)
    c.post("/mapcapture/start", json={})
    _drive(app, store, 2)
    assert c.post("/mapcapture/cancel", json={"discard": True}).json() == {"active": False}
    assert not list(tmp_path.joinpath(*PENDING).glob("*.npz"))
    c.post("/mapcapture/start", json={})
    assert c.get("/mapcapture/status").json()["laps"] == []     # nothing resurrected
    c.post("/mapcapture/cancel", json={})
    # Rebuild falls back to the track's live laps (the 2 stored mapping laps' .npz).
    r = c.post("/mapcapture/rebuild", json={"slug": "maptrack"})
    assert r.status_code == 200, r.text
    assert app.state.last_ribbon["built_from"] == "live_laps"


def test_discard_after_restart_uses_the_live_track(tmp_path):
    app, store = _app(tmp_path)
    _drive(app, store, 2)
    app2, _ = _app(tmp_path)                                    # empty buffer
    app2.state.latest = {"record": _edge(1), "lap_id": 0}
    _tc(app2).post("/mapcapture/cancel", json={"discard": True})
    assert not list(tmp_path.joinpath(*PENDING).glob("*.npz"))
