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


def _pending(tmp_path):
    return sorted(tmp_path.joinpath(*PENDING).glob("*.npz"))


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
    assert not _pending(tmp_path)                     # promoted, not duplicated
    assert app2.state.mapping is False and app2.state.map_buffer == []


def test_pending_capture_never_outvotes_live_laps_on_rebuild(tmp_path):
    # F2: one abandoned mapping lap must not replace 10 live laps as the rebuild source
    # (and the /tracks count), which a winning pending capture did.
    app, store = _app(tmp_path)
    _capture_loop(_Src([_edge(1 if i % 2 else -1) for i in range(10)]), store, app.state)
    c = _tc(app)

    def row():
        return next(t for t in c.get("/tracks").json() if t["slug"] == "maptrack")
    before = row()["captures"]
    app.state.latest = {"record": _edge(1), "lap_id": 0}
    c.post("/mapcapture/start", json={})
    _drive(app, store, 1)
    c.post("/mapcapture/cancel", json={})
    assert len(_pending(tmp_path)) == 1                        # precondition
    assert row()["captures"] == before + 1                     # +1 live lap, not "1"
    r = c.post("/mapcapture/rebuild", json={"slug": "maptrack"})
    assert r.status_code == 200, r.text
    assert r.json()["lap_count"] == 11
    assert app.state.last_ribbon["built_from"] == "live_laps"


def test_pending_capture_does_not_bypass_the_curated_map_guard(tmp_path):
    # F3: a pending capture counted as from_capture and so skipped the curated-map
    # refusal -- Save then replaced a curated map's corners with "Turn N".
    import json
    tracks = tmp_path / "tracks"
    tracks.mkdir()
    curated = {"track": "maptrack", "name": "MapTrack",
               "corners": [{"number": 1, "label": "La Source", "name": "La Source",
                            "direction": "right", "start_dist": 10, "apex_dist": 20,
                            "end_dist": 30}]}
    (tracks / "maptrack.json").write_text(json.dumps(curated))
    app, store = _app(tmp_path)
    _drive(app, store, 2)
    assert len(_pending(tmp_path)) == 2                        # precondition
    c = _tc(app)
    assert c.post("/mapcapture/rebuild", json={"slug": "maptrack"}).status_code == 422
    names = [x["name"] for x in json.loads((tracks / "maptrack.json").read_text())["corners"]]
    assert names == ["La Source"]


def test_save_keeps_laps_driven_after_build(tmp_path):
    # F1: Save used to rmtree ALL pending and clear the buffer, deleting durable laps
    # driven while the preview was up.
    app, store = _app(tmp_path)
    _drive(app, store, 3)
    c = _tc(app)
    assert c.post("/mapcapture/build", json={}).status_code == 200
    _drive(app, store, 1)                                      # 4th lap, after Build
    assert len(_pending(tmp_path)) == 4
    assert c.post("/mapcapture/save", json={}).status_code == 200
    with np.load(str(tmp_path.joinpath(*SAVED))) as z:
        assert len({k.split("_", 1)[0] for k in z.files}) == 3
    assert [f.name for f in _pending(tmp_path)] == ["00003.npz"]  # the 4th stays pending
    assert len(app.state.map_buffer) == 1                      # and buffered
    assert app.state.mapping is False


def test_next_index_never_overwrites_after_a_gap(tmp_path):
    # M1: index = file count overwrote the newest lap once a lower one was gone.
    from simcoach.server.app import _append_pending_lap
    data = tmp_path / "data"
    for z in (1, 2, 3):
        _append_pending_lap(data, _edge(z))
    _pending(tmp_path)[0].unlink()
    _append_pending_lap(data, _edge(9))
    files = _pending(tmp_path)
    assert [f.name for f in files] == ["00001.npz", "00002.npz", "00003.npz"]
    with np.load(str(files[1])) as z:
        assert z["lap0_pos_z"][0] == 3 * 5.0                  # lap 3 survived


def test_a_failed_delete_never_resurrects_a_promoted_lap(tmp_path, monkeypatch):
    # M2: a promoted lap whose delete fails (Windows AV / indexer lock) must not come
    # back as a pending lap in the next session.
    from pathlib import Path
    app, store = _app(tmp_path)
    app.state.latest = {"record": _edge(1), "lap_id": 0}
    _drive(app, store, 2)
    c = _tc(app)
    c.post("/mapcapture/build", json={})

    def locked(self, *a, **k):
        raise PermissionError(13, "Access is denied", str(self))
    with monkeypatch.context() as m:
        m.setattr(Path, "unlink", locked)
        assert c.post("/mapcapture/save", json={}).status_code == 200
    assert len(list(tmp_path.joinpath(*PENDING).glob("*.dropped"))) == 2  # delete DID fail
    assert not _pending(tmp_path)
    c.post("/mapcapture/start", json={})
    assert app.state.map_buffer == []


