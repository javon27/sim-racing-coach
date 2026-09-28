import math
from dataclasses import dataclass

import numpy as np

from simcoach.corners import Corner, CornerMap, compute_direction, in_window
from simcoach.model_types import LapRecord
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
    # Max brake input (0..1) in the braking zone (#169: the window alone read "@ 0%
    # peak" for a corner braked before turn-in); else over the run `brake_point`
    # walked; the window's max only when neither exists.
    peak_brake: float = 0.0
    # #169: the braking ZONE -- first brake >= 0.1 after the last throttle >= 0.8
    # before the apex (same 250 m look-back, never back past the previous corner's
    # end), so a mid-zone pressure dip doesn't
    # move it the way it moves `brake_point` -- and the peak brake inside it. None
    # with no braking in the zone. Not stored in corner_metrics: computed on read.
    brake_zone_start: float | None = None
    brake_zone_peak: float | None = None
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
    # #207: the car left the road -- _OFF_WHEELS+ wheels on grass/dirt/gravel
    # (mSurfaceType) for _OFF_SAMPLES+ consecutive samples. None = not measured: no
    # surface channels (captured before them), or they read 0 all lap (dead channel).
    # Never guessed from track_edge_min.
    off_surface: bool | None = None
    coast_time: float = float("nan")      # seconds in the corner with NEITHER pedal
    coast_dist: float = float("nan")      # meters in the corner with neither pedal
    brake_to_throttle_dist: float = float("nan")  # last brake-off -> first throttle (m)
    min_spd_delta: float | None = None
    # #102: every other comparable metric, diffed the same way (current - reference).
    # Distance fields are metres, positive = LATER than the reference (further along
    # the lap); the rest are in the field's own native unit. None when either side is
    # None/NaN -- never computed from a "not found" sentinel.
    brake_point_delta: float | None = None
    throttle_point_delta: float | None = None
    trailbrake_overlap_delta: float | None = None
    coast_time_delta: float | None = None
    brake_to_throttle_dist_delta: float | None = None
    peak_brake_g_delta: float | None = None
    peak_lat_g_delta: float | None = None
    abs_pct_delta: float | None = None
    tc_pct_delta: float | None = None
    entry_spd_delta: float | None = None
    exit_spd_delta: float | None = None
    label: str = ""                          # displayed designation ("8A")
    apexes: list[float] | None = None     # apex lap_distances (>1 = multi-apex)
    apex_min_speeds: list[float] | None = None  # min speed per apex (parallel to apexes)
    section: str | None = None            # named section this corner sits in, if any
    # #38: balance read. None when steering/lat_g weren't both captured, or the
    # window was too sparse to read (see handling_read).
    handling: str | None = None       # "understeer" | "oversteer" | "neutral" | None
    handling_why: str | None = None   # short driver-facing reason, or None
    lockup: bool | None = None        # a wheel stopped under braking; None if wheel_rot absent
    wheelspin: bool | None = None     # a driven wheel outpaced the front axle; None if absent
    # #38 P1.4: traction-circle utilization. None when the envelope or the
    # corner's own cornering-phase sample count couldn't support a read (see
    # `grip_envelope` / `_gg_used_and_dead`).
    gg_used: float | None = None          # p95 combined g in the cornering phase / envelope
    gg_dead_middle: float | None = None   # fraction of the phase under half the envelope
    # #38 P1.7: gear/RPM sanity. None when gear/rpm weren't both captured (see
    # `gear_read`); gear_min is also None if the car was in neutral/reverse at
    # the min-speed sample.
    gear_min: int | None = None       # forward gear at the min-speed sample
    bog: bool | None = None           # rpm below its power band at the exit throttle pickup
    short_shift: bool | None = None   # an upshift inside the window well below redline
    # #38 P1.8: input-shape facts (see `input_shape`). Release fields are None
    # with no braking phase or no release inside the window; reversal counts
    # are None when their channel is absent.
    brake_release_m: float | None = None
    brake_release_steep_frac: float | None = None
    brake_reversals: int | None = None
    throttle_reversals: int | None = None
    steer_reversals: int | None = None
    brake_release_m_delta: float | None = None
    brake_release_steep_frac_delta: float | None = None
    brake_reversals_delta: int | None = None
    throttle_reversals_delta: int | None = None
    steer_reversals_delta: int | None = None


def corner_tag(cm) -> str:
    """'T16 Kemmel' for a real name, 'T16' alone when the map has no name for
    this corner beyond its own number/label ('Turn 16', 'T16', or the bare
    label) -- rendering those next to the number produces 'Turn 16 (Turn 16)'
    (#141). Accepts either a raw map Corner (`.number`) or a CornerMetrics
    (`.corner_no`)."""
    no = cm.corner_no if hasattr(cm, "corner_no") else cm.number
    label = cm.label or no
    tag = f"T{label}"
    name = (cm.name or "").strip()
    generic = {str(label).lower(), tag.lower(), f"turn {label}".lower()}
    if not name or name.lower() in generic:
        return tag
    return f"{tag} {name}"


@dataclass
class LapAnalysis:
    lap_time: float
    corners: list[CornerMetrics]
    has_reference: bool
    # Lap-level tire state (the coachable signal: imbalance + warm-up). Per-corner
    # tire temps are deferred until the replay overlay actually consumes them.
    tire_means: dict[str, float] | None = None      # {'fl','fr','rl','rr'}: °C mean over lap
    tire_warmup: tuple[float, float] | None = None  # (early °C, late °C), all-wheel mean
    wheels: dict | None = None  # lap-level brake/pressure/wear from extended capture
    coast_total: "tuple[float, float] | None" = None   # (seconds, meters) with neither pedal
    # #38 P1.4: the car's usable grip radius in g (see `grip_envelope`) and this
    # lap's own p95 combined g in cornering samples / that envelope. None when
    # lat_g/long_g weren't captured or gave no usable samples.
    gg_envelope: float | None = None
    gg_used: float | None = None
    # #108 item 2: how many recent-lap records fed `gg_envelope` (None from a
    # caller with no lap history, e.g. the self-referenced fallback below) --
    # format.py won't report a percentage off an envelope built from too few.
    gg_envelope_laps: int | None = None
    gg_peak: float | None = None   # p95 combined g in THIS lap's cornering phase (not ÷ envelope)


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
    name = {i: corner_tag(c) for i, c in enumerate(cmap.corners)}
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


# --- time on the table (#169) --------------------------------------------------
# A corner segment is listed as "your laps were quicker here" only when two laps beat
# this one by at least this much (s). The exact axis's per-bucket noise is 0.005 s
# (format._BUCKET_NOISE_S_EXACT); this sits well above it.
# ponytail: tuning knobs -- a rig read on a known corner settles the floor and the
# lap minimum.
_FORM_FLOOR_S = 0.03
_FORM_MIN_LAPS = 5
PHASES = ("approach", "entry", "exit", "run-out")


def _sector_of(dist: float, bounds) -> int:
    """0/1/2: which game sector a lap distance falls in (same rule as
    `format.corner_descriptor`)."""
    return 0 if dist <= bounds[0] else (1 if dist <= bounds[1] else 2)


def optimal_sectors(laps: list[dict], bounds) -> dict | None:
    """The optimal lap from the game's own sector times: the best of each sector.

    `laps`: `{"key", "sectors": [s1, s2, s3] | None, "incidents": [lap_distance]}`
    per comparable lap, the subject included; `incidents` are the lap's spin/off
    distances. A sector counts only when its lap had no spin/off inside it; with no
    `bounds` any incident drops the whole lap. None when a sector has no clean time.
    Else `{"total", "best": [(time, key)] * 3, "dropped": [(sector, time, key)]}` --
    `dropped` are incident sectors that were quicker than the clean best."""
    best: list = [None, None, None]
    tainted = []
    for lap in laps:
        s = lap.get("sectors")
        if not s or len(s) != 3 or any(v is None or not v > 0 for v in s):
            continue
        inc = lap.get("incidents") or []
        bad = ({_sector_of(d, bounds) for d in inc} if bounds
               else ({0, 1, 2} if inc else set()))
        for i, t in enumerate(s):
            if i in bad:
                tainted.append((i, float(t), lap["key"]))
            elif best[i] is None or t < best[i][0]:
                best[i] = (float(t), lap["key"])
    if any(b is None for b in best):
        return None
    return {"total": sum(b[0] for b in best), "best": best,
            "dropped": sorted((x for x in tainted if x[1] < best[x[0]][0]),
                              key=lambda x: (x[0], x[1]))}


def optimal_headline(lap_time: float, sectors, optimal: dict | None) -> float | None:
    """The one 'optimal lap' figure the coach states (#169's `get_time_on_table`,
    #176's lap rail) from `optimal_sectors`'s result: this lap's own time minus
    each sector's gain over its own sector, counting only gains >= 0.005 s.

    None when it can't be honestly stated: no sectors on this lap, no comparable
    lap (`optimal` is None), or this lap's OWN sector was one of the dropped ones
    (a spin/off) -- a total built on that sector would rest on the incident, so
    `format_time_on_table` prints gains only and no headline lap time."""
    if not sectors or optimal is None:
        return None
    if any(key == "this" for _, _, key in optimal.get("dropped", [])):
        return None
    under = sum(g for g in (sectors[i] - t for i, (t, _) in enumerate(optimal["best"]))
                if g >= 0.005)
    return lap_time - under


