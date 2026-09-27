"""#152: pure race-state + strategy math for the coach's race tools."""
import dataclasses
import time

import numpy as np
import pytest

from simcoach.lmu.live import FieldSnapshot, VehicleState, empty_snapshot
from simcoach.model_types import LapRecord, RaceSample
from simcoach.race import (
    WEAR_FLOOR_PCT,
    HistLap,
    LapFacts,
    PitWindow,
    RaceTracker,
    build_snapshot,
    catch_lap,
    class_neighbours,
    departure,
    fill_ratio,
    format_provisional,
    format_race_state,
    format_strategy,
    formation_burn,
    gap_slope,
    history_pick,
    is_clean,
    lap_facts,
    laps_left,
    live_race,
    pick_formation,
    pit_window,
    provisional_strategy,
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
    # Stretching 10 laps over 12 is a 1/6 cut: over MAX_SAVE_FRACTION -> a stop, no target.
    assert s.advice.save_per_lap_fraction == pytest.approx(1 / 6) and s.save_per_lap is None
    # 10 over 11 is a 9% cut: over it too since #195 round 2 (5%, the observed ceiling).
    assert strategy(STINT, laps_to_go=11).save_per_lap is None
    assert s.tire_wheel == "fl" and s.tire_rate_pct == pytest.approx(2.0)
    assert s.tire_remaining_pct == pytest.approx(76.0)
    assert s.tire_laps == pytest.approx(23.0)                  # (76 - 30) / 2
    assert s.window == PitWindow(21, 13, "fuel", True)   # floor(10 / 1.05)


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
    assert s.advice.save_per_lap_fraction == pytest.approx(1 - 7.5 / 12)
    assert s.save_per_lap is None                               # a 37.5% cut: stop instead
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
    assert snap.strategy.window == PitWindow(21, 13, "fuel", True)


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
            "a stop is needed") in text
    assert "about 23 laps to the 30% threshold (a chosen threshold, not a measured limit)" in text
    assert "Pit window: laps 13 to 21 (latest set by fuel;" in text


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


def test_tire_wear_rate_is_fitted_on_lap_number_not_sample_index():
    """#152 red-team: an invalid (track-limits) lap drops out of the clean series, but
    its wear still happened -- so the rate is per LAP NUMBER. 2%/lap true."""
    invalid = dataclasses.replace(_clean(12, 25.0, 0.76, 0.88), clean=False)
    stint = [_clean(10, 30.0, 0.80, 0.90), _clean(11, 27.5, 0.78, 0.89), invalid,
             _clean(13, 22.5, 0.74, 0.87)]
    s = strategy(stint, laps_to_go=11)
    assert s.tire_wheel == "fl" and s.tire_rate_pct == pytest.approx(2.0)
    assert s.tire_laps == pytest.approx(22.0)                  # (74 - 30) / 2


def _fmt_strategy(stint, laps_to_go):
    from simcoach.race import RaceSnapshot, RaceState
    state = RaceState(None, None, None, None, None, laps_to_go, None, None)
    return format_strategy(RaceSnapshot(stint[-1].lap_no, None, state,
                                        strategy(stint, laps_to_go)))


def test_pit_window_with_under_a_lap_left_says_pit_now_not_more_stops():
    """#152 red-team: latest = lap_no + floor(0.6) = lap_no, earliest = lap_no + 1: that
    is 'pit at the next opportunity', not 'more than one stop is needed'."""
    assert pit_window(12, 12, [("fuel", 0.6, 40.0)]) == PitWindow(12, 13, "fuel", True)
    stint = [_clean(10, 6.5, 0.80), _clean(11, 4.0, 0.78), _clean(12, 1.5, 0.76)]
    text = _fmt_strategy(stint, 12)
    assert "Pit window: pit at the next opportunity: fuel has under a lap left." in text
    assert "more than one stop" not in text


def test_tires_are_never_worded_as_running_out():
    """#152 red-team: tires reach a CHOSEN threshold; they don't 'run out'."""
    worn = [_clean(10, 30.0, 0.32), _clean(11, 27.5, 0.31), _clean(12, 25.0, 0.30)]
    text = _fmt_strategy(worn, 5)
    assert ("Pit window: pit at the next opportunity: the front-left tire is at or past "
            "the 30% threshold.") in text
    fresh = [_clean(10, 30.0, 0.50), _clean(11, 27.5, 0.48), _clean(12, 25.0, 0.46)]
    text = _fmt_strategy(fresh, 9)                     # tires bind at lap 12 + 8
    assert "runs out" not in text.split("Pit window:")[1]
    assert "tires reach the 30% threshold" in text


@pytest.mark.parametrize("cap", [float("nan"), float("inf"), 1e30, 9000.0, -5.0, 0.0])
def test_lap_facts_drops_an_implausible_tank_size(synthetic_lap, cap):
    """#152 red-team: mFuelCapacity's offset is unverified -- garbage is 'not reported'."""
    ch = {**synthetic_lap.channels, "fuel_capacity": np.full(10, cap)}
    assert lap_facts(dataclasses.replace(synthetic_lap, channels=ch)).fuel_capacity is None


@pytest.mark.parametrize("cap", [float("nan"), float("inf"), 2.0, 3.0, 400.0])
def test_strategy_treats_an_implausible_tank_size_as_not_reported(cap):
    """#152 red-team: NaN made math.floor raise (the whole snapshot froze); a tank
    smaller than one lap's burn or than the fuel on board (25 L) is not a tank."""
    stint = [dataclasses.replace(f, fuel_capacity=cap) for f in STINT]
    s = strategy(stint, laps_to_go=12)
    assert s.resources[0].per_fill is None
    assert s.window.earliest is None


def test_a_strategy_failure_never_costs_the_race_state(monkeypatch):
    """#152 red-team: state and strategy are computed independently."""
    import simcoach.race as race_mod

    def boom(*_a, **_k):
        raise ValueError("bad channel")

    monkeypatch.setattr(race_mod, "strategy", boom)
    tr = _tracker_at_lap_12()
    snap = tr.snapshot
    assert snap.as_of_lap == 12 and snap.state.ahead.slope == pytest.approx(-0.3)
    assert format_strategy(snap).startswith("As of lap 12. No projection: strategy")


def test_a_rivals_pit_stop_starts_a_new_gap_trend():
    """#152 red-team: a +60 s gap step (his stop) is not 'losing 12 s/lap' -- a step
    over GAP_STEP_BREAK_S breaks the trend like a change of car."""
    tr = RaceTracker()
    for lap_no, gap, fuel in ((10, 3.1, 32.5), (11, 2.8, 30.0), (12, 62.8, 27.5)):
        tr.commit(_rec(lap_no, gap, fuel, 0.8))
    assert tr.snapshot.state.ahead.slope is None
    tr.commit(_rec(13, 62.5, 25.0, 0.8))
    assert tr.snapshot.state.ahead.slope == pytest.approx(-0.3)


