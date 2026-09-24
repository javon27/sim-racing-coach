"""#160: the lap in progress is readable corner by corner, before the line.

The driver spun at Turn 6 and the coach, which only saw COMPLETED laps, told him he
hadn't. Each corner is now analysed (the same `_metrics_for_corner` a stored lap gets)
the moment the car leaves its window, on a worker, and kept in SQLite as provisional
until the lap commits."""
import ctypes as C
import dataclasses
import uuid

import numpy as np

from simcoach.coach import Coach
from simcoach.coach_tools import CoachContext
from simcoach.corners import CornerMap
from simcoach.livelap import LiveLap
from simcoach.model.fake import FakeModel
from simcoach.server.app import _capture_loop, create_app
from simcoach.store import LapStore

CMAP = "simcoach/tracks/test_track.json"
BASE = "test-track--test-car--offline"
START_TS = 1_700_000_000.0


class _Source:
    """The piece of LmuSource the worker reads: `live_lap` = (meta, channels), where
    channels is the SAME dict of lists capture appends to."""

    def __init__(self, lap_no=5):
        self.meta = {"track": "test_track", "car": "test_car", "session_id": BASE,
                     "phase": "race", "lap_no": lap_no, "start_ts": START_TS}
        self.channels: dict[str, list] = {}
        self.live_lap = (self.meta, self.channels)

    def drive(self, lap, upto_m):
        """Append the synthetic lap's samples up to `upto_m` (as capture would)."""
        d = lap.channels["lap_distance"]
        have = len(self.channels.get("lap_distance", []))
        stop = int(np.searchsorted(d, upto_m, side="right"))
        for i in range(have, stop):
            for k, v in lap.channels.items():
                self.channels.setdefault(k, []).append(float(v[i]))


def _store(tmp_path):
    return LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))


def _worker(store):
    cmap = CornerMap.load(CMAP)
    live = LiveLap(store, lambda track: cmap)
    return live


def _ctx(store, *, on_track, live=None, cmap=None):
    live_meta, status = live.now({"state": "track"}) if live is not None else (None, None)
    return CoachContext(store, cmap=cmap or CornerMap.load(CMAP), track="test_track",
                        car="test_car", on_track=on_track, session_id=BASE,
                        live_lap=live_meta, car_status=status)


def test_each_corner_is_written_as_the_car_leaves_it(tmp_path, synthetic_lap):
    store = _store(tmp_path)
    live = _worker(store)
    src = live.source = _Source()

    src.drive(synthetic_lap, 1100)          # mid-T1: nothing is complete yet
    live.tick()
    assert store.live_lap_corners(BASE)[1] == []

    src.drive(synthetic_lap, 1300)          # past T1's window end (1200)
    live.tick()
    meta, rows = store.live_lap_corners(BASE)
    assert [r["label"] for r in rows] == ["1"]
    assert meta["lap_no"] == 5 and meta["phase"] == "race"

    src.drive(synthetic_lap, 2500)          # past T2's (2400)
    live.tick()
    _, rows = store.live_lap_corners(BASE)
    assert [r["label"] for r in rows] == ["1", "2"]
    # The SAME per-corner analysis a completed lap gets -- not a second implementation.
    from simcoach.analysis import analyze_lap
    full = analyze_lap(synthetic_lap, CornerMap.load(CMAP))
    assert rows[0]["facts"]["metrics"]["min_spd"] == full.corners[0].min_spd
    assert rows[1]["facts"]["metrics"]["brake_point"] == full.corners[1].brake_point


def test_a_mid_lap_crash_keeps_the_corners_already_driven(tmp_path, synthetic_lap):
    store = _store(tmp_path)
    live = _worker(store)
    live.source = src = _Source()
    src.drive(synthetic_lap, 1300)
    live.tick()
    # Retirement: the lap never commits, capture goes away, the sidecar restarts.
    store.conn.close()
    store = _store(tmp_path)
    _, rows = store.live_lap_corners(BASE)
    assert [r["label"] for r in rows] == ["1"]
    ctx = _ctx(store, on_track=False)
    out = ctx.dispatch("get_corner", {"corner": "1", "which": "this lap"})
    assert "provisional" in out and "never completed" in out
    assert "game lap 5" in out
    out = ctx.dispatch("get_corner", {"corner": "2", "which": "this lap"})
    assert "not captured" in out.lower()


