"""#180: a race review reads the race's own racing laps -- never qualifying or race
lap 0 (the grid/garage segments and the formation lap before the green flag).

The shape is the real Sarthe session (chat-20260926T200354-7469b6 #279): practice
out-lap, qualy out-lap + timed lap 2, three race-lap-0 pit segments, the race-lap-0
formation lap (P6 -> P9), race laps 1-4 timed, race lap 5 partial (#181)."""
import dataclasses

import numpy as np

from simcoach.coach_tools import CoachContext
from simcoach.corners import CornerMap
from simcoach.store import LapStore

SID = "sarthe--online"
T0 = 1_790_000_000.0


def _wear_step(base, wheel):
    ch = dict(base.channels)
    d = np.asarray(ch["lap_distance"], float)
    wear = np.asarray(ch[f"tire_wear_{wheel}"], float).copy()
    wear[int(np.argmin(np.abs(d - 1000.0))):] -= 0.09
    ch[f"tire_wear_{wheel}"] = wear
    return dataclasses.replace(base, channels=ch)


def _sarthe(tmp_path, synthetic_lap):
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    # (phase, lap_no, partial, pit, position, time, event wheel, ts offset)
    shape = [
        ("practice", 1, "out", True, 14, None, None, 0),
        ("qualifying", 1, "out", True, 15, None, None, 540),
        ("qualifying", 2, None, False, 12, 246.5, "rl", 790),     # qualy event
        ("race", 0, "partial", True, 6, None, None, 940),
        ("race", 0, "partial", True, 6, None, None, 971),
        ("race", 0, "partial", True, 6, None, None, 990),
        ("race", 0, "partial", False, 9, None, "fr", 1124),      # formation lap
        ("race", 1, None, False, 5, 251.0, None, 1375),
        ("race", 2, None, False, 4, 243.6, "fl", 1619),
        ("race", 3, None, False, 3, 243.0, None, 1862),
        ("race", 4, None, False, 2, 242.4, "rr", 2104),
        ("race", 5, "partial", False, 2, None, None, 2346),
    ]
    for phase, lap_no, partial, pit, pos, t, wheel, dt in shape:
        lap = _wear_step(synthetic_lap, wheel) if wheel else synthetic_lap
        store.save(dataclasses.replace(
            lap, session_id=SID, phase=phase, lap_no=lap_no, partial=partial, pit=pit,
            position=pos, lap_time=t, valid=t is not None, timestamp=T0 + dt))
    return CoachContext(store, cmap=CornerMap.load("simcoach/tracks/test_track.json"),
                        track=synthetic_lap.track, car=synthetic_lap.car, session_id=SID)


def _laps(out):
    return [line.split(" (game lap")[0].removeprefix("Lap: ")
            for line in out.splitlines() if line.startswith("Lap: ")]


def test_race_scoped_stint_events_read_only_the_racing_laps(tmp_path, synthetic_lap):
    out = _sarthe(tmp_path, synthetic_lap).dispatch("get_stint_events",
                                                    {"laps": 10, "phase": "race"})
    # The race window holds laps 0..5; lap 0 is dropped, the partial lap 5 stays
    # (it is in the window -- whether it is a racing lap at all is #181).
    assert _laps(out) == [f"race lap {n}" for n in range(1, 6)]
    assert "FL wear step" in out and "RR wear step" in out
    assert "RL wear step" not in out     # qualifying lap 2
    assert "FR wear step" not in out     # the formation lap
    # Race lap 1's position is not measured against the formation lap's P9.
    assert "from P9" not in out and "P9" not in out


def test_race_scope_defaults_to_the_whole_race(tmp_path, synthetic_lap):
    out = _sarthe(tmp_path, synthetic_lap).dispatch("get_stint_events", {"phase": "race"})
    assert _laps(out)[0] == "race lap 1"


def test_unscoped_stint_events_read_the_current_phase(tmp_path, synthetic_lap):
    """'that spin a few laps ago' is not a race review: race lap 0 stays in. #212: but
    the unscoped scan is the current phase only -- qualy is reached by phase=."""
    ctx = _sarthe(tmp_path, synthetic_lap)
    out = ctx.dispatch("get_stint_events", {"laps": 10})
    labels = _laps(out)
    assert labels[0] == "race lap 0" and labels.count("race lap 0") == 4
    assert "RL wear step" not in out and "FR wear step" in out
    # ...but a race lap 0 still carries no position: the formation lap's P9 is not one.
    assert "P9" not in out
    qualy = ctx.dispatch("get_stint_events", {"phase": "qualifying"})
    assert _laps(qualy) == ["qualifying lap 1", "qualifying lap 2"]
    assert "RL wear step" in qualy


