import json

import numpy as np
from fastapi.testclient import TestClient
from simcoach.server.app import create_app
from simcoach.store import LapStore
from simcoach.coach import Coach
from simcoach.model.fake import FakeModel
from simcoach.corners import CornerMap
from simcoach.model_types import LapRecord


def _app(tmp_path, synthetic_lap, cmap=None):
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    synthetic_lap.session_id = "s1"
    lap_id = store.save(synthetic_lap)
    cid = store.create_chat()
    store.save_chat(cid, synthetic_lap.session_id, synthetic_lap.track,
                    synthetic_lap.car, lap_id, "q", "a")
    coach = Coach(FakeModel(), "simcoach/knowledge")
    return create_app(store, coach, cmap=cmap), lap_id, cid


def _tc(app):
    return TestClient(app, base_url="http://127.0.0.1")


def test_chats_and_messages(tmp_path, synthetic_lap):
    app, lap_id, cid = _app(tmp_path, synthetic_lap)
    c = _tc(app)
    chats = c.get("/chats").json()
    assert any(x["chat_id"] == cid for x in chats)
    msgs = c.get(f"/chats/{cid}").json()
    assert msgs[0]["question"] == "q"


def test_session_chats_endpoint(tmp_path, synthetic_lap):
    app, lap_id, cid = _app(tmp_path, synthetic_lap)
    c = _tc(app)
    # _app saved a chat stamped session_id="s1".
    rows = c.get("/sessions/s1/chats").json()
    assert [r["chat_id"] for r in rows] == [cid]
    assert c.get("/sessions/other/chats").json() == []


def test_session_race_endpoint(tmp_path, synthetic_lap):
    from simcoach.lmu.results import ResultLap, ResultSession
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    rs = ResultSession(
        sim="lmu", track="Sebring", car="GT3", car_class="GT3", date_ts=1, session_type="race",
        position=3, class_position=2, best_lap=90.0, finish_status="Finished",
        laps=[ResultLap(lap_no=1, lap_time=90.0, sectors=[], valid=True)],
        incidents=["Off track T7"], penalties=[],
    )
    store.save_result_session(rs, session_id="race-1")
    coach = Coach(FakeModel(), "simcoach/knowledge")
    c = _tc(create_app(store, coach))
    body = c.get("/sessions/race-1/race").json()
    assert body == {"position": 3, "class_position": 2, "finish_status": "Finished",
                    "incidents": ["Off track T7"], "penalties": []}
    # No race row -> empty object per contract.
    assert c.get("/sessions/absent/race").json() == {}


def test_setup_empty_without_rest(tmp_path, synthetic_lap):
    app, *_ = _app(tmp_path, synthetic_lap)
    assert _tc(app).get("/setup").json() == {}


def test_sessions_from_laps_table(tmp_path, synthetic_lap):
    app, lap_id, cid = _app(tmp_path, synthetic_lap)
    rows = _tc(app).get("/sessions").json()
    assert isinstance(rows, list)
    assert len(rows) == 1
    row = rows[0]
    assert row["session_id"] == "s1"
    assert row["track"] == synthetic_lap.track
    assert row["car"] == synthetic_lap.car
    assert row["laps"] == 1
    assert row["best"] == synthetic_lap.lap_time


def test_session_laps(tmp_path, synthetic_lap):
    app, lap_id, cid = _app(tmp_path, synthetic_lap)
    rows = _tc(app).get("/sessions/s1/laps").json()
    assert rows == [{"id": lap_id, "lap_time": synthetic_lap.lap_time,
                     "valid": 1, "ts": synthetic_lap.timestamp,
                     "s1": None, "s2": None, "s3": None, "phase": None}]


def test_analysis_404_without_cmap(tmp_path, synthetic_lap):
    app, lap_id, cid = _app(tmp_path, synthetic_lap)
    r = _tc(app).get(f"/laps/{lap_id}/analysis")
    assert r.status_code == 404


def test_analysis_happy_path(tmp_path, synthetic_lap):
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    app, lap_id, cid = _app(tmp_path, synthetic_lap, cmap=cmap)
    r = _tc(app).get(f"/laps/{lap_id}/analysis")
    assert r.status_code == 200
    body = r.json()
    assert body["lap_time"] == synthetic_lap.lap_time
    assert len(body["corners"]) == 2
    assert body["corners"][0]["name"] == "Left Hairpin"
    assert body["corners"][0]["direction"] == "left"


