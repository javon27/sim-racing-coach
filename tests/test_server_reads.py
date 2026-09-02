from fastapi.testclient import TestClient
from simcoach.server.app import create_app
from simcoach.store import LapStore
from simcoach.coach import Coach
from simcoach.model.fake import FakeModel
from simcoach.corners import CornerMap


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
                     "valid": 1, "ts": synthetic_lap.timestamp}]


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


def test_analysis_unanalyzable_is_422(tmp_path, synthetic_lap, monkeypatch):
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    app, lap_id, cid = _app(tmp_path, synthetic_lap, cmap=cmap)

    import simcoach.server.app as app_module

    def _boom(*args, **kwargs):
        raise ValueError("bad telemetry")

    monkeypatch.setattr(app_module, "analyze_lap", _boom)
    r = _tc(app).get(f"/laps/{lap_id}/analysis")
    assert r.status_code == 422
