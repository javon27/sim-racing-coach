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


@dataclass
class StintPlan:
    """Resource-agnostic version of FuelPlan: same math, any consumable
    that can force a pit stop (fuel liters, energy MJ, ...)."""
    resource: str             # "fuel" | "energy" | ...
    use_per_lap: float
    capacity: float
    race_laps: int
    total_needed: float
    start_amount: float
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


use_per_lap_from_channel = fuel_per_lap_from_channel  # alias: same math, any depleting channel


def plan_stint(use_per_lap: float, capacity: float, race_duration_s: float, lap_time_s: float,
                margin: float = 0.05, laps_override: int | None = None,
                resource: str = "fuel") -> StintPlan:
    """Stint plan for a TIMED race against ANY single consumable (fuel liters,
    energy MJ, ...) — whichever resource this call is given.
    race_laps = ceil(duration/lap_time) + 1  (the +1 = lap in progress at time
      expiry / formation lap; educated guess, override via laps_override).
    total_needed = use_per_lap * race_laps * (1 + margin).
    stops_required = max(0, ceil(total_needed / capacity) - 1).
    start_amount = min(total_needed, capacity)."""
    if lap_time_s <= 0:
        raise ValueError("lap_time_s must be positive")
    race_laps = laps_override if laps_override is not None else math.ceil(race_duration_s / lap_time_s) + 1
    total_needed = use_per_lap * race_laps * (1.0 + margin)
    stops_required = max(0, math.ceil(total_needed / capacity) - 1) if capacity > 0 else 0
    start_amount = min(total_needed, capacity)
    return StintPlan(resource, use_per_lap, capacity, race_laps, total_needed, start_amount,
                      stops_required, margin)


def binding_stint(*plans: StintPlan) -> StintPlan:
    """The most constraining plan among several resources (fuel, energy, ...)
    for the same stint = the one that forces a pit first. Ranked by
    stops_required, then by fraction of capacity used."""
    if not plans:
        raise ValueError("binding_stint requires at least one plan")
    return max(plans, key=lambda p: (p.stops_required, p.total_needed / p.capacity if p.capacity > 0 else float("inf")))


def plan_race(fuel_per_lap: float, race_duration_s: float, lap_time_s: float,
              tank_capacity: float, margin: float = 0.05,
              laps_override: int | None = None) -> FuelPlan:
    """Fuel plan for a TIMED race. See plan_stint for the underlying math;
    this delegates to it and stays fuel-shaped for backward compatibility."""
    sp = plan_stint(fuel_per_lap, tank_capacity, race_duration_s, lap_time_s,
                     margin, laps_override, resource="fuel")
    return FuelPlan(sp.use_per_lap, sp.race_laps, sp.total_needed, sp.start_amount,
                     sp.stops_required, sp.margin)
