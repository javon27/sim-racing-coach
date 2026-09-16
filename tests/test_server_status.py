import json

import numpy as np
from fastapi.testclient import TestClient

from simcoach.coach import Coach
from simcoach.model.fake import FakeModel
from simcoach.model_types import LapRecord
from simcoach.server.app import create_app
from simcoach.store import LapStore


def _tc(tmp_path):
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    app = create_app(store, Coach(FakeModel()))  # cmap/rest None
    return TestClient(app, base_url="http://127.0.0.1")


def _pos_lap(track, car, dx=0.0):
    # Same L-shaped line as test_server_reads._curve_lap.
    x = np.array([0.0, 100.0, 200.0, 200.0, 200.0]) + dx
    z = np.array([0.0, 0.0, 0.0, 100.0, 200.0])
    dist = np.array([0.0, 100.0, 200.0, 300.0, 400.0])
    return LapRecord("lmu", track, car, 90.0, True, 0.0,
                     {"pos_x": x, "pos_z": z, "lap_distance": dist})


def _seed_no_outline(tracks_dir, slug="circuit_de_spa_francorchamps"):
    # A shipped track file with corners but NO frozen path/path_dist -- issue #107:
    # the shape shipped Spa was actually in before an outline was ever baked.
    tracks_dir.mkdir(parents=True, exist_ok=True)
    (tracks_dir / f"{slug}.json").write_text(json.dumps({
        "track": slug,
        "corners": [{"number": 1, "name": "La Source", "direction": "right",
                     "start_dist": 0.0, "apex_dist": 50.0, "end_dist": 100.0}],
    }))


def _app_with_laps(tmp_path, track, laps_car=None):
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    tracks_dir = tmp_path / "tracks"
    _seed_no_outline(tracks_dir, slug=track)
    if laps_car is not None:
        store.save(_pos_lap(track, laps_car))
    app = create_app(store, Coach(FakeModel()), tracks_dir=str(tracks_dir))
    return app


def test_status_offline_shape(tmp_path, monkeypatch):
    # Force the offline case (no live SMM): on a dev box with LMU running, /status
    # reads live state and would report the real session.
    from simcoach.lmu import smm
    monkeypatch.setattr(smm, "read_live_status", lambda: None)
    r = _tc(tmp_path).get("/status")
    assert r.status_code == 200
    b = r.json()
    assert set(b) == {"track", "car", "car_model", "on_track", "activity", "has_map",
                      "lmu_connected", "session_id", "lap_id"}
    assert b["has_map"] is False and b["lmu_connected"] is False
    assert b["on_track"] is False and b["activity"] is None
    assert b["session_id"] is None and b["lap_id"] is None


def test_status_on_track_live_with_latest_context(tmp_path, synthetic_lap, monkeypatch):
    # Live state says on-track; a matching captured lap adds session/lap context.
    from simcoach.lmu import smm
    monkeypatch.setattr(smm, "read_live_status",
                        lambda: {"track": synthetic_lap.track, "car": synthetic_lap.car, "state": "track"})
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    app = create_app(store, Coach(FakeModel()))
    app.state.latest = {"record": synthetic_lap, "lap_id": 1}

    b = TestClient(app, base_url="http://127.0.0.1").get("/status").json()
    assert b["on_track"] is True and b["activity"] == "track"
    assert b["track"] == synthetic_lap.track and b["car"] == synthetic_lap.car
    assert b["session_id"] == synthetic_lap.session_id and b["lap_id"] == 1


def test_status_reports_garage_before_any_lap(tmp_path, monkeypatch):
    # The fix: garage/on-track is reported live, with NO captured lap at all.
    from simcoach.lmu import smm
    monkeypatch.setattr(smm, "read_live_status",
                        lambda: {"track": "Spa", "car": "GT3", "state": "garage"})
    b = _tc(tmp_path).get("/status").json()
    assert b["activity"] == "garage" and b["on_track"] is False
    assert b["track"] == "Spa" and b["car"] == "GT3"
    assert b["session_id"] is None and b["lap_id"] is None


def test_status_has_map_borrows_another_cars_laps(tmp_path, monkeypatch):
    # Issue #107: live status keyed the map lookup on the LIVE car -- a shipped
    # track with corners but no frozen outline said "no map" for a car with no
    # laps yet, even though laps existed for another car driven there.
    track = "circuit_de_spa_francorchamps"
    from simcoach.lmu import smm
    monkeypatch.setattr(smm, "read_live_status",
                        lambda: {"track": track, "car": "Lexus", "state": "track"})
    app = _app_with_laps(tmp_path, track, laps_car="Porsche")
    b = TestClient(app, base_url="http://127.0.0.1").get("/status").json()
    assert b["has_map"] is True


def test_status_has_map_false_with_no_laps_at_all(tmp_path, monkeypatch):
    track = "circuit_de_spa_francorchamps"
    from simcoach.lmu import smm
    monkeypatch.setattr(smm, "read_live_status",
                        lambda: {"track": track, "car": "Lexus", "state": "track"})
    app = _app_with_laps(tmp_path, track, laps_car=None)
    b = TestClient(app, base_url="http://127.0.0.1").get("/status").json()
    assert b["has_map"] is False


def test_track_map_borrows_another_cars_laps(tmp_path, monkeypatch):
    track = "circuit_de_spa_francorchamps"
    from simcoach.lmu import smm
    monkeypatch.setattr(smm, "read_live_status",
                        lambda: {"track": track, "car": "Lexus", "state": "track"})
    app = _app_with_laps(tmp_path, track, laps_car="Porsche")
    m = TestClient(app, base_url="http://127.0.0.1").get("/track/map").json()
    assert m.get("path")


def test_track_map_empty_with_no_laps_at_all(tmp_path, monkeypatch):
    track = "circuit_de_spa_francorchamps"
    from simcoach.lmu import smm
    monkeypatch.setattr(smm, "read_live_status",
                        lambda: {"track": track, "car": "Lexus", "state": "track"})
    app = _app_with_laps(tmp_path, track, laps_car=None)
    m = TestClient(app, base_url="http://127.0.0.1").get("/track/map").json()
    assert m == {}


def test_track_map_with_frozen_outline_behaves_as_before(tmp_path, monkeypatch):
    # A track with its own frozen path/path_dist needs no borrowed car at all.
    track = "curveland"
    from simcoach.lmu import smm
    monkeypatch.setattr(smm, "read_live_status",
                        lambda: {"track": track, "car": "Lexus", "state": "track"})
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    tracks_dir = tmp_path / "tracks"
    tracks_dir.mkdir(parents=True, exist_ok=True)
    (tracks_dir / f"{track}.json").write_text(json.dumps({
        "track": track,
        "path": [[float(i), float(i % 3)] for i in range(120)],
        "path_dist": [float(i) for i in range(120)],
        "corners": [{"number": 1, "name": "Turn 1", "direction": "right",
                     "start_dist": 0.0, "apex_dist": 50.0, "end_dist": 100.0}],
    }))
    app = create_app(store, Coach(FakeModel()), tracks_dir=str(tracks_dir))
    client = TestClient(app, base_url="http://127.0.0.1")
    assert client.get("/status").json()["has_map"] is True
    m = client.get("/track/map").json()
    assert m.get("path") and len(m["corners"]) == 1
