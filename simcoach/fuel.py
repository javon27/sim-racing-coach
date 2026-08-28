from __future__ import annotations
from dataclasses import dataclass
import math
import numpy as np


@dataclass
class FuelPlan:
    fuel_per_lap: float      # liters
    race_laps: int
    total_needed: float      # liters, incl. margin
    start_fuel: float        # recommended starting fuel
    stops_required: int
    margin: float


def fuel_per_lap_from_channel(fuel: np.ndarray) -> float:
    """Liters used over one lap = first - last sample. Raises ValueError if
    the lap shows no positive consumption (flat, or refuel making it rise)."""
    if len(fuel) < 2:
        raise ValueError("fuel channel too short")
    consumed = float(fuel[0] - fuel[-1])
    if consumed <= 0:
        raise ValueError(f"no positive fuel consumption over lap ({consumed:.3f} L)")
    return consumed


def mean_fuel_per_lap(records) -> float:
    """Average per-lap consumption over records whose fuel channel shows use.
    records: objects with .channels['fuel']. Skips laps without usable fuel."""
    vals = []
    for r in records:
        try:
            vals.append(fuel_per_lap_from_channel(r.channels["fuel"]))
        except (ValueError, KeyError):
            continue
    if not vals:
        raise ValueError("no records with usable fuel data")
    return sum(vals) / len(vals)


def plan_race(fuel_per_lap: float, race_duration_s: float, lap_time_s: float,
              tank_capacity: float, margin: float = 0.05,
              laps_override: int | None = None) -> FuelPlan:
    """Fuel plan for a TIMED race.
    race_laps = ceil(duration/lap_time) + 1  (the +1 = lap in progress at time
      expiry / formation lap; educated guess, override via laps_override).
    total_needed = fuel_per_lap * race_laps * (1 + margin).
    stops_required = max(0, ceil(total_needed / tank) - 1).
    start_fuel = min(total_needed, tank_capacity)."""
    if lap_time_s <= 0:
        raise ValueError("lap_time_s must be positive")
    race_laps = laps_override if laps_override is not None else math.ceil(race_duration_s / lap_time_s) + 1
    total_needed = fuel_per_lap * race_laps * (1.0 + margin)
    stops_required = max(0, math.ceil(total_needed / tank_capacity) - 1) if tank_capacity > 0 else 0
    start_fuel = min(total_needed, tank_capacity)
    return FuelPlan(fuel_per_lap, race_laps, total_needed, start_fuel, stops_required, margin)
