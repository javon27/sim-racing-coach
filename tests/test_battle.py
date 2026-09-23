"""#152 get_battle: the same-class field trace and per-corner timing (pure)."""
import dataclasses

import pytest

from simcoach.battle import FieldTrace, car_laps
from simcoach.corners import Corner
from simcoach.lmu.live import FieldSnapshot, VehicleState, empty_snapshot

L = 1000.0
T1 = Corner(1, "La Source", "right", 200.0, 250.0, 300.0, label="1")
T2 = Corner(2, "Eau Rouge", "left", 600.0, 650.0, 700.0, label="2")
T_LINE = Corner(4, "Line Kink", "left", 20.0, 50.0, 80.0, label="4")   # just past S/F
SEAM = Corner(3, "Seam", "right", 980.0, 990.0, 20.0, label="3")       # wraps S/F: skipped


def _vs(cid, cls, laps, dist, *, player=False, speed=50.0, pos=1):
    return VehicleState(
        id=cid, place=pos, class_position=pos, driver=f"D{cid}", car=f"E{cid}", cls=cls,
        lap_dist=dist, speed=speed, sector=0, laps=laps, in_pits=False, is_player=player,
        best_lap=None, last_lap=None, best_s1=None, best_s2=None, gap_leader=None,
        laps_behind_leader=0, gap_next=None, laps_behind_next=0)


def _snap(cars):
    s = dataclasses.replace(empty_snapshot().session, live=True, phase="race", track_len=L)
    return FieldSnapshot(session=s, cars=cars)


def _pts(t0, s0, v, n=140, hz=5.0):
    """A 5 Hz trace at constant speed v (m/s) from unwrapped distance s0 at time t0."""
    return [(t0 + i / hz, s0 + v * i / hz, v * 3.6) for i in range(n)]


def _trace(extra=None):
    """The driver is car 1 on lap 2: unwrapped 750 m at t=95 s, 50 m/s, so he crosses
    the line (1000 m) at t=100 and T1's entry (1200 m) at t=104."""
    cars = {1: {"driver": "You", "car": "E1", "class_position": 3,
                "pts": _pts(95.0, 750.0, 50.0)}}
    cars.update(extra or {})
    return {"player_id": 1, "track_len": L, "cars": cars}


def _rival(s0, *, cid=2, pos=2, driver="A. Rival"):
    return {cid: {"driver": driver, "car": f"E{cid}", "class_position": pos,
                  "pts": _pts(95.0, s0, 50.0)}}


def test_feed_records_the_players_class_once_per_scoring_frame():
    tr = FieldTrace()
    snap = _snap([_vs(1, "GT3", 1, 100.0, player=True), _vs(2, "GT3", 1, 150.0, pos=2),
                  _vs(3, "Hyper", 2, 900.0)])
    tr.feed(10.0, snap)
    tr.feed(10.0, snap)                 # the capture polls faster than scoring updates
    out = tr.take()
    assert out["player_id"] == 1 and out["track_len"] == L
    assert set(out["cars"]) == {1, 2}   # other classes are not recorded
    assert out["cars"][2]["pts"] == [(10.0, 1150.0, pytest.approx(180.0))]
    assert out["cars"][2]["class_position"] == 2


def test_feed_drops_a_frame_where_lap_distance_reset_before_the_lap_count():
    tr = FieldTrace()
    for t, laps, dist in ((10.0, 1, 990.0), (10.2, 1, 5.0), (10.4, 2, 15.0)):
        tr.feed(t, _snap([_vs(1, "GT3", laps, dist, player=True)]))
    assert [s for _, s, _ in tr.take()["cars"][1]["pts"]] == [1990.0, 2015.0]


def test_feed_drops_a_frame_where_the_lap_count_ticked_before_lap_distance_reset():
    """The other ordering of the same two fields: the count ticks first, so one frame
    reads a whole lap AHEAD. Kept, it would block every later sample until the car
    really got there -- a lap of lost trace."""
    tr = FieldTrace()
    for t, laps, dist in ((10.0, 1, 990.0), (10.2, 2, 995.0), (10.4, 2, 15.0),
                          (10.6, 2, 25.0)):
        tr.feed(t, _snap([_vs(1, "GT3", laps, dist, player=True)]))
    assert [s for _, s, _ in tr.take()["cars"][1]["pts"]] == [1990.0, 2015.0, 2025.0]


def test_take_keeps_the_last_seconds_for_the_next_lap():
    tr = FieldTrace()
    for i in range(21):
        tr.feed(float(i), _snap([_vs(1, "GT3", 1, 10.0 * i, player=True)]))
    assert len(tr.take()["cars"][1]["pts"]) == 21
    assert [t for t, _, _ in tr.take()["cars"][1]["pts"]] == [15.0, 16.0, 17.0, 18.0, 19.0,
                                                              20.0]


def test_car_laps_times_each_corner_for_the_driver_and_a_rival():
    by_id = {c.car_id: c for c in car_laps(_trace(_rival(800.0)), [T1, T2, SEAM], lap_no=2)}
    me, rival = by_id[1], by_id[2]
    assert me.is_player and not rival.is_player and rival.class_position == 2
    assert me.entry[1] == pytest.approx(104.0) and me.exit[1] == pytest.approx(106.0)
    assert rival.entry[1] == pytest.approx(103.0) and rival.exit[1] == pytest.approx(105.0)
    assert me.apex_kph[1] == pytest.approx(180.0)
    assert 3 not in me.entry and 3 not in rival.entry      # the S/F-seam corner is skipped


def test_a_rival_is_paired_by_time_across_the_line_whichever_car_crosses_it_first():
    """T_LINE sits just past S/F. A rival 1 s AHEAD crossed the line before the driver
    (his lap counter ticked first); one 1 s BEHIND crosses after. Each is paired with the
    driver's own crossing, not with a crossing a whole lap away."""
    extra = {**_rival(800.0), **_rival(700.0, cid=3, pos=4, driver="B. Chaser")}
    by_id = {c.car_id: c for c in car_laps(_trace(extra), [T_LINE], lap_no=2)}
    assert by_id[1].entry[4] == pytest.approx(100.4)
    assert by_id[2].entry[4] == pytest.approx(99.4)
    assert by_id[3].entry[4] == pytest.approx(101.4)


def test_a_car_on_another_lap_count_is_paired_by_time_not_by_the_drivers_lap_number():
    """A same-class car a lap UP (its counter reads one more) but physically 1 s ahead.
    Read by the driver's lap number (unwrapped 1000 + dist on the rival's axis) its T1
    entry would not exist (its trace starts at 1800 m); paired by time it is 103.0."""
    by_id = {c.car_id: c for c in car_laps(_trace(_rival(1800.0)), [T1], lap_no=2)}
    assert by_id[2].entry[1] == pytest.approx(103.0)
    assert by_id[2].exit[1] == pytest.approx(105.0)


def test_car_laps_is_empty_without_the_drivers_own_trace():
    assert car_laps({"player_id": 9, "track_len": L, "cars": {}}, [T1], lap_no=2) == []
