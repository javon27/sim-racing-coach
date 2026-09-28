"""#204: follow-ups to #187's race review.

1. get_race_result(race=N) reads race N's own result row (grid, finish, incidents),
   and an earlier race's lap 1 gets its own grid 'from'.
2. A race window splits where the game's lap number goes back (two races back to
   back); legacy laps with no lap_no (Monza WRT offline, 2026-09-07) can't.
3. The positions tail tags a repeated label with its kind ("(in-lap)").
4. One race grammar: race=N and which='race N' on get_stint_events,
   get_race_result and get_battle, through _race_pick.

The main shape is the real Algarve LGC #10 session (2026-09-26, online): three races,
each preceded by practice + qualy. Lap numbers, partial kinds, pit flags, positions,
lap times and timestamps (relative to race 1's result-log start) are the DB's; the
result rows (start, log write time, finish, grid) are the three race XMLs'."""
import dataclasses
import types

import pytest

from simcoach.coach_tools import CoachContext, race_result_text
from simcoach.corners import CornerMap
from simcoach.lmu.results import ResultSession
from simcoach.store import LapStore

SID = "algarve-international-circuit--logitech-g-challenge-10-lgc--online"
T0 = 1_790_478_029.0

# (ts offset, phase, lap_no, partial, pit, position, lap time)
_ROWS = [
    (-1009.4, "practice", 1, "partial", 1, 2, None),
    (-928.59, "practice", 2, "in", 0, 2, None),
    (-703.73, "qualifying", 0, "out", 1, 6, None),
    (-564.33, "qualifying", 1, "out", 1, 7, None),
    (-457.54, "qualifying", 2, None, 0, 7, 106.776),
    (-351.32, "qualifying", 3, None, 0, 5, 106.218),
    (-244.72, "qualifying", 4, None, 0, 6, 106.609),
    (-138.33, "qualifying", 5, None, 0, 6, 106.492),
    (-32.12, "qualifying", 6, None, 0, 7, 106.161),
    (6.46, "race", 0, "in", 0, 7, None),
    (25.66, "race", 0, "partial", 1, 7, None),
    (34.65, "race", 0, "partial", 1, 7, None),
    (125.45, "race", 0, "partial", 0, 7, None),
    (240.65, "race", 1, None, 0, 6, 115.224),
    (348.24, "race", 2, None, 0, 6, 107.681),
    (459.25, "race", 3, None, 0, 10, 110.965),
    (567.44, "race", 4, "partial", 0, 10, None),
    (673.64, "race", 5, None, 0, 8, 106.192),
    (780.84, "race", 6, None, 0, 8, 107.113),
    (889.45, "race", 7, "partial", 0, 8, None),
    (1000.64, "race", 8, None, 0, 8, 111.212),
    (1110.44, "race", 9, None, 0, 9, 109.740),
    (1677.13, "practice", 1, "partial", 1, 3, None),
    (1789.52, "practice", 3, "in", 0, 1, None),
    (1996.7, "qualifying", 0, "out", 1, 5, None),
    (2152.68, "qualifying", 1, "out", 1, 9, None),
    (2259.26, "qualifying", 2, None, 0, 8, 106.570),
    (2365.66, "qualifying", 3, None, 0, 3, 106.397),
    (2472.48, "qualifying", 4, None, 0, 3, 106.787),
    (2579.06, "qualifying", 5, None, 0, 4, 106.491),
    (2685.87, "qualifying", 6, None, 0, 4, 106.860),
    (2725.48, "race", 0, "in", 0, 4, None),
    (2750.46, "race", 0, "partial", 1, 4, None),
    (2843.66, "race", 0, "partial", 0, 4, None),
    (2957.67, "race", 1, None, 0, 3, 114.007),
    (3064.46, "race", 2, None, 0, 3, 106.900),
    (3170.86, "race", 3, None, 0, 3, 106.366),
    (3277.06, "race", 4, None, 0, 3, 106.131),
    (3382.87, "race", 5, None, 0, 2, 105.945),
    (3489.67, "race", 6, None, 0, 2, 106.769),
    (3596.26, "race", 7, "partial", 0, 2, None),
    (3703.05, "race", 8, None, 0, 2, 106.782),
    (3811.06, "race", 9, None, 0, 3, 108.001),
    (4831.13, "qualifying", 1, "partial", 1, 3, None),
    (4938.11, "qualifying", 2, None, 0, 3, 107.138),
    (5044.51, "qualifying", 3, None, 0, 7, 106.294),
    (5150.52, "qualifying", 4, None, 0, 4, 105.991),
    (5257.12, "qualifying", 5, None, 0, 4, 106.593),
    (5363.11, "qualifying", 6, None, 0, 4, 106.013),
    (5422.57, "race", 0, "partial", 1, 4, None),
    (5541.55, "race", 0, "partial", 0, 6, None),
    (5653.76, "race", 1, None, 0, 2, 112.260),
    (5760.95, "race", 2, None, 0, 1, 107.133),
    (5867.16, "race", 3, None, 0, 1, 106.263),
    (5973.55, "race", 4, None, 0, 1, 106.387),
    (6079.55, "race", 5, None, 0, 1, 105.883),
    (6185.94, "race", 6, None, 0, 1, 106.448),
    (6292.16, "race", 7, None, 0, 1, 106.342),
    (6398.75, "race", 8, None, 0, 1, 106.580),
    (6505.15, "race", 9, None, 0, 1, 106.348),
    (6530.95, "race", 9, "in", 0, 1, None),
]

