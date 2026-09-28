"""#187: in a multi-race session, race review reaches an earlier race -- race=N on
get_stint_events, get_race_result's positions tail + pointer name the race its result
describes, and race lap 1 starts from the result log's grid slot.

The shape is the real Algarve LGC #12 session (2026-09-24/25, online): practice
out-lap, qualy, race 1 (three lap-0 grid/formation rows, laps 1-9 with lap 6 an
untimed partial and a lap-9 in-lap fragment), then practice + qualy again and race 2
(two lap-0 rows, laps 1-9). Positions, lap numbers and timestamps (relative) are the
DB's; grid slots are the result XMLs' <GridPos> (race 1: 2, race 2: 3)."""
import dataclasses

import pytest

from simcoach.coach_tools import CoachContext
from simcoach.corners import CornerMap
from simcoach.lmu.results import ResultSession
from simcoach.store import LapStore

SID = "algarve-international-circuit--logitech-g-challenge-12-lgc--online"
T0 = 1_790_310_678.68

# (ts offset, phase, lap_no, partial, position, lap time)
_ROWS = [
    (-782.8, "practice", 1, "out", 5, None),
    (-749.4, "qualifying", 0, "in", 4, None),
    (-613.4, "qualifying", 1, "out", 5, None),
    (-506.6, "qualifying", 2, None, 4, 106.786),
    (-400.2, "qualifying", 3, None, 1, 106.488),
    (-293.8, "qualifying", 4, None, 2, 106.381),
    (-187.6, "qualifying", 5, None, 2, 106.237),
    (-80.0, "qualifying", 6, None, 2, 107.504),
    (0.0, "race", 0, "partial", 2, None),
    (18.6, "race", 0, "partial", 2, None),
    (91.0, "race", 0, "partial", 2, None),
    (203.4, "race", 1, None, 2, 112.345),
    (309.6, "race", 2, None, 2, 106.289),
    (416.6, "race", 3, None, 2, 106.867),
    (523.2, "race", 4, None, 2, 106.762),
    (629.6, "race", 5, None, 2, 106.4),
    (736.0, "race", 6, "partial", 2, None),
    (842.8, "race", 7, None, 2, 106.713),
    (950.6, "race", 8, None, 2, 107.773),
    (1058.0, "race", 9, None, 2, 107.496),
    (1063.2, "race", 9, "in", 2, None),
    (1927.2, "practice", 1, "out", 5, None),
    (1949.1, "qualifying", 0, "in", 4, None),
    (2087.5, "qualifying", 1, "out", 3, None),
    (2194.5, "qualifying", 2, None, 2, 106.921),
    (2300.5, "qualifying", 3, None, 3, 106.188),
    (2407.5, "qualifying", 4, None, 3, 106.978),
    (2514.5, "qualifying", 5, None, 3, 106.983),
    (2622.1, "qualifying", 6, None, 3, 107.618),
    (2711.7, "race", 0, "partial", 3, None),
    (2820.7, "race", 0, "partial", 3, None),
    (2934.3, "race", 1, None, 2, 113.486),
    (3041.3, "race", 2, None, 2, 107.018),
    (3147.1, "race", 3, None, 2, 105.886),
    (3253.5, "race", 4, None, 2, 106.426),
    (3359.9, "race", 5, None, 1, 106.356),
    (3466.7, "race", 6, None, 1, 106.728),
    (3572.7, "race", 7, None, 1, 105.992),
    (3678.9, "race", 8, None, 1, 106.323),
    (3786.5, "race", 9, None, 1, 107.471),
]

# The rows stored with pit set (ts offsets): out-laps, and the garage/grid segments
# of race lap 0 -- set on a grid start too, so pit on a lap-0 row is no pit-lane start.
_PIT_AT = {-782.8, -613.4, 0.0, 18.6, 1927.2, 2087.5, 2711.7}

