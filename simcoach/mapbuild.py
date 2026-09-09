"""Build a corner map from a captured lap's telemetry — no LLM, no human.

Detects corners as steering peaks (works for fast AND slow corners), reads each
corner's direction from the steering sign and its distance/min-speed from the lap,
and emits a numbered-geometry corner-map JSON that CornerMap.load accepts and
verify_map_directions passes. Corners are named 'Turn N' (detection order) —
attaching real corner NAMES needs an external source (wiki/diagram/driver) and is a
separate enrichment step; the geometry (count, order, direction, distance) is fully
automated here.

CLI:  python -m simcoach.mapbuild <lap.npz> [--slug SLUG] [--length METRES]
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np

from simcoach.corners import Corner, clamp_corner_windows

# tuned on live LMU laps (Silverstone/Interlagos/Monza/etc.), RIGHT_IS_POSITIVE=True
_SMOOTH_M = 15.0     # window for smoothing |steering|
_MIN_STEER = 0.05    # ignore near-straight wobble
_MIN_GAP_M = 120.0   # merge detections closer than this (one corner)
_WIN_M = 55.0        # half-width of each corner's distance window


def detect_corners(channels: dict) -> list[dict]:
    """Return detected corners in track order: [{apex_dist, direction, min_speed}].

    A corner is a local maximum of smoothed |steering|. Direction is the steering
    sign at the apex (RIGHT_IS_POSITIVE=True); min_speed is the slowest speed near it.
    """
    dist = np.asarray(channels["lap_distance"], dtype=float)
    spd = np.asarray(channels["speed"], dtype=float)
    steer = np.asarray(channels["steering"], dtype=float)
    order = np.argsort(dist)
    dist, spd, steer = dist[order], spd[order], steer[order]

    absteer = np.abs(steer)
    smooth = np.empty_like(absteer)
    for i in range(len(absteer)):
        w = (dist >= dist[i] - _SMOOTH_M / 2) & (dist <= dist[i] + _SMOOTH_M / 2)
        smooth[i] = absteer[w].mean()

    peaks: list[int] = []
    for i in range(1, len(smooth) - 1):
        w = (dist >= dist[i] - 100) & (dist <= dist[i] + 100)
        if smooth[i] >= smooth[w].max() - 1e-9 and smooth[i] >= _MIN_STEER:
            peaks.append(i)
    # merge peaks closer than _MIN_GAP_M, keeping the stronger one
    merged: list[int] = []
    for i in peaks:
        if not merged or dist[i] - dist[merged[-1]] > _MIN_GAP_M:
            merged.append(i)
        elif smooth[i] > smooth[merged[-1]]:
            merged[-1] = i

    corners = []
    for i in merged:
        a = dist[i]
        near = (dist >= a - 40) & (dist <= a + 40)
        direction = "right" if float(np.mean(steer[near])) > 0 else "left"
        spd_win = spd[(dist >= a - 80) & (dist <= a + 80)]
        corners.append({
            "apex_dist": round(float(a), 1),
            "direction": direction,
            "min_speed": round(float(spd_win.min()), 1) if len(spd_win) else None,
        })
    return corners


def build_map(slug: str, channels: dict, length_m: float | None = None) -> dict:
    """Build a numbered-geometry corner-map dict from a lap's channels."""
    dist = np.asarray(channels["lap_distance"], dtype=float)
    length = float(length_m) if length_m else math.ceil(float(dist.max()))
    built = []
    for n, c in enumerate(detect_corners(channels), 1):
        apex = c["apex_dist"]
        start = max(0.0, apex - _WIN_M)
        end = min(length, apex + _WIN_M)
        # guard the degenerate window at the very start/end of the lap
        if not (start < apex < end):
            start, end = max(0.0, apex - 1), min(length, apex + 1)
        built.append(Corner(number=n, name=f"Turn {n}", direction=c["direction"],
                            start_dist=round(start, 1), apex_dist=apex,
                            end_dist=round(end, 1)))
    # Clamp here, not only on load: otherwise the file on disk keeps the raw
    # +/-_WIN_M windows while the self-verify step below (and every consumer) sees
    # the clamped ones, so the map QA never looks at what was actually written.
    clamp_corner_windows(built, length)
    corners = [{"number": c.number, "name": c.name, "direction": c.direction,
                "start_dist": c.start_dist, "apex_dist": c.apex_dist,
                "end_dist": c.end_dist} for c in built]
    return {
        "track": slug,
        "_config_note": "Auto-built from one captured lap by simcoach.mapbuild.",
        "_distances_note": "GEOMETRY from telemetry (steering-peak detection): distances + "
                           "directions are real data (RIGHT_IS_POSITIVE=True). 'Turn N' names are "
                           "detection order, NOT official numbers/names — enrich from wiki/diagram/"
                           "driver. Fast/flat corners may be missed on a single lap; more laps help.",
        "length_m": length,
        "corners": corners,
    }


def _main():
    ap = argparse.ArgumentParser(description="Build a corner map from a captured lap (.npz).")
    ap.add_argument("npz", help="path to a captured lap .npz (channels incl. lap_distance/speed/steering)")
    ap.add_argument("--slug", help="track slug / output filename (default: npz stem)")
    ap.add_argument("--length", type=float, help="official lap length in metres (default: max lap_distance)")
    ap.add_argument("--out-dir", default="simcoach/tracks", help="where to write <slug>.json")
    args = ap.parse_args()

    data = np.load(args.npz)
    channels = {k: np.asarray(data[k]) for k in data.files}
    slug = args.slug or Path(args.npz).stem
    obj = build_map(slug, channels, args.length)

    out = Path(args.out_dir) / f"{slug}.json"
    out.write_text(json.dumps(obj, indent=2))
    print(f"wrote {out} — {len(obj['corners'])} corners, {obj['length_m']:.0f}m")
    # self-verify: load + check directions against the same lap
    from simcoach.corners import CornerMap, verify_map_directions
    from types import SimpleNamespace
    m = CornerMap.load(str(out))
    rec = SimpleNamespace(channels=channels)
    mm = verify_map_directions(rec, m)
    print("directions:", "ALL VERIFIED" if not mm else f"MISMATCH {mm}")


if __name__ == "__main__":
    _main()
