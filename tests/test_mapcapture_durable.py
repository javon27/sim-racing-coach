# tests/test_mapcapture_durable.py
# #150: a mapping lap reaches disk AS IT COMPLETES (captures/pending/<slug>/), so a
# crash / restart / app close between driving and Save loses nothing, no lap is
# silently dropped by a buffer cap, and Save never writes a map without its cloud.
import json
import os

import numpy as np
import pytest
from fastapi.testclient import TestClient

from simcoach.coach import Coach
from simcoach.lmu import smm
from simcoach.model.fake import FakeModel
from simcoach.model_types import LapRecord
from simcoach.server import app as app_mod
from simcoach.server import mapcapture as mapcapture_mod
from simcoach.server.app import _capture_loop, create_app
from simcoach.server.trackfile import map_rev
from simcoach.store import LapStore
from tests.conftest import with_map_rev


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
    monkeypatch.setattr(mapcapture_mod, "_save_cloud", boom)
    with pytest.raises(OSError):
        c.post("/mapcapture/save", json={})
    assert not tmp_path.joinpath("tracks", "maptrack.json").exists()


def test_capture_built_ribbon_whose_cloud_is_gone_is_refused(tmp_path):
    app, _ = _app(tmp_path)
    app.state.last_ribbon = {"world": {"left": [], "right": [], "center": [], "width": [],
                                       "bank": [], "dist": []},
                             "corners": [], "sectors": [], "track": "MapTrack",
                             "laps": None, "built_from": "capture",
                             "rev": map_rev({})}   # no track file: a fresh Build's stamp
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


def test_discard_covers_every_track_in_the_buffer(tmp_path):
    # The sim changed track mid-session: both tracks' pending laps go.
    app, store = _app(tmp_path)
    other = _edge(1)
    other.track = "OtherTrack"
    _drive(app, store, 1)
    # A track change now ENDS mapping (#154); a two-track buffer is still handled, so
    # build one by re-pinning the session, as a fresh start on the other track would.
    app.state.mapping, app.state.mapping_key = True, None
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
    _capture_loop(_Src([_on("OtherTrack", 1), _on("OtherTrack", -1)]), store, app.state)
    # A track change ENDS mapping (#154); re-pin to build the mixed buffer regardless.
    app.state.mapping, app.state.mapping_key = True, None
    _capture_loop(_Src([_on("MapTrack", 1), _on("MapTrack", -1)]), store, app.state)
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


def _live_laps(store, n=4):
    # Driven laps of MapTrack (not a capture): what "Rebuild from saved capture" falls
    # back to when there is no captures/<slug>.npz.
    for i in range(n):
        store.save(_edge(1 if i % 2 else -1))