# The three race logs: (ts, logged_ts, finish, grid, incidents), off T0. Incidents are
# the first real entries of each log.
R1 = (0.0, 1174.18, 9, 7, [("RJ PARENT(5) reported contact (151.15) with another "
                            "vehicle Jayson G(11)")])
R2 = (2723.0, 3909.14, 3, 4, [])
R3 = (5415.0, 6609.63, 1, 4, ["No Further Action", "No Further Action"])


def _ctx(tmp_path, lap, logs=(R1, R2, R3), rows=_ROWS, sid=SID, t0=T0, modes=None):
    """`logs`: result rows to import; `modes`: each row's online flag (None = a row
    ingested before mode existed), default online."""
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    for dt, phase, lap_no, partial, pit, pos, t in rows:
        store.save(dataclasses.replace(
            lap, session_id=sid, phase=phase, lap_no=lap_no, partial=partial,
            pit=bool(pit), position=pos, lap_time=t, valid=t is not None,
            timestamp=t0 + dt))
    for i, (ts, logged, finish, grid, incidents) in enumerate(logs):
        store.save_result_session(ResultSession(
            sim="lmu", track=lap.track, car=lap.car, car_class="GT3", date_ts=t0 + ts,
            session_type="race", position=finish, class_position=finish, best_lap=None,
            finish_status="Finished Normally", laps=[], incidents=incidents, penalties=[],
            grid_position=grid, class_grid_position=grid,
            online=True if modes is None else modes[i]),
            session_id=f"result-{ts}", logged_ts=None if logged is None else t0 + logged)
    return CoachContext(store, cmap=CornerMap.load("simcoach/tracks/test_track.json"),
                        track=lap.track, car=lap.car, session_id=sid)


def _tail(out):
    return next(line for line in out.splitlines() if line.startswith("Per-lap positions"))


def _pos(ctx, which):
    head = ctx.get_lap_summary(which).splitlines()[0]
    assert head.startswith(f"Lap: {which} "), head
    return head.split("position ", 1)[1]


def _laps(out):
    return [line.split(" (game lap")[0].removeprefix("Lap: ")
            for line in out.splitlines() if line.startswith("Lap: ")]


# -- 1. get_race_result(race=N) ---------------------------------------------------

def test_race_1_result_is_race_1s_own_row(tmp_path, synthetic_lap):
    ctx = _ctx(tmp_path, synthetic_lap)
    out = ctx.get_race_result(race=1)
    assert "Race 1 of this session" in out
    assert "Started P7 overall on the grid." in out          # R1's GridPos, not R3's P4
    assert "Finished P9 overall; P9 in class" in out
    assert "Incidents (1): RJ PARENT(5) reported contact" in out
    assert "This race is FINISHED" in out
    assert _tail(out) == ("Per-lap positions (overall) from race lap 1: race 1 lap 1 P6, "
                          "race 1 lap 2 P6, race 1 lap 3 P10, race 1 lap 4 P10, race 1 lap "
                          "5 P8, race 1 lap 6 P8, race 1 lap 7 P8, race 1 lap 8 P8, race 1 "
                          "lap 9 P9.")
    assert out.endswith("get_stint_events with phase='race', race=1.")
    assert ctx.get_race_result(which="race 1") == out
    assert ctx.get_race_result(race=1, which="the race 1") == out


