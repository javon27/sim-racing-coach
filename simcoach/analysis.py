from dataclasses import dataclass
from typing import Optional
import numpy as np

from simcoach.model_types import LapRecord
from simcoach.corners import Corner, CornerMap, compute_direction, in_window
from simcoach.ribbon import _menger


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
    # Closest approach to the track edge (m) — not the track's half-width. Low = using
    # all the track; negative = the car crossed the white line by that much.
    track_edge_min: float = float("nan")
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

    # Braking for a corner usually STARTS in the approach — before the corner window
    # (turn-in) — so a window-only search misses it and falls back to the "no brake"
    # sentinel (this is why heavy-braking corners like Parabolica showed "—"). Search
    # an approach-extended region up to the apex, then isolate the braking run that
    # leads INTO this corner (contiguous brake at/before the apex) so a nearby earlier
    # corner's braking doesn't bleed in.
    # ponytail: fixed 250 m look-back; the contiguous-run isolation guards the common
    # case, but two heavy-braking corners <250 m apart could still confuse it.
    appr = np.where(in_window(dist, corner.start_dist - 250.0, corner.apex_dist))[0]
    brake_point = corner.end_dist
    if appr.size:
        ad, on = dist[appr], brake[appr] > 0.1
        aj = int(np.argmin(np.abs(ad - corner.apex_dist)))
        j = aj
        while j >= 0 and not on[j]:  # nearest braking sample at/before the apex
            j -= 1
        if j >= 0:
            k = j
            while k > 0 and on[k - 1]:  # walk back through its contiguous run
                k -= 1
            brake_point = float(ad[k])

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

    # Track-width usage: closest the car got to the track edge (m). mTrackEdge is
    # the EDGE's offset from the centre path (~= half the track width there), not
    # the car's distance to it -- that distance is |track_edge| - |path_lateral|
    # (path_lateral is the car's own offset from that same centre path).
    # Deliberately NOT clamped at 0: negative means the car went that far PAST the
    # white line, and nothing else in the codebase reports that -- the only other
    # off-track surface is the lap-level `valid` flag from mLapInvalidated, which
    # fired for none of the 168-of-468 real corners a clamp flattened to exactly
    # 0.0. New capture, so nan for laps recorded before it. (Edge units PENDING
    # LIVE VALIDATION — see smm.)
    edge = record.channels.get("track_edge")
    path_lat = record.channels.get("path_lateral")
    if edge is not None and path_lat is not None:
        track_edge_min = float(np.nanmin(np.abs(edge[idx]) - np.abs(path_lat[idx])))
    else:
        track_edge_min = float("nan")

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


def corner_geometry(path, dist, corners, *, width=None, bank=None, elev=None):
    """Per-corner physical geometry for coach grounding (pure, deterministic).

    path: Nx2 points parallel to `dist` (lap_distance). `corners`: dicts with
    start_dist/apex_dist/end_dist and label (or number). Optional `width`, `bank`
    (radians, as the ribbon emits), `elev` arrays index-aligned with `path`.
    Returns {label: {radius_m?, width_m?, banking_deg?, elev_delta_m?}}, including
    ONLY the metrics whose source data was supplied -- nothing is fabricated
    (same never-invent rule as corner direction).
    """
    if not len(path) or not len(dist):
        return {}
    pa = np.asarray(path, dtype=float)
    d = np.asarray(dist, dtype=float)
    n = len(d)
    k = _menger(pa[:, 0], pa[:, 1])  # per-station curvature, 1/m
    w = np.asarray(width, dtype=float) if width is not None else None
    b = np.asarray(bank, dtype=float) if bank is not None else None
    e = np.asarray(elev, dtype=float) if elev is not None else None

    def at(target: float) -> int:
        return int(np.argmin(np.abs(d - target)))

    def window(start: float, end: float) -> list[int]:
        i, j = at(start), at(end)
        return list(range(i, j + 1)) if i <= j else list(range(i, n)) + list(range(0, j + 1))

    out: dict[str, dict] = {}
    for c in corners:
        label = str(c.get("label") or c.get("number"))
        win = window(c["start_dist"], c["end_dist"])
        m: dict = {}
        kmax = float(np.max(k[win])) if win else 0.0
        if kmax > 1e-4:  # tightest point in the window; <1e-4 (~>10 km) = effectively straight
            m["radius_m"] = round(1.0 / kmax, 1)
        ai = at(c["apex_dist"])
        if w is not None and ai < len(w):
            m["width_m"] = round(float(w[ai]), 1)
        if b is not None and ai < len(b):
            m["banking_deg"] = round(float(np.degrees(b[ai])), 1)
        if e is not None:
            si, ei = at(c["start_dist"]), at(c["end_dist"])
            if si < len(e) and ei < len(e):
                m["elev_delta_m"] = round(float(e[ei] - e[si]), 1)  # + = uphill
        out[label] = m
    return out


def geometry_clause(metrics: dict) -> str:
    """Render baked per-corner geometry as a digest clause, omitting absent
    metrics. Elevation/banking below a threshold are dropped as noise."""
    parts = []
    r = metrics.get("radius_m")
    if r is not None:
        parts.append(f"radius ~{r:.0f}m")
    ed = metrics.get("elev_delta_m")
    if ed is not None and abs(ed) >= 1.0:
        parts.append(f"{'uphill' if ed > 0 else 'downhill'} {abs(ed):.0f}m")
    w = metrics.get("width_m")
    if w is not None:
        parts.append(f"width {w:.0f}m")
    bk = metrics.get("banking_deg")
    if bk is not None and abs(bk) >= 1.0:
        parts.append(f"{abs(bk):.0f}\N{DEGREE SIGN} banking")
    return ", ".join(parts)
