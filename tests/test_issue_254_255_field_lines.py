"""#254/#255 (Road Atlanta rig, 2026-10-27, race 2 lap 15): P25 of 25 in GT3.

#254: "the guy behind me, he's first, about to lap me" got "nobody behind in class, so
a faster class" -- 'behind in class' is by standings, so the class leader lapping him
never is. #255: "who is number one in my class, his fastest time?" got "the tools only
show the car ahead and behind", and "fastest lap in class is yours" from two cars.

Frames are real rF2Scoring structs through read_field (the payload shape the app reads).
"""
from simcoach.lmu.live import read_field
from simcoach.lmu.rf2structs import VehicleScoringInfoV01, rF2Scoring
from simcoach.race import class_field_line, track_neighbours_line

LEN = 4088.0     # Road Atlanta, m


def _frame(cars):
    """Overall order as listed: (driver, class, total laps, lap dist, best, time behind
    next, laps behind next, in pits). "You" is the player."""
    s = rF2Scoring()
    s.mScoringInfo.mSession = 12
    s.mScoringInfo.mLapDist = LEN
    s.mScoringInfo.mNumVehicles = len(cars)
    for i, (name, cls, laps, dist, best, gap, laps_next, pits) in enumerate(cars):
        v = VehicleScoringInfoV01()
        v.mID, v.mPlace, v.mIsPlayer = i + 1, i + 1, name == "You"
        v.mDriverName, v.mVehicleName = name.encode(), f"{name} Entry".encode()
        v.mVehicleClass, v.mTotalLaps, v.mLapDist = cls.encode(), laps, dist
        v.mBestLapTime, v.mTimeBehindNext, v.mLapsBehindNext = best, gap, laps_next
        v.mInPits = pits
        s.mVehicles[i] = v
    return read_field(s)


# The class leader, a lap up, sits 100 m BEHIND the driver on the road; a Hypercar is
# 400 m up the road on the driver's lap; the class car ahead in the standings is 18 s up.
RIG = [("Leader", "GT3", 16, 1000.0, 82.1, 0.0, 0, False),
       ("Hyper", "Hypercar", 15, 1500.0, 75.0, 3.0, 0, False),
       ("Timur", "GT3", 15, 2900.0, 83.471, 10.0, 0, False),
       ("You", "GT3", 15, 1100.0, 82.764, 18.4, 1, False)]


def test_class_leader_and_class_fastest_lap_are_stated():
    assert class_field_line(_frame(RIG)) == (
        "Class now (live, this session): class leader (P1 in class) Leader (Leader Entry), "
        "1 lap ahead of you; class fastest lap 1:22.100 by Leader (Leader Entry); your "
        "best ranks 2 of 3 timed in class.")


def test_a_class_leader_on_the_same_lap_gets_the_summed_link_gap():
    same = [RIG[0], RIG[1], RIG[2], ("You", "GT3", 15, 1100.0, 80.0, 4.5, 0, False)]
    assert class_field_line(_frame(same)) == (
        "Class now (live, this session): class leader (P1 in class) Leader (Leader Entry), "
        "17.5 s ahead of you; class fastest lap 1:20.000 by you; your best ranks 1 of 3 "
        "timed in class.")


def test_leading_the_class():
    lead = [("You", "GT3", 15, 1100.0, 82.0, 0.0, 0, False), RIG[1]]
    assert class_field_line(_frame(lead)) == (
        "Class now (live, this session): you lead your class; class fastest lap 1:22.000 "
        "by you; your best ranks 1 of 1 timed in class.")


def test_the_car_behind_on_track_is_the_class_leader_lapping_you():
    assert track_neighbours_line(_frame(RIG)) == (
        "On track now (live, any class, by lap distance): ahead Hyper (Hyper Entry), "
        "Hypercar, 400 m ahead; behind Leader (Leader Entry), GT3, 100 m behind, 1 lap up "
        "on you (lapping you).")


def test_across_the_line_a_car_on_the_next_lap_is_not_lapping_you():
    # the driver at 4,050 m, a GT3 on the same race lap just over the line at 30 m:
    # mTotalLaps is one higher, but he is 68 m ahead on the road, not a lap up
    cars = [("A", "GT3", 16, 30.0, 83.0, 0.0, 0, False),
            ("You", "GT3", 15, 4050.0, 83.0, 0.8, 0, False),
            ("B", "GT3", 14, 3900.0, 83.0, 2.0, 1, False)]
    assert track_neighbours_line(_frame(cars)) == (
        "On track now (live, any class, by lap distance): ahead A (A Entry), GT3, 68 m "
        "ahead; behind B (B Entry), GT3, 150 m behind, 1 lap down (you are lapping it).")


def test_cars_in_the_pits_and_an_unknown_track_length_are_left_out():
    pits = [RIG[0][:7] + (True,), RIG[1][:7] + (True,), RIG[2], RIG[3]]
    assert track_neighbours_line(_frame(pits)) == (
        "On track now (live, any class, by lap distance): ahead Timur (Timur Entry), GT3, "
        "1,800 m ahead.")      # the only car out is never also 'behind'
    f = _frame(RIG)
    f.session.track_len = 0.0
    assert track_neighbours_line(f) is None