def test_race_2_result_and_the_default_is_the_latest(tmp_path, synthetic_lap):
    ctx = _ctx(tmp_path, synthetic_lap)
    r2 = ctx.get_race_result(race=2)
    assert "Race 2 of this session" in r2 and "Started P4 overall on the grid." in r2
    assert "Finished P3 overall" in r2 and "race 1 lap" not in r2 and "race 3 lap" not in r2
    default = ctx.get_race_result()
    assert "Race 3 of this session" in default and "Finished P1 overall" in default
    for same in ({"race": 3}, {"which": "race 3"}, {"which": "race"}, {"which": "latest"}):
        assert ctx.dispatch("get_race_result", same) == default, same


def test_earlier_race_arc_stays_inside_its_window(tmp_path, synthetic_lap):
    """Race 1 logged, races 2 and 3 not: race 1's row has no next_race_ts, so only its
    own window bounds the arc -- 9 laps, not all 28 race laps from race 1 on."""
    ctx = _ctx(tmp_path, synthetic_lap, logs=(R1,))
    out = ctx.get_race_result(race=1)
    assert "Started P7 overall on the grid." in out
    assert "Live per-lap positions over 9 captured lap(s): P6 overall on the first " \
           "captured lap, as low as P10 overall, P9 overall on the most recent." in out
    text = race_result_text(ctx.store, SID, 1)
    assert "over 9 captured lap(s)" in text


def test_earlier_race_with_no_log_is_over(tmp_path, synthetic_lap):
    """Only race 3 is logged: race 1 has no row, its arc is its window, and it is over
    because a later race followed -- even with a race live now."""
    ctx = _ctx(tmp_path, synthetic_lap, logs=(R3,))
    ctx.on_track, ctx.race_note = True, "A race is live; its first lap isn't in yet."
    out = ctx.get_race_result(race=1)
    assert "Started P" not in out and "Finished" not in out
    assert "over 9 captured lap(s)" in out
    assert "A later race of this session followed it, so this race is over" in out
    assert "No race is live now" not in out


@pytest.mark.parametrize("args, msg", [
    ({"race": 4}, "No race 4 in this session: it holds 3 races, race 1 to race 3."),
    ({"which": "race 4"}, "No race 4 in this session: it holds 3 races, race 1 to race 3."),
    ({"race": 1.5}, "No race 1.5 in this session: it holds 3 races, race 1 to race 3."),
    ({"which": "the first race"}, ("which must name a race like 'race 2' (this session "
                                   "holds 3 races, race 1 to race 3), not 'the first race'.")),
    ({"race": 1, "which": "race 2"}, "race=1 and which='race 2' name different races."),
])
def test_unknown_race_is_refused_not_the_latest(tmp_path, synthetic_lap, args, msg):
    ctx = _ctx(tmp_path, synthetic_lap)
    assert ctx.dispatch("get_race_result", args) == msg
    assert ctx.dispatch("get_stint_events", args) == msg


def test_race_lap_1_of_every_race_reads_its_own_grid_slot(tmp_path, synthetic_lap):
    ctx = _ctx(tmp_path, synthetic_lap)
    assert _pos(ctx, "race 1 lap 1") == "P6 overall (from P7 overall on the grid)"
    assert _pos(ctx, "race 2 lap 1") == "P3 overall (from P4 overall on the grid)"
    assert _pos(ctx, "race 3 lap 1") == "P2 overall (from P4 overall on the grid)"
    assert _pos(ctx, "race 1 lap 2") == "P6 overall (from P6 overall)"


def test_an_unlogged_races_lap_1_has_no_grid_slot(tmp_path, synthetic_lap):
    """Race 2 has no log. Race 1's has no write time either, so the stale-log cutoff
    can't refuse it: only the window's lower bound keeps it off race 2."""
    ctx = _ctx(tmp_path, synthetic_lap, logs=(R1[:1] + (None,) + R1[2:], R3))
    assert _pos(ctx, "race 2 lap 1") == "P3 overall"      # never R1's P7 or R3's P4
    assert "Started P" not in ctx.get_race_result(race=2)   # nor R1's result as race 2's
    assert _pos(ctx, "race 1 lap 1") == "P6 overall (from P7 overall on the grid)"


