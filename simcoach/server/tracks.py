"""Track routes (#113): map saves from the editor, authored notes, rotation, the Tracks list."""
from __future__ import annotations

import json
import math

from fastapi import Body, HTTPException

from ..analysis import CORNER_GEOMETRY_KEYS, corner_geometry, geometry_clause, measurable_geometry
from ..corners import in_window
from ..track_lookup import corner_apex_key, fill_blank_labels, track_slug
from ..trackmap import average_racing_line, direction_from_path
from .trackfile import content_rev, map_rev

# Bounds for `PUT /tracks/{slug}/notes`, the one endpoint on the notes path that had
# none: every field there is free text that gets persisted verbatim into the track file
# and carried across every later map save. The numbers are the ones this path already
# uses rather than a new scheme -- 100 is the corner cap in `_persist_map`, 80 is its
# corner-name cap. _MAX_NOTE_CHARS is ~300 words: 2.5x the 120-word overview budget and
# 6x the 50-word per-corner budget the drafting prompt states, so it cannot clip prose
# authored to spec. Over-limit input is REJECTED, never truncated -- silently shortening
# authored notes is the class of bug this path just got fixed for.
_MAX_NOTE_KEYS = 100
_MAX_NOTE_KEY_CHARS = 80
_MAX_NOTE_CHARS = 2000

# DOCUMENT cap, on top of the per-field ones, because 200 fields that each pass their own
# cap still landed a 5 MB body on disk -- 2.5x the single-field abuse the caps above were
# added to stop, just spread across 100 corner keys + 100 reference times -- and every
# later map save re-read and re-wrote it. The per-field caps stay: they name the offending
# field, which one document cap cannot.
#
# UTF-8 BYTES of `json.dumps(notes, ensure_ascii=False)` -- the encoding _write_track_json
# writes (dump_track_json, #145), so the quantity enforced here is the quantity that lands
# on disk (less the indent=2/CRLF whitespace, which is not counted: the derivation below
# is of the compact form). Measuring code points instead would not bound bytes: an
# all-emoji payload measured 62,367 code points, passed, and wrote 744,367 bytes (each
# astral char was then a 12-byte escape; it is now 4 raw UTF-8 bytes).
#   Before #145 the writer escaped non-ASCII and this measured the escaped form. The
# NUMBER did not move and still bounds the same thing, bytes of notes on disk; ASCII
# prose (all the shipped notes) measures identically. What moved is how much non-ASCII
# text fits under it: an accented Latin char costs 2 bytes, not a 6-byte escape, and an
# emoji 4, not 12 -- so up to 3x the non-ASCII characters are now accepted, with the
# on-disk size bounded exactly as before.
#
# The number is the intended ceiling MEASURED, not rounded: the largest shipped map has 30
# corners, so 30 notes at the full 2000-char per-note cap plus overview and source at
# theirs serialises to exactly 64,357 bytes. (The raw content is 64,000 -- quoting, keys,
# colons and commas are the other 357, which is why the constant is not a round number.
# Stating 64,000 would have refused the very document the derivation is built from.)
# For scale, the biggest `notes` block in simcoach/tracks/*.json today is 4,412 bytes
# (longest single note 289, longest overview 706, most corner keys 24; shipped notes are
# pure ASCII, so the encoding does not move that number), making this ~14.6x the real worst
# case. The per-FIELD caps still count code points, which is what the editor shows the
# author -- on astral text the UI's UTF-16 count and those caps disagree, accepted, and
# this cap is what bounds the consequence.
_MAX_NOTES_DOC_BYTES = 64_357


def _utf8(s: str, field: str) -> str:
    # A client string bound for a track file must be encodable: JSON carries a lone
    # surrogate ("\ud800") fine, but the file is raw UTF-8 (#145) and encoding it
    # raises -- a 500 where the input is simply invalid.
    try:
        s.encode("utf-8")
    except UnicodeEncodeError:
        raise HTTPException(422, f"{field} is not valid Unicode text") from None
    return s


