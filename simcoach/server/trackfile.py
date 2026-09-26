"""Track JSON files (#113): the one validated writer (#65) and the merged shipped+overlay reads."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import tempfile

from fastapi import HTTPException

from ..analysis import CORNER_GEOMETRY_KEYS, measurable_geometry
from ..corners import validated_sections, validated_sectors
from ..track_lookup import dump_track_json, guard_corner_notes, load_map_data, track_slug
from .core import _in, export


def content_rev(obj) -> str:
    # THE revision hash for a client's copy of track-file data (notes #158, map #166):
    # the server refuses a write whose base rev no longer matches the file. Always hash
    # the dict the caller ALREADY read, never a re-read (#158).
    blob = json.dumps(obj, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def map_rev(data: dict) -> str:
    # Rev of the map an editor holds (#166): the whole merged track file EXCEPT `notes`.
    # A notes save leaves it alone -- every map writer carries the notes forward itself
    # (_write_track_json's guard) -- while any other rewrite (another map save, rotation,
    # a curation script) makes a copy loaded before it stale.
    return content_rev({k: v for k, v in data.items() if k != "notes"})


def register(app, deps) -> None:
    _map_dirs = deps._map_dirs
    _curating = deps._curating

    def _write_track_json(track: str, out: dict) -> dict:
        # Returns the authored per-corner notes this write dropped (key -> text), which
        # every route hands back to the client: a drop that only reaches the log is a drop
        # the driver never hears about.
        #
        # THE one writer of a track JSON file, and therefore the one place the rules for
        # what may land in one live (#65). There used to be four writers and three
        # validators each wired to one of them: `_persist_map` validated sections,
        # sectors and the notes overlay, and the other three wrote whatever they were
        # handed -- which is how POST /mapcapture/save silently deleted every authored
        # note for a track (30,445 bytes / 135 notes across the shipped maps were one
        # "Save ribbon" away), and how an unvalidated `sections` span could commit a map
        # that CornerMap.from_data refuses to open, killing the track for every client.
        # A shared validator only two of four writers call is a half-done
        # centralisation; validate HERE, where nothing can write around it.
        #
        # Atomic write (unique tmp + os.replace) so a crash mid-write never leaves a
        # partial file. Dev (running from source) curates the shipped maps in the repo
        # tracks dir; an installed build writes to app-data — its bundled dir is
        # read-only/ephemeral (a PyInstaller onefile extracts it to a temp dir wiped on
        # exit, so a write there would silently vanish). Bytes come from dump_track_json,
        # the serializer scripts/promote_map.py uses too (#145), so an app save of a
        # shipped map is byte-identical to its committed file; its ValueError on
        # NaN/Infinity hard-fails the write.
        slug = track_slug(track)
        if not slug:
            raise HTTPException(400, "invalid track")
        dropped: dict = {}
        out = _validated_track_data(track, slug, out, dropped)
        user_dir, bundled = _map_dirs()
        target = _in(bundled if _curating() else user_dir, slug)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            payload = dump_track_json(out)
        except UnicodeEncodeError:
            # Backstop for any client string the routes don't check (section names...):
            # a lone surrogate is bad input, not a server fault. Caught before its
            # ValueError base, so NaN still hard-fails.
            raise HTTPException(422, "track data is not valid Unicode text") from None
        fd, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".json.tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(payload)
            os.replace(tmp, target)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        return dropped

    def _track_file_data(track: str) -> dict:
        # Merged map dict: authoritative shipped geometry + the user's annotation
        # overlay (corners/sections/rotation). {} if we have neither. Never raises.
        user_dir, bundled = _map_dirs()
        return load_map_data(track, str(bundled), str(user_dir))

    def _validated_track_data(track: str, slug: str, out: dict, dropped: dict) -> dict:
        """Everything that must be true of a track JSON, applied to what a writer is
        about to persist. Called only from `_write_track_json`, so every writer gets it
        (#65) -- do not call it from a route, and do not write around it.

        Refuses (422) what would commit a map the app cannot open: `CornerMap.from_data`
        raises on a bad `sections` span, so an unvalidated one kills the track for every
        client, and `sectors` reach the client as-is (strings and `true` both used to
        land in a shipped file). Per-corner geometry is DROPPED rather than refused:
        it is derived, re-bakeable data, and a string in `radius_m` is a crash in
        `geometry_clause` on the coach's next answer -- refusing would make the editor
        the only way to fix a file the editor then cannot save.

        The authored `notes` overlay is carried across the rewrite unless the caller
        passes its own (only `PUT /tracks/{slug}/notes` does -- it IS the author typing,
        including keys that name no corner yet, which the Tracks screen surfaces). So a
        writer that rebuilds a track file gets the guard for free, and one that seeds its
        payload from `_track_file_data` keeps what was already there.
        """
        existing = _track_file_data(track)
        out = dict(out)
        length = out.get("length") or out.get("length_m")
        if not measurable_geometry("length", length):
            # No usable length to bound a span against (a corner-less ribbon save writes
            # 0.0). Shape, type and ordering still hold; only the upper bound relaxes.
            length = math.inf
        try:
            sectors = validated_sectors(out.get("sectors") or [], length)
            sections = validated_sections(out.get("sections"), length)
        except (TypeError, ValueError) as e:
            raise HTTPException(422, str(e)) from None
        # Only write the key back if it was there or has content: a validated empty list
        # is not a reason to add `"sections": []` to a file that never had one.
        for key, value in (("sectors", sectors), ("sections", sections)):
            if key in out or value:
                out[key] = value
        if isinstance(out.get("corners"), list):
            out["corners"] = [_clean_corner_geometry(slug, c) for c in out["corners"]]
        if "notes" not in out:
            notes = guard_corner_notes(slug, existing.get("notes"),
                                       existing.get("corners"), out.get("corners"), dropped)
            if notes:
                out["notes"] = notes
        # Keep the existing file's top-level key order (#145): the notes guard above
        # re-adds `notes` at the END (after `pronunciations`), and /mapcapture/save builds
        # its dict from scratch, so without this an unchanged save reorders the file.
        # Keys the file never had follow, in the writer's order.
        ordered = {k: out[k] for k in existing if k in out}
        ordered.update(out)
        return ordered

    def _clean_corner_geometry(slug: str, corner: dict) -> dict:
        # Strip a baked per-corner metric that isn't a finite number of the right sign.
        # `measurable_geometry` is the same owner the save path uses to decide whether a
        # stored metric is a measurement (#62), so "what may be written" and "what may be
        # carried forward" cannot disagree. A string radius loads fine and then raises
        # `Unknown format code 'f'` inside geometry_clause -- on the coach's answer, far
        # from the save that wrote it, and no test over the shipped maps caught it.
        if not isinstance(corner, dict):
            return corner
        bad = [k for k in CORNER_GEOMETRY_KEYS
               if k in corner and not measurable_geometry(k, corner[k])]
        if not bad:
            return corner
        # Types, not values: the value is junk by definition, and a str() of it is
        # unbounded input on its way into a log line.
        logging.getLogger(__name__).warning(
            "track %s corner %.40s: dropped unusable baked geometry %s; each must be a "
            "finite number (a length also > 0).",
            slug, str(corner.get("label") or corner.get("number")),
            {k: type(corner[k]).__name__ for k in bad})
        return {k: v for k, v in corner.items() if k not in bad}

    def _track_rotation(track: str) -> float:
        # Per-track display rotation (degrees), 0 if unset/invalid (back-compat).
        r = _track_file_data(track).get("rotation", 0.0)
        return float(r) if isinstance(r, (int, float)) else 0.0

    def _is_shipped(track: str) -> bool:
        # True when the track has an authoritative geometry map shipped in the install
        # dir. The client hides layout tools (regenerate/rebuild) for these — a user
        # may re-annotate a built-in track but not replace its layout.
        slug = track_slug(track)
        if not slug:
            return False
        _, bundled = _map_dirs()
        return (bundled / f"{slug}.json").exists()

    def _shipped_extras(track: str) -> dict:
        # sectors/length from the shipped track file (if any) -- the map builder's
        # passthrough. Missing file/keys -> {}.
        data = _track_file_data(track)
        out = {}
        if isinstance(data.get("sectors"), list):
            out["sectors"] = data["sectors"]
        for k in ("length", "length_m"):
            if isinstance(data.get(k), (int, float)):
                out["length"] = float(data[k])
                break
        return out

    export(deps, _write_track_json, _track_file_data, _track_rotation, _is_shipped,
           _shipped_extras)
