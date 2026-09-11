"""Resolve the game's live track name (e.g. 'Silverstone Grand Prix Circuit - ELMS',
'Silverstone National Circuit', 'Circuit de la Sarthe Mulsanne') to its corner-map
file. Maps are named by a DETERMINISTIC slug of the full track name, so each distinct
layout gets its own file and unknown layouts simply resolve to None (the caller then
auto-builds one). No fuzzy matching, aliases, or per-layout special cases."""
from __future__ import annotations

import json
import logging
import math
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


def note_key_labels(key: str) -> list[str]:
    """Every corner label a per-corner note key can name: the key itself, then -- for
    a range key -- each label across its span, ascending.

    THE one place that owns the note-key grammar, so the coach's READ path, the
    map-save guard that decides which notes survive a rebuild, and the guard's
    REBINDING of a key onto relabelled corners (#64) cannot drift apart: a note is
    kept, moved, and served for exactly the same set of corners.

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

    TOTAL: returns a list for every key and never raises. Every caller feeds it free
    text (a note key off disk, out of a PUT body, or out of an LLM tool call) -- a
    raise here is a 500 on a map save, or a dead coach tool, so the contract is
    no-exceptions rather than exceptions-documented. The span is bounded by
    _MAX_RANGE_SPAN, so the list is at most 101 short strings however wide the key.
    """
    key = str(key)
    out = [key]
    m = _NOTE_RANGE.fullmatch(key)
    if not m:
        return out
    lo, hi = int(m.group(1)), int(m.group(2))     # NOT sorted, deliberately -- below
    if hi - lo >= _MAX_RANGE_SPAN:
        return out
    # A descending key needs no code: range(13, 12) is empty, so '13-11' names only
    # itself and binds nothing, matching both `main` and the UI.
    out += [str(n) for n in range(lo, hi + 1)]
    return out


def note_key_binds(key: str, labels: set[str]) -> bool:
    """Does a per-corner note key still name at least one corner that exists?

    Kept as its own name because it is the predicate three call sites read as one
    (the coach's read, the save guard, the promote script); the grammar itself lives
    in `note_key_labels` above. TOTAL, for the same reason it is.
    """
    # Probes the label set over the span instead of parsing each label. `labels` comes
    # from the MAP, so no key-side regex reaches it, and the editor's label box is free
    # text where '²' U+00B2, '₂' U+2082 and '⑥' U+2465 all satisfy str.isdigit() and
    # RAISE on int() -- comparing as strings leaves no parse to get wrong. Callers pass
    # a set, so each probe is O(1); a list here would make this quadratic.
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
    return any(lab in labels for lab in note_key_labels(key))


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


def corner_apex_key(corner: dict) -> float | None:
    """Identity of a corner ACROSS A SAVE: its apex distance, rounded the way the
    save paths write it (1 dp).

    `label` and `number` are both rewritten by the editor -- `label` is the free-text
    box the curator types in, `number` is reassigned POSITIONALLY by its renumber() on
    every insert -- so neither names the same physical corner before and after an edit.
    The apex is where the corner IS. Both things that get carried across a save key on
    this: the baked per-corner geometry (#62, which is where this started) and the
    authored per-corner notes (#64).

    None for a corner with no usable apex, which matches nothing rather than
    everything. Total: takes anything off disk and never raises.
    """
    if not isinstance(corner, dict):
        return None
    try:
        a = float(corner["apex_dist"])
    except (KeyError, TypeError, ValueError, OverflowError):   # OverflowError: huge int
        return None
    return round(a, 1) if math.isfinite(a) else None


def _finite(value) -> float | None:
    """A distance off disk as a finite float, or None. Never raises: a bool, a string
    that isn't a number, NaN and an int too large for a double (OverflowError) are all
    just "no usable distance", which matches nothing."""
    if isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return f if math.isfinite(f) else None


def _corner_apexes(corners) -> list[tuple[str, float | None, str]]:
    """(label, apex distance, direction) per corner that has a designation; direction is
    '' when the corner has none. Corners arrive here from arbitrary JSON, so anything that
    isn't a corner-shaped dict is skipped."""
    if not isinstance(corners, list):
        return []
    return [(lab, _finite(c.get("apex_dist")), str(c.get("direction") or "").lower())
            for c in corners
            if isinstance(c, dict) and (lab := str(c.get("label") or c.get("number") or ""))]


