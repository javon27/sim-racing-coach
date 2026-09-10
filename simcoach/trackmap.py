"""Build a normalized, SVG-ready track outline from a lap's position telemetry
(the driven racing line) plus the corner map. Pure function -- no I/O, no LLM.

The output box is `width` x computed-height; coordinates are already normalized
into it with y growing downward (SVG convention). This is our own asset derived
from the driver's own telemetry -- no game images involved.
"""
from __future__ import annotations

import math

import numpy as np

_MAX_PATH_POINTS = 300

# World-coordinate handedness for turn-direction inference. The 2D cross product
# of the incoming×outgoing segment vectors around an apex is signed; this pins
# which sign is a LEFT turn in LMU's (pos_x, pos_z) ground plane. Chosen so the
# standard math orientation (+x right, +z "up", counter-clockwise = left) reads
# as left; test_direction_synthetic_left_right pins that meaning.
#
# ponytail: calibration knob. It could NOT be confirmed against real Monza
# position telemetry -- no stored lap in this repo carries pos_x/pos_z yet
# (position capture via smm.py is a parallel lane; the DB + all scratchpad
# captures only hold speed/steering/lat_g/lap_distance). Confirm this sign
# against a real captured Monza lap once pos_x/pos_z laps exist, using
# test_direction_monza_real (currently skipped for want of that data).
_LEFT_TURN_IS_CROSS_POSITIVE = True


def average_racing_line(laps, bins: int = 500):
    """Mean racing line over multiple laps, aligned on the lap_distance axis.

    `laps` is a list of channel dicts, each with numpy `pos_x`, `pos_z`,
    `lap_distance`. Each lap's (pos_x, pos_z) is resampled by linear
    interpolation onto ONE shared distance grid 0..max_length, then averaged
    per bin. LMU world coords share a fixed origin, so a plain per-bin mean is
    the midline. Laps missing a channel or too short are skipped.

    Returns (path_world=[[x,z],...], dist=[...]); ([], []) when nothing usable.
    """
    grids_x, grids_z = [], []
    lengths = []
    resampled = []
    for ch in laps:
        if not all(k in ch for k in ("pos_x", "pos_z", "lap_distance")):
            continue
        x = np.asarray(ch["pos_x"], dtype=float)
        z = np.asarray(ch["pos_z"], dtype=float)
        d = np.asarray(ch["lap_distance"], dtype=float)
        n = min(len(x), len(z), len(d))
        if n < 2:
            continue
        x, z, d = x[:n], z[:n], d[:n]
        mask = np.isfinite(x) & np.isfinite(z) & np.isfinite(d)
        if mask.sum() < 2:
            continue
        x, z, d = x[mask], z[mask], d[mask]
        # np.interp needs strictly-increasing xp; lap_distance is monotonic by
        # construction but sort defensively (cheap, and a reset would corrupt).
        order = np.argsort(d, kind="stable")
        d, x, z = d[order], x[order], z[order]
        d, uniq = np.unique(d, return_index=True)  # drop duplicate-distance samples
        x, z = x[uniq], z[uniq]
        if len(d) < 2 or (d[-1] - d[0]) <= 0:
            continue
        resampled.append((d, x, z))
        lengths.append(float(d[-1]))

    if not resampled:
        return [], []

    max_length = max(lengths)
    grid = np.linspace(0.0, max_length, bins)
    for d, x, z in resampled:
        # NaN beyond each lap's own range so a short lap doesn't drag the mean's
        # tail toward where it stopped; nanmean ignores those bins. The longest
        # lap spans the whole grid, so no bin is ever all-NaN.
        grids_x.append(np.interp(grid, d, x, left=np.nan, right=np.nan))
        grids_z.append(np.interp(grid, d, z, left=np.nan, right=np.nan))

    mean_x = np.nanmean(grids_x, axis=0)
    mean_z = np.nanmean(grids_z, axis=0)
    path = [[round(float(a), 2), round(float(b), 2)] for a, b in zip(mean_x, mean_z)]
    dist = [round(float(d), 2) for d in grid]
    return path, dist


def turn_sign_along(path_world, k: int = 3) -> np.ndarray:
    """Per-station turn direction along a closed path: **+1 turning RIGHT, -1
    turning LEFT**. Shape (N,).

    The SINGLE owner of the handedness rule: signs the 2D cross product of the
    incoming x outgoing segment vectors (k stations either side for stability)
    against the calibrated ``_LEFT_TURN_IS_CROSS_POSITIVE``. ``direction_from_path``
    is this sampled at one apex, so a caller that needs direction station-by-station
    (``raceline.speed_profile``, re-signing bank against the turn -- #63) reuses this
    rather than re-deriving a second copy of the rule.

    There is deliberately no third "straight" value for ``cross == 0`` (reachable:
    Bahrain Outer has 2 such stations). Every station resolves through the SAME
    ``(cross > 0) == _LEFT_TURN_IS_CROSS_POSITIVE`` comparison, so flipping that
    constant -- which its own note at the top of this module expects, pending real
    positional telemetry -- inverts every station uniformly. A zero branch would
    have stayed put under the flip and diverged silently from the signed ones.

    Closed loop: indices wrap, so station 0 still gets neighbours either side.
    """
    pts = np.asarray(path_world, dtype=float)
    kk = min(k, len(pts) - 1)
    vin = pts - np.roll(pts, kk, axis=0)
    vout = np.roll(pts, -kk, axis=0) - pts
    cross = vin[:, 0] * vout[:, 1] - vin[:, 1] * vout[:, 0]
    is_left = (cross > 0) == _LEFT_TURN_IS_CROSS_POSITIVE
    return np.where(is_left, -1.0, 1.0)