# --- #152 red-team minors ---------------------------------------------------------

def test_an_unsampled_lap_says_not_sampled_not_class_leader_or_no_clock():
    tr = _tracker_at_lap_12()
    tr.commit(dataclasses.replace(_rec(13, 2.2, 25.0, 0.74), race=None))
    text = format_race_state(tr.snapshot)
    assert "Car ahead in class: not sampled this lap." in text
    assert "Car behind in class: not sampled this lap." in text
    assert "Remaining: not sampled this lap." in text
    assert "you lead your class" not in text and "reports no lap count" not in text


def test_an_unplaced_player_is_not_sampled_but_keeps_the_clock():
    field = [dataclasses.replace(c, place=0) if c.is_player else c for c in FIELD]
    rs = race_sample(_snap(field, elapsed=1200.0, end_time=3600.0, max_laps=24))
    assert rs.class_position is None and rs.class_ahead is None and rs.class_behind is None
    assert rs.time_left == pytest.approx(2400.0) and rs.max_laps == 24
    tr = _tracker_at_lap_12()
    tr.commit(dataclasses.replace(_rec(13, 2.2, 25.0, 0.74), race=rs))
    text = format_race_state(tr.snapshot)
    assert "Car ahead in class: not sampled this lap." in text
    assert "you lead your class" not in text


def _race_with_clock(time_left):
    tr = RaceTracker()
    for n in (10, 11, 12):
        r = _rec(n, 3.0, 30.0, 0.8)
        tr.commit(dataclasses.replace(r, race=dataclasses.replace(r.race, time_left=time_left)))
    return format_race_state(tr.snapshot)


def test_the_clock_line_claims_your_pace_only_when_the_clock_ends_the_race():
    text = _race_with_clock(5000.0)                     # 50 laps of clock, 12 of lap count
    assert "Remaining: 12 of 24 laps." in text
    assert "at your recent clean pace" not in text
    assert "83:20 on the clock (the lap count ends the race first)." in text
    text = _race_with_clock(500.0)                      # the clock ends it: 5 laps
    assert "about 5 laps at your recent clean pace" in text


def test_a_zero_margin_is_not_worded_as_making_the_flag_with_laps_spare():
    text = _fmt_strategy(STINT, 10)                     # 10.0 laps of fuel, 10 to go
    assert "0.0 laps spare" not in text
    assert "right on the limit" in text


def test_a_tire_already_below_the_floor_says_so():
    worn = [_clean(10, 30.0, 0.31), _clean(11, 27.5, 0.30), _clean(12, 25.0, 0.29)]
    text = _fmt_strategy(worn, 5)
    assert "already at or below the 30% threshold" in text
    assert "about 0 laps" not in text


def test_catch_lap_needs_a_positive_gap():
    assert catch_lap(12, 0.0, -0.3, 24) is None
    assert catch_lap(12, -0.5, -0.3, 24) is None


def test_prompt_wear_floor_comes_from_the_constant():
    import inspect

    import simcoach.coach as coach_mod
    assert f"'to {WEAR_FLOOR_PCT:.0f}% tread'" in coach_mod.AGENTIC_SYSTEM_PROMPT
    assert "'to 30% tread'" not in inspect.getsource(coach_mod)


def test_a_lap_picked_up_mid_lap_keeps_its_line_amounts_but_not_its_burn(synthetic_lap):
    # Red-team 2: the first lap after the app starts mid-race holds only part of the lap's
    # fuel channel; its half-lap burn read as a full lap's said "makes the flag" for a car
    # 4 laps short. The level AT the line is still true.
    ch = dict(synthetic_lap.channels)
    n = len(ch["lap_distance"])
    ch["lap_distance"] = np.linspace(2000.0, 4000.0, n)
    f = lap_facts(dataclasses.replace(synthetic_lap, channels=ch))
    assert f.fuel_used is None and f.energy_used is None
    assert f.fuel_end == pytest.approx(float(ch["fuel"][-1]))
    assert f.wear_end is not None


# --- #161 provisional pre-race plan / #164 stops as pit-menu settings ----------------

def _practice(n=3, energy=0.125, fuel=2.5, cap=100.0):
    return [LapFacts(lap_no=i, lap_time=101.0, clean=True, fuel_used=fuel,
                     fuel_end=cap - fuel * i, fuel_capacity=cap, energy_used=energy,
                     energy_end=1.0 - energy * i,
                     wear_end={w: 0.99 - 0.01 * i for w in _W})
            for i in range(1, n + 1)]


def test_provisional_plan_before_any_race_lap_assumes_full_energy_not_a_full_tank():
    """#161 red-team: Road Atlanta started on 73.8 L at 100% VE, not a full 120 L tank --
    on a virtual-energy car the fuel on board is unknown until race lap 1."""
    s = provisional_strategy(_practice(), laps_to_go=14)
    assert s.note is None
    by = {r.name: r for r in s.resources}
    assert by["energy"].laps == pytest.approx(8.0) and "fuel" not in by
    assert s.burn_only == {"fuel": pytest.approx(2.5)}
    # latest keeps the 5% reserve: floor(8 / 1.05) = 7; earliest 14 - 8 = 6.
    assert s.window == PitWindow(7, 6, "energy", True)
    assert s.tire_laps is None           # practice wear spans tire changes: not projected


def test_provisional_plan_needs_practice_laps_and_a_race_length():
    assert provisional_strategy(_practice(n=1), 15).note.startswith("not enough laps yet")
    assert provisional_strategy(_practice(), None).note.startswith("race length unknown")


def test_provisional_plan_mid_race_starts_from_the_race_lap_on_board():
    lap1 = LapFacts(lap_no=1, lap_time=None, clean=False, fuel_end=97.0,
                    fuel_capacity=100.0, energy_end=0.88)
    s = provisional_strategy(_practice(), 14, start=lap1)
    assert {r.name: r.on_board for r in s.resources} == {"fuel": 97.0, "energy": 0.88}
    assert s.window.latest == 1 + 6          # floor(0.88 / (0.125 * 1.05))


def test_format_provisional_is_labelled_and_states_the_stop_as_pit_menu_settings():
    s = provisional_strategy(_practice(), laps_to_go=14)
    text = format_provisional(s, as_of_lap=0, basis="15 laps (the live race's lap count)",
                              n_practice=3)
    assert text.startswith("PROVISIONAL — from your last 3 clean practice/qualifying laps "
                           "this session; firms up after 2 clean race laps.")
    assert "Race length: 15 laps (the live race's lap count)." in text
    assert ("Assumes you start on 100% virtual energy; the fuel on board is unknown until "
            "race lap 1.") in text
    assert ("Fuel: 2.50 L/lap measured; the amount on board is unknown, so no laps-left "
            "figure.") in text
    assert "L on board" not in text
    assert "Pit window: laps 6 to 7" in text
    # 7 laps from a stop at the end of lap 7, 12.5 %/lap, +5 % margin = 91.9 -> 92 %;
    # a lap earlier costs 12.5 x 1.05 = 13.1 %.
    assert ("Pit menu: set virtual energy to 92% for a stop at the end of lap 7 "
            "(the 7 laps from there to the flag, +5% margin); each lap earlier needs "
            "about 13.1% more.") in text
    assert "Fuel ratio: not read before a race lap" in text
    assert "Tyres: wear not measured in this race stint yet" in text
    assert "crew" not in text.lower()