# How far apart two apexes may be and still be the same corner. MEASURED, not argued:
# rebuilding all 11 app-data captures and saving over the shipped maps puts the same
# physical corner 2-47 m away (typically 10-30 m). Run through the red-team's rebuild
# probe, 50 m kept the most notes with none misbound; 30 m dropped real matches on 6 of
# the 11 tracks; requiring the old apex inside the new corner's window as well changed
# nothing; window containment alone misbound a note on Bahrain Paddock.
#   What a distance alone cannot tell apart is two neighbours drifting together (Bahrain
# Endurance T7/T8, 50.2 m apart, both +30 m). Rule 2 below settles that with the corners'
# own direction and by refusing any choice with a second candidate; on the red-team's
# drift harness that took cross-corner misbinds from 0/5/16/92/258 to 0/0/1/4/9 at
# 0/10/20/30/40 m of drift, with no note lost on any real rebuild.
_SAME_CORNER_M = 50.0


def corner_identity_map(old_corners, new_corners) -> dict[str, str]:
    """old corner label -> new corner label, for every corner that is THE SAME
    PHYSICAL CORNER on both sides of a save. The key to #64.

    Two rules, in order, and each new corner is claimed AT MOST ONCE:

      1. Same apex, to 1 dp (the precision every save path writes). The corner did not
         move, whatever it is now called -- the whole relabel case: labels 1,2,3,4
         renumbered to 3,4,5,6 leave every apex where it was, so each note moves with
         its corner instead of rebinding to the corner two places up the lap. These
         claims are made first, so an exact match always beats a tolerant one.
      2. For each old corner still unmatched: its nearest new corner that turns the same
         way -- `direction` is computed geometry the map owns; a missing one never blocks
         -- if that corner is unclaimed, within _SAME_CORNER_M, and UNAMBIGUOUS: it is the
         only free same-way corner within reach, no other unmatched old corner nominates
         it, and no other same-way unmatched old corner is at least as near to it. A
         rebuild moves every apex by tens of metres, and a curator drags one by a few;
         both stay the same corner. It also absorbs the 1 dp rounding boundary (100.04
         and 100.06 round apart). If the nearest corner already belongs to an exact
         match, there is no second choice: a corner whose own position is taken is gone.
         Every nomination is made before any is accepted, so the result never depends on
         the order corners are listed in: a check that ran in loop order let Bahrain
         Endurance's T8 claim T7's corner after T7 had stood aside as ambiguous.

    Deliberately NOT here, because each was a misbind: a label match (#64 itself), and
    a match by position in lap order. Position ignored distance, so deleting a corner
    at 80 m and adding one at 10 m in the same save handed the deleted corner's note to
    a live corner as authored fact -- and since a rebuilt map rarely reproduces an apex
    to 0.1 m, position was doing nearly all the matching on real rebuilds, landing notes
    up to 785 m from their corner. When nothing says which corner is which, the note is
    dropped and logged: a wrong binding is served as fact, a drop is not.

    Unmatched old corners are simply absent, and the caller treats a note that names
    only absent corners as unbindable. Total: never raises, whatever is on disk.
    """
    old, new = _corner_apexes(old_corners), _corner_apexes(new_corners)
    by_apex: dict[float, int] = {}
    for j, (_, apex, _) in enumerate(new):
        if apex is not None:
            by_apex.setdefault(round(apex, 1), j)
    out: dict[str, str] = {}
    claimed: set[int] = set()
    left = []
    for i, (lab, apex, _) in enumerate(old):
        j = by_apex.get(round(apex, 1)) if apex is not None else None
        if j is None or j in claimed:
            left.append(i)
            continue
        claimed.add(j)
        out.setdefault(lab, new[j][0])

    def gap(i: int, j: int) -> float:
        a, b = old[i][1], new[j][1]
        return abs(a - b) if a is not None and b is not None else math.inf

    def same_way(i: int, j: int) -> bool:
        a, b = old[i][2], new[j][2]
        return not (a and b and a != b)

    # ponytail: straight-line lap distance, so a corner jittering across the S/F seam
    # (5 m vs length-3 m) finds no match and its note drops, loudly. Wrap needs the lap
    # length passed in -- add it if a shipped map ever has a corner on the seam.
    def nominee(i: int) -> int | None:
        ways = [j for j in range(len(new)) if same_way(i, j)]
        if not ways:
            return None
        g = min(gap(i, j) for j in ways)
        near = [j for j in ways if gap(i, j) == g]
        if g > _SAME_CORNER_M or len(near) != 1 or near[0] in claimed:
            return None                            # too far, a tie, or already someone's
        if sum(1 for j in ways if j not in claimed and gap(i, j) <= _SAME_CORNER_M) > 1:
            return None                            # two free corners it could be
        return near[0]

    # ponytail: O(leftovers^2); both sides are capped at 100 corners.
    nominated = {i: nominee(i) for i in left}
    for i, j in nominated.items():
        if j is None or sum(1 for v in nominated.values() if v == j) > 1:
            continue                               # nothing, or a corner two old ones want
        if any(gap(k, j) <= gap(i, j) and same_way(k, j) for k in left if k != i):
            continue                               # another is at least as near to it
        out.setdefault(old[i][0], new[j][0])
    return out


