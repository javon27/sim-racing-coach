"""Reconstruct the true track ribbon (left/right edges + centerline + width)
from two slow boundary laps, plus screen-normalization for the editor preview.
Pure functions -- no I/O, no LLM. Our own asset, derived from the driver's own
telemetry (no game imagery).

The two boundary laps differ in length (the inside line is shorter), so they are
paired by NORMALIZED track progress t in [0,1] -- both anchored at lap_distance 0
(the shared S/F line) -- not by raw lap_distance.
"""
from __future__ import annotations

import numpy as np

from simcoach.corners import RIGHT_IS_POSITIVE
from simcoach.trackmap import direction_from_path, rotate_xy


def _resample_progress(channels: dict, grid: np.ndarray):
    """(x, z, y) resampled onto a shared progress grid t in [0,1], or None if the
    lap lacks usable position data. y is None when pos_y (elevation) is absent."""
    if "pos_x" not in channels or "pos_z" not in channels:
        return None
    x = np.asarray(channels["pos_x"], dtype=float)
    z = np.asarray(channels["pos_z"], dtype=float)
    d = np.asarray(channels.get("lap_distance", np.arange(len(x))), dtype=float)
    has_y = "pos_y" in channels
    y = np.asarray(channels["pos_y"], dtype=float) if has_y else None
    n = min(len(x), len(z), len(d), len(y) if has_y else len(x))
    if n < 2:
        return None
    x, z, d = x[:n], z[:n], d[:n]
    mask = np.isfinite(x) & np.isfinite(z) & np.isfinite(d)
    if has_y:
        y = y[:n]
        mask &= np.isfinite(y)
    if mask.sum() < 2:
        return None
    x, z, d = x[mask], z[mask], d[mask]
    if has_y:
        y = y[mask]
    order = np.argsort(d, kind="stable")
    d, x, z = d[order], x[order], z[order]
    if has_y:
        y = y[order]
    d, uniq = np.unique(d, return_index=True)  # np.interp needs strictly-increasing xp
    x, z = x[uniq], z[uniq]
    if has_y:
        y = y[uniq]
    if len(d) < 2 or d[-1] <= d[0]:
        return None
    t = (d - d[0]) / (d[-1] - d[0])
    ry = np.interp(grid, t, y) if has_y else None
    return np.interp(grid, t, x), np.interp(grid, t, z), ry


def reconstruct_ribbon(left_channels: dict, right_channels: dict, bins: int = 500) -> dict:
    """True track geometry from a left-edge lap and a right-edge lap. Returns
    world coords {left, right, center, width, dist} (dist = the centerline's
    cumulative arc length, i.e. the lap-distance axis), or {} if either edge
    lacks position telemetry."""
    grid = np.linspace(0.0, 1.0, bins)
    left = _resample_progress(left_channels, grid)
    right = _resample_progress(right_channels, grid)
    if left is None or right is None:
        return {}
    lx, lz, ly = left
    rx, rz, ry = right
    # Pair each left station with its NEAREST right point (a true cross-section),
    # not the equal-progress point -- equal progress cuts a diagonal at asymmetric
    # corners, inflating width and skewing bank/the solver corridor.
    # ponytail: O(bins^2) broadcast (~fine at 500); can pick a wrong point where a
    # track nearly touches itself -- acceptable for v0.1 layouts.
    j = np.argmin((lx[:, None] - rx[None, :]) ** 2 + (lz[:, None] - rz[None, :]) ** 2, axis=1)
    rx, rz = rx[j], rz[j]
    ry = ry[j] if ry is not None else None
    cx, cz = (lx + rx) / 2.0, (lz + rz) / 2.0
    width = np.hypot(lx - rx, lz - rz)
    seg = np.hypot(np.diff(cx), np.diff(cz))
    dist = np.concatenate([[0.0], np.cumsum(seg)])
    # Bank from elevation. Convention MATCHES reconstruct_surface: positive =
    # banked for a right-hander, negative = banked for a left-hander
    # (rig-validated, 9756b65). reconstruct_surface gets that by fitting y against
    # the +lateral offset and +lateral is the driver's LEFT (#58, see _lateral), so
    # here it is the LEFT edge being higher that reads positive. This subtracted the
    # other way round while claiming the two matched -- they disagreed by a sign.
    # Nothing in the app calls reconstruct_ribbon (capture goes through
    # reconstruct_surface), so no shipped number moves; it was a trap waiting for
    # whoever revived the edge-lap capture path. atan2 handles width->0; no pos_y
    # -> flat.
    if ly is not None and ry is not None:
        bank = np.arctan2(ly - ry, np.maximum(width, 1e-9))
    else:
        bank = np.zeros(len(grid))

    def _pairs(xs, zs):
        return [[round(float(a), 2), round(float(b), 2)] for a, b in zip(xs, zs)]

    return {
        "left": _pairs(lx, lz),
        "right": _pairs(rx, rz),
        "center": _pairs(cx, cz),
        "width": [round(float(w), 2) for w in width],
        "bank": [round(float(b), 4) for b in bank],
        "dist": [round(float(d), 2) for d in dist],
    }


