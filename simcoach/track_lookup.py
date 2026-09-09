"""Resolve the game's live track name (e.g. 'Silverstone Grand Prix Circuit - ELMS',
'Silverstone National Circuit', 'Circuit de la Sarthe Mulsanne') to its corner-map
file. Maps are named by a DETERMINISTIC slug of the full track name, so each distinct
layout gets its own file and unknown layouts simply resolve to None (the caller then
auto-builds one). No fuzzy matching, aliases, or per-layout special cases."""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from pathlib import Path

# Annotation fields a user may customize on a SHIPPED track (saved to app-data and
# merged over the shipped map on read). Everything else — path/dist/edges/sectors
# and other reconstructed geometry — is authoritative and ships with the app.
_ANNOTATION_FIELDS = ("corners", "sections", "rotation", "notes")

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


def _read_map_json(p: Path) -> dict:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def load_map_data(
    track_name: str, tracks_dir: str, user_dir: str | None = None
) -> dict:
    """Merged track-map dict for a live track name, or {} if we have none.

    Authoritative geometry (path/dist/edges/sectors/…) comes from the shipped map in
    ``tracks_dir``; the user's annotation overlay (corners/sections/rotation/notes) in
    ``user_dir`` is layered on top so a re-named/re-annotated shipped track keeps the
    app's layout while honouring the user's edits. An UNSHIPPED track (no shipped map)
    falls back to the user's file as the whole map. Slug-built paths only — no traversal."""
    slug = track_slug(track_name)
    if not slug:
        return {}
    bundled = _read_map_json(Path(tracks_dir) / f"{slug}.json")
    user = _read_map_json(Path(user_dir) / f"{slug}.json") if user_dir else {}
    if not bundled:
        return user
    if not user:
        return bundled
    merged = dict(bundled)
    for k in _ANNOTATION_FIELDS:
        if k in user:
            merged[k] = user[k]
    return merged


def guard_corner_notes(slug: str, old_notes: dict | None, old_corner_count: int,
                       new_labels: list[str]) -> dict | None:
    """Carry an authored `notes` overlay across a map rebuild (#37).

    Per-corner notes are keyed by corner LABEL, and a rebuild can re-split a
    chicane or relabel '2' as '2A'/'2B'. A stale key then rebinds a note to a
    DIFFERENT corner, which get_corner states as owned fact ("Authored note: ...")
    -- worse than losing it. So keep a per-corner note only when the corner count
    is unchanged AND its label still exists; overview/reference_times/source are
    not corner-keyed and are always kept. Dropped notes are logged with their text
    so the author can recover them.

    Returns the notes block to write, or None when it would be hollow. Shared by
    the editor's save path and the promote-to-shipped script so the two can't drift.
    """
    old_corners = {str(k): v for k, v in ((old_notes or {}).get("corners") or {}).items()}
    labels = set(new_labels)
    kept = ({k: v for k, v in old_corners.items() if k in labels}
            if old_corner_count == len(new_labels) else {})
    dropped = {k: v for k, v in old_corners.items() if k not in kept}
    if dropped:
        # Loud, and with the text, so the author can recover it from the log.
        logging.getLogger(__name__).warning(
            "track %s: dropped %d authored per-corner note(s) on map save (%s); "
            "re-author them against the new layout. Dropped: %s",
            slug, len(dropped),
            "corner count changed" if old_corner_count != len(new_labels)
            else "labels no longer exist",
            json.dumps(dropped, ensure_ascii=False))
    notes = {**(old_notes or {}), "corners": kept}
    return notes if any(notes.values()) else None