def form_segments(cmap: CornerMap) -> list[tuple]:
    """(corner, seg_start, seg_end) per corner: the corner with its approach and
    run-out, from the previous corner's end to the next corner's start (owned map
    points). The first/last segment runs from/to the lap's ends.
    ponytail: a seam-wrapped window keeps only its pre-line part (it sorts last);
    split it across the seam if a track's last corner wraps and reads wrong."""
    cs = sorted(cmap.corners, key=lambda c: c.start_dist)
    return [(c, cs[i - 1].end_dist if i else 0.0,
             cs[i + 1].start_dist if i + 1 < len(cs) else math.inf)
            for i, c in enumerate(cs)]


def corner_form(subject: LapRecord, others: list[tuple], cmap: CornerMap) -> dict:
    """This lap against the driver's own other laps, segment by segment.

    `others`: `(key, record, incident_distances)` per comparable lap. For each lap L,
    `d` = L's time minus this lap's across the segment (negative = L quicker); a
    segment with an incident on L skips L. Per segment: `typical` (median),
    `repeatable` (second smallest -- two laps were at least this much quicker),
    `donor` (the lap with the smallest), `spread` (population stddev).

    `{"ok": False, "n"}` with fewer than `_FORM_MIN_LAPS` laps. Else `{"ok", "n",
    "listed", "at_best"}`: `listed` = segments whose `repeatable` <= -floor, ranked by
    it; `at_best` = segments where no lap was quicker by the floor. Never summed."""
    segs = form_segments(cmap)
    per = [[] for _ in segs]
    n = 0
    for key, rec, incidents in others:
        aligned = align_time_axes(subject, rec)
        if aligned is None:
            continue
        n += 1
        grid, delta = aligned
        for vals, (_, a, b) in zip(per, segs):
            if any(a <= x <= b for x in incidents):
                continue
            vals.append((-float(np.interp(b, grid, delta) - np.interp(a, grid, delta)), key))
    if n < _FORM_MIN_LAPS:
        return {"ok": False, "n": n}
    rows = []
    for vals, (c, a, b) in zip(per, segs):
        if not vals:
            continue
        ds = sorted(vals, key=lambda v: v[0])
        arr = np.array([v[0] for v in vals])
        rows.append({"corner": c, "seg": (a, b), "typical": float(np.median(arr)),
                     "repeatable": ds[1][0] if len(ds) > 1 else None,
                     "donor": ds[0][1], "donor_d": ds[0][0], "spread": float(np.std(arr)),
                     "n": len(vals)})
    listed = sorted((r for r in rows if r["repeatable"] is not None
                     and r["repeatable"] <= -_FORM_FLOOR_S), key=lambda r: r["repeatable"])
    return {"ok": True, "n": n, "listed": listed,
            "at_best": [r for r in rows if r["donor_d"] > -_FORM_FLOOR_S]}


def phase_split(subject: LapRecord, donor: LapRecord, corner: Corner, seg) -> dict | None:
    """Seconds this lap LOST to `donor` (positive = slower) per phase of one corner
    segment, cut at owned map points: approach [seg start, corner start], entry
    [start, first apex], exit [first apex, end], run-out [end, seg end]. None when
    the laps aren't comparable or the window wraps the S/F seam."""
    aligned = align_time_axes(subject, donor)
    if aligned is None or corner.start_dist > corner.end_dist:
        return None
    grid, delta = aligned
    apex = min(corner.apexes) if corner.apexes else corner.apex_dist
    t = np.interp([seg[0], corner.start_dist, apex, corner.end_dist, seg[1]], grid, delta)
    return dict(zip(PHASES, (float(x) for x in np.diff(t))))


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


# --- handling read (#38) -------------------------------------------------
# A heuristic balance index, NOT a vehicle model: steering vs lateral g (plus
# wheel_rot for lockup/wheelspin) is all this codebase can read (grip_*/
# tire_load_* are dead -- LMU never fills them, #43). Every threshold below is a
# tuning knob; a rig session on a corner the driver KNOWS is understeering/
# oversteering is what settles them, not more code.
# ponytail: steering-rise threshold (fraction of full lock) for "still winding
# lock and getting nothing" -- loosen if clean laps false-trigger, tighten if a
# known understeer corner reads neutral.
_US_STEER_RISE = 0.10
# ponytail: how flat lat_g must sit near its own peak to call it "plateaued".
_US_LATG_PLATEAU = 0.10
# ponytail: consecutive samples of countersteer (~50 Hz, so 5 ~= 0.1 s) before
# it's a real correction rather than a twitch.
_OS_MIN_SAMPLES = 5
# ponytail: minimum |lat_g| to trust a countersteer/exit read as loaded, not parked.
_OS_LATG_MIN = 0.5
# ponytail: how much steering must ease off at the exit (fraction of lock) to
# read as the car rotating on its own rather than the driver just unwinding normally.
_OS_STEER_DROP = 0.15
# ponytail: minimum window size to trust any of the above -- fewer samples is
# noise, not a read.
_HANDLING_MIN_SAMPLES = 10
# ponytail: wheel_rot (rad/s) below this while braking hard at speed reads as
# a stopped wheel (lockup), not just a slow one.
_LOCKUP_ROT = 1.0
# ponytail: how far a driven wheel must outpace the front axle mean to call it
# wheelspin rather than normal diff/track-surface scatter.
_SPIN_RATIO = 1.15


def _longest_run(mask: np.ndarray) -> int:
    """Longest run of consecutive True in a 1-D boolean array."""
    best = run = 0
    for v in mask:
        run = run + 1 if v else 0
        best = max(best, run)
    return best


def handling_read(win_steering, win_lat_g, win_speed, win_brake, win_throttle,
                  wheel_rot: "dict[str, np.ndarray] | None") -> dict:
    """Pure per-corner balance read from already-windowed channels (no LLM, no
    corner identity -- the caller supplies that separately).

    Returns {"handling": "understeer"|"oversteer"|"neutral"|None, "handling_why":
    str|None, "lockup": bool|None, "wheelspin": bool|None}. `handling`/
    `handling_why` are None when steering/lat_g/speed aren't all present or the
    window is too short (`_HANDLING_MIN_SAMPLES`); `lockup`/`wheelspin` are None
    when `wheel_rot` doesn't carry all four wheels.
    """
    handling = handling_why = None
    n = len(win_steering) if win_steering is not None else 0
    if (win_steering is not None and win_lat_g is not None and win_speed is not None
            and n >= _HANDLING_MIN_SAMPLES):
        steer = np.asarray(win_steering, dtype=float)
        lat_g = np.asarray(win_lat_g, dtype=float)
        abs_steer, abs_latg = np.abs(steer), np.abs(lat_g)
        peak_steer = float(np.nanmax(abs_steer))
        peak_latg = float(np.nanmax(abs_latg))
        min_i = int(np.nanargmin(win_speed))

        over = np.where(abs_steer > 0.5 * peak_steer)[0] if peak_steer > 0 else np.array([], dtype=int)
        mid_start = min(int(over[0]), min_i) if over.size else 0

        understeer = False
        if min_i > mid_start:
            mid = slice(mid_start, min_i + 1)
            steer_rise = abs_steer[min_i] - abs_steer[mid_start]
            plateau = peak_latg - float(np.nanmin(abs_latg[mid]))
            if steer_rise > _US_STEER_RISE and plateau <= _US_LATG_PLATEAU:
                understeer = True
                handling = "understeer"
                handling_why = (f"steering still winding +{steer_rise * 100:.0f}% lock "
                                f"with lat g flat at {peak_latg:.1f}")

        if not understeer:
            corner_sign = 1.0 if float(np.nanmean(lat_g)) >= 0 else -1.0
            countersteer = (np.sign(steer) * corner_sign > 0) & (abs_latg >= _OS_LATG_MIN)
            run = _longest_run(countersteer)
            oversteer = run >= _OS_MIN_SAMPLES
            if oversteer:
                handling_why = f"countersteer against the corner, lat g holding {peak_latg:.1f}"
            elif min_i < n - 1:
                exit_drop = abs_steer[min_i] - abs_steer[-1]
                exit_throttle = float(np.nanmean(win_throttle[min_i:])) if win_throttle is not None else 0.0
                exit_plateau = peak_latg - float(np.nanmin(abs_latg[min_i:]))
                if (exit_drop > _OS_STEER_DROP and exit_throttle > 0.3
                        and exit_plateau <= _US_LATG_PLATEAU):
                    oversteer = True
                    handling_why = (f"steering easing off exit while throttle "
                                    f"{exit_throttle:.0%}, lat g flat at {peak_latg:.1f}")
            if oversteer:
                handling = "oversteer"
        if handling is None:
            handling = "neutral"

    lockup = wheelspin = None
    if wheel_rot is not None and all(k in wheel_rot for k in ("fl", "fr", "rl", "rr")):
        # mRotation is NEGATIVE rolling forward on real laps (mean ~ -176 rad/s), so
        # every comparison below is on magnitude; signed values would flag a rear
        # wheel turning SLOWER than the fronts as wheelspin.
        rot = {k: np.abs(np.asarray(v, dtype=float)) for k, v in wheel_rot.items()}
        if win_brake is not None and win_speed is not None:
            braking = (np.asarray(win_brake) > 0.3) & (np.asarray(win_speed) > 30.0)
            lockup = bool(braking.any() and any(
                np.any(rot[w][braking] < _LOCKUP_ROT) for w in ("fl", "fr", "rl", "rr")))
        if win_throttle is not None:
            on_power = np.asarray(win_throttle) > 0.5
            front_mean = (rot["fl"] + rot["fr"]) / 2.0
            wheelspin = bool(on_power.any() and any(
                np.any(rot[w][on_power] > _SPIN_RATIO * front_mean[on_power]) for w in ("rl", "rr")))

    return {"handling": handling, "handling_why": handling_why,
            "lockup": lockup, "wheelspin": wheelspin}


