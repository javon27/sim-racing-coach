import numpy as np
import pytest
from simcoach.fuel import (
    fuel_per_lap_from_channel,
    mean_fuel_per_lap,
    plan_race,
    FuelPlan,
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