# -- 2. the window split ------------------------------------------------------------

_B2B = [(0.0, "race", 0, "partial", 0, 2, None), (203.4, "race", 1, None, 0, 2, 112.345),
        (309.6, "race", 2, None, 0, 3, 106.289), (416.6, "race", 3, None, 0, 4, 106.867)]
# Race B right after race A (under _RACE_WINDOW_GAP_S), no other lap between.
_BACK_TO_BACK = _B2B + [(dt + 900.0, ph, n, part, pit, pos + 4, t)
                        for dt, ph, n, part, pit, pos, t in _B2B]
_LOG_A = (-36.68, None, 3, 2, [])
_LOG_B = (863.32, None, 9, 6, [])


def test_lap_no_reset_splits_back_to_back_races(tmp_path, synthetic_lap):
    ctx = _ctx(tmp_path, synthetic_lap, logs=(_LOG_A, _LOG_B), rows=_BACK_TO_BACK)
    wins = ctx._race_windows()
    assert [len(w["lap_ids"]) for w in wins] == [4, 4]
    assert [r["race_index"] for r in ctx.store.session_laps(SID)] == [1] * 4 + [2] * 4
    assert _pos(ctx, "race 2 lap 1") == "P6 overall (from P6 overall on the grid)"
    # Race B lap 1 used to read "from P4" -- race A's last lap, via the previous position.
    assert _pos(ctx, "race 1 lap 1") == "P2 overall (from P2 overall on the grid)"
    assert _laps(ctx.get_stint_events(race=1)) == [f"race 1 lap {n}" for n in (1, 2, 3)]


def test_the_arc_of_an_unlogged_later_race_is_that_race_only(tmp_path, synthetic_lap):
    """No log at all: the arc floors on the last window -- once race B's own laps."""
    ctx = _ctx(tmp_path, synthetic_lap, logs=(), rows=_BACK_TO_BACK)
    out = race_result_text(ctx.store, SID)
    assert ("Live per-lap positions over 3 captured lap(s): P6 overall on the first "
            "captured lap, as low as P8 overall, P8 overall on the most recent.") in out


def test_legacy_laps_without_lap_no_cannot_split(tmp_path, synthetic_lap):
    """Monza WRT offline, 2026-09-07: two logs, eleven race laps, lap_no NULL -- one
    window, as before (no reset to see). Positions and times are the DB's."""
    t0 = 1_788_831_565.0     # race 1's log start
    rows = [(dt, "race", None, None, 0, pos, t) for dt, pos, t in [
        (212.98, 27, 130.361), (469.83, 35, 127.677), (584.04, 35, 114.178),
        (1042.12, 34, 119.600), (1966.49, 34, 117.119), (2512.84, 25, 131.593),
        (2905.40, 28, 120.200), (3149.99, 29, 117.090), (3364.39, 29, 114.719),
        (3680.76, 28, 115.277), (4494.47, 29, 115.859)]]
    logs = ((0.0, 2245.66, 34, 23, []), (2251.0, 5367.66, 35, 23, []))
    sid = "autodromo-nazionale-monza--team-wrt-2026-32-wec--offline~20260907T2042"
    store_ctx = _ctx(tmp_path, synthetic_lap, logs=logs, rows=rows, sid=sid, t0=t0,
                     modes=(False, False))
    assert len(store_ctx._race_windows()) == 1


def test_next_race_ts_keeps_the_earlier_logs_grid_off_the_next_race(tmp_path, synthetic_lap):
    """A restart during the formation lap: race A ends on lap 0 and race B starts on lap
    0, so the lap number never goes back and both stay one window. A's log carries the
    session's mode; B's was ingested before mode existed (NULL), so the window's row is
    A's, with next_race_ts = B's start. B's lap 1 is after that: no 'from' A's grid."""
    rows = [(0.0, "race", 0, "partial", 0, 5, None),
            (120.0, "race", 0, "partial", 0, 5, None),
            (300.0, "race", 1, None, 0, 4, 110.0),
            (410.0, "race", 2, None, 0, 4, 107.0)]
    logs = ((-30.0, None, 6, 9, []), (100.0, None, 4, 3, []))
    ctx = _ctx(tmp_path, synthetic_lap, logs=logs, rows=rows, modes=(True, None))
    assert len(ctx._race_windows()) == 1
    rr = ctx.store.race_session(SID, 1)
    assert (rr["grid_position"], rr["next_race_ts"]) == (9, T0 + 100.0)
    assert _pos(ctx, "race lap 1") == "P4 overall"


