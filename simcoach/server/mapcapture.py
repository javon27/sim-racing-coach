"""Mapping capture (#113): drive boundary laps, build a ribbon, save it and its point cloud."""
from __future__ import annotations

from fastapi import Body, HTTPException

from ..model_types import LapRecord
from ..ribbon import multi_signal_direction, reconstruct_surface, ribbon_to_screen
from ..track_lookup import track_slug
from .captures import (
    _POS_CHANNELS,
    _drop_pending,
    _one_track,
    _pending_dir,
    _pending_files,
    _pending_laps,
    _save_cloud,
)
from .core import export
from .serialize import _clean
from .trackfile import map_rev


def register(app, deps) -> None:
    store = app.state.store
    _live_track = deps._live_track
    _track_file_data = deps._track_file_data
    _track_rotation = deps._track_rotation
    _write_track_json = deps._write_track_json
    _capture_path = deps._capture_path
    _newest_capture = deps._newest_capture
    _invalidate_canon = deps._invalidate_canon
    _live_lap_paths = deps._live_lap_paths
    _rebuild_clouds = deps._rebuild_clouds

    def _place_corners(ribbon, edge_channels):
        # First-pass corners from the naive optimal-line solver over the corridor,
        # placed on the centerline; direction is multi-signal (curvature + steering
        # + lat_g). raceline.py is a parallel lane -- absent -> no corners yet.
        try:
            from .. import raceline
        except ImportError:
            return []
        try:
            # No telem= calibration: the slow boundary laps would under-read grip;
            # GT3 defaults are the safer first pass (plan's documented fallback).
            solved = raceline.solve_corners(ribbon["left"], ribbon["right"],
                                            bank=ribbon.get("bank"))
        except Exception:
            return []
        # Place on the centerline by the solver's STATION INDICES (apex_idx/etc),
        # so distances are exactly the centerline dist axis -- no solved-vs-center
        # axis offset baked into the saved file.
        dist = ribbon["dist"]
        length = dist[-1] if dist else 0.0
        n = len(dist)
        out = []
        for c in solved:
            ai = int(c["apex_idx"]) % n
            si = int(c["start_idx"]) % n
            ei = int(c["end_idx"]) % n
            sd, ed = round(dist[si], 1), round(dist[ei], 1)
            # Drop a collapsed (zero-length) window: the solver can emit
            # start==apex==end, and a degenerate corner makes CornerMap.load reject
            # the WHOLE map on reload (so every corner vanishes and the user re-adds
            # them all). start>end is a valid S/F wrap and is kept.
            if sd == ed:
                continue
            apex = dist[ai]
            number = len(out) + 1  # contiguous numbering after any skips
            direction, confidence = multi_signal_direction(
                ribbon["center"], dist, apex,
                edge_channels=edge_channels, lap_length=length,
            )
            out.append({
                "number": number, "label": str(number), "name": f"Turn {number}",
                "direction": direction,
                "apex_dist": round(apex, 1), "apexes": [round(apex, 1)],
                "start_dist": sd,
                "end_dist": ed,
                "confidence": confidence,
            })
        return out

    # CSRF guard for the mutating POSTs: requiring Body(...) means the only
    # content-type that reaches the handler is application/json, which is NOT a
    # CORS "simple request" -- the browser must preflight, and the origin
    # allowlist blocks that. NOTE this depends on FastAPI's strict_content_type
    # default (0.132+), which is why pyproject pins fastapi>=0.132: on older
    # FastAPI a body with an ABSENT content-type is still parsed as JSON, and an
    # absent content-type IS CORS-simple, so the guard silently does nothing.
    @app.post("/mapcapture/start")
    def mapcapture_start(body: dict = Body(...)):
        # Resume the live track's pending capture (#150): laps an interrupted or
        # cancelled session already put on disk are back in the buffer, so status shows
        # them and Build / Save cloud use them. Those laps carry position channels only.
        track = _live_track()
        slug = track_slug(track) if track else ""
        buf = []
        for f, ch in _pending_laps(store.data_dir, slug):
            r = LapRecord("lmu", track, "", None, False, 0.0, ch)
            r.pending_path = f
            buf.append(r)
        app.state.map_buffer = buf
        app.state.last_ribbon = None
        # Unpinned (#154): the first lap committed after start pins the session mapping
        # belongs to, and _capture_loop ends mapping when a later lap's differs. Never
        # `latest`: it is the last lap driven, possibly another track or an earlier
        # race, and pinning it ended a fresh Generate on its first lap.
        app.state.mapping_key = None
        app.state.mapping = True
        return {"active": True}

    @app.post("/mapcapture/cancel")
    def mapcapture_cancel(body: dict = Body(...)):
        # Leaves the pending capture on disk (#150): the editor calls this on unmount
        # too, so a cancel must not be a way to lose driven laps. Next start resumes it.
        # {"discard": true} (the editor's explicit "Discard capture") deletes it: every
        # track in the buffer (the sim may change track mid-session), else the live one.
        # Mapping ends FIRST, whatever the discard finds (#154): a 409 raised before
        # this left mapping on after a sidecar restart, and every qualifying/race lap for
        # 40 min was stamped a mapping lap. No track to resolve discards nothing and SAYS
        # so in `note` -- never a silent no-op the UI reads as "laps deleted".
        app.state.mapping = False
        discard = body.get("discard") is True
        buf = list(app.state.map_buffer)
        app.state.map_buffer = []
        app.state.last_ribbon = None
        if not discard:
            return {"active": False}   # a plain cancel's reply is unchanged
        names = {r.track for r in buf} if buf else {_live_track()}
        slugs = {track_slug(n) for n in names if n} - {""}
        if not slugs:
            return {"active": False, "discarded": 0,
                    "note": "no track to discard a capture for; is the sim running?"}
        discarded = 0
        for slug in slugs:
            d = _pending_dir(store.data_dir, slug)
            discarded += _drop_pending(sorted(d.glob("*.npz")) if d.is_dir() else [])
        return {"active": False, "discarded": discarded}

    @app.get("/mapcapture/status")
    def mapcapture_status():
        # Snapshot once (#16): the capture thread appends to and dels from
        # app.state.map_buffer from another thread, so iterating the live list
        # directly can see it mutate mid-loop.
        buf = list(app.state.map_buffer)
        laps = []
        for i, r in enumerate(buf):
            ld = r.channels.get("lap_distance")
            n = len(ld) if ld is not None else 0
            length = float(max(ld)) if n else 0.0
            laps.append({"idx": i, "samples": n, "length": round(length, 1)})
        return {"active": app.state.mapping, "live_track": _live_track(), "laps": laps}

    @app.post("/mapcapture/build")
    def mapcapture_build(body: dict = Body(...)):
        # Reconstruct from the ENTIRE buffer as a point cloud -- no left/right
        # designation. Body {} (Body(...) still required for the CSRF preflight).
        # Snapshot once (#16): app.state.map_buffer is mutated by the capture
        # thread; a bare reference here would see appends/dels mid-iteration.
        buf = _one_track(app.state.map_buffer)
        if len(buf) < 2:
            raise HTTPException(422, "need at least 2 passes across the width")
        ribbon = reconstruct_surface([r.channels for r in buf])
        if not ribbon:
            raise HTTPException(422, "mapping laps lack position telemetry")
        track = buf[0].track
        # Resumed laps carry position channels only; direction's steering/lat_g votes
        # need a fresh lap, so take the first one that has them.
        corners = _place_corners(ribbon, next((r.channels for r in buf
                                               if "steering" in r.channels),
                                              buf[0].channels))
        extras = _track_file_data(track)
        sectors = extras.get("sectors") or []
        sections = extras.get("sections") or []
        # Pin the track + raw laps NOW: the sim may change track between build and
        # save, and save persists the raw cloud for later rebuilds.
        raw = [{c: r.channels[c] for c in _POS_CHANNELS if c in r.channels} for r in buf]
        app.state.last_ribbon = {"world": ribbon, "corners": corners, "sectors": sectors,
                                 "sections": sections, "track": track, "laps": raw,
                                 "built_from": "capture",
                                 # The rev of the file `sectors`/`sections` came from:
                                 # Save's stale guard (#178).
                                 "rev": map_rev(extras),
                                 # The buffered laps behind `laps` (#150): Save promotes
                                 # exactly these, never a lap driven after Build.
                                 "buffered": buf}
        res = _clean(ribbon_to_screen(ribbon, corners, sectors,
                                      rotation=_track_rotation(track), sections=sections))
        res["track_name"] = _track_file_data(track).get("name") or track
        return res

    @app.post("/mapcapture/save")
    def mapcapture_save(body: dict = Body(...)):
        rib = app.state.last_ribbon
        if not rib:
            raise HTTPException(422, "no built ribbon to save")
        track = rib.get("track")
        if not track:
            raise HTTPException(422, "no track for the built ribbon")
        world = rib["world"]
        # Strip to the CornerMap.load-compatible Corner shape (drops confidence
        # etc.), so the saved file still loads unchanged.
        keys = ("number", "label", "name", "direction", "start_dist", "apex_dist",
                "end_dist", "apexes")
        corners = [{k: c[k] for k in keys if k in c} for c in rib["corners"]]
        existing = _track_file_data(track)
        # Stale guard (#178), the map PUT's twin (#166): Build/Rebuild snapshot the file's
        # sectors and sections into the ribbon, so a map or rotation save landing before
        # this Save was silently reverted. The rev is stamped server-side with that
        # snapshot, so there is no client rev to omit (no 428), and an unstamped ribbon
        # fails closed. Before ANY write, the cloud included.
        # ponytail: check-then-write, not atomic -- same stance as _check_map_rev.
        if rib.get("rev") != map_rev(existing):
            raise HTTPException(409, "this track's map changed on disk since the ribbon was "
                                     "built; build it again before saving")
        # Start from the existing file, like _persist_map (#87, #203): Save replaces the
        # geometry keys below and nothing else, so `pronunciations`, Spa's
        # `_config_note` and any key added later survive a ribbon save.
        # Built from scratch, it deleted every key not listed here. `notes` is left out
        # for _write_track_json's guard -- see the comment under `built_from`.
        out = {k: v for k, v in existing.items() if k != "notes"}
        out.update({
            "track": track_slug(track),
            # Display name from the session (the live track), unless a custom name was
            # already set — so a saved layout carries its track name.
            "name": existing.get("name") or track,
            "corners": corners,
            "ribbon": {k: world[k] for k in ("left", "right", "center", "width", "bank")},
            "path": world["center"],
            "path_dist": world["dist"],
            "sectors": rib["sectors"],
            "sections": rib.get("sections", []),  # preserve existing named sections
            "length": world["dist"][-1] if world["dist"] else 0.0,
            "rotation": _track_rotation(track),  # preserve any existing rotation
        })
        # Describe the geometry just rebuilt: a stale `length_m` beside the new `length`
        # (Spa: 7004 vs 6986.49) and a note on how the old distances were measured.
        # `_config_note` stays -- the only record of driver-confirmed corner facts.
        for k in ("length_m", "_distances_note"):
            out.pop(k, None)
        # Mark maps this app BUILT (from a capture or from driven laps). A live-lap rebuild
        # may replace only a marked map's corners -- an unmarked map with corners is a
        # curated layout (see mapcapture_rebuild).
        if rib.get("built_from"):
            out["built_from"] = rib["built_from"]
        # No `notes` key, deliberately -- see _persist_map. _write_track_json REPLACES
        # the file, and this route used to build its payload from scratch, which is how
        # "Save ribbon" deleted every authored note for the track (#65). The one writer
        # carries the overlay, including on a corner-less rebuild: a thin capture can
        # yield zero solver corners and this route still has a job to do (it exists to
        # persist the reconstructed SURFACE -- see test_mapcapture_save_writes_track_json),
        # and zero new corners cannot rebind a note to the wrong corner.
        # Persist the raw point cloud (app-data, not the repo tracks dir) so the
        # ribbon can be rebuilt after algorithm changes with no re-drive. BEFORE the map
        # (#150): a capture-built map must never land without its cloud, or "Rebuild
        # from saved capture" later fails for a track the driver is sure they recorded.
        if rib.get("laps"):
            _save_cloud(_capture_path(track), rib["laps"])
        elif rib.get("built_from") == "capture" and not _capture_path(track).exists():
            raise HTTPException(422, "the capture this map was built from is gone; "
                                     "use Generate map layout to record one")
        dropped = _write_track_json(track, out)
        # A saved map ends mapping on EVERY path (#154), a rebuild's too (laps None):
        # left on, it stamped every later lap a mapping lap. FIRST, which also shrinks
        # the window for a lap landing mid-promote.
        app.state.mapping = False
        if rib.get("laps"):
            # Promoted: the Build's laps are in captures/<slug>.npz now. Drop only those
            # laps' files: a lap driven after Build stays pending, and in the buffer,
            # for the next Generate map layout.
            built = rib.get("buffered") or []
            _drop_pending(_pending_files(built))
            rib.pop("buffered", None)   # a second Save must not drop a newer lap
            app.state.map_buffer = [r for r in app.state.map_buffer
                                    if not any(r is b for b in built)]
        _invalidate_canon(track)
        return {"saved": True, "slug": track_slug(track), "dropped_notes": dropped}

    @app.post("/mapcapture/save-cloud")
    def mapcapture_save_cloud(body: dict = Body(...)):
        # Persist ONLY the raw point cloud from the active buffer -- no ribbon
        # rebuild, no track-map write. Lets a track that already has a
        # driver-placed corner map (e.g. Laguna) gain a capture for later
        # cross-section extraction (width/banking/elevation) WITHOUT replacing
        # its geometry. See docs/superpowers/specs/2026-09-08-per-corner-
        # geometry-metrics-design.md.
        # Snapshot once (#16): see mapcapture_build's comment above.
        buf = _one_track(app.state.map_buffer)
        if len(buf) < 2:
            raise HTTPException(422, "need at least 2 passes across the width")
        track = buf[0].track
        raw = [{c: r.channels[c] for c in _POS_CHANNELS if c in r.channels} for r in buf]
        if not any(all(c in lap for c in _POS_CHANNELS) for lap in raw):
            raise HTTPException(422, "mapping laps lack position telemetry")
        _save_cloud(_capture_path(track), raw)
        app.state.mapping = False
        _drop_pending(_pending_files(buf))   # promoted (#150): exactly the laps saved
        app.state.map_buffer = []
        app.state.last_ribbon = None
        return {"saved": True, "slug": track_slug(track)}

    @app.post("/mapcapture/rebuild")
    def mapcapture_rebuild(body: dict = Body(...)):
        # Re-reconstruct from a SAVED raw cloud -- no active capture needed. Resolve
        # the target in priority order: explicit {"slug"}, the lap being EDITED
        # ({"lap_id"} -> its track), the live/last track, else the NEWEST saved capture.
        # The edited lap MUST win over the live sim: editing a past Monza session has to
        # rebuild Monza even while Bahrain is the live track.
        # A lap_id that can't be resolved is an ERROR, never a fall-through: it used
        # to swallow every exception (including the result-log lap's missing
        # telemetry, #28) and then rebuild the live/newest track instead -- writing
        # one track's geometry over another's.
        # Every 422 detail below reaches the driver VERBATIM (TrackMapEditor's rebuildGen),
        # so each one says what to do next, not only what went wrong (#84).
        slug = body.get("slug")
        lap_id = body.get("lap_id")
        track = slug
        if not track and lap_id is not None:
            try:
                tc = app.state.store.lap_track_car(int(lap_id))
            except (TypeError, ValueError):
                raise HTTPException(422, "lap_id must be an integer")
            if tc is None:
                raise HTTPException(404, "lap not found")
            track = tc[0]
        if not track:
            track = _live_track() or (app.state.last_ribbon or {}).get("track")
        if not track:
            newest = _newest_capture()
            if newest is None:
                raise HTTPException(422, "no saved capture available; use Generate map "
                                         "layout to record one")
            track = newest.stem  # offline: take the track (slug) from the file name
        rows = _live_lap_paths()
        # No capture file -> the track's own live laps (#69): the reported case, 27 live
        # laps and no captures/<slug>.npz, used to 422 here with no way forward.
        laps, from_capture = _rebuild_clouds(track, rows)
        existing_map = _track_file_data(track)
        curated = bool(existing_map.get("corners") and not existing_map.get("built_from"))
        if not from_capture and curated:
            # Never rebuild a CURATED corner map from driven laps. Its corners are owned
            # data (GP numbering, the names the knowledge notes key on); a solver rebuild
            # saves "Turn 1..N" over them -- the repo file in dev, the coach's corner map in
            # an installed build. Shipped Spa (no stored outline, #82) reached this through
            # "Add map" -> "Rebuild from saved capture". Maps THIS route or a capture build
            # produced carry `built_from`, so driving more laps still improves them; only a
            # map with corners and no such mark (every shipped layout) is refused. A real
            # mapping capture stays the explicit way to regenerate a curated layout.
            raise HTTPException(422, "this track has a curated corner map; rebuilding it "
                                     "from driven laps would replace its corners; use "
                                     "Generate map layout to record a mapping capture instead")
        if not laps and not from_capture:
            raise HTTPException(422, "no saved capture for this track; use Generate map "
                                     "layout to record one")
        if not laps:
            # The capture exists and yields nothing. Still refused (#69), but name the file
            # and the way out (#84). "Delete it" only where deleting it WOULD build: some
            # usable live laps, and no curated map for the live-lap refusal above to hit.
            n = 0 if curated else len(_rebuild_clouds(track, rows, capture=False)[0])
            way_out = (f"delete it to build from your {n} lap{'' if n == 1 else 's'}" if n
                       else "use Generate map layout to record a new one")
            raise HTTPException(422, f"the saved capture for this track can't be read "
                                     f"({_capture_path(track)}); {way_out}")
        # Stage the live NAME the laps were recorded under when there is one, so Save
        # names a new map "Silverstone National Circuit", not its slug. Same slug, so
        # every slug-keyed lookup below is unchanged.
        slug = track_slug(track)
        track = next((n for s, n, _p in rows if s == slug), track)
        ribbon = reconstruct_surface(laps)
        if not ribbon:
            # Not "saved capture ...": the laps may be the track's live laps, not a capture.
            raise HTTPException(422, "couldn't rebuild the track from its position "
                                     "telemetry; use Generate map layout to record a new "
                                     "capture")
        corners = _place_corners(ribbon, laps[0])
        extras = _track_file_data(track)
        sectors = extras.get("sectors") or []
        sections = extras.get("sections") or []
        # "laps": None -- a rebuild never (re)writes captures/<slug>.npz on Save. Its
        # cloud is already on disk: the capture it read, or the live laps themselves.
        # Freezing live laps into a capture made the capture WIN every later rebuild, so
        # laps driven afterwards never reached the map while the badge counted them.
        app.state.last_ribbon = {"world": ribbon, "corners": corners, "sectors": sectors,
                                 "sections": sections, "track": track, "laps": None,
                                 "built_from": "capture" if from_capture else "live_laps",
                                 "rev": map_rev(extras)}   # Save's stale guard (#178)
        res = _clean(ribbon_to_screen(ribbon, corners, sectors,
                                      rotation=_track_rotation(track), sections=sections))
        res["track_name"] = _track_file_data(track).get("name") or track
        # How many laps back this geometry, so the driver knows before committing it:
        # a map off 1-3 clouds is thin (#41's Imola sector splits rest on a single lap).
        res["lap_count"] = len(laps)
        return res

    export(deps, mapcapture_status)
