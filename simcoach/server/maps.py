"""The per-track canonical map (#113): outline (frozen or averaged), corner map, the cached
screen payload, and which laps a rebuild consumes (#69)."""
from __future__ import annotations

import logging
import os

import numpy as np

from ..corners import CornerMap
from ..track_lookup import track_slug
from ..trackmap import average_racing_line, build_track_map
from .captures import _POS_CHANNELS, _load_cloud, _usable_cloud
from .core import export
from .serialize import _clean
from .trackfile import map_rev

_CLEAN_LAP_RATIO = 1.07  # keep laps within +7% of the track/car best (drop in/out/off laps)


def register(app, deps) -> None:
    store = app.state.store
    _map_for = deps._map_for
    _fresh = deps._fresh
    _curating = deps._curating
    _capture_path = deps._capture_path
    _track_file_data = deps._track_file_data
    _track_rotation = deps._track_rotation
    _is_shipped = deps._is_shipped
    _shipped_extras = deps._shipped_extras

    def _live_lap_paths() -> list[tuple[str, str, str]]:
        # (slug, raw live name, telemetry_path) for every LIVE-captured lap, newest last.
        #
        # telemetry_path IS NOT NULL is the whole distinction: save_result_session rows
        # are summary-only with no file, so a track with ~991 imported laps and no
        # capture is correctly not buildable (#66). Slugged here because laps store the
        # raw live name ("Paul Ricard - ELMS") while a map file is keyed by slug.
        #
        # `partial IS NULL` drops out/in/spin laps (#77): they are stored now, but a
        # pit-lane pass or a half lap is exactly the shape #69's width/banking gate
        # exists to reject, and one guard here covers BOTH the /tracks badge counts
        # and the rebuild that consumes them, so the two can never disagree.
        with store._lock:   # store.py shares one connection with the capture thread
            rows = store.conn.execute(
                "SELECT track, telemetry_path FROM laps "
                "WHERE telemetry_path IS NOT NULL AND track IS NOT NULL "
                "AND partial IS NULL ORDER BY id").fetchall()
        return [(track_slug(n), n, p) for n, p in rows if track_slug(n)]

    def _rebuild_clouds(track: str, rows=None, *, capture: bool = True,
                        cache: dict | None = None) -> tuple[list[dict], bool]:
        # (laps, from_capture): EXACTLY the usable clouds a rebuild of `track` consumes.
        # /tracks counts this and /mapcapture/rebuild builds from it, so the badge and
        # the build cannot disagree (#69) -- e.g. a save-cloud capture of 2 laps beside 7
        # live laps is "2", because the capture is what gets built.
        #
        # A purpose-built mapping capture WINS whenever there is one: its boundary passes
        # cover the full width; racing laps only trace the line driven. Otherwise the
        # position channels off the track's own LIVE laps' .npz files, in the same shape
        # _load_cloud returns. Laps recorded before position capture landed (47 channels,
        # no pos_*) are not usable and so are neither counted nor built from.
        #
        # ([], True) is its own state: a capture EXISTS and yields nothing (corrupt, or no
        # usable lap in it). It still wins -- never a silent fall-back to other data (#69)
        # -- so callers must name it, not read it as "no laps with position data" (#84).
        # capture=False skips the capture, to count what deleting it would leave.
        # cache (#105): an optional {(telemetry_path, mtime): ch} dict, checked/filled
        # per live lap so a re-open only reads files that changed. Only /tracks passes
        # one; the build path leaves it None and is byte-for-byte unaffected.
        slug = track_slug(track)
        if not slug:
            # A map file name with nothing to slug ("---.json" dropped into app-data
            # tracks/) has no capture and no laps. Count it as none -- _capture_path's
            # writer guard would 400 the whole GET /tracks, the same one-bad-file-blanks-
            # the-screen failure R1/R2 fixed for lap files.
            return [], False
        path = _capture_path(track)
        if capture and path.exists():
            return [ch for ch in _load_cloud(path) if _usable_cloud(ch)], True
        # A PENDING capture (#150) is deliberately NOT read here: an abandoned lap would
        # outvote the track's live laps and slip past rebuild's curated-map guard. It is
        # recovered through /mapcapture/start (resume -> Build / Save cloud) only.
        laps = []
        for lap_slug, _name, p in (_live_lap_paths() if rows is None else rows):
            if lap_slug != slug:
                continue
            key = None
            if cache is not None:
                try:
                    key = (p, os.stat(p).st_mtime)
                except OSError:
                    key = None
            if key is not None and key in cache:
                ch = cache[key]
            else:
                try:
                    with np.load(p) as z:
                        ch = {c: z[c] for c in _POS_CHANNELS if c in z.files}
                except Exception as e:
                    # Skip THIS lap, never the listing or the rebuild. Not a narrow tuple:
                    # a zero-byte file raises EOFError and a corrupt member zlib.error, and
                    # either one used to 500 all of GET /tracks. Same stance as _load_cloud.
                    logging.getLogger(__name__).warning("skipping unreadable lap %s: %r", p, e)
                    continue
                if key is not None:
                    cache[key] = ch
            if _usable_cloud(ch):
                laps.append(ch)
        return laps, False

    def _clean_lap_channels(track: str, car: str) -> list[dict]:
        # Channel dicts for full, clean laps of this track+car: skip laps with no
        # telemetry (FileNotFoundError) or no position, drop in/out/off laps
        # (lap_time > best * _CLEAN_LAP_RATIO) and partials (short lap_distance).
        best = store.best_lap(track, car)
        best_t = best.lap_time if best is not None else None
        spans, recs = [], []
        for lap_id in store.list_lap_ids(track, car, complete_only=True):
            try:
                rec = store.load(lap_id)
            except (KeyError, FileNotFoundError):
                continue
            ch = rec.channels
            if "pos_x" not in ch or "pos_z" not in ch or "lap_distance" not in ch:
                continue
            if best_t is not None and rec.lap_time and rec.lap_time > best_t * _CLEAN_LAP_RATIO:
                continue
            ld = ch["lap_distance"]
            if len(ld) < 2:
                continue
            spans.append(float(ld[-1]))
            recs.append(ch)
        if not recs:
            return []
        # Drop partials: keep laps whose distance span is >= 90% of the longest.
        full = max(spans)
        return [ch for ch, sp in zip(recs, spans) if sp >= 0.9 * full]

    def _cmap_for_map(track):
        # Corner map for the map endpoints: the injected resolver/cmap first, else
        # load the shipped track file we own (so a just-saved edit is reflected
        # even when no resolver is wired, e.g. tests / a fresh sidecar).
        m = _map_for(track)
        if m is not None:
            return m
        data = _track_file_data(track)
        if not data.get("corners"):
            return None
        try:
            return CornerMap.from_data(data)
        except (ValueError, KeyError):
            return None

    def _any_pos_lap(track: str, car: str) -> bool:
        # Cheap "does this track+car have ANY position-captured lap" (early-exit,
        # for /status has_map) -- avoids averaging every clean lap just to poll.
        for lap_id in store.list_lap_ids(track, car, complete_only=True):
            try:
                ch = store.load(lap_id).channels
            except (KeyError, FileNotFoundError):
                continue
            if "pos_x" in ch and "pos_z" in ch and "lap_distance" in ch:
                return True
        return False

    def _frozen_outline(track: str):
        # The outline stored in the track file as (path, path_dist), else ([], []).
        data = _track_file_data(track)
        p, pd = data.get("path"), data.get("path_dist")
        if isinstance(p, list) and isinstance(pd, list) and p and len(p) == len(pd):
            return p, pd
        return [], []

    def _canonical_outline(track: str, car: str, force_avg: bool):
        # The track's canonical (path_world, dist): frozen file path first (unless
        # forcing a re-average), else averaged over the track+car's own pos laps,
        # else ([], []). The map is per-track owned data, NOT the selected lap.
        if not force_avg:
            p, pd = _frozen_outline(track)
            if p:
                return p, pd
        laps = _clean_lap_channels(track, car)
        if laps:
            return average_racing_line(laps)
        # Nothing to re-average (a result-log session has no telemetry at all, #28):
        # fall back to the frozen layout rather than returning {}, which the UI
        # reports as "no saved layout" for a track that demonstrably has one.
        return _frozen_outline(track)

    def _has_canonical(track: str, car: str) -> bool:
        if not track:
            return False
        _fresh(track)
        if (track, car) in app.state.canon:
            return True
        data = _track_file_data(track)
        p, pd = data.get("path"), data.get("path_dist")
        if isinstance(p, list) and isinstance(pd, list) and p and len(p) == len(pd):
            return True
        return _any_pos_lap(track, car)

    def _canonical_map(track: str, car: str, force: bool) -> dict:
        # Build (and cache) the per-track canonical screen map. force=True bypasses
        # the frozen file + cache to re-average freshly driven laps (editor use).
        _fresh(track)
        if not force:
            cached = app.state.canon.get((track, car))
            if cached is not None:
                return cached
        # A save that lands while this builds clears `canon` first; caching the payload
        # built from the OLD file would then undo that clear (#155).
        rev = app.state.map_rev
        # The editor's save guard (#166), read BEFORE everything the payload is built
        # from: a rewrite landing mid-build then stamps the payload with the OLDER rev, so
        # a save from it 409s -- never a stale payload carrying the new file's rev.
        file_rev = map_rev(_track_file_data(track))
        path, dist = _canonical_outline(track, car, force_avg=force)
        if not path:
            return {}  # no position data anywhere yet; don't cache (may appear later)
        cmap = _cmap_for_map(track)
        sectors = _shipped_extras(track).get("sectors")
        channels = {
            "pos_x": [pt[0] for pt in path],
            "pos_z": [pt[1] for pt in path],
            "lap_distance": dist,
        }
        payload = _clean(build_track_map(channels, cmap, sectors=sectors,
                                         rotation=_track_rotation(track)))
        payload["rev"] = file_rev
        if not force and app.state.map_rev == rev:
            app.state.canon[(track, car)] = payload
        return payload

    def _map_payload(track: str, car: str, force: bool = False) -> dict:
        # What every map GET returns: the canonical map plus what the editor needs around
        # it. {} stays {} (the pane's "no map yet").
        m = _canonical_map(track, car, force)
        if m:
            # The game's real S1/S2 boundaries (if any lap captured them) so the
            # editor can seed sector splits at the true positions, not thirds.
            # `shipped`: the geometry is a built-in the user may annotate but not rebuild.
            # `curating`: dev (source) build — layout tools + the track name are editable.
            # `track_name`: stored display-name override, else the live/canonical name.
            m = {**m, "detected_sectors": store.sector_bounds(track, car) or [],
                 "shipped": _is_shipped(track), "curating": _curating(),
                 "track_name": _track_file_data(track).get("name") or track}
        return m

    def _live_map_key(track: str, car: str) -> tuple[str, str]:
        # The (track, car) the LIVE session's map is drawn from (#107). Normally
        # (track, car) as-is; but a shipped track with corners and NO frozen outline
        # (shipped Spa predates them, #82) has nothing under a car with no laps yet --
        # borrow the by-slug lookup, which already knows to use the most-recently-
        # driven car's laps for this track (_slug_map_key). Cheap on the 2 s /status
        # poll: with no laps at all for the track, `_slug_map_key` falls through its
        # own DB query returning nothing and `_canonical_map` short-circuits on an
        # empty `_clean_lap_channels` before touching the averager.
        if not track or _has_canonical(track, car):
            return track, car
        if _track_file_data(track).get("corners"):
            return _slug_map_key(track_slug(track))
        return track, car

    def _slug_map_key(slug: str) -> tuple[str, str]:
        # The (track, car) a slug's map is drawn from. Normally (slug, ""): a stored outline
        # needs no car. A track file with corners and NO stored outline (shipped Spa
        # predates them, #82) has nothing to draw by slug -- laps are stored under the live
        # name + car -- so borrow exactly what /laps/{id}/map draws for that track: the
        # clean laps of the car driven there MOST RECENTLY (highest lap id), or of the next
        # most recent car if that one has no clean position lap. Corners still come from
        # the file, and nothing is written.
        # No corners, no fallback: a driven-but-unmapped track keeps {} so the Tracks
        # screen still offers "Build map" (#69) rather than a corner-less outline.
        if _frozen_outline(slug)[0] or not _track_file_data(slug).get("corners"):
            return slug, ""
        with store._lock:   # store.py shares one connection with the capture thread
            rows = store.conn.execute(
                "SELECT track, car FROM laps WHERE telemetry_path IS NOT NULL "
                "AND track IS NOT NULL AND car IS NOT NULL "
                "GROUP BY track, car ORDER BY MAX(id) DESC").fetchall()
        for track, car in rows:
            if track_slug(track) == slug and _canonical_map(track, car, force=False):
                return track, car
        return slug, ""

    export(deps, _live_lap_paths, _rebuild_clouds, _clean_lap_channels, _cmap_for_map,
           _frozen_outline, _canonical_map, _has_canonical, _map_payload, _live_map_key,
           _slug_map_key)
