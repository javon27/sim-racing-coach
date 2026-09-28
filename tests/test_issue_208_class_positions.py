"""#208: a multiclass race is reviewed in CLASS positions, overall second.

The shape is the real Long Beach race (2026-09-27, online, Racing Team Turkey #34 GT3 in
a GT3 + LMP2_ELMS field): each race lap's overall place (mPlace) and class place, lap
numbers, partial kinds, pit flags and timestamps (off the result log's race start) are
the DB's; the result row is the race XML's -- grid P31 overall / P18 in class, finished
P17 overall / P9 in class. The driver heard only the overall numbers."""
import dataclasses

from simcoach.coach_tools import CoachContext
from simcoach.corners import CornerMap
from simcoach.lmu.results import ResultSession
from simcoach.race import RaceSample
from simcoach.store import LapStore

SID = "grand-prix-of-long-beach--racing-team-turkey-by-tf-2026-34-lm--online"
T0 = 1_790_564_614.0
FIRST = " (the first captured lap of this race: where it ENDED, NOT the starting position)"

# (ts offset, lap_no, partial, pit, overall place, class place)
_ROWS = [
    (7.92, 0, "in", 0, 31, None), (120.83, 0, "out", 1, 31, 18),
    (219.03, 1, None, 0, 24, 12), (302.03, 2, None, 0, 21, 11),
    (385.63, 3, None, 0, 21, 12), (483.63, 4, None, 0, 26, 17),
    (569.43, 5, None, 0, 26, 17), (651.44, 6, None, 0, 26, 17),
    (736.84, 7, None, 0, 26, 17), (818.44, 8, None, 0, 25, 16),
    (899.84, 9, None, 0, 24, 15), (982.24, 10, None, 0, 24, 15),
    (1064.44, 11, None, 0, 24, 15), (1146.43, 12, None, 0, 23, 14),
    (1228.04, 13, None, 0, 23, 14), (1310.63, 14, None, 0, 21, 13),
    (1392.83, 15, None, 0, 19, 12), (1474.24, 16, None, 0, 19, 12),
    (1556.04, 17, None, 0, 18, 11), (1636.84, 18, None, 0, 17, 10),
    (1718.83, 19, None, 0, 17, 10), (1801.65, 20, None, 0, 17, 10),
    (1883.44, 21, None, 0, 18, 10), (1965.45, 22, None, 0, 18, 10),
    (1980.03, 22, "in", 0, 17, None),
]


def _ctx(tmp_path, lap, one_class=False, pit_start=True, log=True):
    """`one_class`: the same race with every class place = the overall one (a GT3-only
    field) -- the output before #208, byte for byte."""
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    for dt, lap_no, partial, pit, pos, cls in _ROWS:
        cls = pos if one_class and cls is not None else cls
        store.save(dataclasses.replace(
            lap, session_id=SID, phase="race", lap_no=lap_no, partial=partial,
            pit=bool(pit) and pit_start, position=pos, valid=partial is None,
            lap_time=None if partial else 82.0, timestamp=T0 + dt,
            race=RaceSample(class_position=cls)))
    if log:
        store.save_result_session(ResultSession(
            sim="lmu", track=lap.track, car=lap.car, car_class="GT3", date_ts=T0,
            session_type="race", position=17, class_position=17 if one_class else 9,
            best_lap=None, finish_status="Finished Normally", laps=[], incidents=[],
            penalties=[], grid_position=31, class_grid_position=31 if one_class else 18,
            online=True), session_id="2026_09_27_22_37_25-56R1", logged_ts=T0 + 2031.87)
    return CoachContext(store, cmap=CornerMap.load("simcoach/tracks/test_track.json"),
                        track=lap.track, car=lap.car, session_id=SID)


def _line(out, head):
    return next(line for line in out.splitlines() if line.startswith(head))


def _pos(ctx, which):
    head = ctx.get_lap_summary(which).splitlines()[0]
    assert head.startswith(f"Lap: {which} "), head
    return head.split("position ", 1)[1]


