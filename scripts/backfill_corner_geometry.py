"""One-time backfill: bake per-corner geometry (radius/width/banking/elevation)
into the shipped track maps, computed from local captures/laps.

- radius:        from the map's 2D path curvature (always).
- width/banking: from the track's stored ribbon (index-aligned with the path).
- elevation:     from pos_y -- the track's point-cloud capture if present, else a
                 few driven laps from the store; binned + interpolated onto the path.

Reads machine-local app-data (captures + lap store); writes simcoach/tracks/*.json.
Run once on the curator machine, then commit the JSON changes.

    python scripts/backfill_corner_geometry.py [slug ...]
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import numpy as np

from simcoach.analysis import CORNER_GEOMETRY_KEYS, corner_geometry
from simcoach.ribbon import reconstruct_surface
from simcoach.server.__main__ import db_path, telemetry_dir
from simcoach.server.app import _load_cloud
from simcoach.store import LapStore
from simcoach.track_lookup import track_slug

TRACKS_DIR = Path("simcoach/tracks")
# This script is the ONLY writer allowed to change these -- the editor's save path
# carries them forward instead of re-deriving them (#62). Shared owner so the two
# lists cannot drift.
_METRIC_KEYS = CORNER_GEOMETRY_KEYS


def _elev_on_path(samples, path_dist, bins: int = 500):
    """Mean pos_y per distance-bin across all samples, interpolated onto path_dist.
    `samples`: list of (dist_arr, y_arr). None when there's nothing usable."""
    ds, ys = [], []
    for d, y in samples:
        d, y = np.asarray(d, float), np.asarray(y, float)
        m = min(len(d), len(y))
        if m:
            ds.append(d[:m])
            ys.append(y[:m])
    if not ds:
        return None
    d, y = np.concatenate(ds), np.concatenate(ys)
    pd = np.asarray(path_dist, float)
    lo, hi = float(pd.min()), float(pd.max())
    if hi <= lo:
        return None
    edges = np.linspace(lo, hi, bins + 1)
    idx = np.clip(np.digitize(d, edges) - 1, 0, bins - 1)
    prof = np.full(bins, np.nan)
    for b in range(bins):
        sel = y[idx == b]
        if len(sel):
            prof[b] = sel.mean()
    centers = 0.5 * (edges[:-1] + edges[1:])
    good = ~np.isnan(prof)
    if good.sum() < 2:
        return None
    return np.interp(pd, centers[good], prof[good])


def _capture_samples(slug, cap_dir):
    cap = cap_dir / f"{slug}.npz"
    if not cap.exists():
        return []
    return [(lap["lap_distance"], lap["pos_y"]) for lap in _load_cloud(cap)
            if "lap_distance" in lap and "pos_y" in lap]


def _ribbon_from_cloud(slug, cap_dir, path_dist):
    """Width/banking on the frozen path, reconstructed from the saved point cloud.

    For an already-curated map (frozen path, no stored ribbon) the cloud from a
    boundary-weave capture is the only width/banking source -- reconstruct the
    ribbon and resample its width/bank (on the reconstruction's own lap-distance
    grid) onto the frozen path_dist. Returns (width, bank) lists aligned to
    path_dist, or (None, None) when there's no usable cloud."""
    cap = cap_dir / f"{slug}.npz"
    if not cap.exists():
        return None, None
    world = reconstruct_surface(_load_cloud(cap))
    rd, rw, rb = world.get("dist"), world.get("width"), world.get("bank")
    if not (rd and rw and rb):
        return None, None
    rd = np.asarray(rd, float)
    pd = np.asarray(path_dist, float)
    width = list(np.interp(pd, rd, np.asarray(rw, float)))
    bank = list(np.interp(pd, rd, np.asarray(rb, float)))
    return width, bank


def _store_samples(slug, store, conn, limit=12):
    out = []
    for lap_id, track in conn.execute("SELECT id, track FROM laps"):
        if track_slug(track) != slug:
            continue
        try:
            ch = store.load(lap_id).channels
        except Exception:
            continue
        if "pos_y" in ch and "lap_distance" in ch:
            out.append((ch["lap_distance"], ch["pos_y"]))
        if len(out) >= limit:
            break
    return out


def backfill(slugs=None):
    cap_dir = Path(telemetry_dir()) / "captures"
    store = LapStore(str(db_path()), str(telemetry_dir()))
    conn = sqlite3.connect(str(db_path()))
    files = sorted(TRACKS_DIR.glob("*.json"))
    for f in files:
        slug = f.stem
        if slugs and slug not in slugs:
            continue
        d = json.loads(f.read_text(encoding="utf-8"))
        path, dist, corners = d.get("path"), d.get("path_dist"), d.get("corners")
        if not (path and dist and corners):
            print(f"{slug}: skip (no path/corners)")
            continue
        rib = d.get("ribbon") or {}
        width = rib.get("width") if len(rib.get("width") or []) == len(path) else None
        bank = rib.get("bank") if len(rib.get("bank") or []) == len(path) else None
        # No stored ribbon (curated map saved via "Save capture only") -> reconstruct
        # width/banking from the raw boundary-weave cloud and resample onto the path.
        if width is None or bank is None:
            cw, cb = _ribbon_from_cloud(slug, cap_dir, dist)
            width = width if width is not None else cw
            bank = bank if bank is not None else cb
        samples = _capture_samples(slug, cap_dir) or _store_samples(slug, store, conn)
        elev = _elev_on_path(samples, dist) if samples else None
        geo = corner_geometry(path, dist, corners, width=width, bank=bank, elev=elev)
        for c in corners:
            lab = str(c.get("label") or c.get("number"))
            m = geo.get(lab, {})
            for k in _METRIC_KEYS:
                if k in m:
                    c[k] = m[k]
                else:
                    c.pop(k, None)  # don't leave a stale metric if its source vanished
        f.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
        got = sorted({k for c in corners for k in _METRIC_KEYS if k in c})
        src = "capture" if (cap_dir / f"{slug}.npz").exists() else ("laps" if samples else "none")
        print(f"{slug}: baked {len(corners)} corners | metrics={got} | elev_src={src}")
    conn.close()


if __name__ == "__main__":
    backfill(set(sys.argv[1:]) or None)
