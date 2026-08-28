from dataclasses import dataclass
from typing import Optional
import numpy as np

from simcoach.model_types import LapRecord
from simcoach.corners import Corner, CornerMap


@dataclass
class CornerMetrics:
    corner_no: int
    name: str
    direction: str
    entry_spd: float
    apex_spd: float
    min_spd: float
    exit_spd: float
    brake_point: float
    throttle_point: float
    trailbrake_overlap: float
    min_spd_delta: Optional[float] = None


@dataclass
class LapAnalysis:
    lap_time: float
    corners: list[CornerMetrics]
    has_reference: bool


def _metrics_for_corner(record: LapRecord, corner: Corner) -> CornerMetrics:
    dist = record.channels["lap_distance"]
    speed = record.channels["speed"]
    brake = record.channels["brake"]
    throttle = record.channels["throttle"]
    steering = record.channels["steering"]

    mask = (dist >= corner.start_dist) & (dist <= corner.end_dist)
    idx = np.where(mask)[0]

    if idx.size == 0:
        # Degenerate window: no telemetry samples fall inside this corner.
        return CornerMetrics(
            corner_no=corner.number,
            name=corner.name,
            direction=corner.direction,
            entry_spd=float("nan"),
            apex_spd=float("nan"),
            min_spd=float("nan"),
            exit_spd=float("nan"),
            brake_point=corner.end_dist,
            throttle_point=corner.end_dist,
            trailbrake_overlap=0.0,
        )

    win_dist = dist[idx]
    win_speed = speed[idx]
    win_brake = brake[idx]
    win_throttle = throttle[idx]
    win_steering = steering[idx]

    entry_spd = float(win_speed[0])
    exit_spd = float(win_speed[-1])
    min_i = int(np.argmin(win_speed))
    min_spd = float(win_speed[min_i])

    apex_i = int(np.argmin(np.abs(win_dist - corner.apex_dist)))
    apex_spd = float(win_speed[apex_i])

    braking = np.where(win_brake > 0.1)[0]
    brake_point = float(win_dist[braking[0]]) if braking.size else corner.end_dist

    after_min = np.where(win_throttle[min_i:] > 0.8)[0]
    throttle_point = (
        float(win_dist[min_i + after_min[0]]) if after_min.size else corner.end_dist
    )

    trailbrake_overlap = float(np.mean((win_brake > 0.1) & (np.abs(win_steering) > 0.1)))

    return CornerMetrics(
        corner_no=corner.number,
        name=corner.name,
        direction=corner.direction,
        entry_spd=entry_spd,
        apex_spd=apex_spd,
        min_spd=min_spd,
        exit_spd=exit_spd,
        brake_point=brake_point,
        throttle_point=throttle_point,
        trailbrake_overlap=trailbrake_overlap,
    )


def analyze_lap(
    record: LapRecord, cmap: CornerMap, reference: Optional[LapRecord] = None
) -> LapAnalysis:
    corners = [_metrics_for_corner(record, corner) for corner in cmap.corners]

    has_reference = reference is not None
    if has_reference:
        ref_metrics = {c.number: _metrics_for_corner(reference, c) for c in cmap.corners}
        for cm in corners:
            ref = ref_metrics.get(cm.corner_no)
            if ref is not None:
                cm.min_spd_delta = cm.min_spd - ref.min_spd

    return LapAnalysis(lap_time=record.lap_time, corners=corners, has_reference=has_reference)