def test_the_lap_commit_supersedes_its_provisional_corners(tmp_path, synthetic_lap):
    store = _store(tmp_path)
    live = _worker(store)
    live.source = src = _Source(lap_no=5)
    src.drive(synthetic_lap, 1300)
    live.tick()
    lap = dataclasses.replace(synthetic_lap, session_id=BASE, phase="race", lap_no=5,
                              timestamp=START_TS + 92.5)
    store.save(lap)
    assert store.live_lap_corners(BASE)[1] == []       # hidden at once, whatever the order
    # ...and deleted by the worker when the next lap starts.
    live.source = _Source(lap_no=6)
    live.tick()
    assert store.conn.execute("SELECT COUNT(*) FROM live_corners").fetchone()[0] == 0


def test_off_track_this_lap_is_the_newest_lap_that_never_completed(tmp_path, synthetic_lap):
    """Lap 4 crashed (never committed); lap 5 ran and committed. Off track, 'this lap'
    is lap 4 -- a newer lap's superseded rows must not hide it."""
    store = _store(tmp_path)
    live = _worker(store)
    crashed = _Source(lap_no=4)
    crashed.meta["start_ts"] = START_TS - 300
    live.source = crashed
    crashed.drive(synthetic_lap, 1300)
    live.tick()
    live.source = src = _Source(lap_no=5)
    src.drive(synthetic_lap, 1300)
    live.tick()
    store.save(dataclasses.replace(synthetic_lap, session_id=BASE, phase="race", lap_no=5,
                                   timestamp=START_TS + 92.5))
    out = _ctx(store, on_track=False).dispatch("get_corner", {"corner": "1",
                                                              "which": "this lap"})
    assert "game lap 4" in out and "never completed" in out


def test_a_committed_lap_from_another_phase_does_not_supersede(tmp_path, synthetic_lap):
    store = _store(tmp_path)
    live = _worker(store)
    live.source = src = _Source(lap_no=5)
    src.drive(synthetic_lap, 1300)
    live.tick()
    store.save(dataclasses.replace(synthetic_lap, session_id=BASE, phase="qualifying",
                                   lap_no=5, timestamp=START_TS + 600))
    assert [r["label"] for r in store.live_lap_corners(BASE)[1]] == ["1"]


def test_car_status_reports_an_impact_mid_lap_and_a_detached_wheel(tmp_path, synthetic_lap):
    lap = synthetic_lap
    n = len(lap.channels["lap_distance"])
    ch = dict(lap.channels)
    et = np.full(n, 10.0)
    hit = int(np.searchsorted(ch["lap_distance"], 1000.0))
    et[hit:] = 150.0                                     # a NEW impact at ~1000 m (T1)
    ch.update({
        "elapsed_time": np.linspace(100.0, 192.5, n), "lap_start_et": np.full(n, 100.0),
        "last_impact_et": et, "last_impact_mag": np.where(et > 10, 365.0, 20.0),
        "last_impact_x": np.full(n, -1.0), "last_impact_z": np.full(n, 1.7),
        "dent_max": np.where(et > 10, 1.0, 0.0), "dent_total": np.where(et > 10, 1.0, 0.0),
        "detached": np.zeros(n), "overheating": np.zeros(n),
    })
    for w in ("fl", "fr", "rl", "rr"):
        ch[f"flat_{w}"] = np.zeros(n)
        ch[f"wheel_detached_{w}"] = np.zeros(n)
    ch["wheel_detached_fl"] = np.where(et > 10, 1.0, 0.0)
    store = _store(tmp_path)
    live = _worker(store)
    live.source = src = _Source()
    src.drive(dataclasses.replace(lap, channels=ch), 1500)
    ctx = _ctx(store, on_track=True, live=live)
    out = ctx.dispatch("get_car_status", {})
    assert "365" in out and "front-left" in out and "T1" in out
    assert "FL wheel detached" in out
    assert "game lap 5" in out


def test_live_tools_are_gated_to_the_live_session(tmp_path, synthetic_lap):
    store = _store(tmp_path)
    live = _worker(store)
    live.source = src = _Source()
    src.drive(synthetic_lap, 500)                        # on track, no corner done yet
    # Off track, even a status left over from a buffer is not read (the gate is the
    # live session, not whether a status string happens to exist).
    ctx = _ctx(store, on_track=False, live=live)
    assert "no live session" in ctx.dispatch("get_car_status", {}).lower()
    ctx = _ctx(store, on_track=True, live=live)
    out = ctx.dispatch("get_corner", {"corner": "1", "which": "this lap"})
    assert "not captured" in out.lower() and "game lap 5" in out
    # Live, the in-progress lap is the only one "this lap" reads -- never an older
    # crashed lap's provisional rows.
    old = _Source(lap_no=2)
    old.meta["start_ts"] = START_TS - 3600
    live.source = old
    old.drive(synthetic_lap, 1300)
    live.tick()
    out = ctx.dispatch("get_corner", {"corner": "1", "which": "this lap"})
    assert "game lap 2" not in out and "game lap 5" in out