# The two race result logs: (race_sessions ts, logged_ts, finish, grid, class grid)
# off T0.
_R1 = (-36.68, 1169.19, 2, 2, 2)
_R2 = (2687.32, 3885.03, 1, 3, 3)

RACE2_TAIL = ("Per-lap positions (overall) from race lap 1: race 2 lap 1 P2, race 2 lap 2 "
              "P2, race 2 lap 3 P2, race 2 lap 4 P2, race 2 lap 5 P1, race 2 lap 6 P1, "
              "race 2 lap 7 P1, race 2 lap 8 P1, race 2 lap 9 P1.")


def _lgc12(tmp_path, synthetic_lap, logs=(), rows=_ROWS, sid=SID, t0=T0, pit_at=_PIT_AT):
    """`logs`: (result, logged_ts or None) pairs to import. A logged_ts of None is a
    row whose write time is unknown -- the only way the latest result can be race 1's
    while race 2 is stored (a known write time before race 2's laps refuses it)."""
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    for dt, phase, lap_no, partial, pos, t in rows:
        store.save(dataclasses.replace(
            synthetic_lap, session_id=sid, phase=phase, lap_no=lap_no, partial=partial,
            pit=dt in pit_at, position=pos, lap_time=t, valid=t is not None,
            timestamp=t0 + dt))
    for (ts, _, finish, grid, class_grid), logged in logs:
        store.save_result_session(ResultSession(
            sim="lmu", track=synthetic_lap.track, car=synthetic_lap.car, car_class="GT3",
            date_ts=t0 + ts, session_type="race", position=finish, class_position=finish,
            best_lap=None, finish_status="Finished Normally", laps=[], incidents=[],
            penalties=[], grid_position=grid, class_grid_position=class_grid, online=True),
            session_id=f"result-{ts}", logged_ts=None if logged is None else t0 + logged)
    return CoachContext(store, cmap=CornerMap.load("simcoach/tracks/test_track.json"),
                        track=synthetic_lap.track, car=synthetic_lap.car, session_id=sid)


def _laps(out):
    return [line.split(" (game lap")[0].removeprefix("Lap: ")
            for line in out.splitlines() if line.startswith("Lap: ")]


def _tail(out):
    return next(line for line in out.splitlines() if line.startswith("Per-lap positions"))


def _pos(ctx, which):
    head = ctx.get_lap_summary(which).splitlines()[0]
    assert head.startswith(f"Lap: {which} "), head
    return head.split("position ", 1)[1]


# -- 1. race selector ------------------------------------------------------------

@pytest.mark.parametrize("args", [{"phase": "race", "race": 1}, {"race": 1}])
def test_race_selector_reads_the_earlier_race(tmp_path, synthetic_lap, args):
    out = _lgc12(tmp_path, synthetic_lap).dispatch("get_stint_events", args)
    labels = _laps(out)
    assert labels[0] == "race 1 lap 1" and "race 1 lap 8" in labels
    assert all(label.startswith("race 1 lap ") for label in labels), labels
    assert "race 1 lap 0" not in labels


def test_race_selector_default_and_race_2_are_the_latest_race(tmp_path, synthetic_lap):
    ctx = _lgc12(tmp_path, synthetic_lap)
    want = [f"race 2 lap {n}" for n in range(1, 10)]
    assert _laps(ctx.dispatch("get_stint_events", {"phase": "race"})) == want
    assert _laps(ctx.dispatch("get_stint_events", {"phase": "race", "race": 2})) == want


def test_race_selector_names_a_race_the_session_does_not_hold(tmp_path, synthetic_lap):
    ctx = _lgc12(tmp_path, synthetic_lap)
    for bad in (3, 0, "first", 1.5, True, 10 ** 400):
        out = ctx.dispatch("get_stint_events", {"phase": "race", "race": bad})
        assert out == (f"No race {bad} in this session: it holds 2 races, race 1 to "
                       "race 2."), out
    assert _laps(ctx.dispatch("get_stint_events", {"race": 1.0}))[0] == "race 1 lap 1"