def test_format_strategy_pit_menu_fuel_ratio_direction_tyres_and_repairs():
    stint = [dataclasses.replace(f, energy_used=0.1, energy_end=e)
             for f, e in zip(STINT, (0.9, 0.8, 0.75))]     # 7.5 laps of energy vs 10 of fuel
    # 30 to go: the FL (76 %, 2 %/lap -> 23 laps to 30 %) can't make it, the rest (88 %,
    # 1 %/lap -> 58 laps) can.
    snap = dataclasses.replace(build_snapshot(stint, "s"), strategy=strategy(stint, 30))
    text = format_strategy(snap, damage={"dent_max": 2.0, "dent_total": 3.0, "detached": False,
                                         "overheating": False, "wheels_flat": [],
                                         "wheels_detached": []}, damage_lap=12)
    assert "LMU has no crew radio" not in text      # that is the prompt's job, not the data
    assert ("Fuel ratio: fuel on board lasts 10.0 laps, virtual energy 7.5 — energy runs "
            "out first: shift the ratio toward energy (less fuel per % of virtual energy). "
            "Direction only: the ratio's unit isn't confirmed, so no number.") in text
    assert ("Tyres: change FL (it reaches the 30% threshold before the flag on the "
            "measured wear); keep FR, RL, RR.") in text
    assert "Repairs: damage measured on lap 12 (dent severity max 2" in text
    assert "crew" not in text.lower()


def test_format_strategy_fuel_only_car_sets_fuel_litres_and_keeps_tyres():
    text = format_strategy(_tracker_at_lap_12().snapshot)
    # Stop at lap 21 (floor(10 / 1.05)); 3 laps x 2.5 L x 1.05 = 7.9 -> 8 L.
    assert ("Pit menu: set fuel to 8 L for a stop at the end of lap 21 (the 3 laps from "
            "there to the flag, +5% margin); each lap earlier needs about 2.6 L more.") in text
    assert "Tyres: no change needed" in text
    assert "Repairs: no damage data captured" in text


def _road_atlanta_quali(n=5):
    """Road Atlanta 2026-09-23 qualifying burn: 2.27 L and 3.44 % virtual energy a lap."""
    return [LapFacts(i, 83.0, True, fuel_used=2.27, fuel_capacity=120.0,
                     energy_used=0.0344) for i in range(1, n + 1)]


def test_the_stint_one_stop_lap_keeps_the_margin():
    """#161 red-team: the raw latest (lap 29) left 0.3% virtual energy in the tank."""
    s = provisional_strategy(_road_atlanta_quali(), 44)
    assert s.window.latest == 27 and s.window.earliest == 15   # floor(29.07 / 1.05) = 27
    text = format_provisional(s, 0, "44 laps", 5)
    assert "Pit window: laps 15 to 27" in text


def test_a_full_fill_that_barely_covers_the_rest_says_how_thin_it_is():
    """#161 red-team: 3.44 %/lap x 58 laps needs 104.7 % after the stop with the margin;
    plan_stint capped it at 100 % and the line still claimed '+5% margin'."""
    text = format_provisional(provisional_strategy(_road_atlanta_quali(), 58), 0, "58 laps", 5)
    assert ("Pit menu: set virtual energy to 100% for a stop at the end of lap 29 — a full "
            "fill covers 29.1 laps for the 29 from there to the flag: only 0.2% spare, "
            "under the 5% margin.") in text
    assert "each lap earlier" not in text


def test_a_fuel_only_car_full_tank_overflow_and_no_virtual_energy_wording():
    practice = [LapFacts(i, 90.0, True, fuel_used=3.2, fuel_capacity=100.0)
                for i in range(1, 6)]
    text = format_provisional(provisional_strategy(practice, 62), 0, "62 laps", 5)
    assert "Assumes you start on a full tank." in text
    assert "virtual energy" not in text.lower()
    assert ("Pit menu: set fuel to a full tank (100 L) for a stop at the end of lap 31 — a "
            "full fill covers 31.2 laps for the 31 from there to the flag: only 0.8 L "
            "spare, under the 5% margin.") in text


def test_no_fuel_or_energy_projection_is_never_no_stop_needed():
    """#161 red-team, Fuji GT3 laps 7251-7255: VE flat at 0 and no tank size -> nothing
    projected, which printed 'Pit window: no stop needed'."""
    flat = [LapFacts(i, 100.0, True, fuel_end=50.0) for i in range(1, 4)]
    text = format_strategy(dataclasses.replace(build_snapshot(flat, "s"),
                                               strategy=strategy(flat, 20)))
    assert "no stop needed" not in text
    assert "Pit window: no fuel/energy projection, so no stop call." in text
    assert "Pit menu: no fuel/energy projection — nothing computed." in text


def test_a_wheel_the_car_reports_flat_is_named_in_the_pit_menu():
    """#164 red-team: Road Atlanta lap 19 -- the FL puncture (tread read 0.0) -- a flat
    the car reports is a change, named, not left to the wear projection."""
    dmg = {"dent_max": 0.0, "dent_total": 0.0, "detached": False, "overheating": False,
           "wheels_flat": ["fl"], "wheels_detached": []}
    text = format_strategy(_tracker_at_lap_12().snapshot, damage=dmg, damage_lap=12)
    assert ("Tyres: change FL — reads flat on lap 12; the others stay above the 30% "
            "threshold to the flag on the measured wear.") in text
    assert text.count("Tyres:") == 1 and "no change needed" not in text
    no_stop = dataclasses.replace(_tracker_at_lap_12().snapshot,
                                  strategy=strategy(STINT, 8))      # fuel makes the flag
    text = format_strategy(no_stop, damage=dmg, damage_lap=12)
    assert "Tyres: change FL — reads flat on lap 12." in text