# --- gear/RPM sanity (#38 P1.7) ------------------------------------------
# ponytail: tuning knobs -- a rig read on a car with a known redline/power band
# settles them.
_BOG_RPM_FRAC = 0.55     # exit rpm below this fraction of the lap's peak rpm = bogged
_SHORT_SHIFT_FRAC = 0.80  # upshift rpm below this fraction of the lap's peak = short-shifted


def gear_read(win_gear, win_rpm, win_throttle, win_speed, min_i, rpm_max_lap) -> dict:
    """Pure per-corner gear/RPM read from already-windowed channels + this lap's
    own peak rpm (`rpm_max_lap`, computed once over the whole lap by the caller).

    Returns {"gear_min", "bog", "short_shift"}, all None when gear or rpm wasn't
    captured for this lap (or the lap never showed a positive rpm).

    gear_min: the forward gear at the min-speed sample (`min_i`); None if the
    car was in neutral/reverse there (gear <= 0 -- smm.py: 0 neutral, -1 reverse).
    bog: at the first throttle pickup (throttle > 0.5) after `min_i`, whether
    rpm sat below `_BOG_RPM_FRAC` of the lap's peak -- engine below its power
    band on exit. None with no such pickup in the window.
    short_shift: whether any upshift inside the window (gear rising between
    consecutive samples) happened at rpm below `_SHORT_SHIFT_FRAC` of the lap's
    peak. None with no upshift in the window.
    """
    none = {"gear_min": None, "bog": None, "short_shift": None}
    if (win_gear is None or win_rpm is None or rpm_max_lap is None
            or not math.isfinite(rpm_max_lap) or rpm_max_lap <= 0):
        return none
    gear = np.asarray(win_gear, dtype=float)
    rpm = np.asarray(win_rpm, dtype=float)
    n = min(gear.size, rpm.size)
    if min_i >= n:
        return none

    g_min = gear[min_i]
    gear_min = round(g_min) if np.isfinite(g_min) and g_min > 0 else None

    bog = None
    if win_throttle is not None:
        throttle = np.asarray(win_throttle, dtype=float)[:n]
        after = np.where(throttle[min_i + 1:] > 0.5)[0]
        if after.size:
            r = rpm[min_i + 1 + int(after[0])]
            if np.isfinite(r):
                bog = bool(r < _BOG_RPM_FRAC * rpm_max_lap)

    short_shift = None
    if n >= 2:
        gear, rpm = gear[:n], rpm[:n]
        up = np.where(np.diff(gear) > 0)[0]      # index i where gear[i+1] > gear[i]
        shift_rpm = rpm[up]
        valid = shift_rpm[np.isfinite(shift_rpm)]
        if valid.size:
            short_shift = bool(np.any(valid < _SHORT_SHIFT_FRAC * rpm_max_lap))

    return {"gear_min": gear_min, "bog": bog, "short_shift": short_shift}


# --- traction-circle (G-G) utilization (#38 P1.4) ------------------------
# lat_g/long_g are the only load channels LMU actually fills (grip_*/tire_load_*
# are dead, #43): "how much of the car's own grip is being used" has to come
# from those two, combined.
_GG_LOAD_GATE = 0.3       # ponytail: |lat_g| or |long_g| below this is parked/straight-line noise
_GG_ENVELOPE_PCTL = 98.0  # ponytail: p98, not max -- a single crash spike must not set the
                          # envelope (#78: a raw max was set by one 40g spike)
_GG_USED_PCTL = 95.0      # ponytail: p95 of the cornering phase, not its own max
_GG_DEAD_FRAC = 0.5       # ponytail: below half the envelope reads as "inputs not combined"
_GG_MIN_SAMPLES = 10      # ponytail: fewer cornering-phase samples is noise, not a read
# #108 item 2: fewer than this many laps' worth of samples feeding the envelope
# isn't a real ceiling yet -- a first hot lap read "143% of the theoretical grip
# envelope" off a 1-2 lap envelope. format.py reports peak g instead below this.
# ponytail: a rig session settles whether 5 is the right knob.
_ENVELOPE_MIN_LAPS = 5


def combined_g(lat_g, long_g) -> np.ndarray:
    """Combined lateral+longitudinal load magnitude (g), elementwise."""
    return np.hypot(np.asarray(lat_g, dtype=float), np.asarray(long_g, dtype=float))


def grip_envelope(records: "list[LapRecord]") -> float | None:
    """The car's usable grip radius (g), estimated from lat_g/long_g over all
    the given laps' samples where |lat_g| or |long_g| clears `_GG_LOAD_GATE`
    (drops parked/straight-line noise).

    ponytail: naive percentile-of-observed-max -- a fitted circle would be more
    accurate later. p98 rather than max because a single crash spike otherwise
    sets the envelope (#78 measured a raw max of long_g at 40g from one such
    sample). None when no record supplies both channels, or no sample clears
    the gate.
    """
    samples = []
    for r in records or []:
        if r is None:
            continue
        lat_g, long_g = r.channels.get("lat_g"), r.channels.get("long_g")
        if lat_g is None or long_g is None:
            continue
        n = min(len(lat_g), len(long_g))
        lat_g = np.asarray(lat_g[:n], dtype=float)
        long_g = np.asarray(long_g[:n], dtype=float)
        cg = combined_g(lat_g, long_g)
        used = (np.abs(lat_g) > _GG_LOAD_GATE) | (np.abs(long_g) > _GG_LOAD_GATE)
        used &= np.isfinite(cg)
        if used.any():
            samples.append(cg[used])
    if not samples:
        return None
    return float(np.percentile(np.concatenate(samples), _GG_ENVELOPE_PCTL))


def _cornering_phase_cg(lat_g, long_g) -> "np.ndarray | None":
    """Combined g restricted to the cornering phase (|lat_g| > `_GG_LOAD_GATE`,
    finite), or None with no channels or fewer than `_GG_MIN_SAMPLES` samples.
    Shared by `_gg_used_and_dead` (divided by an envelope) and the lap-level
    peak (#108 item 2: reads intelligibly on its own, before an envelope forms)."""
    if lat_g is None or long_g is None:
        return None
    n = min(len(lat_g), len(long_g))
    lat_g = np.asarray(lat_g[:n], dtype=float)
    long_g = np.asarray(long_g[:n], dtype=float)
    cg = combined_g(lat_g, long_g)
    phase = (np.abs(lat_g) > _GG_LOAD_GATE) & np.isfinite(cg)
    if int(np.sum(phase)) < _GG_MIN_SAMPLES:
        return None
    return cg[phase]


def _gg_used_and_dead(lat_g, long_g, envelope: float | None) -> "tuple[float | None, float | None]":
    """(gg_used, gg_dead_middle) for one phase (a corner window or a whole lap):
    p95 combined g over cornering-phase samples (|lat_g| > `_GG_LOAD_GATE`)
    divided by `envelope`, and the fraction of that phase below
    `_GG_DEAD_FRAC` of the envelope (coasting/inputs-not-combined). (None, None)
    when the envelope, or either channel, is missing, or fewer than
    `_GG_MIN_SAMPLES` cornering-phase samples survive."""
    if envelope is None:
        return None, None
    cg_phase = _cornering_phase_cg(lat_g, long_g)
    if cg_phase is None:
        return None, None
    used = float(np.percentile(cg_phase, _GG_USED_PCTL)) / envelope
    dead = float(np.mean(cg_phase < _GG_DEAD_FRAC * envelope))
    return used, dead


# --- input-shape facts (#38 P1.8) ----------------------------------------
# What a race engineer reads off the input graph beyond point-in-time metrics:
# how the brake was released, and how much the driver modulated each
# pedal/the wheel. Pure functions on already-windowed channels, same shape as
# handling_read/gear_read above.
# ponytail: brake fully released below this fraction of full pressure.
_BRAKE_OFF = 0.05
# ponytail: peak brake below this means no real braking phase to measure a
# release shape from.
_INPUT_SHAPE_MIN_BRAKE = 0.1
# ponytail: hysteresis (fraction of full pedal/steering range) below which a
# wiggle doesn't count as a reversal -- a rig read on a known pumping/sawing
# corner settles it.
_MOD_HYST = 0.05


