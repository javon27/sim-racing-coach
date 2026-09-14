import math
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
    # None = no such point exists in this corner: the driver never braked / never got
    # past the throttle gate. A distance sentinel can't express that -- any value here
    # reads as a real point to anything computing `apex_dist - brake_point`, and stays
    # silent only while it happens to sit on the correct side of apex_dist.
    # brake_point may legitimately be NEGATIVE: a corner braking across the S/F seam
    # reports a distance before the line, so `apex_dist - brake_point` stays a positive
    # metres-before-apex (#7). Not-None + negative is a valid state; only "not found"
    # is None.
    brake_point: float | None
    throttle_point: float | None
    trailbrake_overlap: float
    peak_brake: float = 0.0          # max brake input in the corner (0..1)
    peak_lat_g: float = 0.0          # max |lateral g| — how hard the car was loaded
    # Extended inputs/loads — nan when the source channel is absent (older laps).
    peak_brake_g: float = float("nan")   # max deceleration g while braking
    peak_accel_g: float = float("nan")   # max acceleration g while on throttle
    peak_steer: float = float("nan")     # max |steering| (0..1 of full lock)
    abs_pct: float = float("nan")        # fraction of corner with ABS engaged (braking too hard)
    tc_pct: float = float("nan")         # fraction of corner with TC engaged (too much throttle)
    # Closest approach of the car's CENTRE to the track edge (m) — not the track's
    # half-width, and not the outer wheels. Low = using all the track; negative = the
    # centre crossed the white line by that much.
    track_edge_min: float = float("nan")
    coast_time: float = float("nan")      # seconds in the corner with NEITHER pedal
    coast_dist: float = float("nan")      # meters in the corner with neither pedal
    brake_to_throttle_dist: float = float("nan")  # last brake-off -> first throttle (m)
    min_spd_delta: Optional[float] = None
    # #102: every other comparable metric, diffed the same way (current - reference).
    # Distance fields are metres, positive = LATER than the reference (further along
    # the lap); the rest are in the field's own native unit. None when either side is
    # None/NaN -- never computed from a "not found" sentinel.
    brake_point_delta: Optional[float] = None
    throttle_point_delta: Optional[float] = None
    trailbrake_overlap_delta: Optional[float] = None
    coast_time_delta: Optional[float] = None
    brake_to_throttle_dist_delta: Optional[float] = None
    peak_brake_g_delta: Optional[float] = None
    peak_lat_g_delta: Optional[float] = None
    abs_pct_delta: Optional[float] = None
    tc_pct_delta: Optional[float] = None
    entry_spd_delta: Optional[float] = None
    exit_spd_delta: Optional[float] = None
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
    d = _interp_lap_distance(record)
    return _coast_totals(d[idx],
                         np.asarray(ch["speed"], dtype=float)[idx],
                         np.asarray(ch["throttle"], dtype=float)[idx],
                         np.asarray(ch["brake"], dtype=float)[idx])


def _interp_lap_distance(record: LapRecord) -> np.ndarray:
    """De-staircase `lap_distance`: it comes off the SCORING struct at ~5 Hz while
    every other channel is ~48.6 Hz (see `input_traces`), so the stored channel is
    a staircase -- one new value per ~10 samples, ~8.5 m median step (#101). This
    linearly interpolates it across each held run using the same running-maximum
    knot logic `input_traces` uses for the UI chart, so every corner metric and
    time-axis integration downstream sees the real ~1 m axis instead.

    Returns a full-length array (same shape/order as the raw channel). A sample
    with non-finite raw distance stays non-finite. Unlike `input_traces`, the
    TRAILING partial run (past the last knot that beat the running maximum -- lap
    end, or the tail of a wrapped/glitched next-lap that failed the knot filter)
    is left non-finite rather than extrapolated: this feeds corner metrics and the
    time axis, where a handful of unknown trailing samples is harmless, and
    inventing a distance past a possible seam wrap is not.

    Cached on the record: `analyze_lap` calls in here once per corner (via
    `_metrics_for_corner`) plus the time-axis functions, so interpolating once and
    reusing it matters.
    """
    cached = getattr(record, "_lap_distance_interp", None)
    if cached is not None:
        return cached
    d = np.asarray(record.channels["lap_distance"], dtype=float)
    out = np.full(d.size, np.nan)
    keep = np.flatnonzero(np.isfinite(d))
    if keep.size >= 2:
        dk = d[keep]
        run_start = np.r_[True, np.diff(dk) != 0]
        ki, kd = keep[run_start], dk[run_start]
        forward = np.r_[True, kd[1:] > np.maximum.accumulate(kd)[:-1]]
        ki, kd = ki[forward], kd[forward]
        if ki.size >= 2:
            inner = keep[(keep >= ki[0]) & (keep <= ki[-1])]
            out[inner] = np.interp(inner, ki, kd)
        else:
            out[keep] = dk
    elif keep.size == 1:
        out[keep] = d[keep]
    record._lap_distance_interp = out
    return out