def test_race_selector_on_a_single_race_session(tmp_path, synthetic_lap):
    ctx = _lgc12(tmp_path, synthetic_lap, rows=_ROWS[:21])
    out = ctx.dispatch("get_stint_events", {"phase": "race", "race": 2})
    assert out == "No race 2 in this session: it holds 1 race."


# -- 2 + 3. the positions tail and its pointer read the race the result describes ----

def test_race_result_tail_is_race_1_when_its_log_is_race_1s(tmp_path, synthetic_lap):
    """Race 1's log (write time unknown) with race 2 stored unlogged: the result, the
    tail and the pointer all describe race 1. Kills 'always the latest window'."""
    out = _lgc12(tmp_path, synthetic_lap, logs=[(_R1, None)]).get_race_result()
    assert "Race 1 of this session" in out and "Started P2 overall on the grid" in out
    tail = _tail(out)
    assert tail.startswith("Per-lap positions (overall) from race lap 1: race 1 lap 1 P2,")
    assert "race 1 lap 9 P2" in tail
    assert "race 2" not in tail and "lap 0" not in tail
    assert out.endswith("get_stint_events with phase='race', race=1.")


@pytest.mark.parametrize("logs", [
    [(_R1, _R1[1]), (_R2, _R2[1])],    # both logs imported (the real DB)
    [(_R1, _R1[1])],                   # race 2 not logged yet: race 1's log is refused
    [],                                # no log at all
], ids=["both-logged", "race-2-unlogged", "no-logs"])
def test_race_result_tail_is_race_2_otherwise(tmp_path, synthetic_lap, logs):
    """Kills 'phase == race only' (every race's laps in one tail)."""
    out = _lgc12(tmp_path, synthetic_lap, logs=logs).get_race_result()
    assert _tail(out) == RACE2_TAIL
    assert "race 1 lap" not in out
    assert out.endswith("get_stint_events with phase='race', race=2.")


def test_single_race_pointer_is_unchanged(tmp_path, synthetic_lap):
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    for dt, phase, lap_no, partial, pos, t in _ROWS[:21]:     # through race 1
        store.save(dataclasses.replace(
            synthetic_lap, session_id=SID, phase=phase, lap_no=lap_no, partial=partial,
            position=pos, lap_time=t, valid=t is not None, timestamp=T0 + dt))
    ctx = CoachContext(store, cmap=CornerMap.load("simcoach/tracks/test_track.json"),
                       track=synthetic_lap.track, car=synthetic_lap.car, session_id=SID)
    assert ctx.get_race_result().endswith("get_stint_events with phase='race'.")


# -- 4. race lap 1 starts from the grid slot -----------------------------------------

def test_race_lap_1_is_measured_from_this_races_grid_slot(tmp_path, synthetic_lap):
    ctx = _lgc12(tmp_path, synthetic_lap, logs=[(_R1, _R1[1]), (_R2, _R2[1])])
    # Race 2 lap 1 ran P2 from the P3 grid slot -- not "from P2", race 1's in-lap.
    assert _pos(ctx, "race 2 lap 1") == "P2 overall (from P3 overall on the grid)"
    assert _pos(ctx, "race 2 lap 5") == "P1 overall (from P2 overall)"
    # #204: race 1's own result row gives its lap 1 race 1's grid slot (P2), never
    # race 2's P3.
    assert _pos(ctx, "race 1 lap 1") == "P2 overall (from P2 overall on the grid)"


def test_race_lap_1_uses_race_1s_grid_when_its_log_is_the_one(tmp_path, synthetic_lap):
    ctx = _lgc12(tmp_path, synthetic_lap, logs=[(_R1, None)])
    assert _pos(ctx, "race 1 lap 1") == "P2 overall (from P2 overall on the grid)"
    assert _pos(ctx, "race 2 lap 1") == "P2 overall"