def _road_atlanta_lap_11(**over):
    """#162: race lap 11 at Road Atlanta, 2026-09-23 (chat turn 224), replayed through
    the current code: 31 laps to go, energy 3.4 %/lap with 64 % left (18.9 laps),
    fuel 2.23 L/lap with 49.3 L on board."""
    rs = RaceSample(class_position=7, max_laps=over.get("max_laps", 42))
    return build_snapshot([
        LapFacts(lap_no=10, lap_time=83.5, clean=True, race=rs, fuel_used=2.23,
                 fuel_end=51.53, fuel_capacity=90.0, energy_used=0.0338,
                 energy_end=0.6738),
        LapFacts(lap_no=11, lap_time=82.7, clean=True, race=rs, fuel_used=2.23,
                 fuel_end=49.3, fuel_capacity=90.0, energy_used=0.0338,
                 energy_end=0.64),
    ], "s")


def test_strategy_states_the_number_of_stops_itself():
    """#162: 'short by 12.1 laps; a stop is needed' beside 'Pit window: laps 13 to 29'
    and the coach said "one stop won't be enough". The stop COUNT is code's to state."""
    text = format_strategy(_road_atlanta_lap_11())
    assert "Pit window: laps 13 to 29" in text
    assert "Stops to the flag: one." in text
    assert "more than one stop" not in text


def test_strategy_states_more_than_one_stop_when_one_full_service_cannot_reach():
    # An 80-lap race: a full charge (29.6 laps) can't cover the 69 to go in one stop.
    snap = _road_atlanta_lap_11(max_laps=80)
    text = format_strategy(snap)
    assert not snap.strategy.window.one_stop
    assert "Stops to the flag: more than one." in text
    assert "Stops to the flag: one." not in text


def test_strategy_states_no_stop_when_every_resource_makes_the_flag():
    snap = _road_atlanta_lap_11()
    snap.strategy.window = None
    assert "Stops to the flag: none on these rates." in format_strategy(snap)
    # No projection at all: no stop count either way.
    snap.strategy.resources = []
    assert "Stops to the flag" not in format_strategy(snap)


def test_strategy_never_counts_stops_off_an_unknown_tank():
    snap = _road_atlanta_lap_11()
    snap.strategy.window = PitWindow(latest=20, earliest=None, binding="fuel", one_stop=True)
    assert "Stops to the flag: not known (tank size not reported)." in format_strategy(snap)


# --- #184: pre-race plan from this car's stored laps at this track ---------------------

def _h(ts, session, phase, lap_time, fuel, energy):
    return HistLap(ts, phase, session, LapFacts(0, lap_time, True, fuel_used=fuel,
                                                energy_used=energy))


# Road Atlanta Z06, 2026-09-23 (real rows): the 13:xx TF Sport session ran fuel ratio
# 1.00 (L per 1% VE after a fill; ids 7486-7490), the 21:xx 13 Autosport one 0.75 (ids
# 7531-7532) -- the same 2.25 L/lap at 2.4 vs 3.4 %VE/lap. Newest first, as the caller
# passes them.
_RA_EVENING = "michelin-raceway-road-atlanta--13-autosport-2026-13-lm--online"
_RA_NOON = "michelin-raceway-road-atlanta--tf-sport-2026-2-lm--online"
_RA = ([_h(1790215383.4888265, _RA_EVENING, "qualifying", 85.7913, 2.2486, 0.03392),
        _h(1790215297.686399, _RA_EVENING, "qualifying", 87.3708, 2.2642, 0.03422)]
       + [_h(ts, _RA_NOON, "race", t, f, e) for ts, t, f, e in (
           (1790188542.7709658, 83.7617, 2.2013, 0.02353),
           (1790188458.968076, 83.1533, 2.2448, 0.02393),
           (1790188375.770235, 82.9324, 2.1757, 0.02318),
           (1790188292.969675, 83.5526, 2.1715, 0.02304),
           (1790188209.3709934, 84.2498, 2.2663, 0.02401))])
_RA_FILLS = {(_RA_EVENING, "qualifying"): 0.75, (_RA_NOON, "race"): 1.00}
_NOON = time.strftime("%Y-%m-%d %H:%M", time.localtime(1790188209.3709934))


def test_history_pick_is_the_median_and_never_projects_a_channel_measured_once():
    # Sarthe Z06 shapes (ids 7425, 7426): a slow lap can't drag the median.
    hist = [_h(3, "s", "race", 245.3115, 7.4275, 0.0922), _h(2, "s", "race", 243.8760, 7.3333,
                                                            None),
            _h(1, "s", "race", 300.0, 4.0, None)]
    med, notes, _ = history_pick(hist, {}, None)
    assert med.fuel_used == pytest.approx(7.3333) and med.lap_time == pytest.approx(245.3115)
    assert med.energy_used is None                 # measured on one lap: not projected
    assert ("Virtual energy: the fuel ratio these laps ran at isn't recorded -- at another "
            "ratio this figure is wrong.") in notes
    assert ("Virtual energy: only 1 clean lap to measure it on at this fuel ratio -- it needs "
            "2, so none is projected yet.") in notes
    assert history_pick(hist[:1], {}, None)[0] is None


def test_history_pick_takes_virtual_energy_only_at_this_sessions_fuel_ratio():
    """#184 red-team C1: pooled, Road Atlanta read 2.4 %/lap -- the evening race burned
    3.16-3.53 and would have run dry around lap 11."""
    med, notes, sessions = history_pick(_RA, _RA_FILLS, 0.75)
    assert med.energy_used == pytest.approx((0.03392 + 0.03422) / 2)
    assert med.fuel_used == pytest.approx(2.2448)          # L/lap is physical: every lap
    assert sessions == {_RA_EVENING}
    assert notes == [(f"Virtual energy: 2 stored laps at your current fuel ratio (0.75 L per "
                      f"1% on board after a fill, within 2%); 5 at fuel ratio 1.00 on {_NOON} "
                      "race left out.")]


def test_history_pick_with_no_lap_this_session_still_keys_on_its_fuel_ratio():
    """#184 red-team F1: cut before the evening session's first clean lap, the pooled plan
    said '15 laps x 2.4%/lap'; the race burned ~51%. Its out-lap already shows the fill:
    0.75 -- and no stored lap ran at it."""
    med, notes, _ = history_pick(_RA[2:], _RA_FILLS, 0.75)
    assert med.energy_used is None and med.fuel_used == pytest.approx(2.2013)
    assert ("Virtual energy: only 0 clean laps to measure it on at this fuel ratio -- it "
            "needs 2, so none is projected yet.") in notes
    # Laps whose sitting's fill can't be read (no pit row stored) can't be confirmed.
    med, notes, _ = history_pick(_RA[2:], {}, 0.75)
    assert med.energy_used is None
    assert notes[0].endswith("; 5 whose fuel ratio isn't recorded left out.")