def direction_from_path(path_world, dist, apex_dist, k: int = 3) -> str:
    """Infer a corner's turn direction ("left"/"right") from path curvature at
    the apex -- pure position geometry, honoring the CLAUDE.md invariant (never
    LLM-guessed, never client-supplied). Finds the path point nearest apex_dist
    and reads ``turn_sign_along`` there.

    Raises ValueError if the path is too short to sample around the apex.
    """
    pts = np.asarray(path_world, dtype=float)
    d = np.asarray(dist, dtype=float)
    if pts.ndim != 2 or pts.shape[0] < 3 or pts.shape[1] != 2 or len(d) != len(pts):
        raise ValueError("path too short or malformed for direction inference")
    idx = int(np.argmin(np.abs(d - apex_dist)))
    return "left" if turn_sign_along(pts, k)[idx] < 0.0 else "right"


def _finite_xz(channels: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    """Return (x, z, dist) as finite-filtered float arrays, or None if position
    channels are absent or too short to draw a line."""
    if "pos_x" not in channels or "pos_z" not in channels:
        return None
    x = np.asarray(channels["pos_x"], dtype=float)
    z = np.asarray(channels["pos_z"], dtype=float)
    dist = np.asarray(channels.get("lap_distance", np.arange(len(x))), dtype=float)
    n = min(len(x), len(z), len(dist))
    if n < 2:
        return None
    x, z, dist = x[:n], z[:n], dist[:n]
    mask = np.isfinite(x) & np.isfinite(z) & np.isfinite(dist)
    if mask.sum() < 2:
        return None
    return x[mask], z[mask], dist[mask]


def rotate_xy(x, z, degrees, cx, cz):
    """Rotate world (x, z) points by `degrees` about (cx, cz). Used to bake a
    per-track display rotation into the world->screen transform BEFORE fitting,
    so the map renders the conventional way and still fits the box at any angle."""
    if not degrees:
        return x, z
    t = math.radians(degrees)
    ct, st = math.cos(t), math.sin(t)
    dx, dz = x - cx, z - cz
    return cx + dx * ct - dz * st, cz + dx * st + dz * ct


def build_track_map(channels: dict, corner_map, width: float = 1000, sectors=None,
                    rotation: float = 0.0) -> dict:
    """Map world (pos_x, pos_z) -> a width x height screen box, preserving aspect
    ratio and flipping vertically so it reads naturally. Returns
    {"width", "height", "path": [[x,y],...], "dist": [d,...],
     "corners": [{number,name,direction,x,y,apex_dist,start_dist,end_dist}],
     "sectors": [d1,d2]} or {} when there is no usable position data.

    `dist` is the lap_distance parallel to the downsampled screen `path` (same
    length) -- the alignment key a replay/Pit-Wall overlay needs to map a
    lap_distance to a screen point. `sectors` (optional) is passed through as-is.

    corner_map may be None or have an empty .corners list -> corners: [].
    """
    xz = _finite_xz(channels)
    if xz is None:
        return {}
    x, z, dist = xz
    # Rotate about the centroid FIRST, then fit -- downstream bounds/corners all
    # use the rotated coords, so the map stays oriented and clip-free at any angle.
    # Negate: _to_screen flips z (screen y-down), which mirrors rotation direction.
    # Negating here makes a positive `rotation` read CLOCKWISE on screen, matching
    # the editor's live SVG-rotate preview (SVG rotate() is CW-positive). Without
    # this, the baked result spun opposite the preview (entered +74 showed as -74).
    if rotation:
        x, z = rotate_xy(x, z, -rotation, float(x.mean()), float(z.mean()))

    xmin, xmax = float(x.min()), float(x.max())
    zmin, zmax = float(z.min()), float(z.max())
    xrange = xmax - xmin
    zrange = zmax - zmin
    if xrange <= 0 and zrange <= 0:
        return {}  # degenerate: all samples at one point

    # Scale the wider axis to `width`, keep aspect on the other. Guard a zero
    # range on either axis (a perfectly straight line) so scale stays finite.
    span = max(xrange, zrange)
    scale = width / span
    height = zrange * scale if zrange > 0 else 1.0
    out_w = xrange * scale if xrange > 0 else 1.0

    def _to_screen(wx: float, wz: float) -> tuple[float, float]:
        sx = (wx - xmin) * scale
        sy = height - (wz - zmin) * scale  # flip vertical
        return sx, sy

    # Downsample the path for a light payload; keep lap_distance parallel to it.
    stride = max(1, math.ceil(len(x) / _MAX_PATH_POINTS))
    path = []
    path_dist = []
    for i in range(0, len(x), stride):
        sx, sy = _to_screen(x[i], z[i])
        path.append([round(sx, 1), round(sy, 1)])
        path_dist.append(round(float(dist[i]), 1))

    corners = []
    cm_corners = getattr(corner_map, "corners", None) or []
    for c in cm_corners:
        idx = int(np.argmin(np.abs(dist - c.apex_dist)))
        sx, sy = _to_screen(x[idx], z[idx])
        corners.append({
            "number": c.number,
            "label": c.label or str(c.number),
            "name": c.name,
            "direction": c.direction,
            "x": round(sx, 1),
            "y": round(sy, 1),
            "apex_dist": round(float(c.apex_dist), 1),
            "apexes": [round(float(a), 1) for a in (c.apexes or [c.apex_dist])],
            "start_dist": round(float(c.start_dist), 1),
            "end_dist": round(float(c.end_dist), 1),
        })

    return {
        "width": round(out_w, 1),
        "height": round(height, 1),
        "path": path,
        "dist": path_dist,
        "corners": corners,
        "sectors": list(sectors) if sectors else [],
        "sections": getattr(corner_map, "sections", None) or [],
        "rotation": round(float(rotation), 1),
    }
