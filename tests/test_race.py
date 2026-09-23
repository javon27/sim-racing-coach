"""#152: pure race-state + strategy math for the coach's race tools."""
import dataclasses

import numpy as np
import pytest

from simcoach.lmu.live import FieldSnapshot, VehicleState, empty_snapshot
from simcoach.race import (
    LapFacts,
    PitWindow,
    catch_lap,
    class_neighbours,
    gap_slope,
    is_clean,
    lap_facts,
    laps_left,
    pit_window,
    race_sample,
    strategy,
)


def _car(cid, place, cls, gap_next, *, player=False, driver=None, car="Entry",
         class_position=0):
    return VehicleState(
        id=cid, place=place, class_position=class_position, driver=driver or f"D{cid}",
        car=car, cls=cls, lap_dist=0.0, speed=0.0, sector=0, laps=10, in_pits=False,
        is_player=player, best_lap=None, last_lap=None, best_s1=None, best_s2=None,
        gap_leader=None, laps_behind_leader=0, gap_next=gap_next,
        laps_behind_next=0 if gap_next is not None else 1)


# Overall order: P3 GT3 rival, P4 a Hypercar, P5 the player (GT3), P6 GT3 chaser.
# gap_next is each car's gap to the car ONE PLACE UP, as the game reports it.
FIELD = [
    _car(3, 5, "GT3", 1.0, player=True, driver="You", class_position=2),
    _car(1, 3, "GT3", 4.0, driver="A. Rival", car="Rival Entry", class_position=1),
    _car(2, 4, "Hypercar", 0.8),
    _car(4, 6, "GT3", 2.4, driver="B. Chaser", car="Chaser Entry", class_position=3),
]


def _snap(cars, **session):
    s = dataclasses.replace(empty_snapshot().session, live=True, phase="race",
                            track_len=4000.0, **session)
    return FieldSnapshot(session=s, cars=cars)


def test_class_gap_sums_the_links_through_a_car_of_another_class():
    ahead, gap_a, behind, gap_b = class_neighbours(FIELD)
    assert ahead.driver == "A. Rival" and gap_a == pytest.approx(1.8)   # 0.8 + 1.0, not 1.0
    assert behind.driver == "B. Chaser" and gap_b == pytest.approx(2.4)


def test_a_lapped_link_makes_the_class_gap_none_not_a_false_seconds_figure():
    field = [dataclasses.replace(c, gap_next=None, laps_behind_next=1) if c.id == 2 else c
             for c in FIELD]
    ahead, gap_a, _, gap_b = class_neighbours(field)
    assert ahead.driver == "A. Rival" and gap_a is None
    assert gap_b == pytest.approx(2.4)


def test_the_class_leader_has_no_car_ahead_and_the_last_car_none_behind():
    ahead, gap_a, behind, _ = class_neighbours([c for c in FIELD if c.id != 1])
    assert ahead is None and gap_a is None and behind.driver == "B. Chaser"
    _, _, behind, gap_b = class_neighbours([c for c in FIELD if c.id != 4])
    assert behind is None and gap_b is None


def test_race_sample_reads_class_position_neighbours_and_the_clock():
    rs = race_sample(_snap(FIELD, elapsed=1200.0, end_time=3600.0, max_laps=None))
    assert rs.class_position == 2
    assert rs.class_ahead == "A. Rival (Rival Entry)"
    assert rs.class_gap_ahead == pytest.approx(1.8)
    assert rs.class_behind == "B. Chaser (Chaser Entry)"
    assert rs.class_gap_behind == pytest.approx(2.4)
    assert rs.time_left == pytest.approx(2400.0) and rs.max_laps is None


def test_race_sample_has_no_clock_without_an_end_time_and_is_none_when_not_live():
    rs = race_sample(_snap(FIELD, elapsed=1200.0, end_time=0.0, max_laps=24))
    assert rs.time_left is None and rs.max_laps == 24
    assert race_sample(empty_snapshot()) is None


def test_gap_slope_is_the_least_squares_change_per_lap_and_needs_two_laps():
    assert gap_slope([(10, 3.1), (11, 2.8), (12, 2.5)]) == pytest.approx(-0.3)
    assert gap_slope([(12, 2.5)]) is None
    assert gap_slope([]) is None


def test_catch_lap_only_when_closing_faster_than_steady_and_before_the_flag():
    assert catch_lap(12, 2.5, -0.3, 24) == 21        # 2.5 / 0.3 = 8.3 -> 9 laps
    assert catch_lap(12, 2.5, -0.3, 20) is None      # not before the flag
    assert catch_lap(12, 2.5, -0.3, None) == 21      # race length unknown: still a lap
    assert catch_lap(12, 2.5, -0.02, 24) is None     # inside the steady band
    assert catch_lap(12, 2.5, 0.2, 24) is None       # opening, not closing


