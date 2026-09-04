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


def build_track_map(channels: dict, corner_map, width: float = 1000) -> dict:
    """Map world (pos_x, pos_z) -> a width x height screen box, preserving aspect
    ratio and flipping vertically so it reads naturally. Returns
    {"width", "height", "path": [[x,y],...], "corners": [{number,name,direction,x,y}]}
    or {} when there is no usable position data.

    corner_map may be None or have an empty .corners list -> corners: [].
    """
    xz = _finite_xz(channels)
    if xz is None:
        return {}
    x, z, dist = xz

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

    # Downsample the path for a light payload.
    stride = max(1, math.ceil(len(x) / _MAX_PATH_POINTS))
    path = []
    for i in range(0, len(x), stride):
        sx, sy = _to_screen(x[i], z[i])
        path.append([round(sx, 1), round(sy, 1)])

    corners = []
    cm_corners = getattr(corner_map, "corners", None) or []
    for c in cm_corners:
        idx = int(np.argmin(np.abs(dist - c.apex_dist)))
        sx, sy = _to_screen(x[idx], z[idx])
        corners.append({
            "number": c.number,
            "name": c.name,
            "direction": c.direction,
            "x": round(sx, 1),
            "y": round(sy, 1),
        })

    return {
        "width": round(out_w, 1),
        "height": round(height, 1),
        "path": path,
        "corners": corners,
    }