def test_history_pick_with_no_row_this_session_states_the_ratio_it_measured_at():
    """No row of this session at all: the newest stored sitting's ratio is used, and
    said, every time -- at another setting the VE figure is wrong."""
    med, notes, _ = history_pick(_RA[2:], _RA_FILLS, None)
    assert med.energy_used == pytest.approx(0.02353)
    assert notes == [("Virtual energy: 5 stored laps at your newest stored fuel ratio (1.00 L "
                      "per 1% on board after a fill, within 2%) -- measured at 1.00: at "
                      "another ratio this figure is wrong, so set your fuel ratio before "
                      "trusting it.")]
    # Two stored settings: only the newest one's laps, the other named.
    med, notes, _ = history_pick(_RA, _RA_FILLS, None)
    assert med.energy_used == pytest.approx((0.03392 + 0.03422) / 2)
    assert f"; 5 at fuel ratio 1.00 on {_NOON} race left out -- measured at 0.75" in notes[0]


def test_one_odd_lap_this_session_never_throws_out_history_at_the_same_ratio():
    """#184 red-team F2, Long Beach Z06 (real rows): 09-26 qualifying lap 7642 burned 1.01 L
    per 1% VE, 6.9% off 09-24's race laps (ids 7579-7582) -- but both days filled at 0.99.
    Per-lap scatter in one sitting runs to 12%; the fill is the setting."""
    lb24 = "grand-prix-of-long-beach--tf-sport-2026-33-elms--online"
    lb26 = "grand-prix-of-long-beach--13-autosport-2026-13-lm--online"
    hist = ([_h(1790474305.0260348, lb26, "qualifying", 106.3879, 2.23459, 0.022073)]
            + [_h(ts, lb24, "race", t, f, e) for ts, t, f, e in (
                (1790309486.404799, 82.8444, 1.93309, 0.020444),
                (1790309403.4138086, 83.7756, 1.98018, 0.020932),
                (1790309319.8148165, 84.3062, 1.97481, 0.020879),
                (1790309235.4010775, 97.1418, 2.06905, 0.019523))])
    med, notes, _ = history_pick(hist, {(lb26, "qualifying"): 0.99, (lb24, "race"): 0.99},
                                 0.99)
    assert med.energy_used == pytest.approx(0.020879)            # all five pooled
    assert not [n for n in notes if "fuel ratio" in n], notes


def test_history_pick_counts_laps_at_the_fastest_sittings_pace_when_they_differ():
    """#184 red-team M3, Silverstone BMW 2026-09-09/10 (ids 7116-7121, 7125-7129): one
    session's practice ran ~56 s laps, its race ~62 s; pooled, a 20-minute race counted
    too few laps."""
    s = "silverstone-national-circuit--team-wrt-2026-32-wec--offline"
    hist = ([_h(ts, s, "race", t, f, e) for ts, t, f, e in (
                (1789018564.3926907, 63.274, 1.5931, 0.01741),
                (1789018501.1900609, 62.413, 1.5328, 0.01674),
                (1789018438.796852, 60.812, 1.5884, 0.01738),
                (1789018377.9925086, 62.253, 1.5703, 0.01722))]
            + [_h(ts, s, "practice", t, f, e) for ts, t, f, e in (
                (1789015392.5161924, 55.332, 1.6275, 0.01778),
                (1789015337.1223092, 55.983, 1.6084, 0.01757),
                (1789015281.1241658, 55.806, 1.604, 0.01763))])
    med, notes, _ = history_pick(hist, {}, None)
    assert med.lap_time == pytest.approx(55.806)
    practice = time.strftime("%Y-%m-%d %H:%M", time.localtime(1789015281.1241658))
    assert (f"Lap time: 0:55.806, the median of your {practice} practice session -- the "
            "fastest of your sessions here") in notes[-1]


def test_the_fuel_ratio_is_read_off_the_tank_while_the_car_moves(synthetic_lap):
    """Long Beach 2026-09-26 qualifying row 7641: it opens on the LAST session's car still
    at speed (Sarthe's 17.29 L at 19%, 188 km/h), sits parked on Sarthe's 56.55 L at 65%
    for 13 minutes, shows the 1.0 L-at-100% garage placeholder, then the real fill driven
    away: 27.72 L at 28% -- 0.99. Here the stale tail is set to 40% VE (above the fill,
    as a longer previous session's would be): it must still never count."""
    ch = dict(synthetic_lap.channels)
    n = len(ch["lap_distance"])
    t, a, b = n // 10, n // 3, 2 * n // 3
    ch["speed"] = np.concatenate([np.full(t, 188.0), np.zeros(b - t), np.full(n - b, 150.0)])
    ch["fuel"] = np.concatenate([np.full(t, 34.8), np.full(a - t, 56.55), np.full(b - a, 1.0),
                                 np.linspace(27.72, 25.28, n - b)])
    ch["virtual_energy"] = np.concatenate([np.full(t, 0.40), np.full(a - t, 0.65),
                                           np.full(b - a, 1.0),
                                           np.linspace(0.28, 0.2602, n - b)])
    dep = departure(dataclasses.replace(synthetic_lap, channels=ch))
    assert dep == pytest.approx((0.28, 27.72))
    # Sarthe 2026-09-17 row 7418 showed a 76 L / 100% garage preview before its real load
    # (33.93 L at 39%, 0.87). A row that ENDS parked on it must not read 0.76.
    park = dict(synthetic_lap.channels)
    park["speed"] = np.concatenate([np.full(a, 150.0), np.zeros(n - a)])
    park["fuel"] = np.concatenate([np.linspace(33.93, 32.4, a), np.full(n - a, 76.0)])
    park["virtual_energy"] = np.concatenate([np.linspace(0.39, 0.377, a), np.full(n - a, 1.0)])
    assert departure(dataclasses.replace(synthetic_lap, channels=park)) == pytest.approx(
        (0.39, 33.93))
    # The fullest departure is the fill; later ones drift as both drain.
    assert fill_ratio([dep, (0.2602, 25.28), None]) == pytest.approx(0.99)
    assert fill_ratio([None]) is None


def test_formation_burn_reads_a_flat_channel_as_zero_and_a_refill_as_none(synthetic_lap):
    ch = dict(synthetic_lap.channels)
    n = len(ch["lap_distance"])
    # Sarthe 2026-09-26 formation (id 7635): 56.55 -> 54.54 L, VE flat at 65%.
    ch.update(fuel=np.linspace(56.55, 54.5393, n), virtual_energy=np.full(n, 0.65))
    b = formation_burn(dataclasses.replace(synthetic_lap, channels=ch))
    assert b["fuel"] == pytest.approx(2.0107) and b["energy"] == 0.0
    ch.update(fuel=np.linspace(53.94, 56.55, n))            # id 7634: a garage refill
    assert formation_burn(dataclasses.replace(synthetic_lap, channels=ch))["fuel"] is None