def test_analysis_404_missing_lap(tmp_path, synthetic_lap):
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    app, lap_id, cid = _app(tmp_path, synthetic_lap, cmap=cmap)
    r = _tc(app).get("/laps/99999/analysis")
    assert r.status_code == 404


def _pos_lap():
    x = np.array([0.0, 100.0, 100.0, 0.0, 0.0])
    z = np.array([0.0, 0.0, 100.0, 100.0, 0.0])
    dist = np.array([0.0, 100.0, 200.0, 300.0, 400.0])
    return LapRecord("lmu", "test_track", "test_car", 90.0, True, 0.0,
                     {"pos_x": x, "pos_z": z, "lap_distance": dist})


def test_map_with_pos_and_cmap(tmp_path):
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    lap_id = store.save(_pos_lap())
    coach = Coach(FakeModel(), "simcoach/knowledge")
    body = _tc(create_app(store, coach, cmap=cmap)).get(f"/laps/{lap_id}/map").json()
    assert body["width"] == 1000
    assert body["path"]
    assert len(body["corners"]) == 2
    assert body["corners"][0]["name"] == "Left Hairpin"


def test_map_has_dist_and_corner_windows(tmp_path):
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    lap_id = store.save(_pos_lap())
    coach = Coach(FakeModel(), "simcoach/knowledge")
    body = _tc(create_app(store, coach, cmap=cmap)).get(f"/laps/{lap_id}/map").json()
    assert len(body["dist"]) == len(body["path"])
    assert "apex_dist" in body["corners"][0]
    assert "start_dist" in body["corners"][0] and "end_dist" in body["corners"][0]
    assert body["sectors"] == []  # no shipped file for test_track slug in tmp dir


def _curve_lap(track, car, lap_time, valid=True, dx=0.0):
    # An L-shaped line: +x from dist 0..200, then a LEFT turn to +z 200..400.
    x = np.array([0.0, 100.0, 200.0, 200.0, 200.0]) + dx
    z = np.array([0.0, 0.0, 0.0, 100.0, 200.0])
    dist = np.array([0.0, 100.0, 200.0, 300.0, 400.0])
    return LapRecord("lmu", track, car, lap_time, valid, 0.0,
                     {"pos_x": x, "pos_z": z, "lap_distance": dist})


def test_map_avg_averages_clean_laps(tmp_path):
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    a = store.save(_curve_lap("Curveland", "GT3", 90.0, dx=0.0))
    store.save(_curve_lap("Curveland", "GT3", 90.5, dx=10.0))
    store.save(_curve_lap("Curveland", "GT3", 200.0, dx=999.0))  # off lap -> excluded
    coach = Coach(FakeModel(), "simcoach/knowledge")
    app = create_app(store, coach, tracks_dir=str(tmp_path / "tracks"))
    body = _tc(app).get(f"/laps/{a}/map?avg=1").json()
    assert body["path"]  # averaged outline built
    assert len(body["dist"]) == len(body["path"])


def test_map_is_canonical_for_lap_without_position(tmp_path):
    # THE core bug: a lap with no pos_x/pos_z still returns the track's canonical
    # map, built from OTHER position-captured laps of the same track+car.
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    store.save(_curve_lap("Curveland", "GT3", 90.0))          # pos lap (builds the map)
    no_pos = store.save(LapRecord("lmu", "Curveland", "GT3", 90.2, True, 0.0,
                                  {"lap_distance": np.arange(5.0)}))  # selected, no pos
    coach = Coach(FakeModel(), "simcoach/knowledge")
    app = create_app(store, coach, tracks_dir=str(tmp_path / "tracks"))
    body = _tc(app).get(f"/laps/{no_pos}/map").json()
    assert body.get("path")  # canonical outline present despite the lap having no pos