def _turning_points(x, hyst: float) -> "tuple[int, int]":
    """(ups, downs): direction changes in `x` that exceed `hyst`, split by the
    direction the trace turns INTO. A state machine tracks the last extreme in
    the current direction and counts a turn only once the trace has moved more
    than `hyst` back against it. The first movement away from `x[0]` only
    establishes a direction. `ups` = the trace turned back upward (a pedal
    re-applied after easing off); `downs` = it turned back downward."""
    x = np.asarray(x, dtype=float)
    if x.size < 2:
        return 0, 0
    ups = downs = 0
    extreme = x[0]
    direction = 0  # 0 = undetermined, 1 = rising, -1 = falling
    for v in x[1:]:
        if direction == 0:
            if v - extreme > hyst:
                direction, extreme = 1, v
            elif extreme - v > hyst:
                direction, extreme = -1, v
        elif direction == 1:
            if v > extreme:
                extreme = v
            elif extreme - v > hyst:
                downs += 1
                direction, extreme = -1, v
        else:
            if v < extreme:
                extreme = v
            elif v - extreme > hyst:
                ups += 1
                direction, extreme = 1, v
    return ups, downs


def _reversals(x, hyst: float) -> int:
    """Pedal re-applications: the number of times `x` turns back UP by more
    than `hyst` after easing off. A single clean apply-then-release has none
    (that is one downward turn, not a reversal), so a smooth corner reads 0
    and "3 brake reversals" means three pumps -- the driver-facing meaning."""
    return _turning_points(x, hyst)[0]


def _reversals_masked(x, mask, hyst: float) -> int:
    """Sum of `_reversals` over each contiguous run of `mask`, rather than
    `_reversals(x[mask], hyst)`. Flattening the masked-out samples away first
    would splice two unrelated on-pedal segments together at the seam -- e.g.
    full throttle on the approach, masked out through braking, then the exit
    ramp -- and read the jump between them as a fake reversal that never
    happened in time."""
    x = np.asarray(x, dtype=float)
    mask = np.asarray(mask, dtype=bool)
    total = 0
    i = 0
    n = mask.size
    while i < n:
        if not mask[i]:
            i += 1
            continue
        j = i
        while j < n and mask[j]:
            j += 1
        total += _reversals(x[i:j], hyst)
        i = j
    return total


def _brake_release_shape(win_dist, win_brake) -> "tuple[float | None, float | None]":
    """(brake_release_m, brake_release_steep_frac): metres from the sample of
    peak brake (within the window) to the first sample after it where brake
    drops below `_BRAKE_OFF`, and the fraction of that release's total
    pressure drop that happens within its steepest contiguous 20% of samples
    (1.0 = dumped in one step, ~0.2 = perfectly linear). (None, None) when
    there's no braking phase (peak < `_INPUT_SHAPE_MIN_BRAKE`) or no release
    inside the window."""
    brake = np.asarray(win_brake, dtype=float)
    dist = np.asarray(win_dist, dtype=float)
    n = min(brake.size, dist.size)
    if n < 2:
        return None, None
    brake, dist = brake[:n], dist[:n]
    peak_i = int(np.nanargmax(brake))
    peak = brake[peak_i]
    if not np.isfinite(peak) or peak < _INPUT_SHAPE_MIN_BRAKE:
        return None, None
    after = np.where(brake[peak_i + 1:] < _BRAKE_OFF)[0]
    if after.size == 0:
        return None, None
    rel_i = peak_i + 1 + int(after[0])
    release_m = float(dist[rel_i] - dist[peak_i])
    seg = brake[peak_i:rel_i + 1]
    total_drop = float(seg[0] - seg[-1])
    if total_drop <= 0:
        return release_m, None
    drops = np.clip(-np.diff(seg), 0.0, None)
    win_len = max(1, round(0.2 * drops.size))
    if drops.size <= win_len:
        steepest = float(np.sum(drops))
    else:
        steepest = float(np.max(np.convolve(drops, np.ones(win_len), mode="valid")))
    return release_m, min(1.0, steepest / total_drop)


def input_shape(win_dist, win_brake, win_throttle, win_steering) -> dict:
    """Pure per-corner input-shape read from already-windowed channels (no
    LLM, no corner identity -- the caller supplies that separately).

    Returns {"brake_release_m", "brake_release_steep_frac", "brake_reversals",
    "throttle_reversals", "steer_reversals"}. The release fields are None with
    no braking phase or no release inside the window (see
    `_brake_release_shape`). Reversal counts (pumping/stabbing/sawing, via the
    shared `_reversals` helper) are None when their channel is absent;
    brake/throttle reversals only count while the pedal is actually applied
    (brake > `_BRAKE_OFF` / throttle > 0.1), steering counts the whole window
    and both signs.
    """
    release_m = steep_frac = None
    if win_brake is not None and win_dist is not None:
        release_m, steep_frac = _brake_release_shape(win_dist, win_brake)

    brake_reversals = None
    if win_brake is not None:
        b = np.asarray(win_brake, dtype=float)
        brake_reversals = _reversals_masked(b, b > _BRAKE_OFF, _MOD_HYST)

    throttle_reversals = None
    if win_throttle is not None:
        t = np.asarray(win_throttle, dtype=float)
        throttle_reversals = _reversals_masked(t, t > 0.1, _MOD_HYST)

    steer_reversals = None
    if win_steering is not None:
        # Sawing: every direction change beyond the corner's own lock-then-unlock
        # (one turning point), so a clean corner reads 0.
        ups, downs = _turning_points(win_steering, _MOD_HYST)
        steer_reversals = max(0, ups + downs - 1)

    return {
        "brake_release_m": release_m,
        "brake_release_steep_frac": steep_frac,
        "brake_reversals": brake_reversals,
        "throttle_reversals": throttle_reversals,
        "steer_reversals": steer_reversals,
    }


def _metrics_for_corner(
    record: LapRecord, corner: Corner, length: float | None = None,
    envelope: float | None = None, rpm_max_lap: float | None = None,
    prev_end: float | None = None,
) -> CornerMetrics:
    """`prev_end` (see `prev_corner_end`): the previous corner's end, which the
    braking zone never reaches back past (#169)."""
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
    run_peak = brake_zone_start = brake_zone_peak = None
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
            run_peak = float(np.nanmax(brake[order[j:k + 1]]))
        # #169: the zone runs from the last full-throttle sample before the apex (the
        # whole look-back when there is none) to the apex, and never back past the
        # previous corner's end -- a corner that never sees full throttle before the
        # next must not hand it its own braking. Its start is the braking sample
        # furthest back inside it -- the same wrap shift as brake_point.
        zone = order
        if prev_end is not None:
            zone = zone[back[zone] <= (corner.apex_dist - prev_end) % lap_len]
        thr = np.flatnonzero(throttle[zone] >= 0.8)
        zone = zone[:thr[0]] if thr.size else zone
        zb = np.flatnonzero(brake[zone] >= 0.1)
        if zb.size:
            dz = float(dist[zone[zb[-1]]])
            brake_zone_start = dz - lap_len if dz > corner.apex_dist else dz
            brake_zone_peak = float(np.nanmax(brake[zone]))

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
    # #169: the zone's peak survives a modulated run (released under 0.1, re-applied:
    # the run is then only the re-application -- "@ 11% peak" on an 82% stop).
    peak_brake = (brake_zone_peak if brake_zone_peak is not None
                  else run_peak if run_peak is not None
                  else float(np.nanmax(win_brake)) if win_brake.size else 0.0)
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
    # #207: track_edge_min is NOT an off-track signal. At Long Beach T1 it read
    # -2..-3 m on 35 of 40 laps with the car on asphalt (the AI edge there is
    # narrower than the road). "Off" is read from the wheels' own surface instead.
    # Liveness gate: a lap whose surface reads 0 on every wheel all lap never touched
    # a kerb -- far likelier the channel is dead (as mGripFract is, #43) than that the
    # lap was clean, so it measures nothing: None, not False.
    surf = [record.channels.get(f"surface_{w}") for w in _WHEELS]
    surf = [np.asarray(s, dtype=float) for s in surf if s is not None]
    if len(surf) == len(_WHEELS) and any((s > 0).any() for s in surf):
        n_off = sum(np.isin(s[idx], _OFF_SURFACES) for s in surf)
        # A run never joins across a gap in `idx` -- a window wrapping S/F puts the
        # lap's last samples next to its first.
        segs = np.split((n_off >= _OFF_WHEELS).astype(int), np.flatnonzero(np.diff(idx) != 1) + 1)
        off_surface = any((np.convolve(g, np.ones(_OFF_SAMPLES), "valid") >= _OFF_SAMPLES).any()
                          for g in segs)
    else:
        off_surface = None

    # Dead time: distance/seconds in the corner with neither pedal, plus the gap
    # between brake release and throttle pickup. Same ds/(v/3.6) integration as
    # lap_time_axis, restricted to the coasting mask.
    coast_time, coast_dist = _coast_totals(win_dist, win_speed, win_throttle, win_brake)
    brake_to_throttle_dist = _brake_to_throttle(dist, brake, throttle, idx)

    # #38: balance read (understeer/oversteer/lockup/wheelspin) from steering,
    # lat_g and wheel_rot -- the only channels LMU actually fills for this
    # (grip_*/tire_load_* are dead, #43). None of the inputs above; a pure
    # function on this corner's own window.
    win_lat_g_arr = win_lat_g[idx] if win_lat_g is not None else None
    win_long_g_arr = long_g[idx] if long_g is not None else None
    gg_used, gg_dead_middle = _gg_used_and_dead(win_lat_g_arr, win_long_g_arr, envelope)
    wheel_rot_win = None
    if all(f"wheel_rot_{w}" in record.channels for w in _WHEELS):
        wheel_rot_win = {w: record.channels[f"wheel_rot_{w}"][idx] for w in _WHEELS}
    handling_info = handling_read(win_steering, win_lat_g_arr, win_speed, win_brake,
                                  win_throttle, wheel_rot_win)

    # #38 P1.7: gear/RPM sanity from already-windowed channels + the lap's own
    # peak rpm (see `gear_read`).
    gear_ch, rpm_ch = record.channels.get("gear"), record.channels.get("rpm")
    win_gear = gear_ch[idx] if gear_ch is not None else None
    win_rpm = rpm_ch[idx] if rpm_ch is not None else None
    gear_info = gear_read(win_gear, win_rpm, win_throttle, win_speed, min_i, rpm_max_lap)

    # #38 P1.8: input-shape facts (brake release shape, modulation counts)
    # from this corner's own already-windowed channels.
    shape_info = input_shape(win_dist, win_brake, win_throttle, win_steering)

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
        brake_zone_start=brake_zone_start,
        brake_zone_peak=brake_zone_peak,
        peak_lat_g=peak_lat_g,
        peak_brake_g=peak_brake_g,
        peak_accel_g=peak_accel_g,
        peak_steer=peak_steer,
        abs_pct=abs_pct,
        tc_pct=tc_pct,
        track_edge_min=track_edge_min,
        off_surface=off_surface,
        coast_time=coast_time,
        coast_dist=coast_dist,
        brake_to_throttle_dist=brake_to_throttle_dist,
        label=label, apexes=aps, apex_min_speeds=apex_min_speeds,
        handling=handling_info["handling"], handling_why=handling_info["handling_why"],
        lockup=handling_info["lockup"], wheelspin=handling_info["wheelspin"],
        gg_used=gg_used, gg_dead_middle=gg_dead_middle,
        gear_min=gear_info["gear_min"], bog=gear_info["bog"],
        short_shift=gear_info["short_shift"],
        brake_release_m=shape_info["brake_release_m"],
        brake_release_steep_frac=shape_info["brake_release_steep_frac"],
        brake_reversals=shape_info["brake_reversals"],
        throttle_reversals=shape_info["throttle_reversals"],
        steer_reversals=shape_info["steer_reversals"],
    )