def test_laps_left_for_lap_count_timed_and_both():
    assert laps_left(12, 24, None, None) == 12
    assert laps_left(12, None, 2400.0, 100.0) == 24
    assert laps_left(12, None, 2350.0, 100.0) == 24   # a started lap is still driven
    assert laps_left(12, 30, 2400.0, 100.0) == 18     # whichever ends the race first
    assert laps_left(12, None, -5.0, 100.0) == 1      # the clock ran out: the last lap
    assert laps_left(12, None, 2400.0, None) is None  # timed, no clean pace yet
    assert laps_left(12, None, None, 100.0) is None
    assert laps_left(30, 24, None, None) == 0


def test_is_clean_excludes_pit_out_in_partial_invalid_and_untimed(synthetic_lap):
    assert is_clean(synthetic_lap)
    for over in ({"pit": True}, {"partial": "out"}, {"partial": "in"}, {"valid": False},
                 {"lap_time": None}):
        assert not is_clean(dataclasses.replace(synthetic_lap, **over)), over


def test_lap_facts_takes_burn_level_capacity_and_tread_at_the_line(synthetic_lap):
    ch = {**synthetic_lap.channels, "fuel_capacity": np.full(10, 110.0)}
    f = lap_facts(dataclasses.replace(synthetic_lap, channels=ch, lap_no=7, position=4))
    assert f.lap_no == 7 and f.position == 4 and f.clean
    assert f.fuel_used == pytest.approx(2.6) and f.fuel_end == pytest.approx(77.4)
    assert f.fuel_capacity == pytest.approx(110.0)
    assert f.energy_used is None and f.energy_end is None      # no virtual_energy channel
    assert f.wear_end["fl"] == pytest.approx(0.97)


def test_pit_window_from_the_binding_limit():
    assert pit_window(12, 12, [("fuel", 7.4, 40.0)]) == PitWindow(19, 13, "fuel", True)
    assert pit_window(12, 12, [("fuel", 7.4, 40.0), ("tires", 5.2, 30.0)]) == \
        PitWindow(17, 13, "tires", True)
    assert pit_window(12, 12, [("fuel", 7.4, 4.0)]) == PitWindow(19, 20, "fuel", False)
    assert pit_window(12, 12, [("fuel", 7.4, None)]) == PitWindow(19, None, "fuel", True)
    assert pit_window(12, 12, [("fuel", 30.0, 40.0)]) is None


def _clean(lap_no, fuel_end, wear_fl, wear_rest=0.9, **over):
    wear = {"fl": wear_fl, "fr": wear_rest, "rl": wear_rest, "rr": wear_rest}
    return LapFacts(lap_no=lap_no, lap_time=100.0, clean=True, fuel_used=2.5,
                    fuel_end=fuel_end, fuel_capacity=100.0, wear_end=wear, **over)


STINT = [_clean(10, 30.0, 0.80, 0.90), _clean(11, 27.5, 0.78, 0.89),
         _clean(12, 25.0, 0.76, 0.88)]


def test_strategy_fuel_short_save_target_tire_life_and_window():
    s = strategy(STINT, laps_to_go=12)
    assert s.note is None
    (fuel,) = s.resources
    assert fuel.name == "fuel" and fuel.laps == pytest.approx(10.0)
    assert fuel.per_fill == pytest.approx(40.0)
    assert not s.advice.make_it and s.advice.binding_resource == "fuel"
    assert s.save_per_lap == pytest.approx(2.5 / 6)            # stretch 10 laps over 12
    assert s.tire_wheel == "fl" and s.tire_rate_pct == pytest.approx(2.0)
    assert s.tire_remaining_pct == pytest.approx(76.0)
    assert s.tire_laps == pytest.approx(23.0)                  # (76 - 30) / 2
    assert s.window == PitWindow(22, 13, "fuel", True)


def test_strategy_makes_it_with_no_stop():
    s = strategy(STINT, laps_to_go=8)
    assert s.advice.make_it and s.save_per_lap is None and s.window is None


def test_strategy_needs_two_clean_laps_in_this_stint_and_a_race_length():
    out_lap = LapFacts(lap_no=12, lap_time=None, clean=False, fuel_end=90.0)
    assert strategy([_clean(11, 27.5, 0.78), out_lap], 12).note.startswith(
        "not enough laps yet")
    assert strategy(STINT, None).note.startswith("race length unknown")


def test_energy_binds_when_it_runs_out_first():
    stint = [dataclasses.replace(f, energy_used=0.1, energy_end=e)
             for f, e in zip(STINT, (0.9, 0.8, 0.75))]     # 7.5 laps of energy vs 10 of fuel
    s = strategy(stint, laps_to_go=12)
    assert s.advice.binding_resource == "energy"
    assert s.save_per_lap == pytest.approx(0.1 * (1 - 7.5 / 12))
    assert s.window.binding == "energy" and s.window.latest == 19
