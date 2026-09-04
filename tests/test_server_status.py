from fastapi.testclient import TestClient
from simcoach.server.app import create_app
from simcoach.store import LapStore
from simcoach.coach import Coach
from simcoach.model.fake import FakeModel


def _tc(tmp_path):
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    app = create_app(store, Coach(FakeModel(), "simcoach/knowledge"))  # cmap/rest None
    return TestClient(app, base_url="http://127.0.0.1")


def test_status_offline_shape(tmp_path):
    r = _tc(tmp_path).get("/status")
    assert r.status_code == 200
    b = r.json()
    assert set(b) == {"track", "car", "on_track", "has_map", "lmu_connected", "session_id", "lap_id"}
    assert b["has_map"] is False and b["lmu_connected"] is False and b["on_track"] is False
    assert b["session_id"] is None and b["lap_id"] is None


def test_status_on_track_from_latest(tmp_path, synthetic_lap):
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    app = create_app(store, Coach(FakeModel(), "simcoach/knowledge"))
    app.state.latest = {"record": synthetic_lap, "lap_id": 1}

    r = TestClient(app, base_url="http://127.0.0.1").get("/status")
    assert r.status_code == 200
    b = r.json()
    assert b["on_track"] is True
    assert b["track"] == synthetic_lap.track
    assert b["car"] == synthetic_lap.car
    assert b["session_id"] == synthetic_lap.session_id
    assert b["lap_id"] == 1