def _fill_nan(a: np.ndarray, period: float | None = None) -> np.ndarray:
    """Interpolate NaN gaps (sparse/empty bins) from finite neighbours. With
    `period` the fill wraps closed-loop, so a bad S/F-seam bin (index 0 / last)
    is stitched from bins on the OTHER side of the start/finish line."""
    idx = np.arange(len(a))
    good = np.isfinite(a)
    if good.sum() == 0:
        return a
    a = a.copy()
    a[~good] = np.interp(idx[~good], idx[good], a[good], period=period)
    return a


_SMOOTH_W = 5  # legacy half-window (bins), now only _bank_per_bin's never-exercised
#              # default param -- reconstruct_surface derives a DISTANCE-based window
#              # (_SMOOTH_HALF_M) for EVERY _smooth call incl. the bootstrap and bank.
_SMOOTH_HALF_M = 40.0  # smoothing half-window in METRES. ~9 m squiggle << 40 m <<
#                      # a real corner, and (unlike a fixed bin count) it doesn't
#                      # balloon on a long track: the old 5-bin window was ~127 m on
#                      # Monza, flattening the Rettifilo chicane into a gentle S.


def _smooth(a: np.ndarray, w: int) -> np.ndarray:
    """Closed-loop (wrap) boxcar moving average, half-window w. Preserves length.

    # ponytail: boxcar; a Savitzky-Golay local-quadratic fit preserves corner
    # curvature slightly better -- upgrade if apexes read washed out.
    """
    if w < 1 or len(a) <= 2 * w:
        return a
    k = np.ones(2 * w + 1) / (2 * w + 1)
    return np.convolve(np.concatenate([a[-w:], a, a[:w]]), k, mode="valid")


def _menger(x: np.ndarray, z: np.ndarray) -> np.ndarray:
    """Per-station discrete (Menger) curvature of a closed 2D polyline, 1/m."""
    n = len(x)
    xp = np.concatenate([x[-1:], x, x[:1]])
    zp = np.concatenate([z[-1:], z, z[:1]])
    k = np.zeros(n)
    for i in range(n):
        ax, az, bx, bz, cx, cz = xp[i], zp[i], xp[i + 1], zp[i + 1], xp[i + 2], zp[i + 2]
        area = abs((bx - ax) * (cz - az) - (cx - ax) * (bz - az))
        den = np.hypot(bx - ax, bz - az) * np.hypot(cx - bx, cz - bz) * np.hypot(cx - ax, cz - az)
        k[i] = 2 * area / den if den > 1e-9 else 0.0
    return k


