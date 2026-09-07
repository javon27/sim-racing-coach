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


# Scanned table, cached against the Results dir's mtime. LMU adds a result file
# when a session ends, which bumps the directory's mtime -- so a stale signature
# means a new session (possibly a new car) was logged and we re-scan. A cheap stat
# on each call; the full parse only reruns when the directory actually changed.
_cache: dict = {"sig": None, "table": {}}


def _entry_table() -> dict[str, tuple[str, str | None]]:
    rd = results_dir()
    if rd is None:
        return {}
    try:
        sig = os.stat(rd).st_mtime_ns
    except OSError:
        return _cache["table"]  # dir briefly unavailable -- serve the last scan
    # ponytail: no lock. Concurrent sidecar threads may race to re-scan; the scan is
    # idempotent and the worst case is a redundant parse, so a lock isn't worth it.
    if sig != _cache["sig"]:
        _cache["table"] = _scan_results(str(rd))
        _cache["sig"] = sig
    return _cache["table"]


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


def resolve_car_class(vehicle_name: str) -> str | None:
    """The scraped car class ('GT3', 'Hyper', ...) for an entry name, or None if
    the entry isn't in the result logs yet."""
    hit = _entry_table().get((vehicle_name or "").strip())
    return hit[1] if hit else None