def test_race_result_leads_with_class(tmp_path, synthetic_lap):
    out = _ctx(tmp_path, synthetic_lap).get_race_result()
    assert "Started P18 in class (P31 overall) on the grid." in out
    assert "Finished P9 in class (P17 overall); status: Finished Normally." in out
    # The low is the lowest CLASS place with that lap's overall place (laps 4-7); the
    # in-lap sampled no class place, so it says overall only -- never a made-up class.
    assert _line(out, "Live per-lap") == (
        "Live per-lap positions over 23 captured lap(s): P12 in class (P24 overall) on the "
        "first captured lap, as low as P17 in class (P26 overall), P17 overall on the most "
        "recent.")
    assert _line(out, "Per-lap positions") == (
        "Per-lap positions (in class, the overall place in brackets) from race lap 1: race "
        "lap 1 P12 (P24), race lap 2 P11 (P21), race lap 3 P12 (P21), race lap 4 P17 (P26), "
        "race lap 5 P17 (P26), race lap 6 P17 (P26), race lap 7 P17 (P26), race lap 8 P16 "
        "(P25), race lap 9 P15 (P24), race lap 10 P15 (P24), race lap 11 P15 (P24), race "
        "lap 12 P14 (P23), race lap 13 P14 (P23), race lap 14 P13 (P21), race lap 15 P12 "
        "(P19), race lap 16 P12 (P19), race lap 17 P11 (P18), race lap 18 P10 (P17), race "
        "lap 19 P10 (P17), race lap 20 P10 (P17), race lap 21 P10 (P18), race lap 22 P10 "
        "(P18), race lap 22 P17 overall (in-lap).")


def test_lap_lines_and_session_status_lead_with_class(tmp_path, synthetic_lap):
    ctx = _ctx(tmp_path, synthetic_lap)
    # The rig bug: lap 1 said only "P24 overall", lap 2 "(from P24 overall)", and the
    # coach told a P31 starter he was "up from P24 at the start". The start segment is
    # in the pits, so no grid 'from' -- and lap 1 says it is not the start.
    assert _pos(ctx, "race lap 1") == "P12 in class (P24 overall)" + FIRST
    assert _pos(ctx, "race lap 2") == "P11 in class (P21 overall) (from P12 in class, P24 overall)"
    assert _pos(ctx, "race lap 4") == "P17 in class (P26 overall) (from P12 in class, P21 overall)"
    assert _line(ctx.get_session_status(), "Track:").endswith(
        " | Position: P17 overall (P12 in class, P24 overall on the first captured lap; low "
        "P17 in class, P26 overall)")
    events = ctx.get_stint_events(phase="race")
    assert "— position P10 in class (P18 overall) (from P10 in class, P17 overall)" in _line(
        events, "Lap: race lap 21 ")


def test_grid_start_reads_the_class_grid_slot_first(tmp_path, synthetic_lap):
    ctx = _ctx(tmp_path, synthetic_lap, pit_start=False)
    assert _pos(ctx, "race lap 1") == ("P12 in class (P24 overall) (from P18 in class, P31 "
                                       "overall on the grid)")


def test_no_result_log_lap_1_is_never_a_start(tmp_path, synthetic_lap):
    ctx = _ctx(tmp_path, synthetic_lap, pit_start=False, log=False)
    assert _pos(ctx, "race lap 1") == "P12 in class (P24 overall)" + FIRST
    assert "(from" not in _pos(ctx, "race lap 1")