def test_stint_events_include_this_lap_so_far(tmp_path, synthetic_lap):
    store = _store(tmp_path)
    live = _worker(store)
    live.source = src = _Source()
    src.drive(synthetic_lap, 1300)
    live.tick()
    ctx = _ctx(store, on_track=True, live=live)
    out = ctx.dispatch("get_stint_events", {})
    assert "this lap so far" in out.lower() and "provisional" in out.lower()
    assert "T1" in out


def test_a_worker_failure_never_reaches_capture(tmp_path, synthetic_lap, caplog):
    store = _store(tmp_path)
    app = create_app(store, Coach(FakeModel()))
    live = app.state.live_lap
    live.source = src = _Source()
    src.drive(synthetic_lap, 1300)

    def boom(*a, **k):
        raise RuntimeError("disk full")
    store.save_live_corner = boom
    live.step()                                          # logged, never raised
    assert "live corner" in caplog.text

    class _OneLap:
        live_lap = None

        def laps(self):
            yield dataclasses.replace(synthetic_lap, session_id=BASE)
    _capture_loop(_OneLap(), store, app.state)
    assert app.state.latest is not None
    assert live.source is not None and live.source.live_lap is None


def test_poll_once_exposes_the_in_progress_lap_buffer():
    """LmuSource publishes (meta, its own buffer) once per lap: no copy per poll. The
    in-progress lap's number is mTotalLaps + 1 -- mTotalLaps counts laps FINISHED,
    which is what a committed lap's lap_no is (#77)."""
    import mmap

    from simcoach.lmu import smm
    from simcoach.lmu.rf2structs import rF2Scoring, rF2Telemetry

    tag = uuid.uuid4().hex
    tn, sn = f"$simcoach_test_telem_{tag}$", f"$simcoach_test_scoring_{tag}$"
    tmap = mmap.mmap(-1, C.sizeof(rF2Telemetry), tagname=tn, access=mmap.ACCESS_WRITE)
    smap = mmap.mmap(-1, C.sizeof(rF2Scoring), tagname=sn, access=mmap.ACCESS_WRITE)

    def write(dist, last_lap, total_laps):
        t = rF2Telemetry(); t.mVersionUpdateBegin = t.mVersionUpdateEnd = 1; t.mNumVehicles = 1
        v = t.mVehicles[0]; v.mID = 1; v.mLocalVel.z = -50.0
        v.mTrackName = b"Test Circuit"; v.mVehicleName = b"Car"
        s = rF2Scoring(); s.mScoringInfo.mVersionUpdateBegin = s.mScoringInfo.mVersionUpdateEnd = 1
        s.mScoringInfo.mNumVehicles = 1; s.mScoringInfo.mSession = 11
        pv = s.mVehicles[0]; pv.mID = 1; pv.mIsPlayer = True; pv.mLapDist = dist
        pv.mLastLapTime = last_lap; pv.mTotalLaps = total_laps
        tmap.seek(0); tmap.write(bytes(t)); smap.seek(0); smap.write(bytes(s))

    src = smm.LmuSource(poll_hz=60, now_fn=lambda: START_TS)
    assert src.live_lap is None
    for d in (5.0, 900.0, 2000.0):
        write(d, -1.0, 3)
        src._poll_once(tn, sn)
    meta, ch = src.live_lap
    assert ch is src._channels and len(ch["lap_distance"]) == 3
    assert meta == {"track": "Test Circuit", "car": "Car", "phase": "race",
                    "session_id": "test-circuit--car--offline", "lap_no": 4,
                    "start_ts": START_TS}
    write(3.0, 92.5, 4)                                  # the line: lap 4 commits
    assert src._poll_once(tn, sn).lap_no == 4
    meta, ch = src.live_lap
    assert meta["lap_no"] == 5 and ch is src._channels and len(ch["lap_distance"]) == 1
    tmap.close(); smap.close()
