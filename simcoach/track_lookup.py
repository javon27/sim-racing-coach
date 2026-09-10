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

# Widest span still read as a corner complex. Bounds the probe loop below, and bounds
# how many corners ONE authored key can speak for: a blanket '1-999' used to bind every
# numeric corner on the track and be served to the driver as `Authored note:` fact for
# each of them. Only a partial mitigation -- see the note in the test of the same name.
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
    which are flagged to the user as matching no corner. It is deliberately the
    strictest of the three (ASCII digits, ascending only), and this grammar is held
    down to it -- a note the guard keeps and the coach states as owned fact must not
    be the same note the Tracks screen calls an orphan. Loosen one, loosen both.

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
    lo, hi = int(m.group(1)), int(m.group(2))     # NOT sorted, deliberately -- below
    if hi - lo >= _MAX_RANGE_SPAN:
        return False
    # Probes the label set over the span instead of parsing each label. `labels` comes
    # from the MAP, so no key-side regex reaches it, and the editor's label box is free
    # text where '²' U+00B2, '₂' U+2082 and '⑥' U+2465 all satisfy str.isdigit() and
    # RAISE on int() -- comparing as strings leaves no parse to get wrong. It is also
    # why a descending key needs no code: range(13, 12) is empty, so '13-11' binds
    # nothing, matching both `main` and the UI. Both callers pass a set, so each probe
    # is O(1); a list here would make this quadratic.
    #
    # DO NOT turn this back into a label parse. The obvious-looking alternative,
    #     any(len(lab) <= 3 and lab.isascii() and lab.isdigit() and lo <= int(lab) <= hi ...)
    # was written, shipped and reverted (019dc26 / b181604). It is safe only as written:
    # short-circuit `and` is the sole reason int() never sees '²', so moving or weakening
    # the isascii() clause puts the crash straight back. The probe cannot raise at all,
    # which is the property worth keeping -- three separate crash paths on this branch
    # came from a guarded label parse, reached from a free-text UI field and from an
    # LLM-callable tool. The parse's only gain was binding a '07' label under '5-9'; no
    # shipped map has a zero-padded label, and when the two differ the note surfaces as
    # a UI-flagged orphan instead of silently rebinding.
    return any(str(n) in labels for n in range(lo, hi + 1))


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


def guard_corner_notes(slug: str, old_notes: dict | None, old_corner_count: int,
                       new_labels: list[str]) -> dict | None:
    """Carry an authored `notes` overlay across a map rebuild (#37).

    Per-corner notes name corners by LABEL, and a rebuild can re-split a chicane
    or relabel '2' as '2A'/'2B'. A stale key then rebinds a note to a DIFFERENT
    corner, which get_corner states as owned fact ("Authored note: ...") -- worse
    than losing it. So keep a per-corner note only when the corner count is
    unchanged AND the key still names a corner that exists;
    overview/reference_times/source are not corner-keyed and are always kept.
    Dropped notes are logged with their text so the author can recover them.

    A key may name a RANGE ('2-4', '11-13' -- how a complex is annotated, and 16 of
    the 135 shipped note keys), so ask `note_key_binds` what it names rather than
    testing the raw key against the labels: '2-4' is never itself a label, and
    treating it as one silently deleted every range-keyed note on every save.

    Returns the notes block to write, or None when it would be hollow. Shared by
    the editor's save path and the promote-to-shipped script so the two can't drift.
    """
    # `notes` reaches the promote path straight out of an arbitrary user JSON file, so
    # a non-dict `corners` is input, not a bug: refuse it as empty rather than raising.
    old = (old_notes or {}).get("corners")
    old_corners = {str(k): v for k, v in old.items()} if isinstance(old, dict) else {}
    if old and not old_corners:
        # Truthy but not an object: nothing to carry, so say what was ignored. A
        # promote reads this out of an arbitrary user JSON file, where `"corners":
        # "2-3: brake late"` (a string, not an object) is a plausible typo -- silently
        # accepting it would report success and quietly ship no notes at all.
        logging.getLogger(__name__).warning(
            "track %s: ignored a non-object `notes.corners` (%s, %d chars); per-corner "
            "notes must be an object keyed by corner label. Ignored: %.200r",
            slug, type(old).__name__, len(str(old)), old)
    labels = set(new_labels)
    kept = ({k: v for k, v in old_corners.items() if note_key_binds(k, labels)}
            if old_corner_count == len(new_labels) else {})
    dropped = {k: v for k, v in old_corners.items() if k not in kept}
    if dropped:
        # Loud, and with the text, so the author can recover it from the log -- but
        # BOUNDED. This dumped every dropped note in full, and wrote 4.6 MB in a single
        # warning() call on a probe that stored many notes: a log sink is not a place
        # unbounded user input gets to land. 20 keys with 200 chars of each note is enough
        # to re-author from, and the count stays exact. The bound belongs HERE rather than
        # in the endpoint that found it, because the promote script shares this function
        # and reads `notes` straight out of an arbitrary user JSON file -- a more exposed
        # sink than a localhost PUT with per-field caps in front of it.
        sample = {k: v[:200] for k, v in sorted(dropped.items())[:20]}
        logging.getLogger(__name__).warning(
            "track %s: dropped %d authored per-corner note(s) on map save (%s); "
            "re-author them against the new layout. Dropped (%d shown): %s",
            slug, len(dropped),
            "corner count changed" if old_corner_count != len(new_labels)
            else "the corners they name no longer exist",
            len(sample), json.dumps(sample, ensure_ascii=False))
    notes = {**(old_notes or {}), "corners": kept}
    return notes if any(notes.values()) else None