def test_pick_formation_plans_the_largest_when_they_disagree_and_says_so():
    """Sarthe: 2.36% and 2.41% VE on 09-17/18 (ids 7422, 7454), 0 on 09-26 (id 7635). An
    extra 2.4% at the start is cheap; 2.4% short eats half the 5% margin."""
    old = [(1789709360.6580997, "t", {"fuel": 2.1329, "energy": 0.02408}),
           (1789698569.2824166, "t", {"fuel": 2.0952, "energy": 0.02360})]
    fuel, energy, _, how = pick_formation(old, None)
    assert energy == pytest.approx((0.02408 + 0.02360) / 2) and how is None
    assert fuel == pytest.approx((2.1329 + 2.0952) / 2)
    new = [(1790471894.7841697, "r", {"fuel": 2.0107, "energy": 0.0})] + old
    fuel, energy, _, how = pick_formation(new, None)
    days = [time.strftime("%Y-%m-%d", time.localtime(ts)) for ts, _, _ in reversed(new)]
    assert energy == pytest.approx(0.02408)
    assert how == (f"stored formation laps differ: 2.4% ({days[0]}, {days[1]}) or 0.0% "
                   f"({days[2]}); planning 2.4%")
    assert pick_formation(new, {"r"})[1] == 0.0          # its fuel-ratio pool only


def test_the_pre_race_plan_states_what_the_race_needs_from_the_start():
    """#184: 'how much energy for this race' -- laps x burn + the formation lap, rounded
    UP with the margin like the pit menu; a timed race states N and N+1 laps and is
    planned on N+1."""
    s = provisional_strategy(_practice(energy=0.07), 15)          # timed: planned on N+1
    text = format_provisional(s, 0, "x", 3, timed=True, formation=(1.2, 0.0, "x", None))
    assert "Formation lap: 0.0% virtual energy, 1.20 L (from x)." in text
    # 14 x 7% = 98% -- under a full charge, but not with the margin: 102.9%.
    assert ("Virtual energy needed to the flag: 14 laps × 7.0%/lap = 98.0% + 0.0% formation "
            "lap = 98.0% → start with at least 103% — more than a full charge; or 15 laps = "
            "105.0% + 0.0% formation lap = 105.0% → start with at least 111% — more than a "
            "full charge (with the 5% margin).") in text
    assert ("Fuel needed to the flag: 14 laps × 2.50 L/lap = 35.0 L + 1.2 L formation lap = "
            "36.2 L → start with at least 39 L; or 15 laps = 37.5 L + 1.2 L formation lap = "
            "38.7 L → start with at least 41 L (with the 5% margin).") in text
    assert ("Laps to go: planned on 15, the longer count (the flag at the end of lap 15 at "
            "the latest).") in text
    lap_count = format_provisional(provisional_strategy(_practice(energy=0.07), 14), 0,
                                   "14 laps", 3)
    assert "; or 15 laps" not in lap_count and "formation lap, not measured" in lap_count
    assert "Laps to go: 14 (flag at the end of lap 14)." in lap_count
    lap1 = LapFacts(lap_no=1, lap_time=None, clean=False, fuel_end=97.0,
                    fuel_capacity=100.0, energy_end=0.88)
    # After a race lap the on-board amounts answer it instead.
    assert "needed to the flag" not in format_provisional(
        provisional_strategy(_practice(), 13, start=lap1), 1, "14 laps", 3)


def test_an_assumed_start_loses_the_formation_lap_before_the_green():
    """#184 red-team F3, Sarthe 40 min: '103.3% -> more than a full charge' printed beside
    '100% left = 10.9 laps ... Stops: none' -- the formation lap burns before the green."""
    s = provisional_strategy(_practice(energy=0.09), 11, formation=(2.1, 0.024, "x", None))
    assert {r.name: r.on_board for r in s.resources} == {"energy": pytest.approx(0.976)}
    text = format_provisional(s, 0, "x", 3, timed=True, formation=(2.1, 0.024, "x", None))
    assert ("Assumes you start on 100% virtual energy (97.6% at the green, after the "
            "formation lap)") in text
    # 0.976 / 0.09 = 10.8 laps for the planned 11: short -- by a save (#195 round 1),
    # never a bare "none".
    assert ("Stops to the flag: none if you save 0.1% energy/lap (1% of your burn); "
            "otherwise one.") in text
    assert "Stops to the flag: none on these rates" not in text
    # 10.8 laps: the stop is at the end of lap 10, the shorter count's flag lap.
    assert ("Note: a stop at the end of lap 10 applies only if the overall leader hasn't "
            "taken the flag by then") in text, text


def test_a_start_read_off_the_formation_row_is_stated_not_assumed():
    """#184 red-team M4: race lap 1 in progress (Sarthe 2026-09-26, id 7635) printed
    'Assumes you start on 100% virtual energy' beside '65% left'."""
    start = LapFacts(lap_no=0, lap_time=None, clean=False, fuel_end=54.5393,
                     fuel_capacity=120.0, energy_end=0.65)
    text = format_provisional(provisional_strategy(_practice(), 5, start=start), 0,
                              "5 laps", 3, measured=True)
    assert "As of the start line: 65% virtual energy, 54.5 L on board." in text
    assert "Assumes" not in text and "needed to the flag" not in text


def _lgc(laps, position, time_left, max_laps=None, class_position=None):
    """#195: clean race laps of a stored 15-minute Algarve Logitech G Challenge race
    (lap_facts of the rows; overall place and clock as stored at each line)."""
    return [LapFacts(n, t, True, position=position,
                     race=RaceSample(class_position=class_position or position,
                                     time_left=left,
                                     max_laps=max_laps),
                     fuel_used=fu, fuel_end=fe, fuel_capacity=120.0, energy_used=eu,
                     energy_end=ee)
            for (n, t, fu, fe, eu, ee), left in zip(laps, time_left)]


# ids 7619-7621 (race 2): P2 overall, 679.4 s left at the end of lap 2, 573.6 at lap 3.
# He took P1 at lap 5 and won in 9 laps with about 1.1 laps of fuel spare.
_LGC_P2 = ((1, 113.4858, 2.874190, 24.857746, 0.0344858, 0.3055142),
           (2, 107.0182, 2.711848, 22.145138, 0.0333804, 0.2721243))
_LGC_P2_LAP3 = (3, 105.8863, 2.738089, 19.405531, 0.0338191, 0.2382859)