def prev_corner_end(cmap: CornerMap, corner: Corner) -> float | None:
    """The end of the corner before `corner` in track order (the last corner's for
    the first -- across S/F), or None on a one-corner map."""
    cs = sorted(cmap.corners, key=lambda c: c.start_dist)
    if len(cs) < 2:
        return None
    return cs[cs.index(corner) - 1].end_dist


def tag_sections(corners, cmap) -> None:
    """Tag each corner with the named section its apex falls in (e.g. "The Corkscrew")."""
    for cm in corners:
        for s in getattr(cmap, "sections", []) or []:
            if s["start_dist"] <= cm.apex_dist <= s["end_dist"]:
                cm.section = s["name"]
                break


def _delta(cur, ref) -> float | None:
    """cur - ref, or None when either side is missing (None) or NaN -- a "not found"
    reading (e.g. no braking) must never be diffed as if it were a real point (#102)."""
    if cur is None or ref is None:
        return None
    if math.isnan(cur) or math.isnan(ref):
        return None
    return cur - ref


def analyze_lap(
    record: LapRecord, cmap: CornerMap, reference: LapRecord | None = None,
    envelope: float | None = None, envelope_laps: int | None = None,
) -> LapAnalysis:
    if reference is not None and reference.track != record.track:
        raise ValueError(f"reference lap track {reference.track!r} != {record.track!r}")

    if envelope is None:
        # ponytail: self-referenced envelope (this lap, plus its reference if any) is
        # optimistic -- it's a single lap standing in for the stint history a real
        # envelope would use. `coach_tools._analysis_for` passes a real one estimated
        # across recent laps; this is just a fallback so a lap still gets a read
        # before that history exists.
        envelope = grip_envelope([record] + ([reference] if reference else []))

    length = cmap.length
    # #38 P1.7: computed once here (not per-corner) -- every corner's gear_read
    # needs the SAME lap-wide peak rpm to judge "below the power band" against.
    rpm_ch = record.channels.get("rpm")
    rpm_max_lap = None
    if rpm_ch is not None:
        peak = float(np.nanmax(rpm_ch))
        rpm_max_lap = peak if math.isfinite(peak) else None
    corners = [_metrics_for_corner(record, corner, length, envelope, rpm_max_lap,
                                   prev_corner_end(cmap, corner))
              for corner in cmap.corners]

    tag_sections(corners, cmap)

    has_reference = reference is not None
    if has_reference:
        ref_metrics = {c.number: _metrics_for_corner(reference, c, length,
                                                     prev_end=prev_corner_end(cmap, c))
                       for c in cmap.corners}
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
            cm.brake_release_m_delta = _delta(cm.brake_release_m, ref.brake_release_m)
            cm.brake_release_steep_frac_delta = _delta(
                cm.brake_release_steep_frac, ref.brake_release_steep_frac)
            cm.brake_reversals_delta = _delta(cm.brake_reversals, ref.brake_reversals)
            cm.throttle_reversals_delta = _delta(cm.throttle_reversals, ref.throttle_reversals)
            cm.steer_reversals_delta = _delta(cm.steer_reversals, ref.steer_reversals)

    tire_means, tire_warmup = _tire_summary(record)
    gg_used, _ = _gg_used_and_dead(
        record.channels.get("lat_g"), record.channels.get("long_g"), envelope)
    gg_phase = _cornering_phase_cg(record.channels.get("lat_g"), record.channels.get("long_g"))
    gg_peak = float(np.percentile(gg_phase, _GG_USED_PCTL)) if gg_phase is not None else None
    return LapAnalysis(
        lap_time=record.lap_time, corners=corners, has_reference=has_reference,
        tire_means=tire_means, tire_warmup=tire_warmup, wheels=_wheel_summary(record),
        coast_total=lap_coast_total(record),
        gg_envelope=envelope, gg_used=gg_used,
        gg_envelope_laps=envelope_laps, gg_peak=gg_peak,
    )


# Below this lap count a stddev is noise dressed up as a finding -- 2 laps always
# gives a "spread" (any two numbers differ), and it tells the driver nothing. Same
# floor `get_consistency` (coach_tools) uses to decide whether to call this at all.
_MIN_CONSISTENCY_LAPS = 3
_CONSISTENCY_FIELDS = ("min_spd", "brake_point", "coast_time")


def stint_consistency(analyses: "list[LapAnalysis]", lap_times: "list[float]") -> dict:
    """Lap-time and per-corner spread across one stint (caller has already scoped
    `analyses`/`lap_times` to one session + phase, valid laps only, in lap order --
    this function doesn't know what a "stint" is, only that the two lists are
    parallel).

    Fewer than `_MIN_CONSISTENCY_LAPS` laps can't support a stddev reading:
    returns ``{"ok": False, "n": n}`` rather than a number computed from noise.

    Otherwise returns:
        ok, n, lap_time_sigma (population stddev), lap_time_best, lap_time_mean,
        corners: {label: {field: {"sigma": float, "n": int}}} for each of
            min_spd / brake_point / coast_time that has at least one real sample
            (label = "T<label> <name>", same rendering as format_corner_line),
        ranking: corner labels ordered highest-variance-first.

    NaN/None per field per lap are skipped, not zeroed -- a lap that never braked
    (brake_point=None) or had no telemetry for a corner (min_spd=nan) contributes
    no sample for that field rather than a fake 0.

    Ranking normalises min-speed sigma by the corner's OWN mean min-speed
    (coefficient of variation), not raw sigma: a slow hairpin's speed swings by a
    few km/h in absolute terms for the same relative inconsistency a fast sweeper
    swings by three times as much, so a flat sigma comparison always crowns the
    fast corner regardless of which one the driver is actually missing. Needs at
    least 2 min_spd samples (a single sample has no spread to measure) and a
    positive mean (guards the division).
    """
    n = len(analyses)
    if n < _MIN_CONSISTENCY_LAPS:
        return {"ok": False, "n": n}
    lap_times = [float(t) for t in lap_times]

    order: list[str] = []
    samples: dict[str, dict[str, list[float]]] = {}
    for a in analyses:
        for cm in a.corners:
            label = corner_tag(cm)
            if label not in samples:
                samples[label] = {f: [] for f in _CONSISTENCY_FIELDS}
                order.append(label)
            bucket = samples[label]
            if cm.min_spd is not None and not math.isnan(cm.min_spd):
                bucket["min_spd"].append(cm.min_spd)
            if cm.brake_point is not None:
                bucket["brake_point"].append(cm.brake_point)
            if cm.coast_time is not None and not math.isnan(cm.coast_time):
                bucket["coast_time"].append(cm.coast_time)

    corners: dict[str, dict[str, dict]] = {}
    ranking: list[tuple[str, float]] = []
    for label in order:
        entry = {}
        for field in _CONSISTENCY_FIELDS:
            vals = samples[label][field]
            if vals:
                entry[field] = {"sigma": float(np.std(vals)), "n": len(vals)}
        if entry:
            corners[label] = entry
        ms = samples[label]["min_spd"]
        if len(ms) >= 2:
            mean_ms = float(np.mean(ms))
            if mean_ms > 0:
                ranking.append((label, float(np.std(ms)) / mean_ms))
    ranking.sort(key=lambda pair: -pair[1])

    return {
        "ok": True,
        "n": n,
        "lap_time_sigma": float(np.std(lap_times)),
        "lap_time_best": min(lap_times),
        "lap_time_mean": float(np.mean(lap_times)),
        "corners": corners,
        "ranking": [label for label, _ in ranking],
    }