def _monotonic_idx(record: LapRecord) -> np.ndarray:
    """Indices of the samples with finite distance/speed and strictly increasing
    (de-staircased) `lap_distance`. Strictly increasing distance is a
    precondition of every two-lap comparison: a pit re-entry, an S/F wrap or
    sensor jitter otherwise integrates a negative ds or interpolates backwards.
    Returned as indices so other channels can be sliced onto the same filtered
    grid."""
    d = _interp_lap_distance(record)
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
    return (_interp_lap_distance(record)[idx],
            np.asarray(record.channels["speed"], dtype=float)[idx])


def _exact_time_axis(record: LapRecord):
    """(dist, t) from the measured `elapsed_time`/`lap_start_et` channels, or None
    when the record doesn't carry them or too few samples survive filtering.

    t = elapsed_time - lap_start_et (smm.py's own definition; both are per-poll
    fresh at ~50 Hz, rig-confirmed for #101). Samples carrying the NEXT lap's
    lap_start_et show t dropping back toward zero at the seam -- the same shape
    as a backwards lap_distance jitter -- so a sample only survives if it is the
    running maximum on BOTH distance and time together.
    """
    ch = record.channels
    if "elapsed_time" not in ch or "lap_start_et" not in ch:
        return None
    d = _interp_lap_distance(record)
    et = np.asarray(ch["elapsed_time"], dtype=float)
    lset = np.asarray(ch["lap_start_et"], dtype=float)
    n = min(d.size, et.size, lset.size)
    d, t = d[:n], et[:n] - lset[:n]
    i = np.where(np.isfinite(d) & np.isfinite(t))[0]
    if i.size < 2:
        return None
    dd, tt = d[i], t[i]
    keep = np.concatenate(([True],
                           (dd[1:] > np.maximum.accumulate(dd)[:-1]) &
                           (tt[1:] > np.maximum.accumulate(tt)[:-1])))
    dd, tt = dd[keep], tt[keep]
    if dd.size < 2:
        return None
    return dd, tt


def _uses_exact_time_axis(record: LapRecord) -> bool:
    """Whether `lap_time_axis` resolves this record to the measured elapsed_time
    channel (True) or falls back to ds/v integration (False) -- same condition
    `lap_time_axis` checks. Exposed so a caller presenting a noise floor to the
    driver (`format_time_loss`) knows which precision applies."""
    return _exact_time_axis(record) is not None