def test_a_timed_race_plans_the_live_figures_on_the_longer_count():
    """#195: after race lap 2 the live read planned N laps while the pre-race plan
    planned N+1 -- 'Stops: none' against the plan's count. 679.4 / 110.25 = 6.2 -> 7
    laps, or 8 when the leader's flag comes after this car's lap 9: 22.1 L at 2.79 L/lap
    is 7.9 laps -- enough for 7, short of 8. Round 1 (R1b): 0.02 L/lap covers it, so the
    Stops line says so -- a bare 'one' was repeated as 'pit on lap 9' (he won with fuel
    to spare) -- and the pit menu is set only in case he doesn't save."""
    snap = build_snapshot(_lgc(_LGC_P2, 2, (786.4, 679.4)), "algarve--lgc--online")
    assert snap.state.laps_to_go == 7 and snap.strategy.laps_to_go == 8
    text = format_strategy(snap)
    assert ("Laps to go: planned on 8, the longer count (the flag at the end of lap 10 at "
            "the latest): 7 laps if the overall leader takes the flag before you finish lap "
            "9, 8 if not.") in text, text
    assert ("Fuel: 2.79 L/lap, 22.1 L on board = 7.9 laps: short by 0.1 laps; save 0.02 "
            "L/lap (1% of your burn) to finish without stopping.") in text
    assert ("Stops to the flag: none if you save 0.02 L/lap (1% of your burn); otherwise "
            "one.") in text
    assert "\nPit menu, only if you stop instead of saving: set virtual energy to 4%" in text
    assert "\nPit window, only if you stop instead of saving: laps 3 to 9" in text


def test_road_atlanta_2_needed_a_stop_not_a_12_percent_save():
    """#195 round 2, ids 7485-7487 (P38, 1484.6 s left at lap 3): 39% VE at 2.35%/lap
    is 16.6 laps for the planned 19 -- a 12.6% cut every lap, where the store's laps saved
    at most ~7% at a 1-3% pace cost. He pitted. Said: a stop, never 'none if you save'."""
    stint = [LapFacts(1, 147.2163, False, position=45, fuel_end=43.705361,
                      fuel_capacity=120.0, energy_end=0.4375405),
             LapFacts(2, 84.2498, True, position=38, fuel_used=2.266273, fuel_end=41.437493,
                      fuel_capacity=120.0, energy_used=0.0240079, energy_end=0.4135154),
             LapFacts(3, 83.5526, True, position=38, race=RaceSample(time_left=1484.6),
                      fuel_used=2.171507, fuel_end=39.265501, fuel_capacity=120.0,
                      energy_used=0.0230446, energy_end=0.3904658)]
    snap = build_snapshot(stint, "road-atlanta--tf-2--online")
    assert snap.strategy.laps_to_go == 19
    assert snap.strategy.advice.save_per_lap_fraction == pytest.approx(0.1265, abs=1e-3)
    text = format_strategy(snap)
    assert ("Virtual energy: 2.4%/lap, 39% left = 16.6 laps: short by 2.4 laps; a stop is "
            "needed (saving that much — over 5% a lap — is not realistic).") in text, text
    assert "Stops to the flag: one." in text
    assert "if you save" not in text and "instead of saving" not in text


@pytest.mark.parametrize("cut, saved", [(0.049, True), (0.051, False)])
def test_a_save_is_offered_up_to_five_percent_of_the_burn(cut, saved):
    """#195 round 2, at the MAX_SAVE_FRACTION boundary: the Algarve lap-2 stint with the
    fuel on board set (derived; no stored lap lands near 5%) to 8 laps less `cut`."""
    per = (2.874190 + 2.711848) / 2
    laps = _LGC_P2[:1] + ((*_LGC_P2[1][:3], per * 8 * (1 - cut), *_LGC_P2[1][4:]),)
    text = format_strategy(build_snapshot(_lgc(laps, 2, (786.4, 679.4)), "algarve"))
    assert ("Stops to the flag: none if you save 0.14 L/lap (5% of your burn); otherwise "
            "one." in text) is saved, text
    assert ("Stops to the flag: one." in text) is not saved


def test_a_margin_under_a_twentieth_of_a_lap_either_way_is_right_on_the_limit():
    """#195 round 1 (R1a), the same race at lap 3: 19.4 L at 2.77 L/lap = 6.998 laps for
    the planned 7 printed 'short by 0.0 laps; save 0.00 L/lap' beside 'Stops: one'."""
    snap = build_snapshot(_lgc(_LGC_P2 + (_LGC_P2_LAP3,), 2, (786.4, 679.4, 573.6)),
                          "algarve--lgc--online")
    assert snap.strategy.laps_to_go == 7
    text = format_strategy(snap)
    assert ("Fuel: 2.77 L/lap, 19.4 L on board = 7.0 laps: right on the limit — no "
            "measurable margin to the flag.") in text, text
    assert "short by 0.0" not in text and "save 0.00" not in text
    assert ("Stops to the flag: none if you save a little (you're right on the limit); "
            "otherwise one.") in text


def test_a_save_never_stands_in_for_a_tire_change():
    """#195 round 1: the same lap-3 stint with the tires wearing 10%/lap (derived: 70%
    tread, 4 laps to the threshold) -- saving fuel can't avoid that stop."""
    laps = _lgc(_LGC_P2 + (_LGC_P2_LAP3,), 2, (786.4, 679.4, 573.6))
    for f, w in zip(laps, (0.9, 0.8, 0.7)):
        f.wear_end = dict.fromkeys(("fl", "fr", "rl", "rr"), w)
    text = format_strategy(build_snapshot(laps, "algarve"))
    assert "Stops to the flag: one." in text, text
    assert "\nPit menu: set" in text and "instead of saving" not in text


def test_a_stop_at_the_end_of_the_shorter_counts_flag_lap_is_said_to_be_conditional():
    """#195 round 1 (R1c): lap 9 is the flag lap if the leader's flag comes first -- a
    stop there must never read as scheduled."""
    text = format_strategy(build_snapshot(_lgc(_LGC_P2, 2, (786.4, 679.4)), "algarve"))
    caveat = ("a stop at the end of lap 9 applies only if the overall leader hasn't taken "
              "the flag by then — if he has, lap 9 is your last.")
    assert ("(latest set by fuel; earliest is the first lap one full service reaches the "
            f"flag from); {caveat}") in text, text
    assert ("(the 1 laps from there to the flag, +5% margin); each lap earlier needs about "
            f"3.6% more. Note: {caveat}") in text
    # Derived from the same rows with 15 L on board (5.4 laps): the stop is at lap 7,
    # before the shorter count's flag lap -- nothing to add.
    laps = _LGC_P2[:1] + ((*_LGC_P2[1][:3], 15.0, *_LGC_P2[1][4:]),)
    early = format_strategy(build_snapshot(_lgc(laps, 2, (786.4, 679.4)), "algarve"))
    assert "for a stop at the end of lap 7" in early, early
    assert "overall leader hasn't taken" not in early
    assert "Stops to the flag: one." in early


def test_the_class_leader_behind_the_overall_leader_still_plans_the_longer_count():
    """#195 round 1: only OVERALL P1 makes the clock's count his own -- a class leader
    overall P5 plans on N+1."""
    snap = build_snapshot(_lgc(_LGC_P2, 5, (786.4, 679.4), class_position=1), "algarve")
    assert snap.strategy.laps_to_go == 8
    text = format_strategy(snap)
    assert "planned on 8" in text and "you lead overall" not in text