def test_race_result_positions_start_at_race_lap_1(tmp_path, synthetic_lap):
    ctx = _sarthe(tmp_path, synthetic_lap)
    out = ctx.get_race_result()
    assert ("over 5 captured lap(s): P5 overall on the first captured lap, as low as "
            "P5 overall, P2 overall on the most recent") in out
    assert "P6" not in out and "P9" not in out
    assert "low P5 overall" in ctx.get_session_status()


def test_lap_list_shows_no_position_on_race_lap_0(tmp_path, synthetic_lap):
    listing = _sarthe(tmp_path, synthetic_lap).list_recent_laps(limit=20)
    lap0 = [line for line in listing.splitlines() if line.startswith("race lap 0")]
    assert len(lap0) == 4 and not any("overall" in line for line in lap0)
    assert "race lap 1: 4:11.000 valid P5 overall" in listing
    assert "qualifying lap 2" in listing     # the list itself is not race-scoped


def test_no_race_in_session_says_so(tmp_path, synthetic_lap):
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    store.save(dataclasses.replace(synthetic_lap, session_id="q", phase="qualifying"))
    ctx = CoachContext(store, cmap=CornerMap.load("simcoach/tracks/test_track.json"),
                       track=synthetic_lap.track, car=synthetic_lap.car, session_id="q")
    assert ctx.get_stint_events(phase="race").startswith("No race in this session")


def _race(store, synthetic_lap, n, t0, lap0=True):
    rows = ([(0, "partial", 6)] if lap0 else []) + [(i, None, 5) for i in range(1, n + 1)]
    for k, (lap_no, partial, pos) in enumerate(rows):
        store.save(dataclasses.replace(
            synthetic_lap, session_id=SID, phase="race", lap_no=lap_no, partial=partial,
            position=pos, lap_time=None if partial else 90.0, valid=not partial,
            timestamp=t0 + 100 * k))


def _ctx(store, synthetic_lap):
    return CoachContext(store, cmap=CornerMap.load("simcoach/tracks/test_track.json"),
                        track=synthetic_lap.track, car=synthetic_lap.car, session_id=SID)


def test_race_scope_ignores_laps_and_reads_the_whole_race(tmp_path, synthetic_lap):
    """Red-team round 1: laps=10 on a 25-lap race returned laps 16-25 -- the start lost."""
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    _race(store, synthetic_lap, 14, T0)
    ctx = _ctx(store, synthetic_lap)
    for args in ({"phase": "race"}, {"laps": 10, "phase": "race"}):
        out = ctx.dispatch("get_stint_events", args)
        assert _laps(out) == [f"race lap {n}" for n in range(1, 15)], args
        assert "(last " not in out


def test_race_scope_says_when_the_cap_cuts(tmp_path, synthetic_lap):
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    _race(store, synthetic_lap, 32, T0, lap0=False)
    out = _ctx(store, synthetic_lap).dispatch("get_stint_events", {"phase": "race"})
    assert out.startswith("(last 30 of 32 race laps)")
    assert _laps(out)[0] == "race lap 3" and len(_laps(out)) == 30


def test_race_scope_reads_only_the_latest_race(tmp_path, synthetic_lap):
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    _race(store, synthetic_lap, 3, T0)
    _race(store, synthetic_lap, 2, T0 + 3600)      # > _RACE_WINDOW_GAP_S later
    out = _ctx(store, synthetic_lap).dispatch("get_stint_events", {"phase": "race"})
    assert _laps(out) == ["race 2 lap 1", "race 2 lap 2"]


def test_race_scope_on_the_grid_says_the_race_has_not_started(tmp_path, synthetic_lap):
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    _race(store, synthetic_lap, 0, T0)
    out = _ctx(store, synthetic_lap).get_stint_events(phase="race")
    assert out.startswith("The race hasn't started yet")


def test_race_lap_0_is_labelled_not_racing_everywhere(tmp_path, synthetic_lap):
    """Safety net when the model skips phase='race'."""
    ctx = _sarthe(tmp_path, synthetic_lap)
    out = ctx.dispatch("get_stint_events", {"laps": 10})
    lap0 = [line for line in out.splitlines() if line.startswith("Lap: race lap 0")]
    assert len(lap0) == 4 and all("not racing" in line for line in lap0)
    listing = ctx.list_recent_laps(limit=20)
    assert sum("not racing" in line for line in listing.splitlines()) == 4
    assert "not racing" not in ctx.get_lap_summary("race lap 1").splitlines()[0]


def test_race_result_lists_the_race_laps_positions(tmp_path, synthetic_lap):
    out = _sarthe(tmp_path, synthetic_lap).get_race_result()
    assert ("Per-lap positions (overall) from race lap 1: race lap 1 P5, race lap 2 P4, "
            "race lap 3 P3, race lap 4 P2, race lap 5 P2.") in out
    assert out.endswith("get_stint_events with phase='race'.")
