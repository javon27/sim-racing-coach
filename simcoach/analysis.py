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
    track_edge_min: float = float("nan") # closest approach to the track edge (m); low = using all the track
    coast_time: float = float("nan")      # seconds in the corner with NEITHER pedal
    coast_dist: float = float("nan")      # meters in the corner with neither pedal
    brake_to_throttle_dist: float = float("nan")  # last brake-off -> first throttle (m)
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
    coast_total: "tuple[float, float] | None" = None   # (seconds, meters) with neither pedal


_TIRE_KEYS = ("tire_temp_fl", "tire_temp_fr", "tire_temp_rl", "tire_temp_rr")
_WHEELS = ("fl", "fr", "rl", "rr")

# --- distance-keyed time axis -------------------------------------------------
# The `speed` channel is km/h (smm.py: |mLocalVel| * 3.6), so every ds/v
# integration divides by speed/3.6. This is the ONE place that conversion lives:
# get it wrong and every delta-t silently scales by 3.6.
_SPEED_KMH_TO_MS = 1.0 / 3.6
# A stopped (or pit-lane crawling) sample must not divide by ~0 and produce an
# infinite time loss. Clamp floor, in km/h.
_MIN_SPEED_KMH = 1.0
# Two laps are only comparable when they overlap over most of the longer lap's
# distance span; below this an out-lap / partial lap would read as a huge delta.
_MIN_OVERLAP_FRAC = 0.5


# Coasting = neither pedal. Pedal inputs are 0..1 fractions.
# ponytail: tuning knobs. A driver who rests a foot on the throttle would need the
# throttle floor raised; a rig check on a known coasting corner settles them.
_COAST_THROTTLE = 0.05
_COAST_BRAKE = 0.05
# How far past a corner window to look for the throttle pickup that ends the dead
# zone — throttle often comes up after the window closes. Mirrors the 250 m
# approach look-back `brake_point` already uses.
# ponytail: fixed look-ahead; two corners <250 m apart could see the next one's
# throttle pickup, which under-reports nothing but caps the gap at that distance.
_THROTTLE_LOOKAHEAD_M = 250.0


# An interval wider than this is not two adjacent ~50 Hz samples: it's a dropped
# frame or the jump across a seam-wrapped corner window's two distance runs.
# Counting it would invent coasting distance, so it is dropped (under-reporting,
# the safe direction for a coaching claim).
# ponytail: fixed cap; a per-lap median-spacing multiple would adapt to sparse laps.
_MAX_SAMPLE_GAP_M = 50.0


def _coast_totals(dist, speed, throttle, brake) -> tuple[float, float]:
    """(seconds, meters) with NEITHER pedal applied, over the samples given,
    integrating ds / (v/3.6) exactly as `lap_time_axis` does. An interval counts
    only when both of its endpoints are coasting."""
    n = min(len(dist), len(speed), len(throttle), len(brake))
    if n < 2:
        return 0.0, 0.0
    d = np.asarray(dist[:n], dtype=float)
    v = np.asarray(speed[:n], dtype=float)
    v = np.where(np.isfinite(v), v, _MIN_SPEED_KMH)
    coast = (np.asarray(throttle[:n], dtype=float) < _COAST_THROTTLE) & (
        np.asarray(brake[:n], dtype=float) < _COAST_BRAKE)
    ds = np.diff(d)
    ds = np.where(coast[:-1] & coast[1:] & (ds > 0) & (ds <= _MAX_SAMPLE_GAP_M), ds, 0.0)
    inv = 1.0 / (np.maximum(v, _MIN_SPEED_KMH) * _SPEED_KMH_TO_MS)
    return float(np.sum(ds * (inv[:-1] + inv[1:]) / 2.0)), float(np.sum(ds))


def _brake_to_throttle(dist, brake, throttle, idx) -> float:
    """Distance from the last braking sample inside a corner window to the first
    throttle pickup after it. Searches up to `_THROTTLE_LOOKAHEAD_M` past the
    window, because throttle often comes up after the window closes. nan when the
    corner has no braking phase, or no pickup within the look-ahead."""
    on_brake = np.where(np.asarray(brake)[idx] >= _COAST_BRAKE)[0]
    if on_brake.size == 0:
        return float("nan")
    d_off = float(np.asarray(dist)[idx][on_brake[-1]])
    ahead = np.where((dist > d_off) & (dist <= d_off + _THROTTLE_LOOKAHEAD_M)
                     & (np.asarray(throttle) >= _COAST_THROTTLE))[0]
    if ahead.size == 0:
        return float("nan")
    return float(np.min(np.asarray(dist)[ahead])) - d_off


