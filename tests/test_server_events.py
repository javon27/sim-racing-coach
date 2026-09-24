import numpy as np
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from simcoach.coach import Coach
from simcoach.model.fake import FakeModel
from simcoach.model_types import LapRecord
from simcoach.server import live_ws as live_ws_mod
from simcoach.server import maps as maps_mod
from simcoach.server.app import create_app
from simcoach.store import LapStore


def _client(tmp_path):
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    app = create_app(store, Coach(FakeModel(reply="ok")))
    return TestClient(app, base_url="http://127.0.0.1"), store, app


def _fast(monkeypatch):
    monkeypatch.setattr(live_ws_mod, "_EVENTS_PERIOD", 0.01)


def _drain_until(ws, topic, limit=20):
    for _ in range(limit):
        f = ws.receive_json()
        if f["topic"] == topic:
            return f
    raise AssertionError(f"no {topic!r} frame in {limit} reads")


def test_events_rejects_bad_host(tmp_path):
    client, _, _ = _client(tmp_path)
    with pytest.raises(WebSocketDisconnect), \
            client.websocket_connect("/events", headers={"host": "evil.com"}) as ws:
        ws.receive_json()


def test_events_initial_frames_order_no_game_no_laps(tmp_path, monkeypatch):
    from simcoach.lmu import smm
    monkeypatch.setattr(smm, "read_live_status", lambda: None)
    client, _, _ = _client(tmp_path)
    with client.websocket_connect("/events", headers={"host": "127.0.0.1"}) as ws:
        f1 = ws.receive_json()
        f2 = ws.receive_json()
        f3 = ws.receive_json()
    assert f1["topic"] == "status" and f1["data"]["track"] is None
    assert f2 == {"topic": "laps", "data": {"session_id": None, "laps": []}}
    assert f3 == {"topic": "map", "data": {}}


def test_status_http_matches_events_status_frame(tmp_path, monkeypatch):
    from simcoach.lmu import smm
    monkeypatch.setattr(smm, "read_live_status",
                        lambda: {"track": "test_track", "car": "test_car", "state": "garage"})
    client, _, _ = _client(tmp_path)
    http = client.get("/status").json()
    with client.websocket_connect("/events", headers={"host": "127.0.0.1"}) as ws:
        f1 = ws.receive_json()
    assert f1["topic"] == "status"
    assert f1["data"] == http


def test_events_laps_frame_tracks_session_and_updates(tmp_path, monkeypatch, synthetic_lap):
    from simcoach.lmu import smm
    monkeypatch.setattr(smm, "read_live_status", lambda: None)
    _fast(monkeypatch)
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    rec = LapRecord(**{**synthetic_lap.__dict__, "session_id": "s1"})
    lap_id = store.save(rec)
    app = create_app(store, Coach(FakeModel(reply="ok")))
    app.state.latest = {"record": rec, "lap_id": lap_id}
    client = TestClient(app, base_url="http://127.0.0.1")

    with client.websocket_connect("/events", headers={"host": "127.0.0.1"}) as ws:
        f1 = ws.receive_json()
        f2 = ws.receive_json()
        assert f1["topic"] == "status"
        assert f2["topic"] == "laps"
        assert f2["data"]["session_id"] == "s1"
        assert [lp["id"] for lp in f2["data"]["laps"]] == [lap_id]

        rec2 = LapRecord(**{**synthetic_lap.__dict__, "session_id": "s1"})
        lap_id2 = store.save(rec2)

        laps_frame = _drain_until(ws, "laps")
        assert {lp["id"] for lp in laps_frame["data"]["laps"]} == {lap_id, lap_id2}

        # no store change now -> no further laps frame while status keeps flowing
        seen_topics = [ws.receive_json()["topic"] for _ in range(9)]
        assert seen_topics.count("status") >= 3
        assert "laps" not in seen_topics


def test_events_map_frame_resent_only_on_track_change(tmp_path, monkeypatch):
    from simcoach.lmu import smm
    _fast(monkeypatch)
    track_holder = {"track": "test_track"}
    monkeypatch.setattr(
        smm, "read_live_status",
        lambda: {"track": track_holder["track"], "car": "test_car", "state": "garage"})
    client, _, _ = _client(tmp_path)
    with client.websocket_connect("/events", headers={"host": "127.0.0.1"}) as ws:
        f1 = ws.receive_json()
        f2 = ws.receive_json()
        f3 = ws.receive_json()
        assert f1["topic"] == "status" and f2["topic"] == "laps" and f3["topic"] == "map"

        # same track: a few more ticks must carry no new map frame
        for _ in range(3):
            assert ws.receive_json()["topic"] != "map"

        track_holder["track"] = "test_track_2"
        map_frame = _drain_until(ws, "map")
        assert map_frame["data"] == {}  # neither track has any stored geometry