def lap_time_axis(record: LapRecord) -> tuple[np.ndarray, np.ndarray]:
    """Cumulative elapsed seconds along `lap_distance`.

    Uses the measured `elapsed_time - lap_start_et` channel when the record
    carries it (exact to capture resolution); falls back to trapezoidal
    integration of ds / (v/3.6) on the de-staircased distance axis for older
    captures that lack it (exact only to ~50 Hz sampling resolution — smm.py
    measured a 0.206 s median / 1.212 s max net-delta error against the exact
    axis, #101).

    Returns (dist, t) with `t[0] == 0.0` on the ds/v path (integration starts at
    zero by construction); on the exact path t[0] is whatever `elapsed_time -
    lap_start_et` reads at the record's first sample (~0 in practice, since
    capture starts at lap start). There is no captured `time` channel — this is
    the lap's time axis, and the alignment key stays lap distance.
    """
    exact = _exact_time_axis(record)
    if exact is not None:
        return exact
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

    Each bucket also carries `"exact"`: whether BOTH laps resolved to the
    measured elapsed_time axis (see `lap_time_axis`) rather than the ds/v
    fallback for either -- `format_time_loss` picks its noise floor from it.
    """
    aligned = align_time_axes(record, reference)
    if aligned is None:
        return None
    grid, delta = aligned
    exact = _uses_exact_time_axis(record) and _uses_exact_time_axis(reference)
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
                        "delta": t_at(a) - t_at(cursor), "exact": exact,
                        "start_dist": cursor, "end_dist": a})
        d = t_at(b) - t_at(a)
        if no in by_corner:
            # The far side of a seam-wrapped window: fold the delta into the one
            # bucket and report the map's own (wrapped) window, not the clipped run.
            by_corner[no]["delta"] += d
            by_corner[no]["start_dist"] = float(wrapped[no].start_dist)
            by_corner[no]["end_dist"] = float(wrapped[no].end_dist)
        else:
            bucket = {"label": name[no], "kind": "corner", "delta": d, "exact": exact,
                      "start_dist": a, "end_dist": b}
            by_corner[no] = bucket
            out.append(bucket)
        cursor = b
    if hi0 > cursor + 1e-9:
        out.append({"label": gap_label(len(tiled)), "kind": "straight",
                    "delta": t_at(hi0) - t_at(cursor), "exact": exact,
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


def _metrics_for_corner(
    record: LapRecord, corner: Corner, length: float | None = None
) -> CornerMetrics:
    dist = _interp_lap_distance(record)
    speed = record.channels["speed"]
    brake = record.channels["brake"]
    throttle = record.channels["throttle"]
    steering = record.channels["steering"]

    label = corner.label or str(corner.number)
    apexes = list(corner.apexes) if corner.apexes else [corner.apex_dist]

    # Lap length: the modulus everything that reasons across the S/F seam needs.
    # Fall back to the lap's own furthest distance for maps that declare no length
    # (within a sample of the truth, which is well under coaching resolution).
    lap_len = float(length) if length else float(np.nanmax(dist))

    mask = in_window(dist, corner.start_dist, corner.end_dist)  # wrap-aware (S/F seam)
    idx = np.where(mask)[0]
    wrap = corner.start_dist > corner.end_dist
    if wrap and idx.size:
        # Sample ORDER is not along-track order once a window straddles S/F: the
        # post-seam samples (lap_distance ~0) sort first. Re-order along the window
        # so entry/exit/throttle -- all of which read the window front-to-back --
        # mean what they say (issue #7). Non-wrapping windows are left untouched.
        idx = idx[np.argsort((dist[idx] - corner.start_dist) % lap_len)]

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
            brake_point=None,
            throttle_point=None,
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
            brake_point=None,
            throttle_point=None,
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
    #
    # Both the look-back and the walk are done on a wrap-aware "metres BEFORE the
    # apex" axis (issue #7). A raw distance window loses the approach whenever it
    # crosses S/F -- `start_dist - 250` goes negative and clamps at the line -- and
    # even a wrap-aware mask can't be walked by array index, because the samples
    # either side of the seam are not in distance order.
    back = (corner.apex_dist - dist) % lap_len       # 0 at the apex, grows backwards
    sel = np.where(back <= (corner.apex_dist - corner.start_dist + 250.0) % lap_len)[0]
    brake_point = None   # no braking found; never a distance standing in for that
    if sel.size:
        order = sel[np.argsort(back[sel], kind="stable")]  # apex first, then backwards
        on = brake[order] > 0.1
        j = 0
        while j < on.size and not on[j]:  # nearest braking sample at/before the apex
            j += 1
        if j < on.size:
            k = j
            while k + 1 < on.size and on[k + 1]:  # walk back through its contiguous run
                k += 1
            d0 = float(dist[order[k]])
            # Selected samples sit at/before the apex on the wrapped axis, so a raw
            # distance ABOVE the apex is one that crossed S/F -- shift it a lap back
            # rather than report a "brake point" the apex appears to precede.
            brake_point = d0 - lap_len if d0 > corner.apex_dist else d0

    # Throttle point: first sample past the 0.8 gate at/after the min-speed point, and
    # None when there isn't one. Explicitly None rather than a distance sentinel -- the
    # old `corner.end_dist` sits AFTER the apex by construction, so format_corner_line
    # rendered (end_dist - apex_dist) as "back to throttle Nm after apex" for every
    # corner the driver never took past 80% throttle: track geometry stated to the
    # coach as a driver input.
    after_min = np.where(win_throttle[min_i:] > 0.8)[0]
    throttle_point = None
    if after_min.size:
        d1 = float(win_dist[min_i + after_min[0]])
        # Is d1 at/after the apex? Not "d1 >= apex_dist" -- across the seam the later
        # distance is the SMALLER number. Measure forward from the apex, wrap-aware,
        # and accept only what still lands inside the corner's post-apex extent.
        fwd = (d1 - corner.apex_dist) % lap_len
        if fwd <= (corner.end_dist - corner.apex_dist) % lap_len:
            throttle_point = d1 if d1 >= corner.apex_dist else d1 + lap_len

    trailbrake_overlap = float(np.mean((win_brake > 0.1) & (np.abs(win_steering) > 0.1)))
    peak_brake = float(np.nanmax(win_brake)) if win_brake.size else 0.0
    win_lat_g = record.channels.get("lat_g")
    peak_lat_g = (
        float(np.nanmax(np.abs(win_lat_g[idx]))) if win_lat_g is not None and idx.size else 0.0
    )
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
    # ponytail: centre-to-line, not wheel-to-line. mPathLateral is the car's CENTRE
    # and we hold no per-car width, so a ~2m GT car's outer wheels sit ~0.8m further
    # out than this number says -- and the reported median is only 0.24m, i.e. the
    # whole interesting range hides inside one unmodeled half-car-width (the main
    # reason so many corners read ~0). Worded as "centre" in format.py rather than
    # guessing a width; if per-car width ever lands, subtract half of it here.
    # (Not a contradiction of the clamp note above: that 168-of-468 count was
    # exact-0.0 saturation from clamping real excursions; this near-zero-but-not-0
    # spread is the separate, still-present effect of the unmodeled half-car-width.)
    edge = record.channels.get("track_edge")
    path_lat = record.channels.get("path_lateral")
    if edge is not None and path_lat is not None:
        track_edge_min = float(np.nanmin(np.abs(edge[idx]) - np.abs(path_lat[idx])))
    else:
        track_edge_min = float("nan")

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


def _delta(cur, ref) -> float | None:
    """cur - ref, or None when either side is missing (None) or NaN -- a "not found"
    reading (e.g. no braking) must never be diffed as if it were a real point (#102)."""
    if cur is None or ref is None:
        return None
    if math.isnan(cur) or math.isnan(ref):
        return None
    return cur - ref


def analyze_lap(
    record: LapRecord, cmap: CornerMap, reference: Optional[LapRecord] = None
) -> LapAnalysis:
    if reference is not None and reference.track != record.track:
        raise ValueError(f"reference lap track {reference.track!r} != {record.track!r}")

    length = cmap.length
    corners = [_metrics_for_corner(record, corner, length) for corner in cmap.corners]

    # Tag each corner with the named section its apex falls in (e.g. "The Corkscrew").
    for cm in corners:
        for s in getattr(cmap, "sections", []) or []:
            if s["start_dist"] <= cm.apex_dist <= s["end_dist"]:
                cm.section = s["name"]
                break

    has_reference = reference is not None
    if has_reference:
        ref_metrics = {c.number: _metrics_for_corner(reference, c, length) for c in cmap.corners}
        for cm in corners:
            ref = ref_metrics.get(cm.corner_no)
            if ref is None:
                continue
            cm.min_spd_delta = _delta(cm.min_spd, ref.min_spd)
            cm.brake_point_delta = _delta(cm.brake_point, ref.brake_point)
            cm.throttle_point_delta = _delta(cm.throttle_point, ref.throttle_point)
            cm.trailbrake_overlap_delta = _delta(cm.trailbrake_overlap, ref.trailbrake_overlap)
            cm.coast_time_delta = _delta(cm.coast_time, ref.coast_time)
            cm.brake_to_throttle_dist_delta = _delta(cm.brake_to_throttle_dist, ref.brake_to_throttle_dist)
            cm.peak_brake_g_delta = _delta(cm.peak_brake_g, ref.peak_brake_g)
            cm.peak_lat_g_delta = _delta(cm.peak_lat_g, ref.peak_lat_g)
            cm.abs_pct_delta = _delta(cm.abs_pct, ref.abs_pct)
            cm.tc_pct_delta = _delta(cm.tc_pct, ref.tc_pct)
            cm.entry_spd_delta = _delta(cm.entry_spd, ref.entry_spd)
            cm.exit_spd_delta = _delta(cm.exit_spd, ref.exit_spd)

    tire_means, tire_warmup = _tire_summary(record)
    return LapAnalysis(
        lap_time=record.lap_time, corners=corners, has_reference=has_reference,
        tire_means=tire_means, tire_warmup=tire_warmup, wheels=_wheel_summary(record),
        coast_total=lap_coast_total(record),
    )


# The per-corner geometry metrics `corner_geometry` can emit, in the order they are
# baked. One owner because three writers touch them -- the backfill script that bakes
# them, the editor's save path that carries them forward, and this function -- and a
# key missing from one of those lists is a metric silently dropped on save (#62).
CORNER_GEOMETRY_KEYS = ("radius_m", "width_m", "banking_deg", "elev_delta_m")

# Which of those are SIGNED measurements rather than lengths. A signed one is
# legitimately zero or negative (a flat corner, a downhill one); a length cannot be, so
# a stored radius/width <= 0 did not come from a measurement and a save recomputes it
# instead of preserving it. Kept HERE, beside the key list, rather than inline at the
# one use site: a metric added to one list and not the other silently inherits the wrong
# rule, and "one writer's list missing a key" is exactly what deleted Laguna's
# width/banking (#62). Add a fifth metric -> classify it here in the same edit.
SIGNED_GEOMETRY_KEYS = frozenset({"banking_deg", "elev_delta_m"})


def measurable_geometry(key: str, value) -> bool:
    """Is a stored geometry value one that could have been MEASURED -- a finite number,
    and for a length also > 0?

    One owner for two questions that must not disagree: whether the editor's save
    carries a baked metric forward rather than recomputing it (#62), and whether any
    writer may put it in a track file at all (#65). These come off disk, including
    hand-edited and third-party maps, so `null`, a string, a dict, a non-finite float
    and a zero/negative LENGTH are not data. A string is the sharp one: it loads, and
    then `geometry_clause` raises `Unknown format code 'f'` on the coach's next answer.

    The bool check is load-bearing, not defensive noise: bool IS an int in Python, so
    `True > 0` would carry a stored `true` through as a measurement. OverflowError is
    too: `math.isfinite` RAISES on an int too large for a double, which JSON parses.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        if not math.isfinite(value):
            return False
    except OverflowError:
        return False
    return value > 0 or key in SIGNED_GEOMETRY_KEYS


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
        # A null/NaN station is a ribbon bin whose samples never spanned the width
        # (ribbon._MIN_SPREAD_M): unknown, so no metric -- and never a NaN, which
        # _write_track_json refuses (allow_nan=False).
        if w is not None and ai < len(w) and np.isfinite(w[ai]):
            m["width_m"] = round(float(w[ai]), 1)
        if b is not None and ai < len(b) and np.isfinite(b[ai]):
            m["banking_deg"] = round(float(np.degrees(b[ai])), 1)
        if e is not None:
            si, ei = at(c["start_dist"]), at(c["end_dist"])
            if si < len(e) and ei < len(e):
                m["elev_delta_m"] = round(float(e[ei] - e[si]), 1)  # + = uphill
        out[label] = m
    return out


def geometry_clause(metrics: dict, direction: str | None) -> str:
    """Render baked per-corner elevation/banking as a digest clause, omitting
    absent metrics; values below a threshold are dropped as noise.

    `direction` is the corner's OWNED turn direction ("left"/"right", computed
    from telemetry geometry -- never inferred). It is required because a
    cross-slope is only meaningful against it: banking_deg's sign names the
    direction the slope favours (rig-validated in 9756b65 on Laguna's Corkscrew,
    negative = left, positive = right), so the same 3 degrees is BANKED for a
    corner turning that way and OFF-CAMBER for one turning the other. Rendering
    the bare magnitude (issue #35) told the driver the track was helping them
    where it was falling away -- they then carry more speed into exactly the
    wrong place. Pass None only when the direction genuinely isn't known; the
    banking term is then omitted rather than guessed.
    """
    # radius_m / width_m are deliberately NOT rendered (#99): the baked radius
    # sits on the window edge for half the shipped corners (#73/#76) and the
    # width is inflated 5-7 m through chicanes (#83), so the coach was stating
    # wrong numbers as owned fact. They stay in the files for the UI ribbon.
    parts = []
    ed = metrics.get("elev_delta_m")
    if ed is not None and abs(ed) >= 1.0:
        parts.append(f"{'uphill' if ed > 0 else 'downhill'} {abs(ed):.0f}m")
    bk = metrics.get("banking_deg")
    if bk is not None and abs(bk) >= 1.0 and direction in ("left", "right"):
        favours = "left" if bk < 0 else "right"
        camber = "banked" if favours == direction else "off-camber"
        parts.append(f"{abs(bk):.0f}\N{DEGREE SIGN} {camber}")
    return ", ".join(parts)


# --- Driver-input traces (UI: Pit Wall graphs, issue #21) ---------------------
#
# The two source structs run at DIFFERENT rates: throttle/brake/steering come from
# the telemetry struct at ~48.6 Hz, `lap_distance` from the scoring struct at ~5 Hz.
# So the stored lap_distance channel is a STAIRCASE -- roughly ten identical values,
# then a jump. Plotting inputs straight against it collapses ~10 input samples onto
# one x and draws visible stair steps that hide the modulation the graph exists to
# show.
#
# Fix, part 1: treat the FIRST sample of each held run as a knot (that is when the
# scoring value became visible) and linearly interpolate lap_distance across the
# samples in between, using the sample index as a uniform-time axis. smm.py polls
# both structs on one loop iteration, so first-of-run is late by 0-20 ms (<=1.1 m at
# 200 km/h), not by a whole tick; assuming constant speed between knots costs <=8 cm
# under 1.5 g. The lag is shared by every lap, so overlaid laps stay aligned -- and
# the lap-distance axis (the repo's alignment key) is preserved rather than swapped
# for a time axis.
#
# Fix, part 2: reduce with a per-bin min/max ENVELOPE, never a point sample. 500
# bins puts one bin at 180 ms (5 km lap) to 400 ms (Le Mans) -- AT OR COARSER than
# the 200 ms scoring tick part 1 exists to see inside -- so point-sampling the grid
# drops a 0.1 s lift at 9 of 10 phases, worse than the naive scoring-rate resample.
# The envelope keeps both extremes of each bin, so no input event can fall between
# grid points, at any phase. Raising `bins` narrows the bins but does not remove
# the aliasing; the envelope does.
_INPUT_CHANNELS = ("throttle", "brake", "steering")


def input_traces(record: LapRecord, bins: int = 500) -> dict:
    """Throttle/brake/steering resampled onto a uniform lap_distance grid.

    Pure and deterministic; UI-facing only (never fed to the model). Returns
    ``{"length", "dist", "throttle", "brake", "steering"}`` where `dist` is the
    `bins` bin centres and each channel is ``{"lo": [...], "hi": [...]}`` — the
    min/max of the samples in each bin. Returns ``{}`` when the lap lacks a
    channel or is too short.

    `bins` bounds the payload: a full lap at 48.6 Hz is 4k-10k samples per channel.
    """
    ch = record.channels
    if not all(k in ch for k in ("lap_distance", *_INPUT_CHANNELS)):
        return {}
    arrays = {k: np.asarray(ch[k], dtype=float) for k in ("lap_distance", *_INPUT_CHANNELS)}
    n = min(len(a) for a in arrays.values())
    if n < 2:
        return {}
    arrays = {k: a[:n] for k, a in arrays.items()}

    mask = np.ones(n, dtype=bool)
    for a in arrays.values():
        mask &= np.isfinite(a)
    keep = np.flatnonzero(mask)
    if keep.size < 2:
        return {}
    d = arrays["lap_distance"][keep]

    # Knots = first sample of each held run, in ORIGINAL sample numbering (so the
    # index axis stays a uniform-time axis even after dropping non-finite samples).
    run_start = np.r_[True, np.diff(d) != 0]
    ki, kd = keep[run_start], d[run_start]
    # Keep only knots that beat the RUNNING MAXIMUM. Comparing each knot to its
    # immediate predecessor is not enough: after a backward step (a wrap, a scoring
    # glitch) the next knot passes as long as it beats the DIPPED value, leaving the
    # axis non-monotonic -- and np.interp returns garbage on an unsorted xp without
    # raising. This guarantees the strictly-increasing xp/fp pair np.interp needs.
    forward = np.r_[True, kd[1:] > np.maximum.accumulate(kd)[:-1]]
    ki, kd = ki[forward], kd[forward]
    if ki.size < 2:
        return {}
    # The trailing partial run has no next knot to interpolate toward; extend it at
    # the last observed rate so the final ~0.2 s isn't clamped flat.
    if ki[-1] < keep[-1]:
        rate = (kd[-1] - kd[-2]) / (ki[-1] - ki[-2])
        kd = np.append(kd, kd[-1] + rate * (keep[-1] - ki[-1]))
        ki = np.append(ki, keep[-1])

    dist = np.interp(keep, ki, kd)

    # Per-bin min/max ENVELOPE, not point-sampling. At 500 bins one bin spans
    # 180 ms (5 km lap) to 400 ms (Le Mans) -- at or COARSER than the 200 ms
    # scoring tick the de-staircasing above exists to see inside. Point-sampling
    # the grid drops any event narrower than a bin whenever it lands between two
    # grid points, which at Le Mans loses a 0.1 s lift more often than the naive
    # scoring-rate resample does. Taking each bin's extremes instead means a stab
    # or a lift cannot vanish at any phase; the chart draws the band.
    edges = np.linspace(float(dist[0]), float(dist[-1]), bins + 1)
    centres = (edges[:-1] + edges[1:]) / 2.0
    idx = np.clip(np.searchsorted(edges, dist, side="right") - 1, 0, bins - 1)
    empty = np.bincount(idx, minlength=bins) == 0

    out: dict = {"length": round(float(dist[-1]), 1),
                 "dist": [round(float(v), 1) for v in centres]}
    for k in _INPUT_CHANNELS:
        v = arrays[k][keep]
        lo = np.full(bins, np.inf)
        hi = np.full(bins, -np.inf)
        np.minimum.at(lo, idx, v)
        np.maximum.at(hi, idx, v)
        if empty.any():
            # Bins finer than the sample spacing hold nothing; interpolate them so
            # the chart has no holes.
            pt = np.interp(centres, dist, v)
            lo = np.where(empty, pt, lo)
            hi = np.where(empty, pt, hi)
        out[k] = {"lo": [round(float(x), 3) for x in lo],
                  "hi": [round(float(x), 3) for x in hi]}
    return out