def lap_coast_total(record: LapRecord):
    """(seconds, meters) of whole-lap coasting, or None when the lap was captured
    without pedal channels."""
    ch = record.channels
    if "throttle" not in ch or "brake" not in ch:
        return None
    idx = _monotonic_idx(record)
    if idx.size < 2:
        return None
    return _coast_totals(np.asarray(ch["lap_distance"], dtype=float)[idx],
                         np.asarray(ch["speed"], dtype=float)[idx],
                         np.asarray(ch["throttle"], dtype=float)[idx],
                         np.asarray(ch["brake"], dtype=float)[idx])


def _monotonic_idx(record: LapRecord) -> np.ndarray:
    """Indices of the samples with finite distance/speed and strictly increasing
    `lap_distance`. Strictly increasing distance is a precondition of every two-lap
    comparison: a pit re-entry, an S/F wrap or sensor jitter otherwise integrates a
    negative ds or interpolates backwards. Returned as indices so other channels
    can be sliced onto the same filtered grid."""
    d = np.asarray(record.channels["lap_distance"], dtype=float)
    v = np.asarray(record.channels["speed"], dtype=float)
    n = min(d.size, v.size)
    d, v = d[:n], v[:n]
    i = np.where(np.isfinite(d) & np.isfinite(v))[0]
    if i.size < 2:
        return i
    # Keep only samples beyond the running maximum distance seen so far.
    dd = d[i]
    keep = np.concatenate(([True], dd[1:] > np.maximum.accumulate(dd)[:-1]))
    return i[keep]


def _monotonic_axis(record: LapRecord) -> tuple[np.ndarray, np.ndarray]:
    """(dist, speed) on the filtered grid — see `_monotonic_idx`."""
    idx = _monotonic_idx(record)
    return (np.asarray(record.channels["lap_distance"], dtype=float)[idx],
            np.asarray(record.channels["speed"], dtype=float)[idx])


def lap_time_axis(record: LapRecord) -> tuple[np.ndarray, np.ndarray]:
    """Cumulative elapsed seconds along `lap_distance`, integrating ds / (v/3.6).

    Returns (dist, t) with `t[0] == 0.0`. There is no captured `time` channel —
    this is the lap's time axis, and the alignment key stays lap distance.

    ponytail: trapezoidal integration of the ~50 Hz ds/v samples, so it is exact
    only to sampling resolution. Sampling `mElapsedTime` / `mLapStartET` would
    give an exact axis (plan Open Question 5 / Task 10) — out of this slice.
    """
    d, v = _monotonic_axis(record)
    if d.size < 2:
        return d, np.zeros_like(d)
    inv = 1.0 / (np.maximum(v, _MIN_SPEED_KMH) * _SPEED_KMH_TO_MS)   # s per meter
    dt = np.diff(d) * (inv[:-1] + inv[1:]) / 2.0
    return d, np.concatenate(([0.0], np.cumsum(dt)))


def align_time_axes(record: LapRecord, reference: LapRecord):
    """(dist_grid, delta_t) where delta_t = t_cur(d) - t_ref(d), positive = time
    LOST vs the reference, zeroed at the start of the overlap.

    Sampled on the current lap's (filtered) distance grid over the distance range
    both laps cover. Returns None when that overlap is under `_MIN_OVERLAP_FRAC`
    of the longer lap — either lap being a partial makes the comparison dishonest.
    """
    dc, tc = lap_time_axis(record)
    dr, tr = lap_time_axis(reference)
    if dc.size < 2 or dr.size < 2:
        return None
    lo, hi = max(dc[0], dr[0]), min(dc[-1], dr[-1])
    span = max(dc[-1] - dc[0], dr[-1] - dr[0])
    if hi <= lo or span <= 0 or (hi - lo) / span < _MIN_OVERLAP_FRAC:
        return None
    grid = dc[(dc >= lo) & (dc <= hi)]
    if grid.size < 2:
        return None
    delta = np.interp(grid, dc, tc) - np.interp(grid, dr, tr)
    return grid, delta - delta[0]