def test_discard_with_no_track_still_ends_mapping(tmp_path):
    # #154: the sidecar had just restarted, so no track resolved and the discard 409'd
    # BEFORE `mapping = False` -- every lap for 40 min was then stamped a mapping lap.
    app, store = _app(tmp_path)
    _drive(app, store, 2)
    app2, _ = _app(tmp_path)                                    # no buffer, no live track
    c = _tc(app2)
    assert c.post("/mapcapture/start", json={}).status_code == 200
    r = c.post("/mapcapture/cancel", json={"discard": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["active"] is False and body["discarded"] == 0
    assert "no track to discard" in body["note"]
    assert app2.state.mapping is False
    assert app2.state.map_buffer == [] and app2.state.last_ribbon is None
    assert len(_pending(tmp_path)) == 2                         # nothing discarded


def test_save_of_a_rebuild_ends_mapping(tmp_path):
    # #154: a rebuild's ribbon has laps=None, and Save only left mapping mode on the
    # capture path -- so start -> rebuild -> save left mapping ON.
    app, store = _app(tmp_path)
    _live_laps(store)
    c = _tc(app)
    assert c.post("/mapcapture/start", json={}).status_code == 200
    assert c.post("/mapcapture/rebuild", json={"slug": "maptrack"}).status_code == 200
    assert c.post("/mapcapture/save", json={}).status_code == 200
    assert app.state.mapping is False


def test_rig_sequence_start_failed_discard_rebuild_save_ends_mapping(tmp_path):
    # The exact server.log sequence from #154: start -> discard (no track) -> rebuild ->
    # save, and the dialog closed with no further cancel. Mapping must be OFF.
    app, store = _app(tmp_path)
    _live_laps(store)
    c = _tc(app)
    assert c.post("/mapcapture/start", json={}).status_code == 200
    assert c.post("/mapcapture/cancel", json={"discard": True}).status_code == 200
    assert c.post("/mapcapture/rebuild", json={"slug": "maptrack"}).status_code == 200
    assert c.post("/mapcapture/save", json={}).status_code == 200
    assert app.state.mapping is False


def test_sidecar_restart_comes_up_with_mapping_off(tmp_path):
    app, _ = _app(tmp_path)
    assert app.state.mapping is False


def test_an_untimed_whole_lap_is_persisted_but_never_buffered(tmp_path):
    # #181: a whole lap LMU invalidated posts no time; its off must not reach the ribbon.
    app, store = _app(tmp_path)
    lap = _edge(1)
    lap.lap_time, lap.valid = None, False
    app.state.mapping = True
    _capture_loop(_Src([lap, _edge(-1)]), store, app.state)
    assert len(app.state.map_buffer) == 1 and app.state.map_buffer[0].lap_time == 90.0
    assert len(_pending(tmp_path)) == 1
    assert store.conn.execute("SELECT COUNT(*) FROM laps WHERE mapping=1").fetchone()[0] == 2


# --- #178: "Save ribbon" refuses a map changed on disk since Build -----------------------
# Build/Rebuild snapshot the file's sectors and sections into the staged ribbon, so a map
# save landing before Save used to be silently reverted. The ribbon carries the rev of the
# file it was built from (server-side), and Save 409s on a stale one, writing nothing.

_SECTIONS = [{"name": "Back straight", "kind": "straight", "start_dist": 10.0, "end_dist": 40.0}]


def _map_file(tmp_path):
    return tmp_path.joinpath("tracks", "maptrack.json")


def _mapped(tmp_path):
    # MapTrack mapped once (Build + Save), with laps buffered again for the next Build.
    app, _store = _app(tmp_path)
    c = _tc(app)
    app.state.map_buffer = [_edge(1), _edge(-1)]
    assert c.post("/mapcapture/build", json={}).status_code == 200
    assert c.post("/mapcapture/save", json={}).status_code == 200
    app.state.map_buffer = [_edge(1), _edge(-1)]
    return app, c


def _put_sections(c):
    # What a second window (or the Tracks editor) does between Build and Save.
    r = c.put("/tracks/maptrack/map", json=with_map_rev(c, "/tracks/maptrack/map", {
        "corners": [{"number": 1, "name": "T1", "apex_dist": 50.0}], "sections": _SECTIONS}))
    assert r.status_code == 200, r.text


def _assert_refused(tmp_path, c, before):
    r = c.post("/mapcapture/save", json={})
    assert r.status_code == 409, r.text
    assert "changed on disk" in r.json()["detail"]
    assert _map_file(tmp_path).read_bytes() == before


def test_save_ribbon_refuses_a_map_saved_since_build(tmp_path):
    app, c = _mapped(tmp_path)
    app.state.map_buffer = []
    _drive(app, app.state.store, 2)              # real pending laps on disk (#150)
    assert c.post("/mapcapture/build", json={}).status_code == 200
    _put_sections(c)
    before = _map_file(tmp_path).read_bytes()
    saved = tmp_path.joinpath(*SAVED)
    os.utime(saved, (1_000_000_000, 1_000_000_000))   # so a rewrite is visible in mtime
    cloud = saved.read_bytes()
    _assert_refused(tmp_path, c, before)
    # Refused before ANY write: the saved cloud untouched, nothing promoted.
    assert saved.read_bytes() == cloud and saved.stat().st_mtime == 1_000_000_000
    assert len(_pending(tmp_path)) == 2
    assert len(app.state.map_buffer) == 2        # Build again has its laps
    # The way out the 409 names: Build again picks up the sections, and that Save lands.
    assert c.post("/mapcapture/build", json={}).status_code == 200
    assert c.post("/mapcapture/save", json={}).status_code == 200
    assert json.loads(_map_file(tmp_path).read_text())["sections"] == _SECTIONS


def test_save_ribbon_saves_when_only_notes_changed_since_build(tmp_path):
    # No map write between Build and Save -> 200. A notes save is not a map conflict (the
    # writer carries notes itself), so one landing in between must not 409 either.
    _app, c = _mapped(tmp_path)
    assert c.post("/mapcapture/build", json={}).status_code == 200
    got = c.get("/tracks/maptrack/notes").json()
    r = c.put("/tracks/maptrack/notes", json={"track": "MapTrack", "rev": got["rev"],
                                              "notes": {"overview": "flat out"}})
    assert r.status_code == 200, r.text
    assert c.post("/mapcapture/save", json={}).status_code == 200
    assert json.loads(_map_file(tmp_path).read_text())["notes"]["overview"] == "flat out"


@pytest.mark.parametrize("rev", [None, "", "garbage", 123])
def test_save_ribbon_refuses_an_unstamped_or_garbage_rev(tmp_path, rev):
    # Build/Rebuild always stamp the rev server-side, so no client can omit it: a ribbon
    # without a matching one can only be a staging path that forgot, and fails closed.
    app, c = _mapped(tmp_path)
    assert c.post("/mapcapture/build", json={}).status_code == 200
    if rev is None:
        del app.state.last_ribbon["rev"]
    else:
        app.state.last_ribbon["rev"] = rev
    _assert_refused(tmp_path, c, _map_file(tmp_path).read_bytes())
    assert len(app.state.map_buffer) == 2


def test_rebuild_from_saved_capture_is_guarded_too(tmp_path):
    # /mapcapture/rebuild snapshots sectors/sections the same way Build does.
    _app, c = _mapped(tmp_path)
    assert c.post("/mapcapture/rebuild", json={"slug": "maptrack"}).status_code == 200
    _put_sections(c)
    _assert_refused(tmp_path, c, _map_file(tmp_path).read_bytes())
    assert c.post("/mapcapture/rebuild", json={"slug": "maptrack"}).status_code == 200
    assert c.post("/mapcapture/save", json={}).status_code == 200
    assert json.loads(_map_file(tmp_path).read_text())["sections"] == _SECTIONS
