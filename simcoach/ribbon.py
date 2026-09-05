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
from simcoach.trackmap import direction_from_path


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


def ribbon_to_screen(ribbon: dict, corners: list, sectors=None, width: float = 1000) -> dict:
    """Normalize the world ribbon (+ corners placed on the centerline) into one
    width x height screen box, aspect-preserved and vertically flipped, so left,
    right, center and the car-dot overlays all share one frame. Mirrors
    trackmap.build_track_map's transform."""
    if not ribbon:
        return {}
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
    }