def time_loss_buckets(record: LapRecord, reference: LapRecord, cmap: CornerMap):
    """Where the lap's time went vs `reference`, bucketed over the WHOLE lap.

    One bucket per corner window (owned data — boundaries and identity come from
    the corner map) plus one per inter-corner gap, in track order, each
    `{"label", "kind": "corner"|"straight", "delta", "start_dist", "end_dist"}`.
    `delta` is seconds, POSITIVE = time lost vs the reference. The buckets tile
    the compared distance range contiguously, so they sum to the net delta — a
    ranked list that doesn't reconcile with the net it's printed beside is worse
    than no tool.

    Gap buckets are labelled from the corners they sit between; no corner name,
    number or direction is invented. None when the laps aren't comparable.
    """
    aligned = align_time_axes(record, reference)
    if aligned is None:
        return None
    grid, delta = aligned
    lo0, hi0 = float(grid[0]), float(grid[-1])

    def t_at(d: float) -> float:
        return float(np.interp(d, grid, delta))

    # Corner windows clipped onto the compared range. A window that wraps the S/F
    # seam (start_dist > end_dist) contributes two distance runs; they are summed
    # back into one bucket below so the corner is reported once.
    # Keyed by index into cmap.corners, not c.number: nothing on load makes
    # numbers unique (8A/8B may share 8), and a collision would fold two corners.
    runs: list[tuple[float, float, int]] = []
    for no, c in enumerate(cmap.corners):
        pairs = ([(c.start_dist, c.end_dist)] if c.start_dist <= c.end_dist
                 else [(lo0, c.end_dist), (c.start_dist, hi0)])
        for a, b in pairs:
            a, b = max(float(a), lo0), min(float(b), hi0)
            if b > a:
                runs.append((a, b, no))
    runs.sort()

    # Overlapping windows (a malformed map) would double-count: clip to the tiling.
    tiled: list[tuple[float, float, int]] = []
    for a, b, no in runs:
        if tiled and a < tiled[-1][1]:
            a = tiled[-1][1]
        if b > a:
            tiled.append((a, b, no))

    tag = {i: f"T{c.label or c.number}" for i, c in enumerate(cmap.corners)}
    name = {i: f"T{c.label or c.number} {c.name}".strip() for i, c in enumerate(cmap.corners)}
    wrapped = {i: c for i, c in enumerate(cmap.corners) if c.start_dist > c.end_dist}

    def gap_label(i: int) -> str:
        before = f"{tag[tiled[i - 1][2]]} exit" if i > 0 else "lap start"
        after = f"{tag[tiled[i][2]]} entry" if i < len(tiled) else "lap end"
        return f"{before} \N{RIGHTWARDS ARROW} {after}"

    out: list[dict] = []
    by_corner: dict[int, dict] = {}
    cursor = lo0
    for i, (a, b, no) in enumerate(tiled):
        if a > cursor + 1e-9:
            out.append({"label": gap_label(i), "kind": "straight",
                        "delta": t_at(a) - t_at(cursor),
                        "start_dist": cursor, "end_dist": a})
        d = t_at(b) - t_at(a)
        if no in by_corner:
            # The far side of a seam-wrapped window: fold the delta into the one
            # bucket and report the map's own (wrapped) window, not the clipped run.
            by_corner[no]["delta"] += d
            by_corner[no]["start_dist"] = float(wrapped[no].start_dist)
            by_corner[no]["end_dist"] = float(wrapped[no].end_dist)
        else:
            bucket = {"label": name[no], "kind": "corner", "delta": d,
                      "start_dist": a, "end_dist": b}
            by_corner[no] = bucket
            out.append(bucket)
        cursor = b
    if hi0 > cursor + 1e-9:
        out.append({"label": gap_label(len(tiled)), "kind": "straight",
                    "delta": t_at(hi0) - t_at(cursor),
                    "start_dist": cursor, "end_dist": hi0})
    return out


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

    # Track-width usage: closest the car got to the track edge (m). New capture, so
    # nan for laps recorded before it. (Edge units PENDING LIVE VALIDATION — see smm.)
    edge = record.channels.get("track_edge")
    track_edge_min = float(np.nanmin(np.abs(edge[idx]))) if edge is not None else float("nan")

    # Dead time: distance/seconds in the corner with neither pedal, plus the gap
    # between brake release and throttle pickup. Same ds/(v/3.6) integration as
    # lap_time_axis, restricted to the coasting mask.
    coast_time, coast_dist = _coast_totals(win_dist, win_speed, win_throttle, win_brake)
    brake_to_throttle_dist = _brake_to_throttle(dist, brake, throttle, idx)

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
        coast_time=coast_time,
        coast_dist=coast_dist,
        brake_to_throttle_dist=brake_to_throttle_dist,
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
        coast_total=lap_coast_total(record),
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