def test_map_prefers_frozen_path_over_reaverage(tmp_path):
    # A frozen path/path_dist in the track file is authoritative; the default GET
    # uses it verbatim rather than re-averaging the driven laps.
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    lap_id = store.save(_curve_lap("Curveland", "GT3", 90.0))
    coach = Coach(FakeModel(), "simcoach/knowledge")
    tracks = tmp_path / "tracks"
    tracks.mkdir()
    # Frozen outline with a distinctive 5-point path_dist; re-averaging would
    # produce a 500-bin (downsampled to ~300) dist array of a different length.
    (tracks / "curveland.json").write_text(json.dumps({
        "track": "curveland", "corners": [],
        "path": [[0, 0], [1, 0], [2, 0], [2, 1], [2, 2]],
        "path_dist": [0, 100, 200, 300, 400],
    }))
    app = create_app(store, coach, tracks_dir=str(tracks))
    default = _tc(app).get(f"/laps/{lap_id}/map").json()
    forced = _tc(app).get(f"/laps/{lap_id}/map?avg=1").json()
    assert default["dist"] == [0.0, 100.0, 200.0, 300.0, 400.0]  # frozen used
    assert len(forced["dist"]) != len(default["dist"])           # avg=1 re-averaged


def test_map_cache_invalidated_after_put(tmp_path):
    app, lap_id, tracks = _curve_app(tmp_path)
    c = _tc(app)
    c.get(f"/laps/{lap_id}/map")  # populate the canonical cache
    c.put(f"/laps/{lap_id}/map", json={"corners": [{"number": 1, "name": "Fresh", "apex_dist": 200.0}]})
    after = c.get(f"/laps/{lap_id}/map").json()  # must reflect the save, not the cache
    assert [x["name"] for x in after["corners"]] == ["Fresh"]


def _curve_app(tmp_path):
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    lap_id = store.save(_curve_lap("Curveland", "GT3", 90.0, dx=0.0))
    store.save(_curve_lap("Curveland", "GT3", 90.4, dx=8.0))
    coach = Coach(FakeModel(), "simcoach/knowledge")
    tracks = tmp_path / "tracks"
    app = create_app(store, coach, tracks_dir=str(tracks))
    return app, lap_id, tracks


