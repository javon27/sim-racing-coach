from dataclasses import dataclass
from typing import Optional
import numpy as np

from simcoach.model_types import LapRecord
from simcoach.corners import Corner, CornerMap, compute_direction, in_window


@dataclass
class CornerMetrics:
    corner_no: int
    name: str
    direction: str
    apex_dist: float
    entry_spd: float
    apex_spd: float
    min_spd: float
    exit_spd: float
    brake_point: float
    throttle_point: float
    trailbrake_overlap: float
    peak_brake: float = 0.0          # max brake input in the corner (0..1)
    peak_lat_g: float = 0.0          # max |lateral g| — how hard the car was loaded
    min_grip: float = float("nan")   # lowest grip fraction any tire saw (near 0 = sliding)
    # Extended inputs/loads — nan when the source channel is absent (older laps).
    peak_brake_g: float = float("nan")   # max deceleration g while braking
    peak_accel_g: float = float("nan")   # max acceleration g while on throttle
    peak_steer: float = float("nan")     # max |steering| (0..1 of full lock)
    abs_pct: float = float("nan")        # fraction of corner with ABS engaged (braking too hard)
    tc_pct: float = float("nan")         # fraction of corner with TC engaged (too much throttle)
    track_edge_min: float = float("nan") # closest approach to the track edge (m); low = using all the track
    min_spd_delta: Optional[float] = None
    label: str = ""                          # displayed designation ("8A")
    apexes: Optional[list[float]] = None     # apex lap_distances (>1 = multi-apex)
    apex_min_speeds: Optional[list[float]] = None  # min speed per apex (parallel to apexes)
    section: Optional[str] = None            # named section this corner sits in, if any


@dataclass
class LapAnalysis:
    lap_time: float
    corners: list[CornerMetrics]
    has_reference: bool
    # Lap-level tire state (the coachable signal: imbalance + warm-up). Per-corner
    # tire temps are deferred until the replay overlay actually consumes them.
    tire_means: Optional[dict[str, float]] = None      # {'fl','fr','rl','rr'}: °C mean over lap
    tire_warmup: Optional[tuple[float, float]] = None  # (early °C, late °C), all-wheel mean
    wheels: Optional[dict] = None  # lap-level brake/pressure/wear from extended capture


_TIRE_KEYS = ("tire_temp_fl", "tire_temp_fr", "tire_temp_rl", "tire_temp_rr")
_WHEELS = ("fl", "fr", "rl", "rr")


def _wheel_summary(record: LapRecord):
    """Lap-level brake temp / pressure / wear per wheel from the extended capture.
    Returns None for laps captured before those channels existed."""
    ch = record.channels
    if not all(f"brake_temp_{w}" in ch for w in _WHEELS):
        return None

    def per_wheel(prefix, fn):
        return {w: float(fn(ch[f"{prefix}_{w}"])) for w in _WHEELS}

    out = {
        "brake_temp_mean": per_wheel("brake_temp", np.nanmean),
        "brake_temp_peak": per_wheel("brake_temp", np.nanmax),
    }
    if all(f"tire_press_{w}" in ch for w in _WHEELS):
        out["tire_press_mean"] = per_wheel("tire_press", np.nanmean)
    if all(f"tire_wear_{w}" in ch for w in _WHEELS):
        # wear is a remaining fraction (1.0 = new); report end-of-lap value.
        out["tire_wear_end"] = {w: float(ch[f"tire_wear_{w}"][-1]) for w in _WHEELS}
    return out


