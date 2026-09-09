"""One-time backfill: seed a shipped map's `sectors` (S1/S2 timing-sector split
distances) from the real boundaries the game already captured on driven laps.

Per-lap S1/S2 boundary distances are captured and persisted as `laps.sector_bounds`
(see `simcoach/lmu/smm.py:sector_bounds_from`), but nothing bakes them into the map
-- they're only exposed to the map editor for a human to seed and save. This mirrors
that editor path as an automated one-time bake, the same way
`backfill_corner_geometry.py` bakes radius/width/banking: read captured data, validate,
write into `simcoach/tracks/*.json`, commit.

Only seeds a map that has NO sectors yet (`d["sectors"]` empty/absent) -- never
overwrites an already-seeded or hand-edited split.

Reads machine-local app-data (the lap store); writes simcoach/tracks/*.json.
Run once on the curator machine, then commit the JSON changes.

[RIG] The `mCurrentSector` -> sector-number mapping these bounds come from is
flagged UNCONFIRMED in `simcoach/lmu/smm.py`. The captured values agree tightly
per track (Laguna: 48 laps within ~11 m) and Monza GP's S1 lands ~1988 m, right
at the real line before Variante della Roggia -- but an in-game eyeball against
the drawn sector lines is still wanted before these are treated as verified.

    python scripts/backfill_sectors.py [slug ...]
"""
from __future__ import annotations

import itertools
import json
import math
import sqlite3
import sys
from pathlib import Path

from simcoach.server.__main__ import db_path
from simcoach.track_lookup import track_slug

TRACKS_DIR = Path("simcoach/tracks")


def _sector_bounds_for_slug(conn: sqlite3.Connection, slug: str) -> list[float] | None:
    """The game's real S1/S2 sector-boundary lap_distances for a track slug, from
    the most recent lap (any car) that captured them -- boundaries are fixed per
    track, so any car's captured bounds are the track's. Mirrors
    `LapStore.sector_bounds`, but slug-matched (across cars) instead of keyed on
    one exact track/car pair, the same way `backfill_corner_geometry._store_samples`
    slug-matches laps. None if no lap for this slug has them yet.

    Across CARS only -- it cannot reach across LAYOUTS (#41). `slug` is a map
    file's stem, which is `track_slug(<full live track name>)`, and the layout is
    part of that name ('Autodromo Nazionale Monza' vs 'Monza Curva Grande
    Circuit'). track_slug collapses only case, accents, punctuation and a trailing
    series suffix (' - ELMS'/' - WEC') -- never a layout -- so two layouts never
    share a slug, and any two names that DID share one would also share the single
    map file. Pinned by tests/test_backfill_sectors.py::
    test_sector_bounds_never_cross_monza_layouts."""
    rows = conn.execute(
        "SELECT track, sector_bounds FROM laps WHERE sector_bounds IS NOT NULL ORDER BY id DESC"
    ).fetchall()
    for track, raw in rows:
        if track_slug(track) != slug:
            continue
        try:
            vals = [float(v) for v in json.loads(raw)]
        except (TypeError, ValueError):
            continue
        if vals:
            return vals
    return None


def seed_sectors(d: dict, bounds: list[float] | None) -> bool:
    """Set d["sectors"] from `bounds` if the map has none yet. Validates exactly
    like the map editor's save endpoint (`app.py::_persist_map`): splits must be
    finite, strictly increasing, and within (0, length) -- anything else is
    dropped rather than risk corrupting a shipped map. Returns True if `d` was
    changed."""
    if d.get("sectors"):
        return False
    if not bounds:
        return False
    length = d.get("length") or d.get("length_m")
    if not length:
        return False
    length = float(length)
    if not all(math.isfinite(b) and 0.0 < b < length for b in bounds):
        return False
    if any(a >= b for a, b in itertools.pairwise(bounds)):
        return False
    d["sectors"] = [round(float(b), 1) for b in bounds]
    return True


def _bounds_lap_count(conn: sqlite3.Connection, slug: str) -> int:
    """How many captured laps back this slug's bounds -- printed so the bake record
    shows whether a map was seeded off 48 agreeing laps or a single one."""
    rows = conn.execute("SELECT track FROM laps WHERE sector_bounds IS NOT NULL")
    return sum(1 for (track,) in rows if track_slug(track) == slug)


def backfill(slugs=None):
    conn = sqlite3.connect(str(db_path()))
    files = sorted(TRACKS_DIR.glob("*.json"))
    for f in files:
        slug = f.stem
        if slugs and slug not in slugs:
            continue
        d = json.loads(f.read_text(encoding="utf-8"))
        bounds = _sector_bounds_for_slug(conn, slug)
        n = _bounds_lap_count(conn, slug)
        if not seed_sectors(d, bounds):
            why = ("sectors already set" if d.get("sectors") else
                   "no captured bounds" if not bounds else
                   "no length" if not (d.get("length") or d.get("length_m")) else
                   f"bounds {bounds} failed validation against length {d.get('length')}")
            print(f"{slug}: skip ({why}) | bounds laps={n}")
            continue
        f.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"{slug}: seeded sectors {d['sectors']} | bounds laps={n} | length={d.get('length')}")
    conn.close()


if __name__ == "__main__":
    backfill(set(sys.argv[1:]) or None)
