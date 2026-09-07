"""Resolve the game's live vehicle/entry name to a car MODEL for display + the coach.

LMU shared memory / REST expose only the *class* ('GT3') and the *team/entry* name
(e.g. 'Iron Dames 2025 #85:ELMS', 'Team WRT 2026 #32:WEC') -- NOT the specific car
model. LMU's own result logs, however, name both together for every entry the player
has ever driven or raced against: each result XML's <Driver> carries VehName (the
exact same string live telemetry reports), CarType (the model), and CarClass. So we
scrape those into a {VehName: (model, class)} table instead of hand-maintaining one.

Unknown entries fall back to the class, so the coach still runs (just without a
model-specific note)."""
from __future__ import annotations

import glob
import os
import xml.etree.ElementTree as ET
from functools import lru_cache

from .lmu.install import results_dir


def _scan_results(results_root: str | None) -> dict[str, tuple[str, str | None]]:
    """Entry name -> (car model, car class) from every LMU result XML. Best-effort:
    a missing dir or an unparseable file is skipped, never raised."""
    table: dict[str, tuple[str, str | None]] = {}
    if not results_root:
        return table
    for path in glob.glob(os.path.join(results_root, "*.xml")):
        try:
            root = ET.parse(path).getroot()
        except Exception:
            continue
        for driver in root.iter("Driver"):
            veh_name = (driver.findtext("VehName") or "").strip()
            car_type = (driver.findtext("CarType") or "").strip()
            if veh_name and car_type:
                table[veh_name] = (car_type, (driver.findtext("CarClass") or "").strip() or None)
    return table


@lru_cache(maxsize=1)
def _entry_table() -> dict[str, tuple[str, str | None]]:
    # ponytail: cached once per process -- new entries appear only when the player
    # meets a new car, and a sidecar restart refreshes it. Add invalidation if the
    # long-running sidecar needs to pick up new results without a restart.
    return _scan_results(str(results_dir()) if results_dir() else None)


def resolve_car(vehicle_name: str, car_class: str | None = None) -> str:
    """Return a car MODEL display name for a live vehicle/entry name.

    Exact match against the scraped result table first; falls back to '<class> car'
    (e.g. 'GT3 car') when the entry is unknown, or the raw entry name if the class is
    also unknown. Never raises."""
    name = (vehicle_name or "").strip()
    hit = _entry_table().get(name)
    if hit:
        return hit[0]
    if car_class:
        return f"{car_class} car"
    return vehicle_name or "unknown car"
