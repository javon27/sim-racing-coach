"""#176: the lap rail's Optimal row under Best. `GET /sessions/{sid}/laps` must state
the exact same headline figure `CoachContext.get_optimal_seconds` (the coach's own
`get_time_on_table`) states for the session's best lap -- never a naive min-of-sectors,
which an off-track sector would understate."""
import dataclasses

import pytest
from fastapi.testclient import TestClient

from simcoach.coach import Coach
from simcoach.coach_tools import CoachContext
from simcoach.corners import CornerMap
from simcoach.model.fake import FakeModel
from simcoach.server import core as core_mod
from simcoach.server.app import create_app
from simcoach.store import LapStore

CMAP = "simcoach/tracks/test_track.json"


def _variant(lap):
    return dataclasses.replace(lap, channels={k: v.copy() for k, v in lap.channels.items()})


def _save(store, lap, lap_time, ts, lap_no, sid="s1", **kw):
    return store.save(dataclasses.replace(lap, lap_time=lap_time, timestamp=float(ts),
                                          session_id=sid, phase="practice", lap_no=lap_no, **kw))


def _seed_session(store, synthetic_lap, sid="s1"):
    """One session: the subject (fastest, 92.7 s) plus a lap whose S1 (30.20) is the
    fastest of all -- but it ran off at T1 (800-1200 m), so the naive min-of-sectors
    total would use it and the real one (30.60 from the subject's own lap) must not."""
    bounds = [1500.0, 2600.0]
    off = _variant(synthetic_lap)
    off.channels["track_edge"][(off.channels["lap_distance"] >= 800.0)
                               & (off.channels["lap_distance"] <= 1200.0)] = 1.0
    _save(store, off, 93.5, 1, 1, sid=sid, sectors=[30.20, 31.40, 31.20], sector_bounds=bounds)
    _save(store, synthetic_lap, 92.9, 2, 2, sid=sid, sectors=[30.60, 31.20, 31.10],
         sector_bounds=bounds)
    return _save(store, synthetic_lap, 92.7, 3, 3, sid=sid, sectors=[30.60, 31.00, 31.10],
                sector_bounds=bounds)   # the session best


def _app(tmp_path, cmap=CMAP):
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    app = create_app(store, Coach(FakeModel()), cmap=CornerMap.load(cmap) if cmap else None)
    return app, store


def _tc(app):
    return TestClient(app, base_url="http://127.0.0.1")


def test_endpoint_optimal_matches_the_coach_tools_headline(tmp_path, synthetic_lap):
    app, store = _app(tmp_path)
    _seed_session(store, synthetic_lap)
    expected = CoachContext(store, cmap=CornerMap.load(CMAP), track="test_track",
                            car="test_car", session_id="s1").get_optimal_seconds()
    assert expected is not None
    body = _tc(app).get("/sessions/s1/laps").json()
    assert body["optimal"] == pytest.approx(expected, abs=1e-9)
    # Never the naive min-of-sectors total, which would count the off lap's 30.20 S1.
    naive = 30.20 + 31.00 + 31.10
    assert body["optimal"] != pytest.approx(naive, abs=1e-6)


def test_endpoint_optimal_none_with_no_such_session(tmp_path):
    app, _store = _app(tmp_path)
    body = _tc(app).get("/sessions/absent/laps").json()
    assert body == {"laps": [], "optimal": None}


def test_endpoint_optimal_none_without_a_comparable_lap(tmp_path, synthetic_lap):
    app, store = _app(tmp_path)
    _save(store, synthetic_lap, 90.0, 1, 1)   # the only lap this session
    body = _tc(app).get("/sessions/s1/laps").json()
    assert len(body["laps"]) == 1
    assert body["optimal"] is None


def test_endpoint_optimal_none_without_a_corner_map(tmp_path, synthetic_lap):
    # get_time_on_table itself refuses without a corner map -- #176 can't do better.
    app, store = _app(tmp_path, cmap=None)
    _seed_session(store, synthetic_lap)
    body = _tc(app).get("/sessions/s1/laps").json()
    assert len(body["laps"]) == 3
    assert body["optimal"] is None


def test_endpoint_optimal_cache_invalidates_when_a_lap_lands(tmp_path, synthetic_lap, monkeypatch):
    app, store = _app(tmp_path)
    _seed_session(store, synthetic_lap)
    calls = []
    real = core_mod.CoachContext.get_optimal_seconds

    def _counted(self, which="best"):
        calls.append(1)
        return real(self, which)
    monkeypatch.setattr(core_mod.CoachContext, "get_optimal_seconds", _counted)

    client = _tc(app)
    first = client.get("/sessions/s1/laps").json()["optimal"]
    assert len(calls) == 1
    # Same lap set: served from the cache, not recomputed (the ~2.3s-cold cost #176
    # flagged must not repeat on every poll).
    second = client.get("/sessions/s1/laps").json()["optimal"]
    assert second == first
    assert len(calls) == 1
    # A new lap lands -> the (count, last id) signature changes -> recomputed.
    _save(store, synthetic_lap, 92.75, 4, 4, sectors=[30.55, 31.00, 31.10],
         sector_bounds=[1500.0, 2600.0])
    client.get("/sessions/s1/laps").json()
    assert len(calls) == 2
