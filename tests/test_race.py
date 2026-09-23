"""#152: pure race-state + strategy math for the coach's race tools."""
import dataclasses

import numpy as np
import pytest

from simcoach.lmu.live import FieldSnapshot, VehicleState, empty_snapshot
from simcoach.model_types import LapRecord, RaceSample
from simcoach.race import (
    LapFacts,
    PitWindow,
    RaceTracker,
    catch_lap,
    class_neighbours,
    format_race_state,
    format_strategy,
    gap_slope,
    is_clean,
    lap_facts,
    laps_left,
    live_race,
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


_W = ("fl", "fr", "rl", "rr")


def _rec(lap_no, gap_ahead, fuel_start, wear, *, phase="race", pit=False, partial=None,
         lap_time=100.0, ahead="A. Rival (Rival Entry)"):
    n = 50
    ch = {"fuel": np.linspace(fuel_start, fuel_start - 2.5, n),
          "fuel_capacity": np.full(n, 100.0)}
    for w in _W:
        ch[f"tire_wear_{w}"] = np.linspace(wear + 0.01, wear, n)
    race = RaceSample(class_position=3, class_ahead=ahead, class_gap_ahead=gap_ahead,
                      class_behind="B. Chaser (Chaser Entry)", class_gap_behind=2.0,
                      time_left=None, max_laps=24)
    return LapRecord("lmu", "Spa", "GT3", lap_time, True, float(lap_no), ch, phase=phase,
                     session_id="spa--gt3--offline", position=7, lap_no=lap_no, pit=pit,
                     partial=partial, race=race)


def _tracker_at_lap_12():
    tr = RaceTracker()
    for lap_no, gap, fuel, wear in ((10, 3.1, 32.5, 0.80), (11, 2.8, 30.0, 0.78),
                                    (12, 2.5, 27.5, 0.76)):
        tr.commit(_rec(lap_no, gap, fuel, wear))
    return tr


def test_tracker_snapshot_is_stamped_and_projects_from_the_stint():
    snap = _tracker_at_lap_12().snapshot
    assert snap.as_of_lap == 12 and snap.state.laps_to_go == 12
    assert snap.state.ahead.slope == pytest.approx(-0.3) and snap.state.ahead.catch_lap == 21
    assert snap.state.behind.slope == pytest.approx(0.0) and snap.state.behind.catch_lap is None
    assert snap.strategy.window == PitWindow(22, 13, "fuel", True)


def test_a_new_car_ahead_starts_a_new_gap_trend():
    tr = _tracker_at_lap_12()
    tr.commit(_rec(13, 0.9, 25.0, 0.74, ahead="C. Other (Other Entry)"))
    assert tr.snapshot.state.ahead.who == "C. Other (Other Entry)"
    assert tr.snapshot.state.ahead.slope is None            # one lap behind this car


def test_a_pit_stop_starts_a_new_stint_and_the_guard_holds_until_two_clean_laps():
    tr = _tracker_at_lap_12()
    tr.commit(_rec(13, 2.5, 25.0, 0.76, pit=True, partial="in", lap_time=None))
    tr.commit(_rec(14, 20.0, 100.0, 1.0, pit=True, partial="out", lap_time=None))
    tr.commit(_rec(15, 19.5, 97.5, 0.99))
    snap = tr.snapshot
    assert snap.strategy.note.startswith("not enough laps yet")
    assert snap.state.ahead.slope is None


def test_leaving_the_race_clears_the_snapshot():
    tr = _tracker_at_lap_12()
    assert tr.commit(_rec(1, 3.0, 50.0, 0.9, phase="practice")) is None
    assert tr.snapshot is None


def test_the_cached_snapshot_only_changes_on_a_lap_commit():
    tr = _tracker_at_lap_12()
    snap = tr.snapshot
    first = (format_race_state(snap), format_strategy(snap))
    assert (format_race_state(snap), format_strategy(snap)) == first
    assert tr.snapshot is snap                        # reading never recomputes
    tr.commit(_rec(13, 2.2, 25.0, 0.74))
    assert tr.snapshot is not snap and tr.snapshot.as_of_lap == 13


def test_format_race_state_leads_with_the_lap_stamp_and_states_only_trends():
    text = format_race_state(_tracker_at_lap_12().snapshot)
    assert text.startswith("As of lap 12 (race).")
    assert "Position: P7 overall, P3 in class." in text
    assert "Remaining: 12 of 24 laps." in text
    assert ("Car ahead in class: A. Rival (Rival Entry), 2.5 s ahead; you are closing "
            "0.30 s/lap; at this rate you reach him around lap 21.") in text
    assert "Car behind in class: B. Chaser (Chaser Entry), 2.0 s behind; holding steady" in text


def test_format_strategy_states_margin_save_target_threshold_and_window():
    text = format_strategy(_tracker_at_lap_12().snapshot)
    assert text.startswith("As of lap 12.")
    assert ("Fuel: 2.50 L/lap, 25.0 L on board = 10.0 laps: short by 2.0 laps; "
            "save 0.42 L/lap") in text
    assert "about 23 laps to the 30% threshold (a chosen threshold, not a measured limit)" in text
    assert "Pit window: laps 13 to 22 (latest set by fuel;" in text


def test_format_strategy_guard_reads_as_not_enough_laps():
    tr = RaceTracker()
    tr.commit(_rec(10, 3.1, 32.5, 0.80))
    assert format_strategy(tr.snapshot) == (
        "As of lap 10. No projection: not enough laps yet: needs 2 clean laps in this stint "
        "(out-, in- and partial laps don't count).")


def test_live_race_only_serves_the_snapshot_of_the_race_being_driven():
    """#152 red-team: identity (track, car) + a race phase + the lap in progress being
    the one right after the snapshot (or one more, while its commit is in flight)."""
    tr = RaceTracker()
    for n in (10, 11, 12):
        tr.commit(_rec(n, 3.0, 30.0, 0.8))
    snap = tr.snapshot
    assert (snap.track, snap.car, snap.session_id) == ("Spa", "GT3", "spa--gt3--offline")
    ok = {"track": "Spa", "car": "GT3", "phase": "race"}
    assert live_race(snap, current_lap=13, **ok) == (snap, None)
    assert live_race(snap, current_lap=14, **ok) == (snap, None)
    for lap in (1, 12, 15):                                   # new race / stale
        got, note = live_race(snap, current_lap=lap, **ok)
        assert got is None and note.startswith("Not enough laps yet"), lap
    assert live_race(snap, track="Monza", car="GT3", phase="race", current_lap=13)[0] is None
    assert live_race(snap, track="Spa", car="LMP2", phase="race", current_lap=13)[0] is None
    assert live_race(snap, track="Spa", car="GT3", phase="practice", current_lap=13) == \
        (None, None)
    assert live_race(None, current_lap=1, **ok)[1].startswith("Not enough laps yet")
