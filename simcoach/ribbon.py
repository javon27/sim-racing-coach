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
    # Bank angle from elevation: outer edge higher than inner -> banked. atan2
    # handles width->0. Missing pos_y on either edge -> flat (all zeros).
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


def _fill_nan(a: np.ndarray) -> np.ndarray:
    """Linear-interpolate NaN gaps (sparse/empty bins) from finite neighbours."""
    idx = np.arange(len(a))
    good = np.isfinite(a)
    if good.sum() == 0:
        return a
    a = a.copy()
    a[~good] = np.interp(idx[~good], idx[good], a[good])
    return a


def reconstruct_surface(laps: list[dict], bins: int = 500) -> dict:
    """Reconstruct the track from a POINT CLOUD of free-driving passes (no left/right
    designation). Pools every lap's (pos_x, pos_z, pos_y, lap_distance), bins by
    lap_distance, bootstraps a centerline (per-bin mean), then takes the lateral
    ENVELOPE per bin. Returns the same shape as reconstruct_ribbon
    {left, right, center, width, bank, dist}, or {} with no usable data.

    # ponytail: one-pass centerline (no refine-iterate) + nearest-bin assignment +
    # O(bins*N) per-bin loop -- naive/quick, fine for a few passes; a refine loop
    # and vectorized binning are the upgrade if accuracy/scale demands. bank sign
    # follows a fixed perpendicular convention -- rig-verify on a real banked track.
    """
    xs, zs, ys, ds = [], [], [], []
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
        xs.append(x[:n][m]); zs.append(z[:n][m]); ys.append(y[:n][m]); ds.append(d[:n][m])
    if not xs:
        return {}
    X, Z, Y, D = np.concatenate(xs), np.concatenate(zs), np.concatenate(ys), np.concatenate(ds)
    max_len = float(D.max())
    if len(X) < 2 or max_len <= 0:
        return {}

    dist_grid = np.linspace(0.0, max_len, bins)
    bi = np.clip(np.round(D / max_len * (bins - 1)).astype(int), 0, bins - 1)
    members = [np.where(bi == b)[0] for b in range(bins)]

    # Bootstrap centerline = per-bin mean (x, z); fill empty bins by interpolation.
    cx = np.array([X[mm].mean() if mm.size else np.nan for mm in members])
    cz = np.array([Z[mm].mean() if mm.size else np.nan for mm in members])
    cx, cz = _fill_nan(cx), _fill_nan(cz)

    # Tangent by centered finite diff (closed loop); lateral unit = tangent rot +90.
    tx, tz = np.roll(cx, -1) - np.roll(cx, 1), np.roll(cz, -1) - np.roll(cz, 1)
    tn = np.hypot(tx, tz); tn[tn == 0] = 1.0
    lat_x, lat_z = -tz / tn, tx / tn

    left, right, center, width, bank = [], [], [], [], []
    for b in range(bins):
        mm = members[b]
        cpt = [cx[b], cz[b]]
        if mm.size == 0:
            left.append(cpt); right.append(cpt); center.append(cpt)
            width.append(0.0); bank.append(0.0)
            continue
        off = (X[mm] - cx[b]) * lat_x[b] + (Z[mm] - cz[b]) * lat_z[b]
        i_lo, i_hi = int(off.argmin()), int(off.argmax())
        left.append([X[mm][i_lo], Z[mm][i_lo]])
        right.append([X[mm][i_hi], Z[mm][i_hi]])
        center.append(cpt)
        width.append(float(off[i_hi] - off[i_lo]))
        yb = Y[mm]
        fin = np.isfinite(yb)
        if fin.sum() >= 2 and np.ptp(off[fin]) > 1e-6:
            bank.append(float(np.arctan(np.polyfit(off[fin], yb[fin], 1)[0])))
        else:
            bank.append(0.0)

    def _pair(p):
        return [round(float(p[0]), 2), round(float(p[1]), 2)]

    return {
        "left": [_pair(p) for p in left],
        "right": [_pair(p) for p in right],
        "center": [_pair(p) for p in center],
        "width": [round(w, 2) for w in width],
        "bank": [round(b, 4) for b in bank],
        "dist": [round(float(d), 2) for d in dist_grid],
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
