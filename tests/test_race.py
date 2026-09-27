"""#152: pure race-state + strategy math for the coach's race tools."""
import dataclasses

import numpy as np
import pytest

from simcoach.lmu.live import FieldSnapshot, VehicleState, empty_snapshot
from simcoach.model_types import LapRecord, RaceSample
from simcoach.race import (
    WEAR_FLOOR_PCT,
    LapFacts,
    PitWindow,
    RaceTracker,
    build_snapshot,
    catch_lap,
    class_neighbours,
    format_provisional,
    format_race_state,
    format_strategy,
    gap_slope,
    history_burn,
    is_clean,
    lap_facts,
    laps_left,
    live_race,
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
    assert strategy(STINT, laps_to_go=11).save_per_lap == pytest.approx(2.5 / 11)
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
    from simcoach.race import RaceSnapshot
    return format_strategy(RaceSnapshot(stint[-1].lap_no, None, None,
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

def test_history_burn_is_the_median_and_never_projects_a_channel_measured_once():
    # Sarthe Z06 shapes (ids 7425, 7426): a pit/yellow-slow lap can't drag the median.
    laps = [LapFacts(3, 245.3115, True, fuel_used=7.4275, energy_used=0.0922),
            LapFacts(4, 243.8760, True, fuel_used=7.3333, energy_used=None),
            LapFacts(5, 300.0, True, fuel_used=4.0, energy_used=None)]
    med = history_burn(laps)
    assert med.fuel_used == pytest.approx(7.3333) and med.lap_time == pytest.approx(245.3115)
    assert med.energy_used is None                 # measured on one lap: not projected
    assert history_burn(laps[:1]) is None


def test_the_pre_race_plan_states_what_the_race_needs_from_the_start():
    """#184: 'how much energy for this race' -- laps x burn, with the margin, in code."""
    text = format_provisional(provisional_strategy(_practice(), 14), 0, "14 laps", 3)
    assert ("Virtual energy needed to the flag: 14 laps × 12.5%/lap = 175.0%, 183.8% with "
            "the 5% margin — more than a full charge.") in text
    assert ("Fuel needed to the flag: 14 laps × 2.50 L/lap = 35.0 L, 36.8 L with the 5% "
            "margin.") in text
    lap1 = LapFacts(lap_no=1, lap_time=None, clean=False, fuel_end=97.0,
                    fuel_capacity=100.0, energy_end=0.88)
    # After a race lap the on-board amounts answer it instead.
    assert "needed to the flag" not in format_provisional(
        provisional_strategy(_practice(), 13, start=lap1), 1, "14 laps", 3)