def test_events_mapcapture_frames_on_start_change_and_stop(tmp_path, monkeypatch):
    from simcoach.lmu import smm
    monkeypatch.setattr(smm, "read_live_status", lambda: None)
    _fast(monkeypatch)
    client, _, app = _client(tmp_path)

    class _FakeLap:
        def __init__(self, dist):
            self.channels = {"lap_distance": np.array(dist)}

    with client.websocket_connect("/events", headers={"host": "127.0.0.1"}) as ws:
        ws.receive_json()  # status
        ws.receive_json()  # laps
        ws.receive_json()  # map

        app.state.mapping = True
        app.state.map_buffer = [_FakeLap([0, 10, 20]), _FakeLap([0, 15, 25])]
        f = _drain_until(ws, "mapcapture")
        assert f["data"]["active"] is True
        assert len(f["data"]["laps"]) == 2

        app.state.map_buffer.append(_FakeLap([0, 5, 10]))
        f2 = _drain_until(ws, "mapcapture")
        assert len(f2["data"]["laps"]) == 3

        app.state.mapping = False
        f3 = _drain_until(ws, "mapcapture")
        assert f3["data"]["active"] is False


def test_events_topic_failure_does_not_stop_status_frames(tmp_path, monkeypatch):
    # A real lap with position channels so the map pipeline actually reaches
    # average_racing_line (empty-position tracks return {} before ever calling it).
    from simcoach.lmu import smm
    monkeypatch.setattr(
        smm, "read_live_status",
        lambda: {"track": "evtrack", "car": "evcar", "state": "garage"})
    _fast(monkeypatch)
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    n = 20
    ch = {"pos_x": np.linspace(0, 1, n), "pos_z": np.linspace(0, 1, n),
         "lap_distance": np.linspace(0, 100, n)}
    store.save(LapRecord("lmu", "evtrack", "evcar", 90.0, True, 0.0, ch))
    app = create_app(store, Coach(FakeModel(reply="ok")))
    client = TestClient(app, base_url="http://127.0.0.1")

    monkeypatch.setattr(maps_mod, "average_racing_line",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    with client.websocket_connect("/events", headers={"host": "127.0.0.1"}) as ws:
        topics = [ws.receive_json()["topic"] for _ in range(6)]
    assert topics.count("status") >= 3
    assert "map" not in topics


def test_voice_setup_frame_carries_engine(tmp_path, monkeypatch):
    from simcoach.lmu import smm
    monkeypatch.setattr(smm, "read_live_status", lambda: None)
    _fast(monkeypatch)
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))

    class FakeInstaller:
        def __init__(self, engine):
            self.engine = engine
            self.installing = True

        def status(self):
            return {"engine": self.engine, "installed": not self.installing, "installing": self.installing,
                     "progress_bytes": 0, "total_bytes": 1, "error": None}

    whisper_inst, kokoro_inst = FakeInstaller("whisper"), FakeInstaller("kokoro")
    app = create_app(store, Coach(FakeModel(reply="ok")), installer=whisper_inst, kokoro_installer=kokoro_inst)
    client = TestClient(app, base_url="http://127.0.0.1")

    with client.websocket_connect("/events", headers={"host": "127.0.0.1"}) as ws:
        engines = set()
        for _ in range(2):
            f = _drain_until(ws, "voice_setup")
            engines.add(f["data"]["engine"])
        assert engines == {"whisper", "kokoro"}


def test_events_map_frame_resent_when_map_saved_mid_session(tmp_path, monkeypatch):
    # #155: the live rail map drew the map /events pushed at connect; a map saved
    # mid-session (same track) was never re-sent, so its corners never reached the rail.
    from pathlib import Path

    import simcoach
    from simcoach.lmu import smm
    _fast(monkeypatch)
    slug = "autodromo_enzo_e_dino_ferrari"
    shipped = Path(simcoach.__file__).resolve().parent / "tracks" / f"{slug}.json"
    tracks = tmp_path / "tracks"
    tracks.mkdir()
    (tracks / f"{slug}.json").write_bytes(shipped.read_bytes())
    monkeypatch.setattr(smm, "read_live_status",
                        lambda: {"track": slug, "car": "", "state": "garage"})
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    app = create_app(store, Coach(FakeModel(reply="ok")), tracks_dir=str(tracks))
    client = TestClient(app, base_url="http://127.0.0.1")
    with client.websocket_connect("/events", headers={"host": "127.0.0.1"}) as ws:
        first = _drain_until(ws, "map")["data"]
        assert first["corners"]
        body = client.get(f"/tracks/{slug}/map").json()
        body["corners"][0]["name"] = "Renamed Mid-Session"
        assert client.put(f"/tracks/{slug}/map", json=body).status_code == 200
        again = _drain_until(ws, "map")["data"]
    assert again["corners"][0]["name"] == "Renamed Mid-Session"