def _rebound_key(key: str, targets: list[str], new_labels: set[str]) -> str | None:
    """The key `key`'s note should be filed under after a save, or None if the corners
    it named are all gone. `targets` are the NEW labels of the corners it named, in
    lap order.

    Keeps the authored key whenever keeping it is harmless -- it binds something, and
    everything it binds is a corner it was written about. '1-3' over corners
    relabelled 1/2A/3 still names 1 and 3 and nothing else, so rewriting it would
    churn the author's text for no gain.

    Otherwise it names the wrong corners (or none), so it is REWRITTEN onto the
    corners it was written about. A range is rebuilt by asking the grammar owner to
    expand the candidate and accepting it only if that expansion is exactly the
    targets -- so the rewritten key cannot name a corner the note was not about, and
    no second parse of a label ever happens here (a label is free text; '²' raises on
    int()). When the targets cannot be expressed as one key -- '2-3' whose corners are
    now '2A' and '3A' -- the note narrows onto its first corner rather than being
    deleted: under-serving an authored note is recoverable, deleting it is not.
    """
    if not targets:
        return None
    binds_now = [lab for lab in note_key_labels(key) if lab in new_labels]
    if binds_now and set(binds_now) <= set(targets):
        return key
    if len(targets) == 1:
        return targets[0]
    candidate = f"{targets[0]}-{targets[-1]}"
    return candidate if note_key_labels(candidate)[1:] == targets else targets[0]