def test_grid_slot_is_only_race_lap_1s_from(tmp_path, synthetic_lap):
    """Capture picked race 2 up at lap 3: the grid slot is not where lap 3 started."""
    rows = [r for r in _ROWS if not (r[0] > 2700 and r[2] < 3)]
    ctx = _lgc12(tmp_path, synthetic_lap, logs=[(_R1, _R1[1]), (_R2, _R2[1])], rows=rows)
    assert _pos(ctx, "race 2 lap 3") == "P2 overall"


def test_no_grid_slot_no_from_and_never_the_previous_race(tmp_path, synthetic_lap):
    ctx = _lgc12(tmp_path, synthetic_lap)
    assert _pos(ctx, "race 2 lap 1") == "P2 overall"
    assert _pos(ctx, "race 2 lap 2") == "P2 overall (from P2 overall)"


# -- M1: a window can hold two races; the grid slot is only the logged race's ------------

def test_grid_slot_never_reaches_the_other_race_of_a_merged_window(tmp_path, synthetic_lap):
    """Two races back to back with no other lap between (an offline restart) -- #204:
    split into two windows by the lap_no reset. Only race B is logged: A's lap 1 must
    not read 'from' B's grid slot; B's lap 1 does."""
    race = [(0.0, "race", 0, "partial", 2, None), (203.4, "race", 1, None, 2, 112.345),
            (309.6, "race", 2, None, 2, 106.289), (416.6, "race", 3, None, 2, 106.867)]
    b0 = 900.0          # under _RACE_WINDOW_GAP_S after A's last lap
    rows = race + [(dt + b0, *rest) for dt, *rest in race]
    log_b = (b0 - 36.68, None, 1, 7, 7)
    ctx = _lgc12(tmp_path, synthetic_lap, logs=[(log_b, None)], rows=rows, pit_at=set())
    assert len(ctx._race_windows()) == 2
    assert _pos(ctx, "race 1 lap 1") == "P2 overall"                # race A: no 'from'
    assert _pos(ctx, "race 2 lap 1") == "P2 overall (from P7 overall on the grid)"


# -- M2 + M3: the real Road Atlanta pit-lane start (13 Autosport, 2026-09-23) -------------

RA_SID = "michelin-raceway-road-atlanta--13-autosport-2026-13-lm--online"
RA_T0 = 1_790_215_719.66
_RA_ROWS = [
    (-87.4, "qualifying", 6, None, 28, 82.592),
    (0.0, "race", 0, "in", 30, None),
    (148.8, "race", 0, "out", 30, None),       # pit set: the start from the pit lane
    (257.2, "race", 1, "in", 32, None),        # pit set
    (359.0, "race", 2, None, 32, 101.84),      # pit set
    (444.6, "race", 3, None, 31, 85.612),
]
# GridPos 30 overall, ClassGridPos 8 (a multiclass grid), finished P22 (DNF).
_RA_LOG = (-2.66, None, 22, 30, 8)


@pytest.mark.parametrize("pit_at", [
    {148.8, 257.2, 359.0},      # the real flags
    {148.8, 359.0},             # only the start segment before lap 1
    {257.2, 359.0},             # only lap 1
], ids=["real", "start-segment", "lap-1"])
def test_pit_lane_start_has_no_grid_from(tmp_path, synthetic_lap, pit_at):
    ctx = _lgc12(tmp_path, synthetic_lap, logs=[(_RA_LOG, None)], rows=_RA_ROWS,
                 sid=RA_SID, t0=RA_T0, pit_at=pit_at)
    assert _pos(ctx, "race lap 1") == "P32 overall"


def test_grid_start_reads_the_overall_grid_slot_not_the_class_one(tmp_path, synthetic_lap):
    ctx = _lgc12(tmp_path, synthetic_lap, logs=[(_RA_LOG, None)], rows=_RA_ROWS,
                 sid=RA_SID, t0=RA_T0, pit_at={0.0, 359.0})   # garage segment only
    assert _pos(ctx, "race lap 1") == "P32 overall (from P30 overall on the grid)"
