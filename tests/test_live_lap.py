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
    # The committed record carries the provisional lap's exact key (capture knows it),
    # so the commit deletes exactly those rows -- no scan, no lap-number guess.
    lap = dataclasses.replace(synthetic_lap, session_id=BASE, phase="race", lap_no=5,
                              timestamp=START_TS + 92.5, live_meta=src.meta)
    store.save(lap)
    assert store.live_lap_corners(BASE)[1] == []
    assert store.conn.execute("SELECT COUNT(*) FROM live_corners").fetchone()[0] == 0
    # A worker write that lands AFTER the commit (it snapshotted just before the line)
    # is dropped, not resurrected.
    src.drive(synthetic_lap, 2500)
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
                                   timestamp=START_TS + 92.5, live_meta=src.meta))
    out = _ctx(store, on_track=False).dispatch("get_corner", {"corner": "1",
                                                              "which": "this lap"})
    assert "game lap 4" in out and "never completed" in out


def test_only_the_lap_itself_supersedes_its_rows(tmp_path, synthetic_lap):
    """Same session, phase and game lap number is NOT the same lap: a crashed lap 5,
    then a lap 5 driven again (mTotalLaps didn't advance) that commits, must not hide
    the crash. Nor does another phase's lap 5, nor a stored partial numbered
    mTotalLaps rather than +1."""
    store = _store(tmp_path)
    live = _worker(store)
    live.source = crashed = _Source(lap_no=5)
    crashed.drive(synthetic_lap, 1300)
    live.tick()
    live.source = again = _Source(lap_no=5)
    again.meta["start_ts"] = START_TS + 120
    again.drive(synthetic_lap, 1300)
    live.tick()
    for phase, no in (("race", 5), ("qualifying", 5), ("race", 4)):
        store.save(dataclasses.replace(synthetic_lap, session_id=BASE, phase=phase, lap_no=no,
                                       timestamp=START_TS + 300))
    store.save(dataclasses.replace(synthetic_lap, session_id=BASE, phase="race", lap_no=5,
                                   timestamp=START_TS + 212, live_meta=again.meta))
    meta, rows = store.live_lap_corners(BASE)
    assert meta["start_ts"] == START_TS and [r["label"] for r in rows] == ["1"]


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
    # Positive only, and marked: flat_*/wheel_detached_* are not rig-verified (#143).
    assert "FL wheel detached (unverified channel)" in out
    assert "game lap 5" in out


def test_car_status_never_states_a_negative_from_an_unverified_channel(tmp_path,
                                                                        synthetic_lap):
    lap = synthetic_lap
    n = len(lap.channels["lap_distance"])
    ch = dict(lap.channels)
    ch.update({"elapsed_time": np.linspace(100.0, 192.5, n),
               "last_impact_et": np.zeros(n), "last_impact_mag": np.zeros(n),
               "last_impact_x": np.zeros(n), "last_impact_z": np.zeros(n),
               "dent_max": np.zeros(n), "dent_total": np.zeros(n),
               "detached": np.zeros(n), "overheating": np.zeros(n)})
    for w in ("fl", "fr", "rl", "rr"):
        ch[f"flat_{w}"] = np.zeros(n)
        ch[f"wheel_detached_{w}"] = np.zeros(n)
    store = _store(tmp_path)
    live = _worker(store)
    live.source = src = _Source()
    src.drive(dataclasses.replace(lap, channels=ch), 1500)
    out = _ctx(store, on_track=True, live=live).dispatch("get_car_status", {}).lower()
    for claim in ("no flat", "not overheating", "nothing detached", "no detached"):
        assert claim not in out
    assert "no impacts since the start of this lap" in out    # rig-confirmed channel
    assert "dent severity max 0" in out                       # rig-confirmed channel


def test_car_status_mentions_a_hit_just_before_the_line(tmp_path, synthetic_lap):
    """Right after the line the new buffer holds no rising edge for a hit taken in the
    last seconds of the lap just completed -- its first sample still carries it."""
    lap = synthetic_lap
    n = len(lap.channels["lap_distance"])
    ch = dict(lap.channels)
    ch.update({"elapsed_time": np.linspace(200.0, 292.5, n),
               "last_impact_et": np.full(n, 198.0), "last_impact_mag": np.full(n, 410.0),
               "last_impact_x": np.full(n, 1.0), "last_impact_z": np.full(n, -1.5),
               "dent_max": np.ones(n), "dent_total": np.ones(n),
               "detached": np.zeros(n), "overheating": np.zeros(n)})
    store = _store(tmp_path)
    live = _worker(store)
    live.source = src = _Source()
    src.drive(dataclasses.replace(lap, channels=ch), 20)       # just past the line
    out = _ctx(store, on_track=True, live=live).dispatch("get_car_status", {})
    assert "lap just completed" in out and "410" in out and "rear-right" in out


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