def _check_map_rev(data: dict, body: dict) -> None:
    # The map/rotation writers' stale guard (#166), the notes one's twin: the editor sends
    # back the `rev` its map was loaded with. Without it, an editor held open across an
    # on-disk rewrite (16 -> 13 corners) saved its old corner list and silently reverted
    # the file. REQUIRED like the notes rev -- 428 absent, 409 stale -- so no client can
    # write around it; the editor is the only caller of these routes.
    # ponytail: check-then-write, not atomic -- same stance as PUT /notes.
    rev = body.get("rev")
    if not isinstance(rev, str):
        raise HTTPException(428, "map rev required: load the map first and send back its rev")
    if rev != map_rev(data):
        raise HTTPException(409, "this track's map changed on disk since it was loaded; "
                                 "reload it before saving")


def register(app, deps) -> None:
    store = app.state.store
    _track_file_data = deps._track_file_data
    _write_track_json = deps._write_track_json
    _track_rotation = deps._track_rotation
    _invalidate_canon = deps._invalidate_canon
    _map_dirs = deps._map_dirs
    _frozen_outline = deps._frozen_outline
    _clean_lap_channels = deps._clean_lap_channels
    _canonical_map = deps._canonical_map
    _cmap_for_map = deps._cmap_for_map
    _map_payload = deps._map_payload
    _slug_map_key = deps._slug_map_key
    _live_lap_paths = deps._live_lap_paths
    _rebuild_clouds = deps._rebuild_clouds

    @app.get("/tracks/{slug}/map")
    def track_map_by_slug(slug: str):
        # The track's canonical map keyed on its slug -- for the Tracks screen, which
        # selects a track without any completed lap_id to route through /laps/{id}/map.
        # Track files are slug-keyed (same as the notes endpoints), and a shipped
        # layout carries a frozen path, so no car is needed to build it -- and one without
        # (shipped Spa) borrows its driven laps' outline, see _slug_map_key. {} -> the
        # pane shows "no map yet" (a user track with no driven lap).
        if track_slug(slug) != slug:
            raise HTTPException(400, "invalid slug")
        return _map_payload(*_slug_map_key(slug))

    def _persist_map(track: str, car: str, body: dict) -> dict:
        # SAVE the editor's corner map + sectors as shipped owned data. Direction is
        # NEVER trusted from the client -- the CLAUDE.md corner-direction invariant --
        # and is derived from the averaged/frozen path geometry here for any corner
        # that is new or whose apex moved; a corner matched by apex to an existing one
        # keeps its stored direction instead of re-deriving it (#87 -- see the carry-
        # forward below, same identity rule as the baked-geometry carry-forward).
        # Keyed on (track, car); the lap route resolves those from a lap, the slug route
        # passes the slug with no car (a shipped track edits its frozen ribbon path).
        # If the track already has a reconstructed ribbon, its centerline IS the
        # canonical geometry -- keep it (and re-derive direction on it), don't
        # revert path to the racing-line average on a corner/label/section edit.
        existing = _track_file_data(track)
        if not _frozen_outline(track)[0] and existing.get("corners"):
            # Corners with no stored outline (shipped Spa, #82): any outline this editor
            # shows is BORROWED from driven laps. Saving would store their average and
            # re-derive every curated corner's direction from it -- on shipped Spa that
            # flips T19 Bus Stop right -> left and drops the file's provenance notes.
            # Baking an outline into owned data is a reviewed commit, not a side effect of
            # renaming a corner. Both routes reach the same file this way (#87): the lap
            # route resolves (track, car) to the same live name, so the same refusal
            # applies there too, not just the Tracks-screen slug route.
            raise HTTPException(422, "this track's map file has no stored outline (any "
                                     "outline shown is drawn from your laps), so its map "
                                     "can't be saved from here yet")
        keep_ribbon = existing.get("ribbon")
        ep, epd = existing.get("path"), existing.get("path_dist")
        if keep_ribbon and isinstance(ep, list) and ep and isinstance(epd, list) and len(ep) == len(epd):
            path, dist = ep, epd
        else:
            laps = _clean_lap_channels(track, car)
            path, dist = average_racing_line(laps) if laps else ([], [])
            if not path and isinstance(ep, list) and ep and isinstance(epd, list) and len(ep) == len(epd):
                # No laps to re-average (e.g. a slug edit with no driven lap): keep the
                # track's existing frozen path rather than fail. Editing labels/apexes
                # on an owned layout shouldn't require re-driving it.
                path, dist = ep, epd
            if not path:
                raise HTTPException(422, "no clean laps with position telemetry to build an outline")
        length = float(dist[-1])

        raw_corners = body.get("corners")
        if not isinstance(raw_corners, list) or not raw_corners:
            raise HTTPException(422, "corners must be a non-empty list")
        if len(raw_corners) > 100:  # no real circuit exceeds ~30; cap blocks abuse
            raise HTTPException(422, "too many corners")

        corners = []
        seen_numbers = set()
        for c in raw_corners:
            if not isinstance(c, dict):
                raise HTTPException(422, "each corner must be an object")
            try:
                number = int(c["number"])
                apex = float(c["apex_dist"])
            except (KeyError, TypeError, ValueError):
                raise HTTPException(422, "corner requires integer number and numeric apex_dist")
            name = _utf8(str(c.get("name") or f"Turn {number}")[:80], "corner name")  # cap: owned data + coach prompt
            label = _utf8(str(c.get("label") or "")[:8], "corner label")  # displayed designation ("8A"); blank filled below
            if not math.isfinite(apex) or not (0.0 <= apex <= length):
                raise HTTPException(422, f"corner {number}: apex_dist out of range")
            if number in seen_numbers:
                raise HTTPException(422, f"duplicate corner number {number}")
            seen_numbers.add(number)
            # Default a window around the apex if start/end omitted, clamped to
            # the track; ~2.5% of lap length either side is a sane default.
            pad = max(20.0, length * 0.025)
            start = c.get("start_dist")
            end = c.get("end_dist")
            start = float(start) if start is not None else max(0.0, apex - pad)
            end = float(end) if end is not None else min(length, apex + pad)
            if not (math.isfinite(start) and math.isfinite(end)):
                raise HTTPException(422, f"corner {number}: non-finite window")
            start = min(max(start, 0.0), length)  # clamp client values into the track
            end = min(max(end, 0.0), length)
            # start > end is a valid S/F-seam WRAP; only start == end is degenerate.
            if start == end:
                raise HTTPException(422, f"corner {number}: start_dist == end_dist is degenerate")
            if not in_window(apex, start, end):
                raise HTTPException(422, f"corner {number}: apex_dist must be inside the window")
            direction = direction_from_path(path, dist, apex)  # server-derived, never client
            # Optional multi-apex list; keep only finite apexes inside the (wrap-aware)
            # window, else fall back to the single primary apex.
            raw_apexes = c.get("apexes")
            apexes = [apex]
            if isinstance(raw_apexes, list):
                kept = [float(a) for a in raw_apexes
                        if isinstance(a, (int, float)) and math.isfinite(a) and in_window(a, start, end)]
                if kept:
                    apexes = sorted(round(a, 1) for a in kept)
            corners.append({
                "number": number, "label": label, "name": name, "direction": direction,
                "start_dist": round(start, 1), "apex_dist": round(apex, 1),
                "end_dist": round(end, 1), "apexes": apexes,
            })
        corners.sort(key=lambda c: c["apex_dist"])
        try:
            fill_blank_labels(corners, existing)   # unique, never str(number) blindly (#79)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None

        # `sectors`/`sections` are validated by _write_track_json, the one writer, along
        # with every other writer's (#65) -- the same owners (`validated_sectors` /
        # `validated_sections`), the same 422, one call site.

        # Optional display-name override. Not an annotation-merge field, so an
        # end-user's app-data copy can't rename a shipped track (its read still takes
        # the bundled name); a dev/user-map edit lands in the map itself.
        new_name = body.get("name")
        name = (_utf8(new_name.strip()[:80], "name")
                if isinstance(new_name, str) and new_name.strip()
                else _track_file_data(track).get("name"))

        # Per-corner geometry is OWNED, BAKED data (scripts/backfill_corner_geometry.py),
        # not a save-time derivation. CARRY THE EXISTING BAKE FORWARD UNTOUCHED and
        # compute only the metrics a corner has none of -- one the curator just added.
        # Re-deriving what is already baked rewrote it silently, two ways (#62):
        #
        #  - radius_m. The shipped radii were baked in 1559297 against the PRE-CLAMP
        #    corner windows, which were WIDER than today's and reached into the
        #    neighbouring corner. 35254d5 (#30) narrowed them and did not re-bake, so a
        #    stored radius was the tightest point of a window that no longer existed --
        #    and corner_geometry reports the tightest point, so what it captured was the
        #    NEIGHBOUR's. Recomputing here therefore CHANGED the value: 18 corners
        #    across 7 shipped maps moved, Imola T13 67.7 -> 322.1 m (4.8x) on a save
        #    whose only intended change was a corner NAME. radius_m reaches the driver
        #    as owned fact through geometry_clause (#35), so that was a 2nd-gear corner
        #    described as a sweeper.
        #      Where those wide windows came from: on 13 of the 18 they are exactly
        #    THIS function's own `pad = max(20.0, length * 0.025)` default a few dozen
        #    lines up (Imola 4897.57 m -> 122.4; Laguna 3586.2 -> 89.7); the other 5
        #    are asymmetric spans from some earlier writer. NOT mapbuild's _WIN_M,
        #    which is 55.0 and always has been -- do not "correct" the numbers here to
        #    it. So the editor baked against its own over-wide default and #30 then
        #    narrowed it.
        #  - width_m/banking_deg. Laguna's came from the point cloud, not from a stored
        #    ribbon (d69d5ec), so this path has no source for them and dropped all 24.
        #
        # Re-baking is a deliberate curator commit with a reviewable diff (that is what
        # backfill_corner_geometry.py is for), never a side effect of clicking Save.
        # NOTE this only stopped the save path CHANGING the values; the 18 stale radii
        # themselves were re-baked under #73 (`--only=radius_m`), so stored and
        # recomputed now agree on every shipped corner and this is a no-op for radius_m
        # today. It stays because it is what makes the NEXT window edit safe -- #30
        # narrowed 69 windows across 11 maps without re-baking and nothing noticed for
        # months. The guard that would notice now is
        # test_every_shipped_radius_is_measured_on_its_own_corner_window.
        #
        # Matched on APEX DISTANCE -- not label, not number. Both of those are things
        # the editor rewrites out from under the curator: `label` is the free-text box
        # they type in (TrackMapEditor's `lbl-in`), and `number` is reassigned
        # POSITIONALLY by its renumber() on every insert. Keying on either hands a
        # corner the bake measured at a different corner. Measured on Imola: renaming
        # label "13" to "13A" loses T13's whole bake under a label key; inserting one
        # corner makes 1 corner inherit a neighbour's geometry under a label key and 18
        # under a number key, against 0 under an apex key. The apex is WHERE THE METRIC
        # WAS MEASURED, so it is the only identity that means "the same corner".
        #   Corollary, deliberate: MOVING an apex does drop that corner's bake and
        # re-measure it. That is the curator changing the geometry, in a diff they can
        # see -- and it means any corner they reposition self-heals its stale #73
        # radius. Exact at the 1 dp boundary (+0.049 keeps the bake, +0.05 re-measures).
        rib_w = keep_ribbon.get("width") if isinstance(keep_ribbon, dict) else None
        rib_b = keep_ribbon.get("bank") if isinstance(keep_ribbon, dict) else None
        # corner_geometry keys its result by label, and two corners can still share one:
        # fill_blank_labels makes an auto-filled label unique (#79), but a label the
        # curator TYPED is never renamed, so two typed alike still arrive alike. Address
        # the result positionally so a duplicate label cannot collapse two corners into one.
        probe = [dict(c, label=str(i)) for i, c in enumerate(corners)]
        geo = corner_geometry(path, dist, probe, width=rib_w, bank=rib_b)
        # `existing` is the MERGED read, so on an installed build a corner that ships
        # carries the SHIPPED bake here, never the app-data copy's older one (#75).
        prior = {corner_apex_key(pc): pc for pc in (existing.get("corners") or [])}
        for i, c in enumerate(corners):
            baked = prior.get(corner_apex_key(c), {})
            # Direction is owned data too (#87): a corner matched by apex keeps its
            # existing curated direction rather than re-deriving it from the averaged/
            # frozen path, which is ambiguous at a chicane (Spa T19 Bus Stop flips
            # right -> left in the average). Same identity and the same "moved apex
            # re-measures" rule as the geometry carry-forward just below.
            if baked.get("direction") in ("left", "right"):
                c["direction"] = baked["direction"]
            for k in CORNER_GEOMETRY_KEYS:
                # elev_delta_m is never in `geo` here (no pos_y on a corner/label edit),
                # so for it this is the carry-forward it always was.
                v = baked.get(k)
                if not measurable_geometry(k, v):
                    v = geo.get(str(i), {}).get(k)
                if v is not None:
                    c[k] = v
            # The matched corner's key order too (#145): Laguna's bake lists elev_delta_m
            # before width_m, and an unchanged save must not rewrite the file to reorder it.
            corners[i] = {**{k: c[k] for k in baked if k in c}, **c}

        # Start from the existing file, not a fixed key set (#87): any top-level key this
        # function doesn't know about (Spa's `_config_note`/`_distances_note`/`length_m`,
        # or a future one) survives the save. This is also what carries `ribbon` and
        # `built_from` forward unchanged -- an editor save (names, apexes, sections) keeps
        # a map this app BUILT re-buildable from later laps; dropping the mark would lock
        # a lap-built map against every future live-lap rebuild the first time someone
        # names a corner in it. `notes` is excluded: _write_track_json carries the authored
        # overlay across a rebuild (#37/#64) for every writer via its own guard, which only
        # runs when `notes` is ABSENT from `out` -- carrying it forward here would be this
        # caller opting OUT of that guard.
        out = {k: v for k, v in existing.items() if k != "notes"}
        out.update({
            "track": track_slug(track),
            "corners": corners,
            "path": path,
            "path_dist": dist,
            "sectors": body.get("sectors") or [],
            "sections": body.get("sections"),
            "length": length,
            "rotation": _track_rotation(track),  # preserve existing rotation
        })
        if name:
            out["name"] = name
        # Last, so a bad body still gets its own 422; checked against `existing`, the dict
        # this save was built from (#166).
        _check_map_rev(existing, body)
        dropped = _write_track_json(track, out)
        # Invalidate cmap + canonical caches so the next GET returns the new
        # canonical map (now frozen into the file).
        _invalidate_canon(track)
        # A copy: _canonical_map's result is the cached object, and the drops are this
        # save's, not the map's.
        return {**_canonical_map(track, car, force=False), "dropped_notes": dropped}

    @app.put("/laps/{lap_id}/map")
    def save_lap_map(lap_id: int, body: dict = Body(...)):
        # Only the lap's track/car, not its telemetry (#28) -- editing an imported
        # session's layout is a legitimate flow, same as reading it.
        tc = store.lap_track_car(lap_id)
        if tc is None:
            raise HTTPException(404, "lap not found")
        return _persist_map(tc[0], tc[1], body)

    @app.put("/tracks/{slug}/map")
    def save_track_map(slug: str, body: dict = Body(...)):
        # Edit a track's owned layout by slug -- the Tracks-screen editor, no driven
        # lap required (a shipped track edits its frozen ribbon path directly).
        # The #82/#87 no-stored-outline refusal lives in `_persist_map` itself, shared
        # with the lap route below.
        if track_slug(slug) != slug:
            raise HTTPException(400, "invalid slug")
        return _persist_map(slug, "", body)

    @app.post("/tracks/{slug}/draft-notes")
    def draft_notes(slug: str, body: dict = Body(...)):
        # Author-time drafting only: builds a geometry digest, asks the model for a
        # narrative, and RETURNS it -- never persisted here. The human reviews/edits
        # and a separate save endpoint (later) writes to disk.
        track = body.get("track") or slug
        if track_slug(track) != slug:
            raise HTTPException(400, "slug/track mismatch")
        cmap = _cmap_for_map(track)
        if cmap is None or not cmap.corners:
            raise HTTPException(404, "no corner map for this track")
        # Baked per-corner geometry (radius/elevation/width/banking), keyed by label,
        # enriches each line -- omitted silently where a track hasn't been baked.
        file_corners = {str(fc.get("label") or fc.get("number")): fc
                        for fc in (_track_file_data(track).get("corners") or [])}
        digest_lines = []
        for c in cmap.corners:
            lab = c.label or c.number
            line = (f"T{lab} {c.name}: {c.direction}, "
                    f"apex@{c.apex_dist:.0f}m, span {c.start_dist:.0f}-{c.end_dist:.0f}m")
            clause = geometry_clause(file_corners.get(str(lab), {}), c.direction)
            if clause:
                line += ", " + clause
            digest_lines.append(line)
        guides = (body.get("guides") or "").strip()
        system = (
            "You are drafting OWNED, author-time track notes a human will verify. "
            "Return STRICT JSON: {\"overview\": str, \"corners\": {label: str}, \"source\": str}. "
            "Keep the overview under 120 words and each corner note under 50 words. "
            "Ground every claim in the geometry and provided guides; do NOT invent corner "
            "names, numbers, or directions -- those are given.")
        user = "Corner geometry:\n" + "\n".join(digest_lines)
        if guides:
            user += "\n\nExternal guide material (reference):\n" + guides
        raw = app.state.coach.model.complete(system, [{"role": "user", "content": user}])
        try:
            draft = json.loads(raw)
        except (ValueError, TypeError):
            draft = None
        if not isinstance(draft, dict):
            raise HTTPException(502, "model did not return valid notes JSON")
        draft = {"overview": str(draft.get("overview", "")),
                 "corners": {str(k): str(v) for k, v in (draft.get("corners") or {}).items()},
                 "source": str(draft.get("source", ""))}
        return {"draft": draft}

    def _notes_rev(data: dict) -> str:
        # Revision of what a client holding the notes was looking at: the stored notes
        # AND the corner labels they are keyed to. PUT /notes writes its body verbatim --
        # it has to, it is the author typing -- so a copy loaded before a map save that
        # re-keyed the notes (#64) would put every old key back, and the old keys match
        # the new labels, so nothing even flags them as orphans. A relabel that moved no
        # note still changes what a key means, hence the labels.
        # Takes the dict the caller ALREADY read, never re-reads (#158): a rewrite between
        # two reads served the old notes stamped with the new rev, and the unchanged PUT
        # that followed wiped the file's notes.
        corners = data.get("corners")
        labels = [str(c.get("label") or c.get("number")) for c in corners
                  if isinstance(c, dict)] if isinstance(corners, list) else []
        return content_rev({"notes": data.get("notes"), "labels": labels})

    @app.put("/tracks/{slug}/notes")
    def save_notes(slug: str, body: dict = Body(...)):
        # Persist human-edited/reviewed notes onto the map file's overlay (shipped
        # track in dev curation, app-data otherwise -- same split as _write_track_json).
        track = body.get("track") or slug
        if track_slug(track) != slug:
            raise HTTPException(400, "slug/track mismatch")
        raw = body.get("notes") or {}

        def _text(value: object, field: str) -> str:
            s = str(value)
            if len(s) > _MAX_NOTE_CHARS:
                raise HTTPException(422, f"{field} exceeds {_MAX_NOTE_CHARS} characters")
            return _utf8(s, field)

        def _text_map(value: object, field: str) -> dict[str, str]:
            # A non-dict REFUSES, never coerces to empty: this write REPLACES the notes
            # already on disk, so accepting `corners: [["1", "brake late"]]` as {} would
            # return 200 and wipe authored prose. (The same shape on the map-promotion
            # path is correctly coerced -- there a malformed `corners` never held notes,
            # so nothing is lost. The question is always what is on the other side.)
            if value is not None and not isinstance(value, dict):
                raise HTTPException(422, f"{field} must be an object")
            items = list(value.items()) if isinstance(value, dict) else []
            if len(items) > _MAX_NOTE_KEYS:
                raise HTTPException(422, f"too many {field} (max {_MAX_NOTE_KEYS})")
            out = {}
            for k, v in items:
                k = str(k)
                if len(k) > _MAX_NOTE_KEY_CHARS:
                    raise HTTPException(
                        422, f"{field} key exceeds {_MAX_NOTE_KEY_CHARS} characters")
                out[_utf8(k, f"{field} key")] = _text(v, f"{field} note")
            return out

        notes = {"overview": _text(raw.get("overview", ""), "overview"),
                 "corners": _text_map(raw.get("corners"), "corner notes"),
                 "reference_times": _text_map(raw.get("reference_times"), "reference times"),
                 "source": _text(raw.get("source", ""), "source")}
        # Encoded as _write_track_json encodes it (raw UTF-8, see _MAX_NOTES_DOC_BYTES),
        # so this measures the bytes that land on disk. Checked after the per-field caps
        # so an over-limit field still gets its own named error.
        if len(json.dumps(notes, ensure_ascii=False).encode("utf-8")) > _MAX_NOTES_DOC_BYTES:
            raise HTTPException(
                422, f"notes document exceeds {_MAX_NOTES_DOC_BYTES} bytes")
        data = dict(_track_file_data(track))       # merged shipped+overlay
        if not data:
            raise HTTPException(404, "no track file to annotate")
        # REQUIRED, not optional: an optional check leaves every client that omits it free
        # to write stale keys back. 428 when absent, 409 when stale -- the client refetches
        # and tells the driver; it never retries blind.
        # ponytail: check-then-write, not atomic; both sides are one person's clicks on a
        # localhost sidecar. A per-track lock if a second writer ever appears.
        rev = body.get("rev")
        if not isinstance(rev, str):
            raise HTTPException(428, "notes rev required: load the notes first and send back their rev")
        if rev != _notes_rev(data):
            raise HTTPException(409, "these notes changed since they were loaded (a map save "
                                     "can move or drop notes); reload them before saving")
        # An unchanged save must not rewrite the file (#145): GET /notes pads to all four
        # fields, so an EMPTY field the file never had is dropped, not written as "" / {}.
        # A field the file DID have is written even when empty -- that is the author
        # clearing it. Then the file's own field order, and no notes block at all if there
        # was none and nothing was typed.
        prior = data.get("notes") if isinstance(data.get("notes"), dict) else {}
        notes = {k: v for k, v in notes.items() if v or k in prior}
        notes = {**{k: notes[k] for k in prior if k in notes}, **notes}
        if notes or "notes" in data:
            data["notes"] = notes
        dropped = _write_track_json(track, data)    # always {}: the caller's notes are kept
        _invalidate_canon(track)                    # same cache-clear as PUT /laps/{id}/map
        # The rev of what was just written, not a re-read: a rewrite landing after this
        # save must still 409 the next one.
        return {"ok": True, "rev": _notes_rev(data), "dropped_notes": dropped}

    @app.get("/tracks/{slug}/notes")
    def get_notes(slug: str):
        # Merged (shipped+overlay) notes for the editor to pre-fill, normalized to
        # the same 4-field shape PUT persists. No notes (or no track) yet -> all-empty
        # defaults, NOT 404 -- the editor needs a blank-but-valid shape to edit.
        if track_slug(slug) != slug:
            raise HTTPException(400, "invalid slug")
        data = _track_file_data(slug)
        raw = data.get("notes") or {}
        return {"overview": str(raw.get("overview", "")),
                "corners": {str(k): str(v) for k, v in (raw.get("corners") or {}).items()},
                "reference_times": {str(k): str(v) for k, v in (raw.get("reference_times") or {}).items()},
                "source": str(raw.get("source", "")),
                "rev": _notes_rev(data)}   # send it back with PUT; see _notes_rev

    @app.get("/tracks")
    def list_tracks():
        # Coverage listing across bundled (shipped) + user-overlay track dirs, for the
        # track-management UI. Reuses _track_file_data's shipped+overlay merge per slug.
        user_dir, bundled = _map_dirs()
        slugs = set()
        for base in (bundled, user_dir):
            if base.exists():
                slugs.update(p.stem for p in base.glob("*.json"))
        # A track DRIVEN live but never mapped has no .json to glob, so it used to be
        # absent from this list entirely -- and this screen is the only place a map gets
        # built, so there was no entry point for exactly the tracks that need one (#69).
        # Union it in, with the counts the Build action is gated on.
        #
        # `captures` is len(_rebuild_clouds): the laps a Build would actually consume,
        # not laps that merely have a row or a file. Position capture landed partway
        # through this store's history, so pre-position laps carry no pos_* at all --
        # Sebring has 15 live laps and 0 usable, and a DB count there offered "Build map
        # from 15 laps" that then 422'd with the very error #69 exists to remove.
        #
        # `live_laps` is the raw row count, so a row with captures 0 can still say WHY it
        # is there and unbuildable ("none with position data") -- the driver needs to know
        # those laps are unrecoverable and the track wants re-driving (#66).
        #
        # Per-lap position-channel reads are cached on (telemetry_path, mtime) on
        # app.state (#105) -- a re-open only re-reads laps whose file actually changed.
        rows = _live_lap_paths()
        live: dict[str, int] = {}
        names: dict[str, str] = {}
        for slug, name, _p in rows:
            live[slug] = live.get(slug, 0) + 1
            names.setdefault(slug, name)
        slugs.update(live)
        out = []
        for slug in sorted(slugs):
            merged = _track_file_data(slug)
            notes = merged.get("notes") or {}
            clouds, from_capture = _rebuild_clouds(slug, rows, cache=app.state.cloud_check_cache)
            out.append({
                "slug": slug,
                # No map file yet -> the live name the laps were recorded under beats a
                # title-cased slug ("Paul Ricard - ELMS" vs "Paul Ricard Elms").
                "name": (merged.get("name") or names.get(slug)
                         or slug.replace("_", " ").title()),
                "has_map": bool(merged.get("path") or merged.get("corners")),
                "has_notes": bool(notes.get("overview") or notes.get("corners")),
                "user_edited": (user_dir / f"{slug}.json").exists(),
                "captures": len(clouds),
                "live_laps": live.get(slug, 0),
                # A capture that exists and yields nothing blocks the build, so captures is
                # 0 whatever the live laps hold: say so, never "none with position data" (#84).
                "capture_unreadable": from_capture and not clouds,
            })
        return out

    def _persist_rotation(track: str, car: str, body: dict) -> dict:
        # Persist a per-track display rotation (degrees). Merges into the track JSON
        # so corners/path survive. Keyed on (track, car) like _persist_map.
        try:
            degrees = float(body["degrees"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(422, "degrees must be a number")
        if not math.isfinite(degrees):
            raise HTTPException(422, "degrees must be finite")
        data = _track_file_data(track)
        _check_map_rev(data, body)   # before the mutations below (#166)
        if not data.get("corners"):
            # No per-track JSON yet: carry the RESOLVED corners (fallback cmap /
            # auto-detect) into the file so persisting rotation can't zero them.
            cmap = _cmap_for_map(track)
            data["corners"] = [
                {"number": c.number, "label": c.label or str(c.number), "name": c.name,
                 "direction": c.direction, "start_dist": c.start_dist,
                 "apex_dist": c.apex_dist, "end_dist": c.end_dist,
                 "apexes": c.apexes or [c.apex_dist]}
                for c in (getattr(cmap, "corners", None) or [])
            ]
            secs = getattr(cmap, "sections", None)
            if secs and "sections" not in data:
                data["sections"] = secs
        data["rotation"] = degrees % 360.0
        dropped = _write_track_json(track, data)
        _invalidate_canon(track)
        return {**_canonical_map(track, car, force=False), "dropped_notes": dropped}

    @app.post("/laps/{lap_id}/rotation")
    def set_rotation(lap_id: int, body: dict = Body(...)):
        # Body required -> preflight (CSRF), like the mapcapture routes. Track/car
        # only, not telemetry (#28) -- see save_lap_map.
        tc = store.lap_track_car(lap_id)
        if tc is None:
            raise HTTPException(404, "lap not found")
        return _persist_rotation(tc[0], tc[1], body)

    @app.post("/tracks/{slug}/rotation")
    def set_track_rotation(slug: str, body: dict = Body(...)):
        # Rotate a track's map by slug -- the Tracks-screen editor. Body required.
        if track_slug(slug) != slug:
            raise HTTPException(400, "invalid slug")
        return _persist_rotation(slug, "", body)