# --- tire-deg trend (#38 P1.6) -----------------------------------------------
# Same floor idea as `stint_consistency`: fewer laps than this and a slope is
# noise, not a trend.
_MIN_TIRE_TREND_LAPS = 3
# Laps at the start of a stint that are tire warm-up, not a temperature trend.
_WARMUP_LAPS = 2


def stint_tire_trend(analyses: "list[LapAnalysis]", lap_times: "list[float]",
                     incident: "list[bool] | None" = None) -> dict:
    """Per-wheel tire temp/pressure/wear slope (per lap) and lap-time (pace)
    drift across one stint's laps, in order (caller has already scoped
    `analyses`/`lap_times` exactly the way `stint_consistency` does: one
    session + phase, valid laps only, lap order -- this function doesn't know
    what a "stint" is, only that the two lists are parallel).

    Fewer than `_MIN_TIRE_TREND_LAPS` laps can't support a slope reading:
    returns ``{"ok": False, "n": n}``.

    Otherwise returns:
        ok, n,
        temp_slope / temp_mean: {wheel: value} -- least-squares °C/lap slope
            and mean °C over the stint, one entry per wheel with at least
            `_MIN_TIRE_TREND_LAPS` valid samples (a wheel missing entirely --
            e.g. laps predating the extended capture -- has no key, not a
            zeroed one),
        press_slope: {wheel: kPa/lap}, same gating,
        wear_pct_per_lap: {wheel: % worn per lap} -- POSITIVE means wearing
            (wear is stored as a REMAINING fraction, 1.0 = new, so this is
            `-100 * slope` of that fraction -- see `_wheel_summary`), same
            gating,
        pace_slope_s: s/lap least-squares slope of `lap_times` over the laps
            NOT flagged in `incident` (#161: a spin/off/contact lap is slow for
            a reason that is not the tires), fitted at their stint positions;
            None when fewer than `_MIN_TIRE_TREND_LAPS` incident-free laps,
        pace_laps / incident_laps: how many laps the pace fit used / left out,
        hottest: wheel with the highest mean temp over the stint, or None,
        most_worn: wheel with the lowest final remaining wear, or None.

    A lap missing `wheels`/`tire_means` entirely (older capture) contributes no
    sample to any per-wheel field for that lap -- never a fake zero.
    """
    n = len(analyses)
    if n < _MIN_TIRE_TREND_LAPS:
        return {"ok": False, "n": n}

    def _series(getter):
        out = []
        for i, a in enumerate(analyses):
            v = getter(a)
            if v is not None and not (isinstance(v, float) and math.isnan(v)):
                out.append((i, float(v)))
        return out

    temp_slope, temp_mean, press_slope, wear_pct_per_lap, wear_end = {}, {}, {}, {}, {}
    temp_slope_settled = {}
    for w in _WHEELS:
        txy = _series(lambda a, w=w: (a.tire_means or {}).get(w))
        if len(txy) >= _MIN_TIRE_TREND_LAPS:
            xs, ys = zip(*txy)
            temp_slope[w] = float(np.polyfit(xs, ys, 1)[0])
            temp_mean[w] = float(np.mean(ys))
            # The same slope past the warm-up laps: a tire coming up to temperature
            # early in a stint is not a tire overheating (#161 red-team).
            settled = [(x, y) for x, y in txy if x >= _WARMUP_LAPS]
            if len(settled) >= _MIN_TIRE_TREND_LAPS:
                temp_slope_settled[w] = float(np.polyfit(*zip(*settled), 1)[0])

        pxy = _series(lambda a, w=w: (a.wheels or {}).get("tire_press_mean", {}).get(w))
        if len(pxy) >= _MIN_TIRE_TREND_LAPS:
            xs, ys = zip(*pxy)
            press_slope[w] = float(np.polyfit(xs, ys, 1)[0])

        # A 0.0 reading is a lost or flat wheel, not tread worn off (#161 red-team,
        # Silverstone FR on lap 7130 read "+11.0%/lap"): it is no wear sample.
        wxy = [(i, v) for i, v in
               _series(lambda a, w=w: (a.wheels or {}).get("tire_wear_end", {}).get(w))
               if v > 0]
        # Tread only goes down on one tyre: a rise is a new tyre (a change the pit flag
        # missed), so the series restarts there (#161 red-team, Le Mans "-0.4 %/lap").
        rises = [k for k in range(1, len(wxy)) if wxy[k][1] > wxy[k - 1][1]]
        if rises:
            wxy = wxy[rises[-1]:]
        if len(wxy) >= _MIN_TIRE_TREND_LAPS:
            xs, ys = zip(*wxy)
            wear_pct_per_lap[w] = float(-100.0 * np.polyfit(xs, ys, 1)[0])
            wear_end[w] = ys[-1]

    # Wear/temps keep every lap (the tire wore on the spin lap too); pace does not.
    pace = [(i, float(t)) for i, t in enumerate(lap_times)
            if not (incident and incident[i])]
    pace_slope_s = (float(np.polyfit(*zip(*pace), 1)[0])
                    if len(pace) >= _MIN_TIRE_TREND_LAPS else None)
    hottest = max(temp_mean, key=temp_mean.get) if temp_mean else None
    most_worn = min(wear_end, key=wear_end.get) if wear_end else None

    return {
        "ok": True,
        "n": n,
        "temp_slope": temp_slope,
        "temp_mean": temp_mean,
        "temp_slope_settled": temp_slope_settled,
        "press_slope": press_slope,
        "wear_pct_per_lap": wear_pct_per_lap,
        "pace_slope_s": pace_slope_s,
        "pace_laps": len(pace),
        "incident_laps": n - len(pace),
        "hottest": hottest,
        "most_worn": most_worn,
    }


# --- per-lap incident log (#108 items 3+4) -----------------------------------
# Why this exists: the coach could only ever read the LATEST lap, so "I spun at
# turn 10 a few laps ago" got "nothing flagged", and a flat spot from that spin
# was then blamed on cornering load. These are MEASURED incidents placed on a
# lap and a corner; the model narrates them, it never infers them.
#
# The spin read is from the PATH heading (arctan2 over pos_z/pos_x), not body
# yaw: LMU's struct carries an orientation matrix (`mOri`) but smm.py does not
# sample it, so the direction the car is TRAVELLING is all we hold. A car that
# slides straight ahead while the body swaps ends therefore under-reads.
# ponytail: capture `mOri` and take true yaw -- that is the real upgrade path,
# not more thresholds here.
_SPIN_WINDOW_S = 3.0      # ponytail: longest window a spin may take. Raise it and a
                          # slow tight hairpin starts to qualify; a rig lap through a
                          # known hairpin vs a known spin is what settles it.
_SPIN_DEG = 150.0         # ponytail: path rotation inside that window. Spa's La Source
                          # turns ~170 deg, so the angle ALONE cannot separate the two.
_SPIN_SPEED_FRAC = 0.5    # ponytail: the actual discriminator -- a hairpin holds its
                          # minimum speed through the rotation, a spin collapses below
                          # half the speed it started the window at.
_SPIN_MIN_SPEED_KMH = 5.0  # ponytail: below this the heading off two positions is noise
                           # (a parked or crawling car), so those samples are dropped.
# #207: an off is wheels on a non-road surface, from mSurfaceType (codes per the rF2
# InternalsPlugin.hpp: 0 dry, 1 wet, 2 grass, 3 dirt, 4 gravel, 5 rumblestrip,
# 6 special). Kerbs (5) are road; "special" is unknown, so it is not claimed.
_OFF_SURFACES = (2, 3, 4)
# ponytail: 2+ wheels, not 1 -- one wheel on the grass is a kerb-exit clip that costs
# nothing, two is the car's whole side off the road. 5 samples is 0.1 s at the 50 Hz
# poll, so a one-frame blip at a grass/asphalt seam never counts. Both UNVERIFIED at
# the rig: a lap with a known two-wheel off vs a clean kerb-hopping lap settles them.
_OFF_WHEELS = 2
_OFF_SAMPLES = 5
# ponytail: a flat spot is a STEP, not a slope. Multiple of the stint median, with
# an absolute floor so a stint that barely wears (a cold short run, median ~0)
# doesn't flag every lap. A rig lap with a known flat spot settles both.
_WEAR_STEP_MULT = 2.5
_WEAR_STEP_MIN = 0.003    # fraction of tire life, i.e. 0.3 % in one lap