def _spin_lap():
    """180 km/h down a straight to ~1500 m -- between T1 (800-1200) and T2 (2000-2400)
    -- then 200 deg of rotation while speed collapses to 30: a spin OUTSIDE every
    corner window (analysis' own spin shape, test_analysis._path_lap)."""
    from simcoach.analysis import _SPEED_KMH_TO_MS
    hz = 50.0
    v_parts, h_parts, heading = [], [], 0.0
    for dur, turn, v0, v1 in ((30.0, 0.0, 180.0, 180.0), (2.0, 200.0, 180.0, 30.0),
                              (4.0, 0.0, 30.0, 30.0)):
        k = round(dur * hz)
        v_parts.append(np.linspace(v0, v1, k))
        h_parts.append(heading + np.linspace(0.0, turn, k))
        heading += turn
    v = np.concatenate(v_parts)
    h = np.radians(np.concatenate(h_parts))
    t = np.arange(v.size) / hz
    ds = np.r_[0.0, v[1:] * _SPEED_KMH_TO_MS / hz]
    z = np.zeros(v.size)
    ch = {"lap_distance": np.cumsum(ds), "time": t, "speed": v,
          "pos_x": np.cumsum(ds * np.cos(h)), "pos_z": np.cumsum(ds * np.sin(h)),
          "throttle": np.ones(v.size), "brake": z, "steering": z}
    from simcoach.model_types import LapRecord
    return LapRecord("lmu", "test_track", "test_car", None, False, 0.0, ch)


def test_a_spin_between_corner_windows_is_reported(tmp_path):
    spin = _spin_lap()
    store = _store(tmp_path)
    live = _worker(store)
    live.source = src = _Source()
    src.drive(spin, 1e9)
    live.tick()
    # On track: scanned over the WHOLE buffer at read time.
    out = _ctx(store, on_track=True, live=live).dispatch("get_stint_events", {})
    assert "spin" in out and "between T1 Left Hairpin and T2 Right Sweeper" in out
    assert "no incidents" not in out
    # Off track (crash, never committed): the persisted lap-level scan still has it.
    out = _ctx(store, on_track=False).dispatch("get_stint_events", {})
    assert "spin" in out and "between T1 Left Hairpin and T2 Right Sweeper" in out
    assert "no incidents" not in out


def test_no_incidents_is_said_only_after_a_whole_buffer_scan(tmp_path, synthetic_lap):
    store = _store(tmp_path)
    live = _worker(store)
    live.source = src = _Source()
    src.drive(synthetic_lap, 1300)
    live.tick()
    out = _ctx(store, on_track=True, live=live).dispatch("get_stint_events", {})
    assert "no incidents so far (whole lap scanned to 1,297 m)" in out   # last sample


