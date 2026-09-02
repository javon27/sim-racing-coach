"""Resolve the game's live track name (e.g. 'Silverstone Grand Prix Circuit - ELMS',
'Silverstone National Circuit', 'Circuit de la Sarthe Mulsanne') to its corner-map
file. Maps are named by a DETERMINISTIC slug of the full track name, so each distinct
layout gets its own file and unknown layouts simply resolve to None (the caller then
auto-builds one). No fuzzy matching, aliases, or per-layout special cases."""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path

# Championship suffix the game appends to the same physical track ('... - ELMS' /
# '- WEC'). Stripped so one map/knowledge note is shared across series (identical
# geometry); a track's own name (e.g. 'Spa-Francorchamps') is untouched — only a
# trailing ' - <SERIES>' is removed.
_SERIES_SUFFIX = re.compile(r"\s*[-–]\s*(elms|wec|lmu|imsa|gtwc)\s*$", re.IGNORECASE)


def track_slug(track_name: str) -> str:
    """Deterministic filename stem for a live track name: drop the championship
    suffix, strip accents, lowercase, and turn runs of non-alphanumerics into '_'.
    Same track/layout -> same slug."""
    name = _SERIES_SUFFIX.sub("", track_name or "")
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def resolve_track_map(track_name: str, tracks_dir: str = "simcoach/tracks") -> Path | None:
    """The corner-map file for a live track name, or None if we don't have one yet
    (the caller auto-builds it). Exact slug match — no aliases or scoring."""
    slug = track_slug(track_name)
    if not slug:
        return None
    p = Path(tracks_dir) / f"{slug}.json"
    return p if p.exists() else None