def _sample_time(record: LapRecord) -> np.ndarray:
    """Per-sample elapsed seconds, full length and aligned to the raw channels --
    unlike `lap_time_axis`, which returns a filtered grid. Prefers a captured time
    channel; falls back to integrating ds/v when none is present (or when the
    captured one steps backwards at the S/F seam, which would make a time window
    meaningless)."""
    ch = record.channels
    t = None
    if "time" in ch:
        t = np.asarray(ch["time"], dtype=float)
    elif "elapsed_time" in ch:
        t = np.asarray(ch["elapsed_time"], dtype=float)
        if "lap_start_et" in ch:
            lset = np.asarray(ch["lap_start_et"], dtype=float)
            t = t[:min(t.size, lset.size)] - lset[:min(t.size, lset.size)]
    if t is not None and t.size and np.all(np.diff(t[np.isfinite(t)]) >= 0):
        return t
    d = _interp_lap_distance(record)
    v = np.asarray(ch["speed"], dtype=float)
    n = min(d.size, v.size)
    dt = np.diff(d[:n]) / (np.maximum(v[:n][1:], _MIN_SPEED_KMH) * _SPEED_KMH_TO_MS)
    return np.concatenate(([0.0], np.cumsum(np.nan_to_num(dt))))


def _corner_at(cmap: CornerMap, dist) -> int | None:
    """The mapped corner number whose window contains `dist`, or None (off the map,
    between corners, or an unknown distance). Same wrap-aware windows `analyze_lap`
    reads corners through."""
    if cmap is None or dist is None or not math.isfinite(float(dist)):
        return None
    for c in cmap.corners:
        if in_window(float(dist), c.start_dist, c.end_dist):
            return c.number
    return None


def _spin_events(record: LapRecord, cmap: CornerMap) -> list[dict]:
    """Spins, read off the driven path. Empty when the lap has no position
    channels (older captures) or too few moving samples."""
    ch = record.channels
    if "pos_x" not in ch or "pos_z" not in ch:
        return []
    x = np.asarray(ch["pos_x"], dtype=float)
    z = np.asarray(ch["pos_z"], dtype=float)
    v = np.asarray(ch["speed"], dtype=float)
    d = _interp_lap_distance(record)
    t = _sample_time(record)
    n = min(x.size, z.size, v.size, d.size, t.size)
    x, z, v, d, t = x[:n], z[:n], v[:n], d[:n], t[:n]
    keep = (np.isfinite(x) & np.isfinite(z) & np.isfinite(v) & np.isfinite(t)
            & (v > _SPIN_MIN_SPEED_KMH))
    x, z, v, d, t = x[keep], z[keep], v[keep], d[keep], t[keep]
    if x.size < 3:
        return []
    # A repeated position gives arctan2(0, 0) == 0 and would inject a fake ±180°
    # swing into the unwrapped heading. Drop the duplicate, keep the first.
    moved = np.concatenate(([True], (np.diff(x) ** 2 + np.diff(z) ** 2) > 0.0))
    x, z, v, d, t = x[moved], z[moved], v[moved], d[moved], t[moved]
    if x.size < 3:
        return []
    # h[k] = heading of the segment LEAVING sample k, unwrapped so a window's
    # rotation is a plain subtraction rather than a modulo dance.
    h = np.degrees(np.unwrap(np.arctan2(np.diff(z), np.diff(x))))
    m = h.size

    # ponytail: O(samples x window) scan -- ~150 samples per window at 50 Hz, a few
    # ms a lap. A prefix-min/running-extreme structure would make it linear; it is
    # not worth the code until a stint scan is measurably slow.
    spans: list[list[int]] = []
    for i in range(m):
        j = int(np.searchsorted(t, t[i] + _SPIN_WINDOW_S, side="right")) - 1
        if j <= i:
            continue
        k = min(j, m - 1)
        if np.abs(h[i:k + 1] - h[i]).max() <= _SPIN_DEG:
            continue
        if v[i:j + 1].min() >= _SPIN_SPEED_FRAC * v[i]:
            continue
        # The excursion is the WHOLE window, not the first sample that tripped it:
        # the rotation and the speed minimum usually land after that point, and the
        # merge below then reports one spin rather than a run of partial ones.
        if spans and i <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], j)
        else:
            spans.append([i, j])

    out = []
    for lo, hi in spans:
        k = min(hi, m - 1)
        mi = lo + int(np.argmin(v[lo:hi + 1]))
        out.append({
            "kind": "spin",
            "corner_no": _corner_at(cmap, d[mi]),
            "lap_distance": float(d[mi]),
            "detail": {"speed_from_kmh": float(v[lo]),
                       "speed_min_kmh": float(v[mi]),
                       "turned_deg": float(np.abs(h[lo:k + 1] - h[lo]).max())},
        })
    return out


# #210: a stop -- under _STOP_KMH for _STOP_S or more (Long Beach race lap 4: 132 -> 0
# on the brake at 3046 m, 7.7 s, no rotation, no hit). ponytail: flat knobs; a rig
# stop that goes unreported, or a slow hairpin that reports, is what moves them.
_STOP_KMH = 10.0
_STOP_S = 1.5
# A spin or hit this close to the stop is what stopped the car.
_STOP_CAUSE_M = 100.0


def _stop_events(record: LapRecord, cmap: CornerMap, spins: list[dict]) -> list[dict]:
    """Stops on track. Complete, pit-free laps only: a pit box (out/in laps), race lap
    0's grid and a post-flag cool-down are all partial laps -- and never a stop the lap
    began in. Timed off the game clock: `_sample_time` falls back to ds/v at LMU's S/F
    seam, where a car standing still takes no time. A stop within _STOP_CAUSE_M of a
    spin is folded into that spin (its `stopped_s`), not a second incident; one near
    an impact -- a separate contact line -- is annotated (`after_contact`)."""
    ch = record.channels
    et = ch.get("elapsed_time", ch.get("time"))
    if record.partial is not None or record.pit or et is None:
        return []
    v = np.asarray(ch["speed"], dtype=float)
    d = _interp_lap_distance(record)
    et = np.asarray(et, dtype=float)
    n = min(v.size, d.size, et.size)
    slow = np.r_[0, (v[:n] < _STOP_KMH).astype(int), 0]
    edges = np.diff(slow)
    hits = [e["lap_distance"] for e in damage_events(ch)]
    out = []
    for lo, hi in zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1) - 1):
        stopped = float(et[hi] - et[lo])
        if lo == 0 or not stopped >= _STOP_S:
            continue
        k = lo            # back up the braking to where it began: the speed it stopped from
        while k > 0 and v[k - 1] >= v[k]:
            k -= 1
        spin = next((s for s in spins if abs(s["lap_distance"] - d[lo]) <= _STOP_CAUSE_M),
                    None)
        if spin is not None:
            spin["detail"]["stopped_s"] = stopped
            continue
        out.append({
            "kind": "stop",
            "corner_no": _corner_at(cmap, d[lo]),
            "lap_distance": float(d[lo]),
            "detail": {"speed_from_kmh": float(v[k]),
                       "speed_min_kmh": float(np.nanmin(v[lo:hi + 1])),
                       "stopped_s": stopped,
                       "after_contact": any(abs(x - d[lo]) <= _STOP_CAUSE_M for x in hits)},
        })
    return out


def _wear_step_events(record: LapRecord, cmap: CornerMap, baseline: dict) -> list[dict]:
    """Per-wheel wear STEPS: this lap's wear against the stint's own median. A step
    on one lap at one corner is a flat spot; a slope across the stint is
    `stint_tire_trend`'s job, not this one."""
    if not baseline:
        return []
    ch = record.channels
    d = _interp_lap_distance(record)
    out = []
    for w in _WHEELS:
        key = f"tire_wear_{w}"
        if key not in ch or w not in baseline:
            continue
        wear = np.asarray(ch[key], dtype=float)
        if wear.size < 2:
            continue
        lap_wear = float(wear[0] - wear[-1])   # remaining fraction, so start - end
        if not math.isfinite(lap_wear):
            continue
        if lap_wear <= max(_WEAR_STEP_MULT * baseline[w], _WEAR_STEP_MIN):
            continue
        drops = -np.diff(wear[:min(wear.size, d.size)])
        if not drops.size or not np.isfinite(drops).any():
            continue
        i = int(np.nanargmax(drops)) + 1
        out.append({
            "kind": "wear_step",
            "corner_no": _corner_at(cmap, d[i]),
            "lap_distance": float(d[i]),
            "detail": {"wheel": w, "step_pct": lap_wear * 100.0,
                       "baseline_pct": baseline[w] * 100.0},
        })
    return out


def lap_events(record: LapRecord, cmap: CornerMap, analysis: "LapAnalysis | None",
               wear_baseline: dict | None = None) -> list[dict]:
    """Everything that went wrong on one lap, in along-lap order:
    `{"kind", "corner_no", "lap_distance", "detail"}` per incident.

    kinds: "spin" (path rotation with a speed collapse), "stop" (#210: stopped on
    track, `_stop_events`), "off" (wheels on
    grass/dirt/gravel -- `CornerMetrics.off_surface`; never from track_edge, so a
    lap captured without surface channels has no offs), "lockup"/"wheelspin"
    (pass-through of the per-corner reads), "wear_step" (a flat spot). `wear_baseline` comes from `stint_wear_baseline`
    over the stint's laps; without one the wear kind is skipped entirely rather
    than guessed against a single lap."""
    events = _spin_events(record, cmap)
    events += _stop_events(record, cmap, events)
    for cm in (analysis.corners if analysis is not None else []):
        if cm.lockup:
            events.append({"kind": "lockup", "corner_no": cm.corner_no,
                           "lap_distance": cm.apex_dist, "detail": {}})
        if cm.wheelspin:
            events.append({"kind": "wheelspin", "corner_no": cm.corner_no,
                           "lap_distance": cm.apex_dist, "detail": {}})
        if cm.off_surface:
            events.append({"kind": "off", "corner_no": cm.corner_no,
                           "lap_distance": cm.apex_dist, "detail": {}})
    events.extend(_wear_step_events(record, cmap, wear_baseline or {}))
    # Stable, so incidents sharing an apex distance keep their insertion order.
    events.sort(key=lambda e: (e["lap_distance"] if math.isfinite(e["lap_distance"])
                               else math.inf))
    return events