def test_one_class_race_is_unchanged(tmp_path, synthetic_lap):
    """Class place = overall place on every lap and in the log: one number, the pre-#208
    wording exactly."""
    ctx = _ctx(tmp_path, synthetic_lap, one_class=True)
    out = ctx.get_race_result()
    assert "Started P31 overall on the grid." in out
    assert "Finished P17 overall; P17 in class; status: Finished Normally." in out
    assert _line(out, "Live per-lap") == (
        "Live per-lap positions over 23 captured lap(s): P24 overall on the first captured "
        "lap, as low as P26 overall, P17 overall on the most recent.")
    assert _line(out, "Per-lap positions").startswith(
        "Per-lap positions (overall) from race lap 1: race lap 1 P24, race lap 2 P21, ")
    assert _line(out, "Per-lap positions").endswith("race lap 22 P18, race lap 22 P17 (in-lap).")
    assert _pos(ctx, "race lap 1") == "P24 overall" + FIRST
    assert _pos(ctx, "race lap 2") == "P21 overall (from P24 overall)"
    assert _line(ctx.get_session_status(), "Track:").endswith(
        " | Position: P17 overall (P24 overall on the first captured lap, low P26 overall)")
    one = _ctx(tmp_path / "grid", synthetic_lap, one_class=True, pit_start=False)
    assert _pos(one, "race lap 1") == "P24 overall (from P31 overall on the grid)"


def test_past_sessions_finish_leads_with_class(tmp_path, synthetic_lap):
    ctx = _ctx(tmp_path, synthetic_lap)
    ctx.session_id = "another--online"
    assert "finished P9 in class (P17 overall))" in ctx.list_past_sessions()


def test_race_state_states_the_rivals_class_place():
    """Turn 286 made one up ("he's P16 while you're P16"). The class neighbour is the
    next car of the class by place, so his class place is yours -/+ 1."""
    from simcoach.race import GapRead, RaceSnapshot, RaceState, format_race_state
    st = RaceState(position=25, class_position=16, max_laps=30, time_left=None, pace=None,
                   laps_to_go=None, ahead=GapRead("A. Rival (A Entry)", 1.2, None, None),
                   behind=GapRead("B. Chaser (B Entry)", None, None, None))
    text = format_race_state(RaceSnapshot(as_of_lap=8, session_id=SID, state=st,
                                          strategy=None))
    assert "Car ahead in class (P15): A. Rival (A Entry), 1.2 s ahead; trend:" in text
    assert "Car behind in class (P17): B. Chaser (B Entry), a lap or more away." in text
    # Live: he is P15 in class now and a different car is ahead -- its place is P14.
    live = format_race_state(RaceSnapshot(8, SID, st, None), (24, RaceSample(
        class_position=15, class_ahead="C (C Entry)", class_behind="A. Rival (A Entry)")))
    assert "Car ahead in class now (live, P14): C (C Entry); no gap to him yet" in live
    assert "Car behind in class now (live, P16): A. Rival (A Entry); no gap" in live
    # Leading the class: nobody ahead, so no place is named for him.
    lead = dataclasses.replace(st, class_position=1, ahead=None)
    text = format_race_state(RaceSnapshot(8, SID, lead, None))
    assert "Car ahead in class: none — you lead your class." in text
    assert "Car behind in class (P2): B. Chaser" in text


def test_laps_with_no_class_place_list_overall(tmp_path, synthetic_lap):
    """A multiclass log over laps captured before class places were sampled (Monza WRT
    offline, 2026-09-07): the list can't lead with class, so it says overall -- never an
    'in class' header over overall numbers."""
    ctx = _ctx(tmp_path, synthetic_lap)
    with ctx.store._lock, ctx.store.conn:
        ctx.store.conn.execute("UPDATE laps SET class_position = NULL")
    out = ctx.get_race_result()
    assert "Finished P9 in class (P17 overall)" in out
    assert _line(out, "Per-lap positions").startswith(
        "Per-lap positions (overall) from race lap 1: race lap 1 P24, race lap 2 P21, ")
    assert "as low as P26 overall" in out


def test_the_class_low_is_paired_with_its_own_laps_overall_place():
    from simcoach.coach_tools import _arc_places
    arc = {"positions": [(24, 12), (30, 11), (26, 17), (20, 9)], "worst": 30}
    assert _arc_places(arc, True) == ("P12 in class (P24 overall)", "P17 in class (P26 overall)",
                                      "P9 in class (P20 overall)")
    assert _arc_places(arc, False)[1] == "P30 overall"