def guard_corner_notes(slug: str, old_notes: dict | None, old_corners,
                       new_corners, dropped_out: dict | None = None) -> dict | None:
    """Carry an authored `notes` overlay across a map rebuild (#37, #64).

    Per-corner notes name corners by LABEL, and a save can re-split a chicane,
    renumber the lap, or relabel '2' as '2A'/'2B'. A stale key then rebinds a note to
    a DIFFERENT corner, which get_corner states as owned fact ("Authored note: ...")
    -- worse than losing it. But a note is expensive hand-authored prose (30,445 bytes
    across the shipped maps), so the answer is not to delete on sight either.

    So: identify the corners themselves (`corner_identity_map`, by apex distance -- never
    by label, never by position), move each note onto whatever its corners are called
    NOW, and drop only a note whose corners are genuinely gone. On a collision the note
    already filed under that corner's key keeps it; the note being moved onto it drops. Corner COUNT is not consulted anywhere --
    keying on it is #64: it let a same-count relabel rebind every note on the track,
    and it deleted every note on the track whenever a corner was added elsewhere.
    overview/reference_times/source are not corner-keyed and are always kept.

    A key may name a RANGE ('2-4', '11-13' -- how a complex is annotated, and 16 of
    the 135 shipped note keys), so ask `note_key_labels` what a key names rather than
    testing the raw key against the labels: '2-4' is never itself a label, and
    treating it as one silently deleted every range-keyed note on every save.

    Notes are carried VERBATIM when either side has no corners at all. With no NEW
    corners nothing can be rebound, and a corner-less ribbon save is legitimate (it
    exists to persist the reconstructed surface -- test_mapcapture_save_writes_track
    _json saves one deliberately), so dropping every note there is pure loss. With no
    OLD corners there is nothing to identify a note against; the keys were authored
    against whatever the app displayed, so keeping them is the only non-destructive
    reading. Both arms used to delete instead.

    Moves and drops are logged, with the dropped text, so the author can recover them.
    Pass `dropped_out` to also receive the dropped notes (key -> text) -- the log is not
    somewhere the driver looks, so the server hands them back in the save response.

    Returns the notes block to write, or None when it would be hollow. Shared by every
    writer of a track file -- the server's one `_write_track_json` and the
    promote-to-shipped script -- so no two of them can drift.
    """
    # `notes` reaches the promote path straight out of an arbitrary user JSON file, so
    # a non-dict `corners` is input, not a bug: refuse it as empty rather than raising.
    old = (old_notes or {}).get("corners")
    old_notes_by_key = {str(k): v for k, v in old.items()} if isinstance(old, dict) else {}
    if old and not old_notes_by_key:
        # Truthy but not an object: nothing to carry, so say what was ignored. A
        # promote reads this out of an arbitrary user JSON file, where `"corners":
        # "2-3: brake late"` (a string, not an object) is a plausible typo -- silently
        # accepting it would report success and quietly ship no notes at all.
        logging.getLogger(__name__).warning(
            "track %s: ignored a non-object `notes.corners` (%s, %d chars); per-corner "
            "notes must be an object keyed by corner label. Ignored: %.200r",
            slug, type(old).__name__, len(str(old)), old)
    new_labels = {lab for lab, *_ in _corner_apexes(new_corners)}
    identity = corner_identity_map(old_corners, new_corners)
    moved: dict[str, str] = {}
    # By OLD key: which authored notes actually landed somewhere. Computing the drops by
    # comparing old keys against `kept` -- which holds NEW keys -- silently lost a deleted
    # corner's note whenever a renumber re-filed a different note under the same string
    # (delete T2, T3 becomes '2'), and silently lost a collision's loser the same way.
    landed: set[str] = set()
    if not _corner_apexes(old_corners) or not new_labels:
        kept = dict(old_notes_by_key)             # nothing to rebind from, or onto
        landed = set(old_notes_by_key)
    else:
        # Where each note would land. dict.fromkeys DEDUPES while keeping lap order --
        # two labels of one key can name the same corner once a complex is merged.
        placed = [(key, text, _rebound_key(key, list(dict.fromkeys(
                      identity[lab] for lab in note_key_labels(key) if lab in identity)),
                      new_labels))
                  for key, text in old_notes_by_key.items()]
        # A note that stays under its own key claims it first: on a collision, the note
        # already written about that corner beats one being re-filed onto it.
        taken = {key for key, _, new_key in placed if new_key == key}
        kept = {}
        # AUTHORED ORDER, not sorted: this is what the notes block is re-serialised in,
        # and re-promoting a shipped map from its own file has to be a byte-identical
        # no-op (test_self_promoting_a_shipped_map_is_a_byte_identical_no_op). Order is
        # still deterministic -- it is the file's -- so a collision resolves the same
        # way on every run.
        for key, text, new_key in placed:
            if new_key is None or (new_key != key and new_key in taken):
                continue                          # gone, or another note holds that corner
            kept[new_key] = text
            taken.add(new_key)
            landed.add(key)
            if new_key != key:
                moved[key] = new_key
    dropped = {k: v for k, v in old_notes_by_key.items() if k not in landed}
    if dropped_out is not None:
        dropped_out.update(dropped)
    if moved:
        # Surfaced, not silent: this is authored prose being re-filed under a key the
        # author did not type. Keys only (values are unchanged and already on disk),
        # bounded the same way the drop log is -- a note key off a legacy file can be
        # thousands of characters.
        logging.getLogger(__name__).warning(
            "track %s: moved %d authored per-corner note(s) onto the corners they were "
            "written for, which this save relabelled. Moved (%d shown): %s",
            slug, len(moved), min(len(moved), 20),
            json.dumps({str(k)[:80]: str(moved[k])[:80] for k in sorted(moved)[:20]},
                       ensure_ascii=False))
    if dropped:
        # Loud, and with the text, so the author can recover it from the log -- but
        # BOUNDED. This dumped every dropped note in full, and wrote 4.6 MB in a single
        # warning() call on a probe that stored many notes: a log sink is not a place
        # unbounded user input gets to land. 20 keys with 200 chars of each note is enough
        # to re-author from, and the count stays exact. The bound belongs HERE rather than
        # in the endpoint that found it, because the promote script shares this function
        # and reads `notes` straight out of an arbitrary user JSON file -- a more exposed
        # sink than a localhost PUT with per-field caps in front of it.
        #
        # `str(v)`, because that exposure is exactly why the value is not known to BE a
        # str: `PUT /notes` coerces, the promote path does not, and this function keeps
        # values as-is (only keys are stringified). `v[:200]` then raised on an int/float/
        # bool/None note and a KeyError on a dict -- a 500 on the map-save path for any
        # legacy or hand-edited file -- and on a LIST it sliced 200 ELEMENTS, turning the
        # bound into an amplifier: 25 keys of 1000x10k-char lists logged 40 MB in one
        # call, 8.7x the 4.6 MB this is here to prevent. Sorting keys only, never
        # `.items()`, for the same reason: tuple comparison would reach the values.
        sample = {k: str(dropped[k])[:200] for k in sorted(dropped)[:20]}
        logging.getLogger(__name__).warning(
            "track %s: dropped %d authored per-corner note(s) on map save (%s); "
            "re-author them against the new layout. Dropped (%d shown): %s",
            slug, len(dropped),
            "the corners they name no longer exist, or another note already holds the "
            "corner they moved to",
            len(sample), json.dumps(sample, ensure_ascii=False))
    notes = {**(old_notes or {}), "corners": kept}
    return notes if any(notes.values()) else None
