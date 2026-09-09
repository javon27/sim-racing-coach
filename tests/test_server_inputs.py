"""GET /track/inputs — driver-input traces for the Pit Wall graphs (issue #21).

UI-only: the payload is downsampled server-side (UI-agnostic core rule) and never
reaches the coach prompt.
"""

import numpy as np
from fastapi.testclient import TestClient

from simcoach.coach import Coach
from simcoach.model.fake import FakeModel
from simcoach.server.app import create_app
from simcoach.store import LapStore


def _live(monkeypatch, lap):
    from simcoach.lmu import smm
    monkeypatch.setattr(smm, "read_live_status",
                        lambda: {"track": lap.track, "car": lap.car, "state": "track"})


def _app(tmp_path):
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    return create_app(store, Coach(FakeModel())), store


def test_inputs_empty_when_offline(tmp_path, monkeypatch):
    from simcoach.lmu import smm
    monkeypatch.setattr(smm, "read_live_status", lambda: None)
    app, _ = _app(tmp_path)
    b = TestClient(app, base_url="http://127.0.0.1").get("/track/inputs").json()
    assert b == {"last": None, "best": None}


def test_inputs_returns_last_lap_traces(tmp_path, synthetic_lap, monkeypatch):
    _live(monkeypatch, synthetic_lap)
    app, store = _app(tmp_path)
    lap_id = store.save(synthetic_lap)
    app.state.latest = {"record": synthetic_lap, "lap_id": lap_id}

    b = TestClient(app, base_url="http://127.0.0.1").get("/track/inputs").json()
    last = b["last"]
    assert last["lap_id"] == lap_id
    assert last["lap_time"] == synthetic_lap.lap_time
    # Downsampled server-side: a fixed grid, far fewer points than the 1000 raw samples.
    assert len(last["dist"]) == len(last["throttle"]) == 500
    assert len(last["brake"]) == len(last["steering"]) == 500
    assert last["dist"] == sorted(last["dist"])
    assert last["length"] > 0
    # The synthetic lap brakes hard in two corners; the trace must show it.
    assert max(last["brake"]) > 0.5


def test_best_omitted_when_it_is_the_last_lap(tmp_path, synthetic_lap, monkeypatch):
    _live(monkeypatch, synthetic_lap)
    app, store = _app(tmp_path)
    lap_id = store.save(synthetic_lap)
    app.state.latest = {"record": synthetic_lap, "lap_id": lap_id}
    # Only one lap on record -> it is both last and best; don't draw it twice.
    assert TestClient(app, base_url="http://127.0.0.1").get("/track/inputs").json()["best"] is None


def test_best_is_a_faster_other_lap(tmp_path, synthetic_lap, monkeypatch):
    _live(monkeypatch, synthetic_lap)
    app, store = _app(tmp_path)
    import copy
    fast = copy.deepcopy(synthetic_lap)
    fast.lap_time = 80.0
    fast.timestamp = 1.0
    store.save(fast)
    slow_id = store.save(synthetic_lap)
    app.state.latest = {"record": synthetic_lap, "lap_id": slow_id}

    b = TestClient(app, base_url="http://127.0.0.1").get("/track/inputs").json()
    assert b["last"]["lap_time"] == synthetic_lap.lap_time
    assert b["best"]["lap_time"] == 80.0
    # Both traces share the lap_distance axis, so an overlay lines up.
    assert b["best"]["dist"] == b["last"]["dist"]


def test_lap_without_input_channels_yields_null(tmp_path, synthetic_lap, monkeypatch):
    _live(monkeypatch, synthetic_lap)
    app, store = _app(tmp_path)
    import copy
    bare = copy.deepcopy(synthetic_lap)
    bare.channels = {"lap_distance": np.linspace(0, 3000, 10)}
    lap_id = store.save(bare)
    app.state.latest = {"record": bare, "lap_id": lap_id}

    b = TestClient(app, base_url="http://127.0.0.1").get("/track/inputs").json()
    assert b["last"] is None