def _tire_summary(record: LapRecord):
    """Lap-level tire temps: per-wheel mean (imbalance) and an early-vs-late
    warm-up pair. Returns (None, None) for laps captured without tire channels."""
    if not all(k in record.channels for k in _TIRE_KEYS):
        return None, None
    means = {k[-2:]: float(np.nanmean(record.channels[k])) for k in _TIRE_KEYS}
    allt = np.vstack([record.channels[k] for k in _TIRE_KEYS])
    w = max(1, allt.shape[1] // 7)  # first/last ~15% of the lap
    early = float(np.nanmean(allt[:, :w]))
    late = float(np.nanmean(allt[:, -w:]))
    return means, (early, late)


def _metrics_for_corner(record: LapRecord, corner: Corner) -> CornerMetrics:
    dist = record.channels["lap_distance"]
    speed = record.channels["speed"]
    brake = record.channels["brake"]
    throttle = record.channels["throttle"]
    steering = record.channels["steering"]

    label = corner.label or str(corner.number)
    apexes = list(corner.apexes) if corner.apexes else [corner.apex_dist]

    mask = in_window(dist, corner.start_dist, corner.end_dist)  # wrap-aware (S/F seam)
    idx = np.where(mask)[0]

    if idx.size == 0:
        # Degenerate window: no telemetry samples fall inside this corner.
        return CornerMetrics(
            corner_no=corner.number,
            name=corner.name,
            direction=corner.direction,
            apex_dist=corner.apex_dist,
            entry_spd=float("nan"),
            apex_spd=float("nan"),
            min_spd=float("nan"),
            exit_spd=float("nan"),
            brake_point=corner.end_dist,
            throttle_point=corner.end_dist,
            trailbrake_overlap=0.0,
            label=label, apexes=apexes,
        )

    win_dist = dist[idx]
    win_speed = speed[idx]
    win_brake = brake[idx]
    win_throttle = throttle[idx]
    win_steering = steering[idx]

    if np.isnan(win_speed).all():
        # Degenerate: no valid speed samples to anchor any speed-derived metric.
        return CornerMetrics(
            corner_no=corner.number,
            name=corner.name,
            direction=corner.direction,
            apex_dist=corner.apex_dist,
            entry_spd=float("nan"),
            apex_spd=float("nan"),
            min_spd=float("nan"),
            exit_spd=float("nan"),
            brake_point=corner.end_dist,
            throttle_point=corner.end_dist,
            trailbrake_overlap=0.0,
            label=label, apexes=apexes,
        )

    valid_i = np.where(~np.isnan(win_speed))[0]
    entry_spd = float(win_speed[valid_i[0]])
    exit_spd = float(win_speed[valid_i[-1]])
    min_i = int(np.nanargmin(win_speed))
    min_spd = float(np.nanmin(win_speed))

    apex_i = int(np.argmin(np.abs(win_dist - corner.apex_dist)))
    apex_spd = float(win_speed[apex_i])

    braking = np.where(win_brake > 0.1)[0]
    brake_point = float(win_dist[braking[0]]) if braking.size else corner.end_dist

    after_min = np.where(win_throttle[min_i:] > 0.8)[0]
    throttle_point = (
        float(win_dist[min_i + after_min[0]]) if after_min.size else corner.end_dist
    )

    trailbrake_overlap = float(np.mean((win_brake > 0.1) & (np.abs(win_steering) > 0.1)))
    peak_brake = float(np.nanmax(win_brake)) if win_brake.size else 0.0
    win_lat_g = record.channels.get("lat_g")
    peak_lat_g = (
        float(np.nanmax(np.abs(win_lat_g[idx]))) if win_lat_g is not None and idx.size else 0.0
    )
    # Lowest grip fraction any tire saw through the corner (near 0 = at/over the
    # limit — sliding or locking). nan when the extended capture isn't present.
    grip_ch = [record.channels[f"grip_{w}"][idx] for w in ("fl", "fr", "rl", "rr")
               if f"grip_{w}" in record.channels]
    min_grip = float(np.nanmin(np.vstack(grip_ch))) if grip_ch else float("nan")

    # Longitudinal g split by pedal (sign-convention agnostic: magnitude while the
    # brake / throttle is actually applied). nan when long_g wasn't captured.
    long_g = record.channels.get("long_g")
    peak_brake_g = peak_accel_g = float("nan")
    if long_g is not None:
        lgw = np.abs(long_g[idx])
        bmask = win_brake > 0.1
        amask = win_throttle > 0.5
        peak_brake_g = float(np.nanmax(lgw[bmask])) if bmask.any() else float("nan")
        peak_accel_g = float(np.nanmax(lgw[amask])) if amask.any() else float("nan")
    peak_steer = float(np.nanmax(np.abs(win_steering))) if win_steering.size else float("nan")

    def _pct(name: str) -> float:
        ch = record.channels.get(name)
        return float(np.nanmean((ch[idx] > 0.5).astype(float))) if ch is not None else float("nan")
    abs_pct, tc_pct = _pct("abs_active"), _pct("tc_active")

    # Track-width usage: closest the car got to the track edge (m). New capture, so
    # nan for laps recorded before it. (Edge units PENDING LIVE VALIDATION — see smm.)
    edge = record.channels.get("track_edge")
    track_edge_min = float(np.nanmin(np.abs(edge[idx]))) if edge is not None else float("nan")

    try:
        direction = compute_direction(record, corner.start_dist, corner.end_dist)
    except ValueError:
        direction = corner.direction

    # Per-apex min speed: split the window at midpoints between consecutive apexes
    # (a double-apex corner reports a min for each apex, so the coach can advise on
    # both). apexes is the source of truth -- we don't auto-detect extra minima.
    aps = [a for a in apexes if in_window(a, corner.start_dist, corner.end_dist)] or [corner.apex_dist]
    if corner.start_dist <= corner.end_dist and len(aps) > 1:
        edges = [corner.start_dist] + [(aps[i] + aps[i + 1]) / 2 for i in range(len(aps) - 1)] + [corner.end_dist]
        apex_min_speeds = []
        for i in range(len(aps)):
            seg = win_speed[(win_dist >= edges[i]) & (win_dist <= edges[i + 1])]
            apex_min_speeds.append(
                float(np.nanmin(seg)) if seg.size and not np.isnan(seg).all() else min_spd
            )
    else:
        # Single-apex, or a wrapped window where a linear midpoint split is invalid:
        # each apex just reports the window min.
        apex_min_speeds = [min_spd] * len(aps)

    return CornerMetrics(
        corner_no=corner.number,
        name=corner.name,
        direction=direction,
        apex_dist=corner.apex_dist,
        entry_spd=entry_spd,
        apex_spd=apex_spd,
        min_spd=min_spd,
        exit_spd=exit_spd,
        brake_point=brake_point,
        throttle_point=throttle_point,
        trailbrake_overlap=trailbrake_overlap,
        peak_brake=peak_brake,
        peak_lat_g=peak_lat_g,
        min_grip=min_grip,
        peak_brake_g=peak_brake_g,
        peak_accel_g=peak_accel_g,
        peak_steer=peak_steer,
        abs_pct=abs_pct,
        tc_pct=tc_pct,
        track_edge_min=track_edge_min,
        label=label, apexes=aps, apex_min_speeds=apex_min_speeds,
    )


def analyze_lap(
    record: LapRecord, cmap: CornerMap, reference: Optional[LapRecord] = None
) -> LapAnalysis:
    if reference is not None and reference.track != record.track:
        raise ValueError(f"reference lap track {reference.track!r} != {record.track!r}")

    corners = [_metrics_for_corner(record, corner) for corner in cmap.corners]

    # Tag each corner with the named section its apex falls in (e.g. "The Corkscrew").
    for cm in corners:
        for s in getattr(cmap, "sections", []) or []:
            if s["start_dist"] <= cm.apex_dist <= s["end_dist"]:
                cm.section = s["name"]
                break

    has_reference = reference is not None
    if has_reference:
        ref_metrics = {c.number: _metrics_for_corner(reference, c) for c in cmap.corners}
        for cm in corners:
            ref = ref_metrics.get(cm.corner_no)
            if ref is not None and not np.isnan(cm.min_spd) and not np.isnan(ref.min_spd):
                cm.min_spd_delta = cm.min_spd - ref.min_spd
            else:
                cm.min_spd_delta = None

    tire_means, tire_warmup = _tire_summary(record)
    return LapAnalysis(
        lap_time=record.lap_time, corners=corners, has_reference=has_reference,
        tire_means=tire_means, tire_warmup=tire_warmup, wheels=_wheel_summary(record),
    )
