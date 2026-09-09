from fastapi.testclient import TestClient
from simcoach.server.app import create_app
from simcoach.store import LapStore
from simcoach.coach import Coach
from simcoach.model.fake import FakeModel


def _tc(tmp_path):
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    app = create_app(store, Coach(FakeModel()))  # cmap/rest None
    return TestClient(app, base_url="http://127.0.0.1")


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