def test_the_overall_leader_plans_on_the_clocks_own_count_and_is_told_why():
    """#195: P1 overall at the line (ids 7720-7721): the flag is his own, N is exact."""
    snap = build_snapshot(_lgc(((1, 112.2601, 2.847802, 24.847352, 0.0343398, 0.3056602),
                                (2, 107.1328, 2.683538, 22.163054, 0.0332702, 0.2723804)),
                               1, (787.2, 680.0)), "algarve--lgc--online")
    assert snap.strategy.laps_to_go == 7
    text = format_strategy(snap)
    assert ("Laps to go: 7 (flag at the end of lap 9): you lead overall (P1 at the end of "
            "lap 2), so the flag falls at your first crossing after the clock runs out."
            ) in text, text
    assert "planned on" not in text


def test_a_lap_count_race_is_planned_on_its_own_count():
    """#195: the lap count ends the race before the clock does: nothing to add."""
    snap = build_snapshot(_lgc(_LGC_P2, 2, (786.4, 679.4), max_laps=8), "algarve--lgc--online")
    assert snap.strategy.laps_to_go == snap.state.laps_to_go == 6
    text = format_strategy(snap)
    assert "Laps to go: 6 (flag at the end of lap 8)." in text and "planned on" not in text
    assert "Stops to the flag: none on these rates." in text


def test_once_the_clock_has_run_out_the_plan_never_adds_a_lap():
    """#195: ids 7688-7689, P9 with the clock at -84.8 s at the end of lap 9. Every stored
    timed race ended on that lap; planning 2 laps said 'Stops: one' on the cool-down."""
    snap = build_snapshot(_lgc(((8, 111.2122, 2.674817, 6.301032, 0.0329434, 0.0761219),
                                (9, 109.7401, 2.620545, 3.679727, 0.0323598, 0.0437526)),
                               9, (25.0, -84.8)), "algarve--lgc--online")
    assert snap.strategy.laps_to_go == 1
    text = format_strategy(snap)
    # Round 1: never "this is the last lap" -- the race may already be over.
    assert ("Laps to go: 1 — the clock ran out before the end of lap 9: one more lap unless "
            "the overall leader has already taken the flag (then lap 9 was your last)."
            ) in text, text
    assert "planned on" not in text
    assert "Stops to the flag: none on these rates." in text
    assert ("Remaining: the clock ran out before the end of lap 9: one more lap unless the "
            "overall leader has already taken the flag") in format_race_state(snap)


def test_the_overall_leader_crossing_after_the_clock_ran_out_took_the_flag():
    """#195 round 1: ids 7727-7728, P1 with the clock at -64.2 s at the end of lap 9 --
    his crossing was the chequered flag: the race is over, no plan."""
    snap = build_snapshot(_lgc(((8, 106.5799, 2.6477, 6.075461, 0.0328856, 0.0728023),
                                (9, 106.3484, 2.711057, 3.363644, 0.0335158, 0.0392767)),
                               1, (42.2, -64.2)), "algarve--lgc--online")
    over = ("the race is over: you lead overall and crossed the line after the clock ran "
            "out (the end of lap 9) — that was the chequered flag.")
    assert format_strategy(snap) == f"As of lap 9. No projection: {over}"
    assert f"Remaining: none — {over}" in format_race_state(snap)


def _closing(gaps, position, time_left=950.0, max_laps=None, side="ahead",
             class_position=3):
    """#201: laps 10-12, clean at 100 s, the gap to the in-class car on `side` shrinking
    0.3 s/lap. A timed race with 950 s left at lap 12 is 10 laps to go (the flag at lap
    22), or 11 (lap 23)."""
    who = "A. Rival (Rival Entry)" if side == "ahead" else "B. Chaser (Chaser Entry)"
    return [LapFacts(n, 100.0, True, position=position,
                     race=RaceSample(class_position=class_position, time_left=time_left,
                                     max_laps=max_laps,
                                     **{f"class_{side}": who, f"class_gap_{side}": g}))
            for n, g in zip((10, 11, 12), gaps)]


def test_a_catch_on_the_longer_counts_flag_lap_is_said_conditionally():
    """#201: the catch cutoff is the plan's (plans_longer), never the shorter count's --
    3.2 s at 0.3 s/lap is 11 laps: lap 23, possible when the leader's flag comes late."""
    snap = build_snapshot(_closing((3.8, 3.5, 3.2), position=7), "s")
    assert snap.state.laps_to_go == 10 and snap.strategy.laps_to_go == 11
    assert snap.state.ahead.catch_lap == 23
    text = format_race_state(snap)
    assert ("you are closing 0.30 s/lap; at this rate you reach him around lap 23, only if "
            "the race runs that long (laps to go: 10 laps if the overall leader takes the "
            "flag before you finish lap 22, 11 if not).") in text, text
    assert "not before the flag" not in text
    # #186's live read: the place changed, the same car is ahead -- the lap's line, as said.
    live = format_race_state(snap, (8, RaceSample(class_position=3,
                                                  class_ahead="A. Rival (Rival Entry)")))
    assert ("Car ahead in class at the end of lap 12: A. Rival (Rival Entry), 3.2 s ahead; "
            "you are closing 0.30 s/lap; at this rate you reach him around lap 23, only if "
            "the race runs that long") in live, live
    # A catch inside the shorter count (2.9 s: lap 22) needs no caveat.
    text = format_race_state(build_snapshot(_closing((3.5, 3.2, 2.9), position=7), "s"))
    assert "you reach him around lap 22." in text, text


def test_the_overall_leader_is_past_the_flag_on_the_longer_counts_lap():
    """#201: P1 overall (so P1 in class, a car behind closing) -- the flag is his own at
    lap 22, so the chaser's lap-23 catch is past it."""
    snap = build_snapshot(_closing((3.8, 3.5, 3.2), position=1, side="behind",
                                   class_position=1), "s")
    assert snap.state.ahead is None and snap.state.behind.catch_lap is None
    text = format_race_state(snap)
    assert "Car ahead in class: none — you lead your class." in text, text
    assert ("B. Chaser (Chaser Entry), 3.2 s behind; he is closing 0.30 s/lap; not before "
            "the flag at this rate.") in text


def test_a_lap_count_race_keeps_its_own_catch_cutoff():
    """#201: a 22-lap race ends at lap 22 -- a lap-23 catch is past the flag."""
    snap = build_snapshot(_closing((3.8, 3.5, 3.2), position=7, time_left=None,
                                   max_laps=22), "s")
    assert snap.state.laps_to_go == 10 and snap.state.ahead.catch_lap is None
    text = format_race_state(snap)
    assert "; not before the flag at this rate." in text and "only if" not in text
    snap = build_snapshot(_closing((3.5, 3.2, 2.9), position=7, time_left=None,
                                   max_laps=22), "s")
    assert snap.state.ahead.catch_lap == 22
