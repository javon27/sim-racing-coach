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

import json
import math
import os
import sqlite3
import sys
from pathlib import Path

from simcoach.server.__main__ import db_path
from simcoach.track_lookup import track_slug

# Repo-relative, never cwd-relative: run from the wrong directory and a cwd-relative
# glob matches nothing, so the script would exit 0 having silently baked nothing --
# and a later maintainer would read that as "already done".
TRACKS_DIR = Path(__file__).resolve().parent.parent / "simcoach" / "tracks"
SPLITS = 2  # LMU laps capture exactly two boundaries, S1/S2 (smm.sector_bounds_from)


def _num(v) -> float | None:
    """`v` as a finite float, or None. Rejects bools (`float(True) == 1.0` would
    otherwise sail through as a 1 m sector split), strings -- including numeric
    ones -- and NaN/inf. Every number read out of the DB or a map file goes
    through here; nothing else may call float() on untrusted input."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return float(v)


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
    test_sector_bounds_never_cross_monza_layouts.

    Falling back past an unparseable newer row is correct, but it is announced:
    silently dropping to an older lap would make the bake record claim the latest
    capture when it used an older one."""
    rows = conn.execute(
        "SELECT id, track, sector_bounds FROM laps "
        "WHERE sector_bounds IS NOT NULL ORDER BY id DESC"
    ).fetchall()
    for lap_id, track, raw in rows:
        if track_slug(track) != slug:
            continue
        try:
            raw_vals = json.loads(raw)
        except (TypeError, ValueError):
            raw_vals = None
        # Must be a JSON *list* of finite numbers. A bare string or object would
        # otherwise iterate its characters/keys ('"1234"' -> [1,2,3,4]), and a bool
        # would float() to 1.0 -- both plausible-looking splits built from garbage.
        vals = [_num(v) for v in raw_vals] if isinstance(raw_vals, list) else []
        if vals and None not in vals:
            return vals
        print(f"  warning: {slug}: lap {lap_id} has unusable sector_bounds {raw!r} "
              f"-- falling back to an older lap")
    return None


def seed_sectors(d: dict, bounds: list[float] | None) -> bool:
    """Set d["sectors"] from `bounds` if the map has none yet. Validates at least as
    strictly as the map editor's save endpoint (`app.py::_persist_map`): splits must
    be finite numbers, strictly increasing, and within (0, length) -- anything else
    is dropped rather than risk corrupting a shipped map. Returns True if `d` was
    changed.

    Beyond the endpoint's rules, also requires exactly SPLITS bounds and a `length`
    that is a finite positive *number*. The endpoint accepts any arity (a
    pre-existing gap, recorded on #41); here a wrong arity is the tell that the row
    wasn't a real S1/S2 capture, so reject it. A non-numeric or infinite `length`
    must return False rather than raise -- one bad map file may not abort the run
    and leave the remaining maps unseeded."""
    if d.get("sectors"):
        return False
    if not bounds or len(bounds) != SPLITS:
        return False
    vals = [_num(b) for b in bounds]
    length = _num(d.get("length") or d.get("length_m"))
    if None in vals or length is None or length <= 0.0:
        return False
    if not all(0.0 < b < length for b in vals):
        return False
    if vals[0] >= vals[1]:
        return False
    d["sectors"] = [round(b, 1) for b in vals]
    return True


def _bounds_lap_count(conn: sqlite3.Connection, slug: str) -> int:
    """How many captured laps back this slug's bounds -- printed so the bake record
    shows whether a map was seeded off 48 agreeing laps or a single one."""
    rows = conn.execute("SELECT track FROM laps WHERE sector_bounds IS NOT NULL")
    return sum(1 for (track,) in rows if track_slug(track) == slug)


def backfill(slugs=None) -> int:
    """Seed every matching map that has no sectors yet. Returns how many map files
    were considered -- 0 means nothing matched, which the CLI turns into a non-zero
    exit: "already baked" and "pointed at the wrong place" must not look alike."""
    conn = sqlite3.connect(str(db_path()))
    files = sorted(TRACKS_DIR.glob("*.json"))
    seen = 0
    for f in files:
        slug = f.stem
        if slugs and slug not in slugs:
            continue
        seen += 1
        d = json.loads(f.read_text(encoding="utf-8"))
        bounds = _sector_bounds_for_slug(conn, slug)
        n = _bounds_lap_count(conn, slug)
        raw_len = d.get("length") or d.get("length_m")
        length = _num(raw_len)
        if not seed_sectors(d, bounds):
            why = ("sectors already set" if d.get("sectors")
                   else "no captured bounds" if not bounds
                   else f"expected {SPLITS} bounds, got {len(bounds)}" if len(bounds) != SPLITS
                   else f"length {raw_len!r} is not a finite positive number"
                   if length is None or length <= 0.0
                   else f"bounds {bounds} failed validation against length {length}")
            print(f"{slug}: skip ({why}) | bounds laps={n}")
            continue
        # Atomic: write a sibling tmp then rename over the map. A plain open('w')
        # truncates first, so an abort between truncate and write would leave a
        # shipped map empty -- the worst thing this script could do. `finally`
        # guarantees no .tmp is left behind on either path.
        tmp = f.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, f)
        finally:
            tmp.unlink(missing_ok=True)
        print(f"{slug}: seeded sectors {d['sectors']} | bounds laps={n} | length={length}")
    conn.close()
    return seen


if __name__ == "__main__":
    _slugs = set(sys.argv[1:]) or None
    if not backfill(_slugs):
        raise SystemExit(f"no map files matched {sorted(_slugs) if _slugs else '*.json'} "
                         f"under {TRACKS_DIR} -- nothing baked")
