"""#176 fix round 1 (red-team ACCEPT-WITH-FIXES): the lap rail's Optimal row must
never sit inside the lap-list read path. `GET /sessions/{sid}/laps` stays the bare
array it always was; `GET /sessions/{sid}/optimal` is new and always returns
instantly, scheduling `CoachContext.get_optimal_seconds` (the coach's own
get_time_on_table headline) on a single background worker (core._ensure_optimal) and
handing back whatever is cached right now. `/events` pushes a same-named "optimal"
topic once the background compute lands, decoupled from the "laps" topic."""
import dataclasses
import time

import pytest
from fastapi.testclient import TestClient

from simcoach.coach import Coach
from simcoach.coach_tools import CoachContext
from simcoach.corners import CornerMap
from simcoach.model.fake import FakeModel
from simcoach.server import core as core_mod
from simcoach.server import live_ws as live_ws_mod
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
    total would use it and the real one (30.40, from a clean comparable lap) must
    not. That clean lap's S1 IS a real 0.20 s gain over the subject's own 30.60, so
    the headline is a real GAIN below 92.7 -- a row the rail would actually display,
    not merely "not the naive value" (#176 fix round 1 red-team finding)."""
    bounds = [1500.0, 2600.0]
    off = _variant(synthetic_lap)
    off.channels["track_edge"][(off.channels["lap_distance"] >= 800.0)
                               & (off.channels["lap_distance"] <= 1200.0)] = 1.0
    _save(store, off, 93.5, 1, 1, sid=sid, sectors=[30.20, 31.40, 31.20], sector_bounds=bounds)
    _save(store, synthetic_lap, 92.8, 2, 2, sid=sid, sectors=[30.40, 31.30, 31.10],
         sector_bounds=bounds)
    return _save(store, synthetic_lap, 92.7, 3, 3, sid=sid, sectors=[30.60, 31.00, 31.10],
                sector_bounds=bounds)   # the session best


def _app(tmp_path, cmap=CMAP):
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    app = create_app(store, Coach(FakeModel()), cmap=CornerMap.load(cmap) if cmap else None)
    return app, store


def _tc(app):
    return TestClient(app, base_url="http://127.0.0.1")


def _wait_inflight(app, sid, timeout=5.0):
    """Block until the background compute `_ensure_optimal` scheduled for `sid` (if
    any) has finished -- the deterministic way to observe an async result in a test,
    used instead of a sleep-and-hope poll."""
    fut = app.state.optimal_inflight.get(sid)
    if fut is not None:
        fut.result(timeout=timeout)


def _drain(ws, topic, limit=50):
    for _ in range(limit):
        f = ws.receive_json()
        if f["topic"] == topic:
            return f
    return None


# --- the lap list never touches analysis again --------------------------------------

def test_laps_endpoint_is_the_bare_array_again(tmp_path, synthetic_lap):
    app, store = _app(tmp_path)
    lap_id = _seed_session(store, synthetic_lap)
    body = _tc(app).get("/sessions/s1/laps").json()
    assert isinstance(body, list)
    assert lap_id in [r["id"] for r in body]
    assert app.state.optimal_inflight == {}   # GET /laps schedules nothing


# --- GET /sessions/{sid}/optimal: instant, matches the coach tool, recomputes -------

def test_optimal_endpoint_matches_coach_tool_and_recomputes_on_a_new_lap(tmp_path, synthetic_lap):
    app, store = _app(tmp_path)
    _seed_session(store, synthetic_lap)
    c = _tc(app)

    t0 = time.perf_counter()
    first = c.get("/sessions/s1/optimal").json()
    assert time.perf_counter() - t0 < 0.5   # never blocks on analyze_lap
    # Nothing cached yet; a compute was scheduled -- pending tells a Sessions-review
    # caller to check back rather than conclude there's simply no optimal (#176 fix
    # round 2).
    assert first == {"optimal": None, "pending": True}
    _wait_inflight(app, "s1")

    body = c.get("/sessions/s1/optimal").json()
    expected = CoachContext(store, cmap=CornerMap.load(CMAP), track="test_track",
                            car="test_car", session_id="s1").get_optimal_seconds()
    assert expected is not None
    assert body["optimal"] == pytest.approx(expected, abs=1e-9)
    assert body["optimal"] == pytest.approx(92.50, abs=1e-6)   # 92.70 - 0.20 s gained on S1
    assert body["optimal"] < 92.7 - 0.005     # a real, displayable gain under the best
    # Never the naive min-of-sectors total, which would count the off lap's 30.20 S1.
    naive = 30.20 + 31.00 + 31.10
    assert body["optimal"] != pytest.approx(naive, abs=1e-6)

    # A new comparable lap lands, with a real S2/S3 gain of its own (slower overall,
    # 93.0 s, so it never displaces the session best) -> the cached signature is
    # stale -> recomputes to a materially different, lower headline.
    _save(store, synthetic_lap, 93.0, 4, 4, sid="s1", sectors=[31.5, 30.8, 30.7],
         sector_bounds=[1500.0, 2600.0])
    c.get("/sessions/s1/optimal")
    _wait_inflight(app, "s1")
    after = c.get("/sessions/s1/optimal").json()["optimal"]
    expected_after = CoachContext(store, cmap=CornerMap.load(CMAP), track="test_track",
                                  car="test_car", session_id="s1").get_optimal_seconds()
    assert after == pytest.approx(expected_after, abs=1e-9)
    assert after == pytest.approx(91.90, abs=1e-6)
    assert after != pytest.approx(body["optimal"], abs=1e-9)   # actually recomputed, not stale


def test_optimal_endpoint_none_with_no_such_session(tmp_path):
    app, _store = _app(tmp_path)
    # No laps at all -> `_ensure_optimal` never schedules anything.
    assert _tc(app).get("/sessions/absent/optimal").json() == {"optimal": None, "pending": False}


def test_optimal_endpoint_none_without_a_comparable_lap(tmp_path, synthetic_lap):
    app, store = _app(tmp_path)
    _save(store, synthetic_lap, 90.0, 1, 1)   # the only lap this session
    c = _tc(app)
    c.get("/sessions/s1/optimal")
    _wait_inflight(app, "s1")
    assert c.get("/sessions/s1/optimal").json() == {"optimal": None, "pending": False}


def test_optimal_endpoint_none_without_a_corner_map(tmp_path, synthetic_lap):
    # get_time_on_table itself refuses without a corner map -- #176 can't do better.
    app, store = _app(tmp_path, cmap=None)
    _seed_session(store, synthetic_lap)
    c = _tc(app)
    c.get("/sessions/s1/optimal")
    _wait_inflight(app, "s1")
    assert c.get("/sessions/s1/optimal").json() == {"optimal": None, "pending": False}


# --- fix round 2: `pending` while a compute runs, then settles false ----------------

def test_pending_is_true_while_compute_runs_and_false_after(tmp_path, synthetic_lap, monkeypatch):
    app, store = _app(tmp_path)
    _seed_session(store, synthetic_lap)
    real = core_mod.CoachContext.get_optimal_seconds

    def _slow(self, which="best"):
        time.sleep(0.2)
        return real(self, which)
    monkeypatch.setattr(core_mod.CoachContext, "get_optimal_seconds", _slow)

    c = _tc(app)
    body = c.get("/sessions/s1/optimal").json()
    assert body == {"optimal": None, "pending": True}

    _wait_inflight(app, "s1")
    after = c.get("/sessions/s1/optimal").json()
    assert after["pending"] is False
    assert after["optimal"] is not None


# --- map_rev invalidation ------------------------------------------------------------

def test_map_rev_bump_invalidates_the_cache_and_a_real_value_appears(tmp_path, synthetic_lap):
    app, store = _app(tmp_path, cmap=None)   # no corner map yet
    bounds = [1500.0, 2600.0]
    _save(store, synthetic_lap, 92.9, 1, 1, sectors=[30.6, 31.2, 31.1], sector_bounds=bounds)
    _save(store, synthetic_lap, 92.7, 2, 2, sectors=[30.7, 30.9, 31.1], sector_bounds=bounds)
    c = _tc(app)
    c.get("/sessions/s1/optimal")
    _wait_inflight(app, "s1")
    assert c.get("/sessions/s1/optimal").json() == {"optimal": None, "pending": False}

    # A map now exists (the same invalidation _invalidate_canon applies on a save, #155).
    app.state.cmap = CornerMap.load(CMAP)
    app.state.map_rev += 1

    # Right after the bump: the endpoint is still instant and hands back the STALE
    # (None) cached value -- it schedules (pending=True), it never blocks.
    t0 = time.perf_counter()
    right_after = c.get("/sessions/s1/optimal").json()
    assert time.perf_counter() - t0 < 0.5
    assert right_after == {"optimal": None, "pending": True}

    _wait_inflight(app, "s1")
    fresh = c.get("/sessions/s1/optimal").json()["optimal"]
    assert fresh is not None


# --- fix round 3 should-fix: never serve a stale-signature value while pending ------

def test_stale_value_hidden_while_a_new_compute_is_pending(tmp_path, synthetic_lap, monkeypatch):
    # A REAL, already-cached value exists (unlike the map_rev case above, where the
    # cached value was None either way) -- a new best lap must never keep showing it
    # against the new best while the fresh compute is still running (a false gap on
    # the rail for the 2-4 s the compute takes).
    app, store = _app(tmp_path)
    bounds = [1500.0, 2600.0]
    _save(store, synthetic_lap, 92.9, 1, 1, sectors=[30.6, 31.2, 31.1], sector_bounds=bounds)
    _save(store, synthetic_lap, 92.7, 2, 2, sectors=[30.7, 30.9, 31.1], sector_bounds=bounds)
    c = _tc(app)
    c.get("/sessions/s1/optimal")
    _wait_inflight(app, "s1")
    old = c.get("/sessions/s1/optimal").json()
    assert old["optimal"] is not None and old["pending"] is False

    real = core_mod.CoachContext.get_optimal_seconds

    def _slow(self, which="best"):
        time.sleep(0.3)
        return real(self, which)
    monkeypatch.setattr(core_mod.CoachContext, "get_optimal_seconds", _slow)

    _save(store, synthetic_lap, 92.3, 3, 3, sectors=[30.2, 30.9, 31.2],
         sector_bounds=bounds)   # a new, faster best lap -- the cached sig is now stale
    mid = c.get("/sessions/s1/optimal").json()
    # Never the OLD figure against the new best: hidden (None) while the fresh
    # compute for the new best is still running.
    assert mid == {"optimal": None, "pending": True}

    _wait_inflight(app, "s1")
    new = c.get("/sessions/s1/optimal").json()
    assert new["optimal"] is not None
    assert new["pending"] is False
    assert new["optimal"] != old["optimal"]


# --- single-flight: two concurrent requests, one compute -----------------------------

def test_two_concurrent_requests_run_one_compute(tmp_path, synthetic_lap, monkeypatch):
    import threading

    app, store = _app(tmp_path)
    _seed_session(store, synthetic_lap)
    calls = []
    real = core_mod.CoachContext.get_optimal_seconds

    def _slow(self, which="best"):
        calls.append(1)
        time.sleep(0.2)   # widen the race window so both requests really overlap
        return real(self, which)
    monkeypatch.setattr(core_mod.CoachContext, "get_optimal_seconds", _slow)

    c = _tc(app)
    results = []

    def _hit():
        results.append(c.get("/sessions/s1/optimal").json())
    threads = [threading.Thread(target=_hit) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    _wait_inflight(app, "s1")

    assert len(calls) == 1   # single-flight: the second request never re-schedules
    assert len(results) == 2


# --- the /events laps frame is never delayed by a slow compute -----------------------

def test_events_laps_frame_is_not_delayed_by_a_slow_optimal_compute(
        tmp_path, synthetic_lap, monkeypatch):
    from simcoach.lmu import smm
    monkeypatch.setattr(smm, "read_live_status", lambda: None)
    monkeypatch.setattr(live_ws_mod, "_EVENTS_PERIOD", 0.01)

    app, store = _app(tmp_path)
    lap_id = _seed_session(store, synthetic_lap)
    rec = store.load(lap_id)
    app.state.latest = {"record": rec, "lap_id": lap_id}

    def _slow(self, which="best"):
        time.sleep(1.0)
        return 90.0
    monkeypatch.setattr(core_mod.CoachContext, "get_optimal_seconds", _slow)

    c = _tc(app)
    with c.websocket_connect("/events", headers={"host": "127.0.0.1"}) as ws:
        t0 = time.perf_counter()
        f_laps = _drain(ws, "laps", limit=50)
        t_laps = time.perf_counter() - t0
        # The FIRST "optimal" frame is pushed the same tick the compute is merely
        # SCHEDULED (nothing cached yet: {"optimal": None}) -- proving that push
        # itself never waits on the compute either.
        f_opt1 = _drain(ws, "optimal", limit=50)
        t_opt1 = time.perf_counter() - t0
        # The SECOND is the real value, only once the slow compute actually finishes.
        f_opt2 = _drain(ws, "optimal", limit=500)
        t_opt2 = time.perf_counter() - t0

    assert f_laps is not None and f_laps["data"]["laps"]
    assert t_laps < 0.5                   # laps arrives fast, well inside the 1.0s sleep
    assert f_opt1 is not None and f_opt1["data"]["optimal"] is None
    assert t_opt1 < 0.5                    # the "nothing yet" push doesn't wait either
    assert f_opt2 is not None and f_opt2["data"]["optimal"] == 90.0
    assert t_opt2 >= 0.9                   # the real value only after the compute finished


# --- failure isolation: a raising compute never breaks laps or /events ---------------

def test_raising_compute_never_breaks_laps_or_events(tmp_path, synthetic_lap, monkeypatch, caplog):
    from simcoach.lmu import smm
    monkeypatch.setattr(smm, "read_live_status", lambda: None)
    monkeypatch.setattr(live_ws_mod, "_EVENTS_PERIOD", 0.01)

    app, store = _app(tmp_path)
    lap_id = _seed_session(store, synthetic_lap)
    rec = store.load(lap_id)
    app.state.latest = {"record": rec, "lap_id": lap_id}

    calls = []

    def _boom(self, which="best"):
        calls.append(1)
        raise ValueError("analysis blew up")
    monkeypatch.setattr(core_mod.CoachContext, "get_optimal_seconds", _boom)

    c = _tc(app)
    # The bare lap list must arrive untouched.
    laps = c.get("/sessions/s1/laps").json()
    assert isinstance(laps, list) and laps

    with caplog.at_level("WARNING"):
        body = c.get("/sessions/s1/optimal").json()
        assert body == {"optimal": None, "pending": True}
        _wait_inflight(app, "s1")
    assert len(calls) == 1
    assert app.state.optimal_cache["s1"][1] is None
    assert any("optimal compute failed" in r.message for r in caplog.records)

    # /events: status + laps frames still arrive; the connection never dies.
    with c.websocket_connect("/events", headers={"host": "127.0.0.1"}) as ws:
        f_status = _drain(ws, "status")
        f_laps = _drain(ws, "laps")
    assert f_status is not None
    assert f_laps is not None and f_laps["data"]["laps"]


# --- bounded cache --------------------------------------------------------------------

def test_optimal_cache_is_bounded(tmp_path, synthetic_lap):
    app, store = _app(tmp_path)
    c = _tc(app)
    for i in range(core_mod._OPTIMAL_CACHE_MAX + 10):
        sid = f"s{i}"
        _save(store, synthetic_lap, 90.0, i + 1, 1, sid=sid)
        c.get(f"/sessions/{sid}/optimal")
        _wait_inflight(app, sid)
    assert len(app.state.optimal_cache) <= core_mod._OPTIMAL_CACHE_MAX
