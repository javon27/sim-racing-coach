import numpy as np
import pytest
from simcoach.fuel import (
    fuel_per_lap_from_channel,
    mean_fuel_per_lap,
    plan_race,
    plan_stint,
    binding_stint,
    laps_remaining,
    stint_save_advice,
    ratio_balance,
    FuelPlan,
    StintPlan,
    SaveAdvice,
    RatioAdvice,
)


def test_fuel_per_lap_from_channel(synthetic_lap):
    # fixture fuel decreases 80.0 -> 77.4 over the lap = 2.6 L
    fpl = fuel_per_lap_from_channel(synthetic_lap.channels["fuel"])
    assert abs(fpl - 2.6) < 1e-6


def test_fuel_per_lap_raises_on_no_consumption():
    with pytest.raises(ValueError):
        fuel_per_lap_from_channel(np.array([50.0, 50.0, 50.0]))  # flat, no drop
    with pytest.raises(ValueError):
        fuel_per_lap_from_channel(np.array([50.0, 51.0]))  # refuel/increase


def test_plan_race_no_stop():
    # 20 min Bronze race, 92.5s laps, 2.6 L/lap, 100 L tank
    plan = plan_race(
        fuel_per_lap=2.6, race_duration_s=20 * 60, lap_time_s=92.5, tank_capacity=100.0
    )
    assert isinstance(plan, FuelPlan)
    # laps = ceil(1200/92.5)+1 = 13+1 = 14
    assert plan.race_laps == 14
    assert abs(plan.total_needed - 2.6 * 14 * 1.05) < 1e-6
    assert plan.stops_required == 0
    assert abs(plan.start_fuel - plan.total_needed) < 1e-6


def test_plan_race_requires_stop():
    # 40 min Gold race, 92.5s laps, 3.5 L/lap, 60 L tank
    plan = plan_race(
        fuel_per_lap=3.5, race_duration_s=40 * 60, lap_time_s=92.5, tank_capacity=60.0
    )
    # laps = ceil(2400/92.5)+1 = 26+1 = 27
    assert plan.race_laps == 27
    assert plan.total_needed > 60.0
    assert plan.stops_required >= 1
    assert plan.start_fuel == 60.0  # fill the tank when a stop is needed


def test_mean_fuel_per_lap(synthetic_lap):
    assert abs(mean_fuel_per_lap([synthetic_lap, synthetic_lap]) - 2.6) < 1e-6


def test_laps_override():
    plan = plan_race(2.0, 1200, 92.5, 100.0, laps_override=10)
    assert plan.race_laps == 10
    assert abs(plan.total_needed - 2.0 * 10 * 1.05) < 1e-6


def test_plan_stint_matches_plan_race():
    # plan_stint(resource="fuel") must reproduce plan_race's exact numbers.
    fp = plan_race(fuel_per_lap=3.5, race_duration_s=40 * 60, lap_time_s=92.5, tank_capacity=60.0)
    sp = plan_stint(use_per_lap=3.5, capacity=60.0, race_duration_s=40 * 60, lap_time_s=92.5,
                     resource="fuel")
    assert isinstance(sp, StintPlan)
    assert sp.resource == "fuel"
    assert sp.race_laps == fp.race_laps
    assert abs(sp.total_needed - fp.total_needed) < 1e-6
    assert sp.stops_required == fp.stops_required
    assert abs(sp.start_amount - fp.start_fuel) < 1e-6


def test_plan_stint_energy():
    # Hypercar stint: 900 MJ budget, 40 MJ/lap, 40 min race, 92.5s laps.
    sp = plan_stint(use_per_lap=40.0, capacity=900.0, race_duration_s=40 * 60, lap_time_s=92.5,
                     resource="energy")
    assert sp.resource == "energy"
    assert sp.race_laps == 27
    assert abs(sp.total_needed - 40.0 * 27 * 1.05) < 1e-6
    assert sp.stops_required == 1


def test_plan_stint_raises_on_bad_lap_time():
    with pytest.raises(ValueError):
        plan_stint(use_per_lap=2.0, capacity=100.0, race_duration_s=1200, lap_time_s=0)


def test_binding_stint_energy_more_constraining():
    # Same race: fuel needs 0 stops, energy needs 1 stop -> energy binds.
    fuel_plan = plan_stint(use_per_lap=2.6, capacity=100.0, race_duration_s=40 * 60,
                            lap_time_s=92.5, resource="fuel")
    energy_plan = plan_stint(use_per_lap=40.0, capacity=900.0, race_duration_s=40 * 60,
                              lap_time_s=92.5, resource="energy")
    assert fuel_plan.stops_required == 0
    assert energy_plan.stops_required == 1
    assert binding_stint(fuel_plan, energy_plan) is energy_plan


def test_binding_stint_fuel_more_constraining():
    # Fuel needs 2 stops, energy needs 0 -> fuel binds.
    fuel_plan = plan_stint(use_per_lap=5.0, capacity=30.0, race_duration_s=40 * 60,
                            lap_time_s=92.5, resource="fuel")
    energy_plan = plan_stint(use_per_lap=10.0, capacity=900.0, race_duration_s=40 * 60,
                              lap_time_s=92.5, resource="energy")
    assert fuel_plan.stops_required >= 2
    assert energy_plan.stops_required == 0
    assert binding_stint(fuel_plan, energy_plan) is fuel_plan


def test_binding_stint_requires_plans():
    with pytest.raises(ValueError):
        binding_stint()


def test_plan_race_still_returns_fuel_plan():
    plan = plan_race(fuel_per_lap=2.6, race_duration_s=20 * 60, lap_time_s=92.5, tank_capacity=100.0)
    assert isinstance(plan, FuelPlan)
    assert not isinstance(plan, StintPlan)


def test_laps_remaining():
    assert laps_remaining(50, 2.5) == 20


def test_laps_remaining_raises_on_zero_use():
    with pytest.raises(ValueError):
        laps_remaining(50, 0)


def test_stint_save_advice_fuel_binds_and_must_save():
    advice = stint_save_advice(fuel_laps_remaining=10, energy_laps_remaining=15, laps_to_go=12)
    assert isinstance(advice, SaveAdvice)
    assert advice.binding_resource == "fuel"
    assert advice.binding_laps_remaining == 10
    assert advice.laps_to_go == 12
    assert advice.make_it is False
    assert abs(advice.save_per_lap_fraction - (1 - 10 / 12)) < 1e-6


def test_stint_save_advice_energy_binds_but_makes_it():
    advice = stint_save_advice(fuel_laps_remaining=20, energy_laps_remaining=18, laps_to_go=12)
    assert advice.binding_resource == "energy"
    assert advice.binding_laps_remaining == 18
    assert advice.make_it is True
    assert advice.save_per_lap_fraction == 0.0


def test_ratio_balance_energy_shorter():
    advice = ratio_balance(fuel_laps=20, energy_laps=12)
    assert isinstance(advice, RatioAdvice)
    assert advice.shift_toward == "energy"
    assert advice.fuel_laps == 20
    assert advice.energy_laps == 12
    assert advice.gap_laps == 8


def test_ratio_balance_fuel_shorter():
    advice = ratio_balance(fuel_laps=12, energy_laps=20)
    assert advice.shift_toward == "fuel"
    assert advice.gap_laps == 8


def test_ratio_balance_within_tolerance():
    advice = ratio_balance(fuel_laps=15, energy_laps=15.2)
    assert advice.shift_toward == "balanced"
