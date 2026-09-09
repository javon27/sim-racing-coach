"""Resolve the game's live track name (e.g. 'Silverstone Grand Prix Circuit - ELMS',
'Silverstone National Circuit', 'Circuit de la Sarthe Mulsanne') to its corner-map
file. Maps are named by a DETERMINISTIC slug of the full track name, so each distinct
layout gets its own file and unknown layouts simply resolve to None (the caller then
auto-builds one). No fuzzy matching, aliases, or per-layout special cases."""
from __future__ import annotations

import json
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

# Authored prose track notes, `<slug>.md`, keyed on the same slug as the map files.
KNOWLEDGE_DIR = Path(__file__).parent / "knowledge"

# A corner number, in note keys AND in corner labels: one to three ASCII digits.
#
# `[0-9]`, never `\d`, and bounded, because both sides of the comparison are free text
# that gets fed to int(). `\d` also matches Unicode decimal digits, so '١-٣' parsed as
# a range here while the UI's `Number()` reads it as NaN; and an unbounded run matches
# a key past CPython's 4300-digit int()/str() limit, which `PUT /notes` persists happily
# and which then raises on every later read and every map save. Three digits is the
# width of a real corner number -- the largest shipped map has 30 corners.
#
# COUNTERPART of `MAX_CORNER_DIGITS` / `CORNER_NUM` / `RANGE_KEY` / `MAX_RANGE_SPAN` in
# frontend/src/lib/TracksScreen.svelte, which reads this same grammar to decide which
# notes collapse under a range row and which it tells the user match no corner. The two
# must stay identical: a key this guard drops while that screen shows it bound is
# authored prose deleted with no warning. Loosen one, loosen both.
_MAX_CORNER_DIGITS = 3

# A per-corner note key is authored either as a single corner label ("7") or as an
# ASCENDING hyphen range spanning a complex ("2-4", "11-13") -- the only two shapes
# across every shipped map (widest is 3 corners). Ranges are inclusive.
_NOTE_RANGE = re.compile(rf"([0-9]{{1,{_MAX_CORNER_DIGITS}}})-([0-9]{{1,{_MAX_CORNER_DIGITS}}})")

# Widest span still read as a corner complex. Bounds how many corners ONE authored key
# can speak for: a blanket '1-999' bound every numeric corner on the track and was
# served to the driver as `Authored note:` owned fact for each of them.
#
# PARTIAL, and not tuned any tighter on purpose. 100 only removes the range that
# blankets a map of ANY size -- the largest shipped map has 30 corners, so '1-30' still
# blankets a real circuit and still binds. Bounding the blast radius is #64's job (the
# guard keys on corner COUNT, which is why a same-count relabel can rebind a note at
# all); a span cap is a proxy for that, and tightening it toward the real complex width
# would start silently dropping a plausible sector-wide note -- the very failure this
# branch exists to fix.
_MAX_RANGE_SPAN = 100


def note_key_binds(key: str, labels: set[str]) -> bool:
    """Does a per-corner note key still name at least one corner that exists?

    The one place that owns the note-key grammar, so the coach's READ path and the
    map-save guard that decides which notes survive a rebuild cannot drift apart:
    a note is kept exactly when the coach could still serve it.

    A plain key names itself. A numeric range names every label it spans, matching
    how `CoachContext._corner_note` resolves one -- endpoints only would drop a note
    the coach still serves for the corners in between. Anything else (a comma list, a
    GP-style '2A-2B', a DESCENDING '13-11', a group wider or longer than the grammar
    above) names only itself: the read path cannot resolve such a key, so keeping it
    on the strength of its parts would preserve unreachable data.

    There is a THIRD reader of this grammar, in the UI: `TracksScreen.svelte`'s
    `rangeOf`/`cornerView`, which decides which notes collapse under a range row and
    which are flagged to the user as matching no corner. The two are now IDENTICAL by
    construction and a test pins them that way -- a note the guard keeps and the coach
    states as owned fact must not be one the Tracks screen calls an orphan, and a note
    that screen shows collapsed under a range must not be one this guard deletes on the
    next save. Loosen one, loosen both.

    TOTAL: returns a bool for every (key, labels) pair and never raises. Both callers
    use it as a predicate inside a comprehension, and both get their keys and labels
    from free-text user input -- a raise here is a 500 on a map save, or a dead coach
    tool, so the contract is no-exceptions rather than exceptions-documented.
    """
    key = str(key)
    if key in labels:
        return True
    m = _NOTE_RANGE.fullmatch(key)
    if not m:
        return False
    # NOT sorted: a descending '13-11' needs no code of its own, because 13 <= n <= 11
    # is false for every n -- which is what `main` did and what the UI does.
    lo, hi = int(m.group(1)), int(m.group(2))
    if hi - lo >= _MAX_RANGE_SPAN:
        return False
    # The label side takes the SAME bound as the key side, and for the same reason:
    # `labels` comes from the map, so no key-side regex reaches it, and the editor's
    # label box is free text where '²' U+00B2, '₂' U+2082 and '⑥' U+2465 all satisfy
    # str.isdigit() and RAISE on int(). With the bound the parse cannot raise, and
    # int() is what keeps '07' equal to 7 -- as `Number('07')` is in the UI. Spelled
    # out rather than reusing _NOTE_RANGE's class because this runs per label per key.
    return any(len(lab) <= _MAX_CORNER_DIGITS and lab.isascii() and lab.isdigit()
               and lo <= int(lab) <= hi for lab in labels)


def track_slug(track_name: str) -> str:
    """Deterministic filename stem for a live track name: drop the championship
    suffix, strip accents, lowercase, and turn runs of non-alphanumerics into '_'.
    Same track/layout -> same slug."""
    name = _SERIES_SUFFIX.sub("", track_name or "")
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def load_track_knowledge(track_name: str, knowledge_dir: str | Path | None = None) -> str:
    """Authored prose track notes from `knowledge/<slug>.md`, verbatim, or '' if we
    have none.

    The FALLBACK source for tracks whose notes never made it into a corner-map JSON's
    `notes` object — several shipped tracks have an authored note here and no map file
    at all, so this is the only way the coach can reach them. The map file stays
    authoritative wherever it carries notes; this is never merged into it.

    Read as UTF-8 explicitly: a bare `read_text()` decodes as cp1252 on Windows and
    mojibakes the content (issue #29). Slug-built paths only — no traversal."""
    slug = track_slug(track_name)
    if not slug:
        return ""
    base = Path(knowledge_dir) if knowledge_dir is not None else KNOWLEDGE_DIR
    try:
        return (base / f"{slug}.md").read_text(encoding="utf-8")
    except OSError:
        return ""


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