def _smooth_curve(x: np.ndarray, z: np.ndarray, base_w: int, pct: float = 88.0) -> tuple[np.ndarray, np.ndarray]:
    """Curvature-ADAPTIVE closed-loop smoothing of a 2D polyline: a wide window on
    straights (kills the ~9 m capture squiggle) that narrows through corners (so a
    tight infield ess isn't flattened, the way a fixed window did). The per-station
    window scales from base_w on straights down to 1 at the sharpest bends, keyed to
    curvature on a lightly pre-smoothed copy so the window choice isn't itself noisy."""
    n = len(x)
    if base_w < 2 or n <= 2 * base_w:
        return x, z
    pre = max(1, base_w // 3)
    kap = _menger(_smooth(x, pre), _smooth(z, pre))
    ref = float(np.percentile(kap, pct)) or 1.0
    alpha = np.clip(kap / ref, 0.0, 1.0)                       # 1 at corners, 0 on straights
    w = np.clip(np.round(base_w - (base_w - 1) * alpha).astype(int), 1, base_w)
    xp = np.concatenate([x[-base_w:], x, x[:base_w]])
    zp = np.concatenate([z[-base_w:], z, z[:base_w]])
    sx, sz = np.empty(n), np.empty(n)
    for i in range(n):
        wi = int(w[i]); c = base_w + i
        sx[i] = xp[c - wi:c + wi + 1].mean()
        sz[i] = zp[c - wi:c + wi + 1].mean()
    return sx, sz


def _lateral(cx: np.ndarray, cz: np.ndarray):
    """Unit lateral (perpendicular to the centered-diff tangent, rot +90), closed
    loop. **+lateral is the driver's LEFT side of travel** -- an empirical
    (x, z)-handedness fact, not derived from RIGHT_IS_POSITIVE. Settled in #58 off
    the shipped maps: every corner with >=1 deg of baked camber reads positive on
    right-handers and negative on left-handers, and since those corners really are
    cambered (outer edge higher), the higher side of a cambered right-hander is the
    driver's LEFT -- the side this calls positive. The opposite reading would make
    Parabolica, both Lesmos, Rainey and Tosa all off-camber. Agrees with both the
    rig-validated banking sign (9756b65) and trackmap's direction convention.
    Pinned from both sides by test_lateral_points_left_of_travel_not_right and
    test_left_key_is_the_inside_of_a_cambered_left_hander, the latter tying this
    handedness to that banking sign so neither can move alone."""
    tx, tz = np.roll(cx, -1) - np.roll(cx, 1), np.roll(cz, -1) - np.roll(cz, 1)
    tn = np.hypot(tx, tz); tn[tn == 0] = 1.0
    return -tz / tn, tx / tn


def _lap_grid(x, z, d, max_len, bins):
    """Resample one lap's (x, z) onto the shared lap_distance grid (per-bin mean),
    empty bins filled closed-loop."""
    bi = np.clip(np.round(d / max_len * (bins - 1)).astype(int), 0, bins - 1)
    ex = np.array([x[bi == b].mean() if np.any(bi == b) else np.nan for b in range(bins)])
    ez = np.array([z[bi == b].mean() if np.any(bi == b) else np.nan for b in range(bins)])
    return _fill_nan(ex, period=bins), _fill_nan(ez, period=bins)


# The lateral range (metres) a bin's samples must span before WIDTH or BANK is
# estimated there; below it both are written as unknown (None). Laps on one racing
# line never supply that range at any lap count, and a fit across ~1 m of them is
# noise: on real Silverstone National clouds the old >1e-6 m test saved max bank 48 deg
# off 1 elevation lap and 21 deg off 3, and a 4-lap build saved a median width of
# 1.4 m -- both reaching the coach as owned fact via corner_geometry (#69).
# Measured share of bins reaching 6 m: first 1-4 racing laps of Silverstone National/
# Laguna/Algarve 0-6%; mapping captures (Laguna, Algarve, Monza, Bahrain, Le Mans)
# 97-100%; Spa edge laps 100%. Gated on the spread itself, not on where the laps came
# from.
_MIN_SPREAD_M = 6.0

# ...and that spread has to be SUSTAINED along the track: a bin counts only inside a
# closed-loop run of such bins at least this long. Per bin, peak-to-peak lets ONE lap
# running wide lend its bins a spread: on 17 real Fuji racing laps (no edge laps) it let
# 26% of bins through and baked "width 26m, 2 deg banked" into two corners, off a single
# qualifying lap 24.8 m off the pack.
#   There is no clean gap, so this is a trade, set on the ASYMMETRY: a fabricated width
# or camber reaches the coach as owned fact, an unknown one just goes unsaid. Measured
# (% of bins with width known, at 500 m -> 1000 m):
#   - racing-line clouds: Fuji 17, Laguna 45, Algarve 19 and every first-4 are 0 at both
#     (longest local run 236 m). Silverstone National 17 is 24 -> 0: its one 632 m run
#     straddles start/finish with a 30 s-slow race lap sitting ~16 m off the pack and
#     reads 18-23 m "wide" -- consistent with the pit lane, not the track.
#   - spanning clouds: Spa 1 left + 1 right edge lap, Laguna, Imola, Bahrain Endurance,
#     Le Mans, Monza, Algarve captures 93-100 at both; but Bahrain International 84 -> 74,
#     Interlagos 79 -> 45, Bahrain Outer 55 -> 30, Monza Curva Grande 66 -> 21, Bahrain
#     Paddock 47 -> 0. A rebuild or re-bake from THOSE captures says less than it could;
#     the maps already shipped keep their baked values.
# ponytail: a length cannot reject a pit lane by design -- Silverstone's is only shorter
# than this. Excluding in/out laps from a rebuild is the real fix for pit laps.
_MIN_SPREAD_RUN_M = 1000.0


def _sustained(mask: np.ndarray, min_bins: int) -> np.ndarray:
    """`mask` with every True run shorter than `min_bins` cleared. Closed loop: a run
    through the start/finish line is one run. An all-True mask is a whole lap and
    always kept, even on a lap shorter than `min_bins`."""
    if mask.all() or not mask.any():
        return mask.copy()
    start = int(np.argmin(mask))           # a False bin: rotate it first so no run wraps
    m = np.roll(mask, -start)
    out = np.zeros(len(m), dtype=bool)
    i = 0
    while i < len(m):
        if not m[i]:
            i += 1
            continue
        j = i
        while j < len(m) and m[j]:
            j += 1
        out[i:j] = j - i >= min_bins
        i = j
    return np.roll(out, start)


def _bank_per_bin(X, Z, Y, members, ctr_x, ctr_z, lat_x, lat_z, bins, w=_SMOOTH_W):
    """Cross-slope angle per bin from ALL samples: fit pos_y vs lateral offset.
    Positive = the +lateral side is higher; +lateral is the driver's LEFT (#58),
    so positive = banked for a right-hander and negative = banked for a
    left-hander (rig-validated, 9756b65; matches reconstruct_ribbon).
    NaN (unknown) where the elevation samples span < _MIN_SPREAD_M laterally."""
    bnk = np.full(bins, np.nan)
    for b in range(bins):
        mm = members[b]
        if mm.size == 0:
            continue
        off = (X[mm] - ctr_x[b]) * lat_x[b] + (Z[mm] - ctr_z[b]) * lat_z[b]
        yb, fin = Y[mm], np.isfinite(Y[mm])
        if fin.sum() >= 2 and np.ptp(off[fin]) >= _MIN_SPREAD_M:
            bnk[b] = np.arctan(np.polyfit(off[fin], yb[fin], 1)[0])
    known = np.isfinite(bnk)
    out = _smooth(_fill_nan(bnk, period=bins), w)
    out[~known] = np.nan    # smoothing may borrow from neighbours, never invent a bin
    return out


def reconstruct_surface(laps: list[dict], bins: int = 500) -> dict:
    """Reconstruct the track from free-driving passes. The two BOUND laps build the
    EDGES -- most-POSITIVE mean lateral offset = the driver's LEFT, most-negative =
    the RIGHT, since +lateral is the left side of travel (#58) -- while in-between
    weaving laps only contribute elevation across the width. Returns
    {left, right, center, width, bank, dist} (world), or {} with no usable data.

    Falls back to the lateral-envelope of all samples when laps can't be cleanly
    classified (all similar mean offset). bank fits pos_y vs offset over ALL
    samples (that's what the weave is for).

    # ponytail: nearest-bin assignment + O(bins*N) loops -- naive, fine for a few
    # passes. bank sign rig-validated on Laguna's Corkscrew (T8 left -> negative,
    # T9 right -> positive): negative = left, positive = right.
    """
    xs, zs, ys, ds, slices = [], [], [], [], []
    start = 0
    for ch in laps:
        if not all(k in ch for k in ("pos_x", "pos_z", "lap_distance")):
            continue
        x = np.asarray(ch["pos_x"], dtype=float)
        z = np.asarray(ch["pos_z"], dtype=float)
        d = np.asarray(ch["lap_distance"], dtype=float)
        y = np.asarray(ch["pos_y"], dtype=float) if "pos_y" in ch else np.full(len(x), np.nan)
        n = min(len(x), len(z), len(d), len(y))
        m = np.isfinite(x[:n]) & np.isfinite(z[:n]) & np.isfinite(d[:n])
        if m.sum() < 1:
            continue
        cnt = int(m.sum())
        xs.append(x[:n][m]); zs.append(z[:n][m]); ys.append(y[:n][m]); ds.append(d[:n][m])
        slices.append((start, start + cnt)); start += cnt
    if not xs:
        return {}
    X, Z, Y, D = np.concatenate(xs), np.concatenate(zs), np.concatenate(ys), np.concatenate(ds)
    max_len = float(D.max())
    if len(X) < 2 or max_len <= 0:
        return {}

    dist_grid = np.linspace(0.0, max_len, bins)
    bi = np.clip(np.round(D / max_len * (bins - 1)).astype(int), 0, bins - 1)
    members = [np.where(bi == b)[0] for b in range(bins)]

    # Distance-based smoothing half-window (bins), so it's ~_SMOOTH_HALF_M metres on
    # EVERY track -- a fixed bin count over-smoothed long tracks (chicanes flattened).
    sw = max(1, round(_SMOOTH_HALF_M * bins / max_len))
    # Light structural smoothing for the reconstruction; corner-preserving detail on
    # the rendered edges/center comes from the adaptive final pass (_smooth_curve),
    # so a fixed wide window no longer flattens tight esses.
    swl = max(1, sw // 3)

    # Bootstrap centerline (mean of all), smooth, lateral from it.
    cx = np.array([X[mm].mean() if mm.size else np.nan for mm in members])
    cz = np.array([Z[mm].mean() if mm.size else np.nan for mm in members])
    cx, cz = _smooth(_fill_nan(cx), sw), _smooth(_fill_nan(cz), sw)
    lat_x, lat_z = _lateral(cx, cz)

    # Classify laps by mean lateral offset. +lateral is the driver's LEFT (see
    # _lateral), so most-POSITIVE = LEFT and most-negative = RIGHT; the rest weave
    # (elevation only). Edges from the bound laps directly. #58 relabelled these --
    # they shipped inverted, harmlessly (width is symmetric, both edges are drawn),
    # but any per-edge feature would have inherited a mirrored answer. Labels only:
    # width, center, bank and dist are bit-identical either way, so the
    # rig-validated banking sign of 9756b65 is untouched.
    #   Residual: the 10 shipped maps that store a `ribbon` (every one except
    #   test_track and Laguna) still hold left/right under the OLD labels --
    #   measured mirrored, all 10 the same way, no mixed state, so a rebuild is one
    #   uniform swap. Left alone because no consumer reads those arrays per-edge
    #   (both are drawn; solve_corners is order-invariant). A per-edge feature must
    #   rebuild them first.
    off_all = (X - cx[bi]) * lat_x[bi] + (Z - cz[bi]) * lat_z[bi]
    lap_means = np.array([off_all[a:b].mean() for a, b in slices])
    total_spread = float(np.ptp(off_all))
    if len(slices) >= 2 and total_spread > 0 and np.ptp(lap_means) > 0.2 * total_spread:
        li, ri = int(lap_means.argmax()), int(lap_means.argmin())
        a, b = slices[li]; lx, lz = _lap_grid(X[a:b], Z[a:b], D[a:b], max_len, bins)
        a, b = slices[ri]; rx, rz = _lap_grid(X[a:b], Z[a:b], D[a:b], max_len, bins)
        lx, lz, rx, rz = (_smooth(lx, swl), _smooth(lz, swl),
                          _smooth(rx, swl), _smooth(rz, swl))
        # Bound laps cross S/F mid-track, so the edges PINCH at the seam -> stitch
        # those bins closed-loop from neighbours (same seam guard as the fallback).
        wid = np.hypot(lx - rx, lz - rz)
        med = float(np.median(wid))
        seam = np.zeros(bins, dtype=bool)
        w = max(1, round(bins * 0.03))
        seam[:w] = True; seam[-w:] = True
        bad = seam & (med > 0) & (wid < 0.5 * med)
        if bad.any() and (~bad).any():
            for arr in (lx, lz, rx, rz):
                arr[bad] = np.nan
                arr[:] = _fill_nan(arr, period=bins)
        ctr_x, ctr_z = _smooth((lx + rx) / 2.0, swl), _smooth((lz + rz) / 2.0, swl)
        lat_x, lat_z = _lateral(ctr_x, ctr_z)
        wid = np.hypot(lx - rx, lz - rz)
    else:
        # Fallback: lateral ENVELOPE of all samples (min/max offset per bin), with
        # seam/sparse stitching, edges rebuilt on the smoothed normal.
        cnt = np.array([mm.size for mm in members])
        off_lo = np.array([off_all[mm].min() if mm.size else np.nan for mm in members])
        off_hi = np.array([off_all[mm].max() if mm.size else np.nan for mm in members])
        wid = off_hi - off_lo
        solid = (cnt >= 2) & np.isfinite(wid)
        med = float(np.median(wid[solid])) if solid.any() else 0.0
        seam = np.zeros(bins, dtype=bool)
        w = max(1, round(bins * 0.03))
        seam[:w] = True; seam[-w:] = True
        bad = (cnt < 2) | (seam & (med > 0) & (wid < 0.5 * med))
        if (~bad).any():
            for arr in (off_lo, off_hi):
                arr[bad] = np.nan
                arr[:] = _fill_nan(arr, period=bins)
            off_lo, off_hi = _smooth(off_lo, swl), _smooth(off_hi, swl)
            # +lateral is the driver's LEFT, so off_hi is the LEFT edge (#58).
            lx, lz = cx + off_hi * lat_x, cz + off_hi * lat_z
            rx, rz = cx + off_lo * lat_x, cz + off_lo * lat_z
            wid = off_hi - off_lo
        else:  # single pass / no envelope -> degenerate centerline, zero width
            lx, lz, rx, rz = cx.copy(), cz.copy(), cx.copy(), cz.copy()
            wid = np.zeros(bins)
        ctr_x, ctr_z = (lx + rx) / 2.0, (lz + rz) / 2.0

    # Curvature-adaptive final smoothing of the rendered edges + center: a fixed wide
    # window flattened tight infield esses (the layout lost visible corners). This
    # keeps straights clean and preserves the bends; center is the midpoint of the
    # smoothed edges.
    lx, lz = _smooth_curve(lx, lz, sw)
    rx, rz = _smooth_curve(rx, rz, sw)
    ctr_x, ctr_z = (lx + rx) / 2.0, (lz + rz) / 2.0
    lat_x, lat_z = _lateral(ctr_x, ctr_z)
    wid = np.hypot(lx - rx, lz - rz)
    # Same gate as bank, over ALL samples (width needs no pos_y): the drawn edges stay
    # (the editor and the corner solver need a corridor), but a width the samples never
    # spanned is not written as a measurement. The EDGE separation must clear it too:
    # the bound-lap branch takes width from two laps, and a third lap straying wide
    # lends the bin a spread those two never had (real Algarve racing laps: 97 bins
    # passed on spread alone with a median width of 1.8 m).
    off = (X - ctr_x[bi]) * lat_x[bi] + (Z - ctr_z[bi]) * lat_z[bi]
    spread = np.array([np.ptp(off[mm]) if mm.size else 0.0 for mm in members])
    wid = np.where((spread >= _MIN_SPREAD_M) & (wid >= _MIN_SPREAD_M), wid, np.nan)

    bnk = _bank_per_bin(X, Z, Y, members, ctr_x, ctr_z, lat_x, lat_z, bins, w=sw)
    run = max(1, int(np.ceil(_MIN_SPREAD_RUN_M * bins / max_len)))
    wid = np.where(_sustained(np.isfinite(wid), run), wid, np.nan)
    bnk = np.where(_sustained(np.isfinite(bnk), run), bnk, np.nan)

    def _round(a, n):  # non-finite -> 0.0, JSON-safe (edges/dist are always finite)
        return [round(float(v), n) if np.isfinite(v) else 0.0 for v in a]

    def _known(a, n):  # width/bank: non-finite is UNKNOWN -> null, never 0.0 (= flat)
        return [round(float(v), n) if np.isfinite(v) else None for v in a]

    return {
        "left": [list(p) for p in zip(_round(lx, 2), _round(lz, 2))],
        "right": [list(p) for p in zip(_round(rx, 2), _round(rz, 2))],
        "center": [list(p) for p in zip(_round(ctr_x, 2), _round(ctr_z, 2))],
        "width": _known(wid, 2),
        "bank": _known(bnk, 4),
        "dist": _round(dist_grid, 2),
    }


def _edge_mean(channels: dict, name: str, apex_progress: float, half: float) -> float | None:
    """Mean of an edge lap's channel over a window around a corner, located by
    normalized progress (the edge lap's own lap_distance axis differs from the
    centerline's, so align by progress, not distance)."""
    ld = channels.get("lap_distance")
    ch = channels.get(name)
    if ld is None or ch is None:
        return None
    ld = np.asarray(ld, dtype=float)
    ch = np.asarray(ch, dtype=float)
    n = min(len(ld), len(ch))
    if n < 2 or ld[n - 1] <= ld[0]:
        return None
    ld, ch = ld[:n], ch[:n]
    t = (ld - ld[0]) / (ld[-1] - ld[0])
    mask = np.abs(t - apex_progress) <= half
    if not np.any(mask):
        return None
    m = np.nanmean(ch[mask])
    return None if np.isnan(m) else float(m)


def multi_signal_direction(center, dist, apex_dist, edge_channels=None,
                           lap_length=None, half=0.02):
    """Corner direction with a confidence flag. Path CURVATURE is authoritative
    (owned geometry -- the CLAUDE.md invariant that direction is computed, never
    guessed); steering + lat_g merely corroborate. Returns (direction, confidence)
    where confidence is "low" iff a present corroborating signal disagrees.

    Sign conventions (LMU, telemetry-verified): steering > 0 -> right
    (RIGHT_IS_POSITIVE); lat_g > 0 -> left (right turns read negative lat_g)."""
    direction = direction_from_path(center, dist, apex_dist)
    votes = []
    if edge_channels is not None and lap_length:
        p = apex_dist / lap_length
        s = _edge_mean(edge_channels, "steering", p, half)
        if s is not None:
            votes.append("right" if (s > 0) == RIGHT_IS_POSITIVE else "left")
        g = _edge_mean(edge_channels, "lat_g", p, half)
        if g is not None:
            votes.append("left" if g > 0 else "right")
    confidence = "low" if any(v != direction for v in votes) else "high"
    return direction, confidence


def ribbon_to_screen(ribbon: dict, corners: list, sectors=None, width: float = 1000,
                     rotation: float = 0.0, sections=None) -> dict:
    """Normalize the world ribbon (+ corners placed on the centerline) into one
    width x height screen box, aspect-preserved and vertically flipped, so left,
    right, center and the car-dot overlays all share one frame. Mirrors
    trackmap.build_track_map's transform (rotate about the shared centroid first,
    then fit)."""
    if not ribbon:
        return {}
    allpts = np.array(ribbon["left"] + ribbon["right"] + ribbon["center"], dtype=float)
    if rotation:  # rotate every polyline about ONE shared centroid so they stay aligned
        cx0, cz0 = float(allpts[:, 0].mean()), float(allpts[:, 1].mean())
        rot = {k: [list(p) for p in zip(*rotate_xy(
            np.array([q[0] for q in ribbon[k]]), np.array([q[1] for q in ribbon[k]]),
            rotation, cx0, cz0))] for k in ("left", "right", "center")}
        ribbon = {**ribbon, **rot}
        allpts = np.array(ribbon["left"] + ribbon["right"] + ribbon["center"], dtype=float)
    xmin, xmax = float(allpts[:, 0].min()), float(allpts[:, 0].max())
    zmin, zmax = float(allpts[:, 1].min()), float(allpts[:, 1].max())
    xrange, zrange = xmax - xmin, zmax - zmin
    if xrange <= 0 and zrange <= 0:
        return {}
    span = max(xrange, zrange)
    scale = width / span
    height = zrange * scale if zrange > 0 else 1.0
    out_w = xrange * scale if xrange > 0 else 1.0

    def _screen(pts):
        return [[round((x - xmin) * scale, 1), round(height - (z - zmin) * scale, 1)]
                for x, z in pts]

    dist = np.asarray(ribbon["dist"], dtype=float)
    center = ribbon["center"]
    scorners = []
    for c in corners:
        idx = int(np.argmin(np.abs(dist - c["apex_dist"])))
        cx, cz = center[idx]
        sx = round((cx - xmin) * scale, 1)
        sy = round(height - (cz - zmin) * scale, 1)
        scorners.append({**c, "x": sx, "y": sy})

    return {
        "width": round(out_w, 1),
        "height": round(height, 1),
        "left": _screen(ribbon["left"]),
        "right": _screen(ribbon["right"]),
        "center": _screen(center),
        "corners": scorners,
        "sectors": list(sectors) if sectors else [],
        "sections": list(sections) if sections else [],
        "rotation": round(float(rotation), 1),
    }