def test_supersede_and_reads_stay_cheap_at_scale(tmp_path, synthetic_lap):
    """10k provisional rows (a long history of abandoned laps): the commit's delete,
    the worker's age-out and the reads are index lookups, never a table scan."""
    import time as _time
    store = _store(tmp_path)
    rows = [(BASE, "race", i, START_TS - 10_000 + i, lbl, j, 0.0, "{}")
            for i in range(2500) for j, lbl in enumerate(("1", "2", "3", "_lap"))]
    with store.conn:
        store.conn.executemany("INSERT INTO live_corners VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                               rows)

    def best(fn):
        out = []
        for _ in range(5):
            t0 = _time.perf_counter(); fn(); out.append(_time.perf_counter() - t0)
        return min(out)
    meta = {"session_id": BASE, "phase": "race", "lap_no": 1234,
            "start_ts": START_TS - 10_000 + 1234}
    assert best(lambda: store.supersede_live_lap(meta)) < 0.010
    assert best(lambda: store.age_out_live_corners(START_TS - 10_000 + 5)) < 0.010
    assert best(lambda: store.live_lap_corners(BASE)) < 0.010
    assert best(lambda: store.live_lap_corners(BASE, 2000, START_TS - 8000)) < 0.010


def test_the_worker_ages_out_day_old_rows_on_a_new_lap(tmp_path, synthetic_lap):
    store = _store(tmp_path)
    live = _worker(store)
    for lap_no, age in ((1, 90_000), (2, 3_600)):
        old = _Source(lap_no=lap_no)
        old.meta["start_ts"] = START_TS - age
        live.source = old
        old.drive(synthetic_lap, 1300)
        live.tick()
    live.source = new = _Source(lap_no=3)
    new.drive(synthetic_lap, 100)
    live.tick()
    left = {r[0] for r in store.conn.execute("SELECT lap_no FROM live_corners")}
    assert left == {2, 3}


def test_now_refuses_a_buffer_from_another_session(tmp_path, synthetic_lap):
    store = _store(tmp_path)
    live = _worker(store)
    live.source = src = _Source()
    src.drive(synthetic_lap, 1300)
    assert live.now({"state": "track"}, live=("test_track", "test_car"),
                    session_id=BASE)[0] is not None
    assert live.now({"state": "track"}, live=("Other Track", "test_car"),
                    session_id=BASE) == (None, None)
    assert live.now({"state": "track"}, live=("test_track", "test_car"),
                    session_id="other--test-car--online") == (None, None)


def test_a_buffer_spanning_a_session_restart_is_not_analysed(tmp_path, synthetic_lap):
    """Menu -> a new session with no wrap appends to the old buffer: the game clock
    (elapsed_time) goes BACKWARDS. Nothing mixed is analysed or reported."""
    lap = synthetic_lap
    n = len(lap.channels["lap_distance"])
    et = np.linspace(500.0, 592.5, n)
    et[n // 4:] -= 480.0
    store = _store(tmp_path)
    live = _worker(store)
    live.source = src = _Source()
    src.drive(dataclasses.replace(lap, channels={**lap.channels, "elapsed_time": et}), 2500)
    live.tick()
    assert store.conn.execute("SELECT COUNT(*) FROM live_corners").fetchone()[0] == 0
    assert live.now({"state": "track"}) == (None, None)
    out = _ctx(store, on_track=True, live=live).dispatch("get_car_status", {})
    assert "isn't readable yet" in out


def test_a_map_that_arrives_later_is_picked_up(tmp_path, synthetic_lap):
    store = _store(tmp_path)
    maps = [None]
    live = LiveLap(store, lambda track: maps[0])
    live.source = src = _Source()
    src.drive(synthetic_lap, 1300)
    live.tick()
    maps[0] = CornerMap.load(CMAP)            # saved from the map editor meanwhile
    live.tick()
    assert [r["label"] for r in store.live_lap_corners(BASE)[1]] == ["1"]


def test_this_lap_aliases_and_section_tags(tmp_path, synthetic_lap):
    cmap = CornerMap.load(CMAP)
    cmap.sections = [{"name": "The Loop", "start_dist": 700, "end_dist": 1300}]
    store = _store(tmp_path)
    live = LiveLap(store, lambda track: cmap)
    live.source = src = _Source()
    src.drive(synthetic_lap, 1300)
    live.tick()
    for which in ("this", "now", "current"):
        out = _ctx(store, on_track=True, live=live, cmap=cmap).dispatch(
            "get_corner", {"corner": "1", "which": which})
        assert "provisional" in out and "[part of The Loop]" in out


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
    first = meta
    write(3.0, 92.5, 4)                                  # the line: lap 4 commits
    rec = src._poll_once(tn, sn)
    assert rec.lap_no == 4 and rec.live_meta is first    # its provisional rows' exact key
    meta, ch = src.live_lap
    assert meta["lap_no"] == 5 and ch is src._channels and len(ch["lap_distance"]) == 1
    # A partial (a spin, a garage return) is numbered mTotalLaps, not +1 -- it still
    # carries its own provisional key, so it never shows twice.
    for i in range(250):
        write(3.0 + 4.0 * i, 92.5, 4)
        src._poll_once(tn, sn)
    fifth = src.live_lap[0]
    write(1.0, 92.5, 4)                                  # wrap, stale time -> partial
    rec = src._poll_once(tn, sn)
    assert rec.partial == "partial" and rec.live_meta is fifth
    tmap.close(); smap.close()