# -- 3. the repeated in-lap label ---------------------------------------------------

def test_in_lap_after_the_flag_is_tagged(tmp_path, synthetic_lap):
    out = _ctx(tmp_path, synthetic_lap).get_race_result()
    assert _tail(out).endswith("race 3 lap 8 P1, race 3 lap 9 P1, race 3 lap 9 P1 (in-lap).")


def test_a_retirement_in_lap_keeps_its_own_position(tmp_path, synthetic_lap):
    """Long Beach tf-sport #33 (2026-09-24, DNF): lap 4 P12, then its in-lap P14."""
    t0 = 1_790_309_017.0
    rows = [(dt, "race", n, part, pit, pos, t) for dt, n, part, pit, pos, t in [
        (7.0, 0, "in", 0, 13, None), (121.0, 0, "out", 1, 13, None),
        (218.0, 1, None, 0, 15, 97.142), (302.0, 2, None, 0, 13, 84.306),
        (386.0, 3, None, 0, 12, 83.776), (469.0, 4, None, 0, 12, 82.844),
        (541.0, 4, "in", 0, 14, None)]]
    ctx = _ctx(tmp_path, synthetic_lap, logs=(), rows=rows,
               sid="grand-prix-of-long-beach--tf-sport-2026-33-elms--online", t0=t0)
    assert _tail(ctx.get_race_result()).endswith(
        "race lap 3 P12, race lap 4 P12, race lap 4 P14 (in-lap).")


# -- 4. one race grammar ------------------------------------------------------------

def test_stint_events_which_race_n_is_race_n(tmp_path, synthetic_lap):
    ctx = _ctx(tmp_path, synthetic_lap)
    want = _laps(ctx.get_stint_events(race=1))
    assert want[0] == "race 1 lap 1" and all(x.startswith("race 1 lap") for x in want)
    assert _laps(ctx.dispatch("get_stint_events", {"which": "race 1"})) == want
    assert _laps(ctx.dispatch("get_stint_events", {"which": "race"}))[0] == "race 3 lap 1"


def test_battle_race_n(tmp_path, synthetic_lap):
    ctx = _ctx(tmp_path, synthetic_lap)
    out = ctx.dispatch("get_battle", {"race": 1})
    assert out.startswith("Race 1 of this session (started "), out
    assert ctx.dispatch("get_battle", {"which": "race 1"}) == out
    assert ctx.dispatch("get_battle", {"race": 1, "which": "latest"}) == out
    msg = "No race 5 in this session: it holds 3 races, race 1 to race 3."
    assert ctx.dispatch("get_battle", {"race": 5}) == msg
    # Used to read race 3 lap 5 -- the latest race -- through the unqualified lap ref.
    assert ctx.dispatch("get_battle", {"which": "race 5"}) == msg
    # race=N is a stored read even with a race live: never the live read or its note.
    ctx.on_track, ctx.race_note = True, "A race is live; its first lap isn't in yet."
    assert ctx.dispatch("get_battle", {"race": 1}) == out
    ctx.race = types.SimpleNamespace(battle=None, as_of_lap=3, battle_note="no read yet")
    assert ctx.dispatch("get_battle", {}) != out
    assert ctx.dispatch("get_battle", {"race": 1}) == out


def test_battle_on_a_single_race_session_keeps_race_n_as_a_lap(tmp_path, synthetic_lap):
    """#77: in a one-race session 'race 3' is race lap 3 -- which= is unchanged there;
    race=1 is the whole race."""
    ctx = _ctx(tmp_path, synthetic_lap, logs=(R1,), rows=_ROWS[:22])
    assert ctx.dispatch("get_battle", {"which": "race 3"}).startswith(
        "No battle read is stored for race lap 3")
    whole = ctx.dispatch("get_battle", {"which": "race"})
    assert ctx.dispatch("get_battle", {"race": 1}) == whole
    assert ctx.dispatch("get_battle", {"race": 2}) == (
        "No race 2 in this session: it holds 1 race.")