def is_incident_lap(record: LapRecord, cmap: CornerMap,
                    analysis: "LapAnalysis | None") -> bool:
    """#161: a lap slowed by a spin, a stop, an off or contact -- the same detection
    get_stint_events reports -- so its lap time says nothing about the tires.
    Lockups/wheelspin are corner-level and don't cost a lap on their own."""
    return bool(damage_events(record.channels)) or any(
        e["kind"] in ("spin", "stop", "off") for e in lap_events(record, cmap, analysis))


# #206: the ONE reader of mLastImpactPos's frame -- every consumer (damage_events,
# livelap's car status) labels a hit through `_damage_label`, never its own signs.
# mLastImpactPos is in the rF2 local vehicle frame, the same frame the verified
# accel channels are read in (mLocalAccel): +x = LEFT (lat_g > 0 in a left turn,
# ribbon.multi_signal_direction), +z = REAR (long_g > 0 under braking,
# raceline._calibrate). So x < 0 is the right side and z < 0 the front.
# #143 shipped both axes inverted, from one Imola sample (x -1.02, z +1.75) read
# as front-left; in this frame it reads rear-right, so that hit was misattributed.
# Long Beach lap 19 (2026-09-27, a front-right wall hit on the game's own damage
# display) logged x -1.08 z -1.83 and x -1.04 z +1.58: front-right + rear-right.
# A dead zone near x=0 keeps a centred hit reading "front" instead of a coin-flip
# "front-right", and one near z=0 keeps a hit between the axles reading "right side"
# instead of a coin-flip end (#206 red team: Long Beach 7745, mag 4541 at z +0.22).
# Neither is measured -- guessed geometric widths, so kept as knobs.
_DAMAGE_DEAD_ZONE_M = 0.15
_DAMAGE_DEAD_ZONE_Z_M = 0.5


def _damage_label(x: float, z: float) -> str:
    end = "front" if z <= 0 else "rear"
    if abs(x) < _DAMAGE_DEAD_ZONE_M:
        return end
    side = "right" if x < 0 else "left"
    if abs(z) < _DAMAGE_DEAD_ZONE_Z_M:
        return f"{side} side"
    return f"{end}-{side}"


def _is_impact(mag: float, x: float, z: float) -> bool:
    """#206: a new mLastImpactET with magnitude 0 at exactly (0, 0) is no hit -- it
    printed as a "front" impact and inflated the count (Long Beach 7744, 7562)."""
    return mag > 0 and not (x == 0 and z == 0)


def damage_events(channels: dict) -> list[dict]:
    """One entry per impact this lap: `{"et", "magnitude", "lap_distance", "label"}`.

    An impact is a RISING EDGE of last_impact_et, not a magnitude threshold -- the
    field holds the LAST impact indefinitely, so watching magnitude alone would
    re-report the same hit on every sample. [] for a lap with no damage channels
    (captured before #143) or with no impacts -- never raised, never fabricated."""
    et = channels.get("last_impact_et")
    if et is None:
        return []
    et = np.asarray(et, dtype=float)
    mag = np.asarray(channels.get("last_impact_mag", []), dtype=float)
    x = np.asarray(channels.get("last_impact_x", []), dtype=float)
    z = np.asarray(channels.get("last_impact_z", []), dtype=float)
    dist = np.asarray(channels.get("lap_distance", []), dtype=float)
    n = min(et.size, mag.size, x.size, z.size, dist.size)
    events = []
    prev = et[0] if n else math.nan
    for i in range(1, n):
        if (math.isfinite(et[i]) and (not math.isfinite(prev) or et[i] > prev)
                and _is_impact(float(mag[i]), float(x[i]), float(z[i]))):
            events.append({
                "et": float(et[i]),
                "magnitude": float(mag[i]),
                "lap_distance": float(dist[i]),
                "label": _damage_label(float(x[i]), float(z[i])),
            })
        prev = et[i]
    return events


def damage_state(channels: dict) -> dict:
    """End-of-lap damage snapshot: dent_max/dent_total (raw 0-255 severity,
    UNVERIFIED which panel), detached/overheating, and which wheels read flat or
    detached (also UNVERIFIED -- nothing broke on the rig hit that confirmed the
    other fields). {} for a lap with no damage channels -- never fabricated as
    clean."""
    dent_max = channels.get("dent_max")
    if dent_max is None or not len(dent_max):
        return {}
    return {
        "dent_max": float(dent_max[-1]),
        "dent_total": float(channels["dent_total"][-1]),
        "detached": bool(channels["detached"][-1]),
        "overheating": bool(channels["overheating"][-1]),
        "wheels_flat": [w for w in _WHEELS if bool(channels.get(f"flat_{w}", [0])[-1])],
        "wheels_detached": [w for w in _WHEELS
                            if bool(channels.get(f"wheel_detached_{w}", [0])[-1])],
    }


def stint_wear_baseline(records: "list[LapRecord]") -> dict[str, float]:
    """{wheel: median per-lap wear (fraction)} over the laps that carry the channel.

    {} below two laps: one lap cannot be its own baseline, and a "step" measured
    against itself is not a finding. The median (not the mean) is what lets the
    flat-spot lap stay in the population without inflating the bar it has to clear.
    """
    if len(records) < 2:
        return {}
    out = {}
    for w in _WHEELS:
        vals = []
        for rec in records:
            arr = rec.channels.get(f"tire_wear_{w}")
            if arr is None:
                continue
            arr = np.asarray(arr, dtype=float)
            if arr.size >= 2 and math.isfinite(arr[0] - arr[-1]):
                vals.append(float(arr[0] - arr[-1]))
        if vals:
            out[w] = float(np.median(vals))
    return out


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

# Which of those `corner_geometry` reads at the APEX alone (`at(apex_dist)`), so they
# still describe a corner whose window moved but whose apex did not. Every other metric
# reads the WINDOW -- radius_m is the tightest point inside it, elev_delta_m its end minus
# its start -- and is stale the moment the window changes (#196: Laguna's merged T2 kept
# old T1's -5.1 m for a window it no longer had). Listed positively so a metric added
# above and not classified here is treated as window-read: dropped, never carried stale.
APEX_GEOMETRY_KEYS = frozenset({"width_m", "banking_deg"})

# Which of those are read from telemetry keyed on LAP DISTANCE rather than off the stored
# path's shape, so they still describe a corner whose path was replaced (#198: the lap
# route re-averages it). width_m/banking_deg come from a ribbon or the capture cloud's
# reconstruction, elev_delta_m from binned pos_y -- each on its own lap-distance grid,
# interpolated onto path_dist -- while radius_m is the path's own curvature. Listed
# positively, like APEX_GEOMETRY_KEYS: an unclassified metric is dropped on a path change.
LAP_DISTANCE_GEOMETRY_KEYS = frozenset({"width_m", "banking_deg", "elev_delta_m"})


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
        return list(range(i, j + 1)) if i <= j else list(range(i, n)) + list(range(j + 1))

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
_INPUT_CHANNELS = ("throttle", "brake", "steering", "speed")


def input_traces(record: LapRecord, bins: int = 500) -> dict:
    """Throttle/brake/steering/speed resampled onto a uniform lap_distance grid.

    Pure and deterministic; UI-facing only (never fed to the model). Returns
    ``{"length", "dist", "throttle", "brake", "steering", "speed"}`` where `dist`
    is the `bins` bin centres and each channel is
    ``{"lo": [...], "hi": [...], "mean": [...]}`` — the min/max/mean of the
    samples in each bin (speed in km/h). Returns ``{}`` when the lap lacks a
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
    counts = np.bincount(idx, minlength=bins)
    empty = counts == 0

    out: dict = {"length": round(float(dist[-1]), 1),
                 "dist": [round(float(v), 1) for v in centres]}
    for k in _INPUT_CHANNELS:
        v = arrays[k][keep]
        lo = np.full(bins, np.inf)
        hi = np.full(bins, -np.inf)
        np.minimum.at(lo, idx, v)
        np.maximum.at(hi, idx, v)
        mean = np.divide(np.bincount(idx, weights=v, minlength=bins), counts,
                         out=np.zeros(bins), where=~empty)
        if empty.any():
            # Bins finer than the sample spacing hold nothing; interpolate them so
            # the chart has no holes.
            pt = np.interp(centres, dist, v)
            lo = np.where(empty, pt, lo)
            hi = np.where(empty, pt, hi)
            mean = np.where(empty, pt, mean)
        out[k] = {"lo": [round(float(x), 3) for x in lo],
                  "hi": [round(float(x), 3) for x in hi],
                  "mean": [round(float(x), 3) for x in mean]}
    return out
