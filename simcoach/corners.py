from dataclasses import dataclass
from pathlib import Path
import json
from typing import Optional
import numpy as np
from simcoach.model_types import LapRecord


# Calibration knob: true value for LMU confirmed by feasibility spike (Task 2)
RIGHT_IS_POSITIVE = True


@dataclass
class Corner:
    """A single corner in a track map."""
    number: int
    name: str
    direction: str
    start_dist: float
    apex_dist: float
    end_dist: float


class CornerMap:
    """Loads and queries corner data from a track map JSON file."""

    def __init__(self, corners: list[Corner]):
        self.corners = corners

    @classmethod
    def load(cls, path: str) -> "CornerMap":
        """Load corner map from JSON file.

        Raises ValueError if a corner's window is degenerate/wrapped
        (start_dist >= end_dist) or its apex_dist falls outside
        [start_dist, end_dist].
        """
        with open(path) as f:
            data = json.load(f)
        corners = [Corner(**c) for c in data["corners"]]
        for c in corners:
            if not (c.start_dist < c.end_dist):
                raise ValueError(
                    f"corner {c.number}: start_dist ({c.start_dist}) must be < end_dist ({c.end_dist})"
                )
            if not (c.start_dist <= c.apex_dist <= c.end_dist):
                raise ValueError(
                    f"corner {c.number}: apex_dist ({c.apex_dist}) must be within "
                    f"[start_dist, end_dist] ({c.start_dist}, {c.end_dist})"
                )
        return cls(corners)

    def corner_at(self, dist: float) -> Optional[Corner]:
        """Return the first corner whose [start_dist, end_dist] contains dist (inclusive boundaries)."""
        for corner in self.corners:
            if corner.start_dist <= dist <= corner.end_dist:
                return corner
        return None


def compute_direction(record: LapRecord, start: float, end: float) -> str:
    """
    Compute corner direction from steering geometry.

    Returns "left" or "right" based on mean steering over [start, end] distance range.
    Sign convention controlled by RIGHT_IS_POSITIVE constant.

    Raises ValueError if the window contains no telemetry or all NaN steering samples.
    """
    lap_distance = record.channels["lap_distance"]
    steering = record.channels["steering"]

    mask = (lap_distance >= start) & (lap_distance <= end)

    if not np.any(mask):
        raise ValueError(f"no telemetry in window [{start}, {end}]")

    # Use nanmean to skip NaN samples; result is nan if all samples are NaN
    mean_steering = np.nanmean(steering[mask])

    if np.isnan(mean_steering):
        raise ValueError(f"no telemetry in window [{start}, {end}]")

    if mean_steering > 0:
        return "right" if RIGHT_IS_POSITIVE else "left"
    else:
        return "left" if RIGHT_IS_POSITIVE else "right"


def verify_map_directions(record: LapRecord, cmap: CornerMap) -> list[tuple[int, str, str]]:
    """
    Verify that map directions match computed directions from telemetry.

    Returns list of (corner_no, declared, computed) for any mismatches or undetermined corners.
    Empty list means all directions are consistent and verifiable.
    """
    mismatches = []
    for corner in cmap.corners:
        try:
            computed = compute_direction(record, corner.start_dist, corner.end_dist)
        except ValueError:
            # Out-of-range or no telemetry window
            computed = "undetermined"

        if corner.direction != computed:
            mismatches.append((corner.number, corner.direction, computed))
    return mismatches