def test_put_map_saves_and_direction_is_server_derived(tmp_path):
    app, lap_id, tracks = _curve_app(tmp_path)
    c = _tc(app)
    # Client sends a WRONG direction; the server must ignore it and derive "left"
    # from the averaged path geometry (the corner turns +x -> +z at dist 200).
    r = c.put(f"/laps/{lap_id}/map", json={
        "corners": [{"number": 1, "name": "The Bend", "apex_dist": 200.0,
                     "direction": "right"}],
        "sectors": [150.0],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["corners"][0]["direction"] == "left"
    assert body["sectors"] == [150.0]
    # File written and reloadable by CornerMap.load (back-compat shape).
    saved = json.loads((tracks / "curveland.json").read_text())
    assert saved["corners"][0]["direction"] == "left"
    from simcoach.corners import CornerMap as CM
    cm = CM.load(str(tracks / "curveland.json"))
    assert cm.corners[0].name == "The Bend"
    # GET avg reflects the saved corner.
    got = c.get(f"/laps/{lap_id}/map?avg=1").json()
    assert got["corners"][0]["name"] == "The Bend"


def test_put_map_persists_label_apexes_sections(tmp_path):
    app, lap_id, tracks = _curve_app(tmp_path)
    c = _tc(app)
    r = c.put(f"/laps/{lap_id}/map", json={
        "corners": [{"number": 1, "label": "8A", "name": "Bend", "apex_dist": 200.0,
                     "apexes": [190.0, 210.0], "start_dist": 150.0, "end_dist": 250.0}],
        "sectors": [150.0],
        "sections": [{"name": "The Esses", "kind": "complex", "start_dist": 100.0, "end_dist": 300.0}],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["corners"][0]["label"] == "8A"
    assert body["corners"][0]["apexes"] == [190.0, 210.0]
    assert body["sections"][0]["name"] == "The Esses"
    saved = json.loads((tracks / "curveland.json").read_text())
    assert saved["corners"][0]["label"] == "8A" and saved["sections"][0]["kind"] == "complex"
    # GET serves the persisted label/apexes/sections.
    got = c.get(f"/laps/{lap_id}/map?avg=1").json()
    assert got["corners"][0]["label"] == "8A" and got["sections"][0]["name"] == "The Esses"


def test_put_map_bad_section_span_is_422(tmp_path):
    app, lap_id, _ = _curve_app(tmp_path)
    r = _tc(app).put(f"/laps/{lap_id}/map", json={
        "corners": [{"number": 1, "apex_dist": 200.0}],
        "sections": [{"name": "x", "start_dist": 300.0, "end_dist": 100.0}],
    })
    assert r.status_code == 422


def test_put_map_invalid_window_is_422(tmp_path):
    app, lap_id, tracks = _curve_app(tmp_path)
    r = _tc(app).put(f"/laps/{lap_id}/map", json={
        "corners": [{"number": 1, "apex_dist": 200.0, "start_dist": 250.0, "end_dist": 260.0}],
    })
    assert r.status_code == 422
    assert not (tracks / "curveland.json").exists()  # nothing written on validation failure


def test_put_map_bad_sectors_is_422(tmp_path):
    app, lap_id, _ = _curve_app(tmp_path)
    r = _tc(app).put(f"/laps/{lap_id}/map", json={
        "corners": [{"number": 1, "apex_dist": 200.0}],
        "sectors": [300.0, 100.0],  # not increasing
    })
    assert r.status_code == 422


def test_put_map_too_many_corners_is_422(tmp_path):
    app, lap_id, _ = _curve_app(tmp_path)
    corners = [{"number": i, "apex_dist": 100.0 + i} for i in range(101)]
    r = _tc(app).put(f"/laps/{lap_id}/map", json={"corners": corners})
    assert r.status_code == 422


def test_put_map_caps_corner_name_length(tmp_path):
    app, lap_id, tracks = _curve_app(tmp_path)
    r = _tc(app).put(f"/laps/{lap_id}/map", json={
        "corners": [{"number": 1, "name": "X" * 500, "apex_dist": 200.0}],
    })
    assert r.status_code == 200
    saved = json.loads((tracks / "curveland.json").read_text())
    assert len(saved["corners"][0]["name"]) == 80


def test_put_map_unknown_lap_is_404(tmp_path):
    app, *_ = _curve_app(tmp_path)
    r = _tc(app).put("/laps/99999/map", json={"corners": [{"number": 1, "apex_dist": 200.0}]})
    assert r.status_code == 404


def test_put_map_slug_cannot_escape_tracks_dir(tmp_path):
    # A malicious track name is sanitized by track_slug to a safe stem; the file
    # must land inside the tracks dir, never outside it.
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    lap_id = store.save(_curve_lap("../../../evil", "GT3", 90.0))
    store.save(_curve_lap("../../../evil", "GT3", 90.3, dx=5.0))
    coach = Coach(FakeModel(), "simcoach/knowledge")
    tracks = tmp_path / "tracks"
    app = create_app(store, coach, tracks_dir=str(tracks))
    r = _tc(app).put(f"/laps/{lap_id}/map", json={"corners": [{"number": 1, "apex_dist": 200.0}]})
    assert r.status_code == 200
    written = list(tracks.glob("*.json"))
    assert written and all(p.parent == tracks for p in written)
    assert not (tmp_path / "evil.json").exists()  # did not escape


def test_put_map_no_clean_laps_is_422(tmp_path):
    # A lap with no position telemetry cannot yield an averaged outline.
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    lap_id = store.save(LapRecord("lmu", "Bare", "GT3", 90.0, True, 0.0,
                                  {"lap_distance": np.arange(5.0)}))
    coach = Coach(FakeModel(), "simcoach/knowledge")
    app = create_app(store, coach, tracks_dir=str(tmp_path / "tracks"))
    r = _tc(app).put(f"/laps/{lap_id}/map", json={"corners": [{"number": 1, "apex_dist": 2.0}]})
    assert r.status_code == 422


def test_map_without_pos_channels_is_empty(tmp_path, synthetic_lap):
    # synthetic_lap has no pos_x/pos_z -> {} per contract.
    app, lap_id, cid = _app(tmp_path, synthetic_lap)
    assert _tc(app).get(f"/laps/{lap_id}/map").json() == {}


def test_map_unknown_lap_is_404(tmp_path, synthetic_lap):
    app, *_ = _app(tmp_path, synthetic_lap)
    assert _tc(app).get("/laps/99999/map").status_code == 404


def test_setup_survives_rest_failure(tmp_path, synthetic_lap):
    class BrokenRest:
        def get_setup(self):
            raise RuntimeError("LMU closed")

    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    coach = Coach(FakeModel(), "simcoach/knowledge")
    app = create_app(store, coach, rest=BrokenRest())
    r = _tc(app).get("/setup")
    assert r.status_code == 200
    assert r.json() == {}


def test_setup_lazily_connects_via_factory(tmp_path, synthetic_lap):
    class FakeRest:
        def get_setup(self):
            return {"wing": 5}

    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    coach = Coach(FakeModel(), "simcoach/knowledge")
    app = create_app(store, coach, rest=None, rest_factory=FakeRest)
    c = _tc(app)
    # No boot rest, but the factory connects on first /setup call.
    assert c.get("/setup").json() == {"wing": 5}
    # …and /status now reports the live connection.
    assert c.get("/status").json()["lmu_connected"] is True


def test_analysis_resolves_map_per_track(tmp_path, synthetic_lap):
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    track_a = synthetic_lap.track
    # Resolver returns a map only for track A; anything else -> None.
    resolver = lambda track: cmap if track == track_a else None
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    lap_id = store.save(synthetic_lap)
    coach = Coach(FakeModel(), "simcoach/knowledge")
    app = create_app(store, coach, map_resolver=resolver)
    c = _tc(app)
    assert c.get(f"/laps/{lap_id}/analysis").status_code == 200

    synthetic_lap.track = "some_other_track"
    other_id = store.save(synthetic_lap)
    assert c.get(f"/laps/{other_id}/analysis").status_code == 404


def test_status_has_map_reflects_canonical(tmp_path):
    # has_map = a canonical outline is available for the live track (a pos lap
    # exists to average, or a frozen file) -- not merely a loaded CornerMap.
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    pos = store.save(_curve_lap("Curveland", "GT3", 90.0))
    coach = Coach(FakeModel(), "simcoach/knowledge")
    app = create_app(store, coach, tracks_dir=str(tmp_path / "tracks"))
    app.state.latest = {"record": store.load(pos), "lap_id": pos}
    assert _tc(app).get("/status").json()["has_map"] is True
    # A track with only a no-position lap -> no canonical outline -> has_map False.
    bare = store.save(LapRecord("lmu", "Bare", "GT3", 90.0, True, 0.0,
                                {"lap_distance": np.arange(5.0)}))
    app.state.latest = {"record": store.load(bare), "lap_id": bare}
    assert _tc(app).get("/status").json()["has_map"] is False


def _map_app(tmp_path):
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    coach = Coach(FakeModel(), "simcoach/knowledge")
    app = create_app(store, coach, tracks_dir=str(tmp_path / "tracks"))
    return app, store


def _edge(track, zsign, dist_max):
    x = np.array([0.0, 50.0, 100.0])
    z = np.array([zsign * 5.0] * 3)
    return LapRecord("lmu", track, "GT3", 90.0, True, 0.0,
                     {"pos_x": x, "pos_z": z, "lap_distance": np.array([0.0, dist_max / 2, dist_max])})


def test_mapcapture_start_buffers_laps_never_persists(tmp_path):
    from simcoach.server.app import _capture_loop
    app, store = _map_app(tmp_path)
    c = _tc(app)
    # JSON body required (CSRF guard) -- a bodyless POST is rejected.
    assert c.post("/mapcapture/start").status_code == 422
    assert c.post("/mapcapture/start", json={}).json() == {"active": True}
    assert app.state.mapping is True

    laps = [_edge("MapTrack", 1, 100.0), _edge("MapTrack", -1, 100.0)]

    class Src:
        def laps(self):
            return iter(laps)

    _capture_loop(Src(), store, app.state)  # the daemon body, mapping ON
    assert store.list_lap_ids("MapTrack", "GT3") == []   # NEVER persisted
    assert len(app.state.map_buffer) == 2

    st = c.get("/mapcapture/status").json()
    assert st["active"] is True
    assert [l["idx"] for l in st["laps"]] == [0, 1]
    assert st["laps"][0]["samples"] == 3 and st["laps"][0]["length"] == 100.0

    assert c.post("/mapcapture/cancel", json={}).json() == {"active": False}
    assert app.state.mapping is False and app.state.map_buffer == []


def test_mapcapture_buffer_is_capped(tmp_path):
    from simcoach.server.app import _MAP_BUFFER_MAX, _capture_loop
    app, store = _map_app(tmp_path)
    app.state.mapping = True
    laps = [_edge("MapTrack", 1, 100.0) for _ in range(_MAP_BUFFER_MAX + 5)]

    class Src:
        def laps(self):
            return iter(laps)

    _capture_loop(Src(), store, app.state)
    assert len(app.state.map_buffer) == _MAP_BUFFER_MAX  # bounded, keeps last N


def test_mapcapture_build_rejects_negative_and_equal_indices(tmp_path):
    app, _ = _map_app(tmp_path)
    app.state.map_buffer = [_edge("MapTrack", 1, 100.0), _edge("MapTrack", -1, 100.0)]
    c = _tc(app)
    assert c.post("/mapcapture/build", json={"left": -1, "right": 0}).status_code == 422
    assert c.post("/mapcapture/build", json={"left": 1, "right": 1}).status_code == 422


def test_mapcapture_build_returns_ribbon_shape(tmp_path):
    app, _ = _map_app(tmp_path)
    app.state.map_buffer = [_edge("MapTrack", 1, 100.0), _edge("MapTrack", -1, 100.0)]
    body = _tc(app).post("/mapcapture/build", json={"left": 0, "right": 1}).json()
    for k in ("width", "height", "left", "right", "center", "corners", "sectors"):
        assert k in body
    assert body["left"] and len(body["center"]) == len(body["left"])


def test_mapcapture_build_bad_index_is_422(tmp_path):
    app, _ = _map_app(tmp_path)
    app.state.map_buffer = [_edge("MapTrack", 1, 100.0)]
    assert _tc(app).post("/mapcapture/build", json={"left": 0, "right": 9}).status_code == 422


def test_mapcapture_save_writes_track_json(tmp_path):
    app, _ = _map_app(tmp_path)
    app.state.latest = {"record": _edge("MapTrack", 1, 100.0), "lap_id": 0}  # live track
    app.state.map_buffer = [_edge("MapTrack", 1, 100.0), _edge("MapTrack", -1, 100.0)]
    c = _tc(app)
    c.post("/mapcapture/build", json={"left": 0, "right": 1})
    # Sim changed track between build and save -> save must use the BUILT track.
    app.state.latest = {"record": _edge("OtherTrack", 1, 100.0), "lap_id": 1}
    r = c.post("/mapcapture/save", json={}).json()
    assert r == {"saved": True, "slug": "maptrack"}
    saved = json.loads((tmp_path / "tracks" / "maptrack.json").read_text())
    assert "ribbon" in saved and "path" in saved and "path_dist" in saved
    # The saved file still loads as a corner map (back-compat with CornerMap.load).
    CornerMap.load(str(tmp_path / "tracks" / "maptrack.json"))


def test_mapcapture_save_without_build_is_422(tmp_path):
    app, _ = _map_app(tmp_path)
    app.state.latest = {"record": _edge("MapTrack", 1, 100.0), "lap_id": 0}
    assert _tc(app).post("/mapcapture/save", json={}).status_code == 422


def test_rotation_persists_and_reflected(tmp_path):
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    lid = store.save(_curve_lap("Rot", "GT3", 90.0))
    store.save(_curve_lap("Rot", "GT3", 90.3, dx=8.0))
    coach = Coach(FakeModel(), "simcoach/knowledge")
    tracks = tmp_path / "tracks"
    c = _tc(create_app(store, coach, tracks_dir=str(tracks)))
    assert c.get(f"/laps/{lid}/map").json()["rotation"] == 0.0
    # 450 normalizes to 90; response is the refreshed rotated map.
    assert c.post(f"/laps/{lid}/rotation", json={"degrees": 450}).json()["rotation"] == 90.0
    assert json.loads((tracks / "rot.json").read_text())["rotation"] == 90.0
    assert c.get(f"/laps/{lid}/map").json()["rotation"] == 90.0  # cache invalidated


def test_rotation_csrf_and_validation(tmp_path):
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    lid = store.save(_curve_lap("Rot", "GT3", 90.0))
    coach = Coach(FakeModel(), "simcoach/knowledge")
    c = _tc(create_app(store, coach, tracks_dir=str(tmp_path / "tracks")))
    assert c.post(f"/laps/{lid}/rotation").status_code == 422             # no body (CSRF)
    assert c.post(f"/laps/{lid}/rotation", json={"degrees": "x"}).status_code == 422
    assert c.post("/laps/99999/rotation", json={"degrees": 10}).status_code == 404


def test_analysis_unanalyzable_is_422(tmp_path, synthetic_lap, monkeypatch):
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    app, lap_id, cid = _app(tmp_path, synthetic_lap, cmap=cmap)

    from simcoach.server import app as app_module

    def _boom(*args, **kwargs):
        raise ValueError("bad telemetry")

    monkeypatch.setattr(app_module, "analyze_lap", _boom)
    r = _tc(app).get(f"/laps/{lap_id}/analysis")
    assert r.status_code == 422