def test_save_cloud_after_restart_promotes_pending(tmp_path):
    app, store = _app(tmp_path)
    _drive(app, store, 2)
    app2, _ = _app(tmp_path)
    app2.state.latest = {"record": _edge(1), "lap_id": 0}
    c = _tc(app2)
    c.post("/mapcapture/start", json={})
    assert c.post("/mapcapture/save-cloud", json={}).status_code == 200
    assert tmp_path.joinpath(*SAVED).exists()
    assert not _pending(tmp_path)


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
    # The explicit "Discard capture": bad laps must not resurrect on every start.
    app, store = _app(tmp_path)
    app.state.latest = {"record": _edge(1), "lap_id": 0}
    c = _tc(app)
    c.post("/mapcapture/start", json={})
    _drive(app, store, 2)
    assert len(_pending(tmp_path)) == 2                        # precondition
    r = c.post("/mapcapture/cancel", json={"discard": True})
    assert r.json() == {"active": False, "discarded": 2}
    assert not _pending(tmp_path)
    c.post("/mapcapture/start", json={})
    assert c.get("/mapcapture/status").json()["laps"] == []     # nothing resurrected
    c.post("/mapcapture/cancel", json={})
    r = c.post("/mapcapture/rebuild", json={"slug": "maptrack"})
    assert r.status_code == 200, r.text
    assert app.state.last_ribbon["built_from"] == "live_laps"


def test_discard_after_restart_uses_the_live_track(tmp_path):
    app, store = _app(tmp_path)
    _drive(app, store, 2)
    assert len(_pending(tmp_path)) == 2                        # precondition
    app2, _ = _app(tmp_path)                                    # empty buffer
    app2.state.latest = {"record": _edge(1), "lap_id": 0}
    r = _tc(app2).post("/mapcapture/cancel", json={"discard": True})
    assert r.json()["discarded"] == 2
    assert not _pending(tmp_path)


def test_discard_with_no_track_is_an_error_not_a_silent_noop(tmp_path):
    app, store = _app(tmp_path)
    _drive(app, store, 2)
    app2, _ = _app(tmp_path)                                    # no buffer, no live track
    assert _tc(app2).post("/mapcapture/cancel", json={"discard": True}).status_code == 409
    assert len(_pending(tmp_path)) == 2


def test_discard_covers_every_track_in_the_buffer(tmp_path):
    # The sim changed track mid-session: both tracks' pending laps go.
    app, store = _app(tmp_path)
    other = _edge(1)
    other.track = "OtherTrack"
    _drive(app, store, 1)
    app.state.mapping = True
    _capture_loop(_Src([other]), store, app.state)
    other_dir = tmp_path / "data" / "captures" / "pending" / "othertrack"
    assert _pending(tmp_path) and list(other_dir.glob("*.npz"))  # precondition
    r = _tc(app).post("/mapcapture/cancel", json={"discard": True})
    assert r.json()["discarded"] == 2
    assert not _pending(tmp_path) and not list(other_dir.glob("*.npz"))


def _on(track, zsign):
    lap = _edge(zsign)
    return LapRecord("lmu", track, lap.car, lap.lap_time, lap.valid, lap.timestamp,
                     lap.channels)


def test_a_mixed_track_buffer_saves_and_drops_only_the_current_track(tmp_path):
    # Red-team 2 #3: the sim changed track mid-capture. Save-cloud must neither mix
    # OtherTrack's laps into this cloud nor drop them as "promoted".
    app, store = _app(tmp_path)
    app.state.mapping = True
    _capture_loop(_Src([_on("OtherTrack", 1), _on("OtherTrack", -1),
                        _on("MapTrack", 1), _on("MapTrack", -1)]), store, app.state)
    other = tmp_path / "data" / "captures" / "pending" / "othertrack"
    assert len(list(other.glob("*.npz"))) == 2
    assert _tc(app).post("/mapcapture/save-cloud", json={}).status_code == 200
    with np.load(str(tmp_path.joinpath(*SAVED))) as z:
        assert len({k.split("_", 1)[0] for k in z.files}) == 2   # MapTrack's only
    assert len(list(other.glob("*.npz"))) == 2                   # OtherTrack kept


def test_a_second_save_never_drops_a_newer_lap(tmp_path):
    # Red-team 2 #2: Save left last_ribbon["buffered"] set, so a second Save with the
    # same ribbon dropped a lap that landed in the reused index after the first.
    app, store = _app(tmp_path)
    _drive(app, store, 2)
    c = _tc(app)
    assert c.post("/mapcapture/build", json={}).status_code == 200
    assert c.post("/mapcapture/save", json={}).status_code == 200
    _drive(app, store, 1)                                   # lands as the reused 00000
    assert [f.name for f in _pending(tmp_path)] == ["00000.npz"]
    c.post("/mapcapture/save", json={})
    assert [f.name for f in _pending(tmp_path)] == ["00000.npz"]


def test_a_locked_old_tombstone_does_not_resurrect_a_promoted_lap(tmp_path):
    # Red-team 2 #1b: tombstones were <n>.dropped, so an undeletable one from an earlier
    # drop of the same index made the next rename fail and the lap came back.
    from simcoach.server.app import _drop_pending
    d = tmp_path.joinpath(*PENDING)
    d.mkdir(parents=True)
    (d / "00000.dropped").write_bytes(b"locked leftover")
    (d / "00000.npz").write_bytes(b"x")
    with open(d / "00000.dropped", "rb"):          # held open: Windows won't replace it
        assert _drop_pending([d / "00000.npz"]) == 1
    assert _pending(tmp_path) == []
