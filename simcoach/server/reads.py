"""Read routes (#113): sessions, per-lap analysis/inputs/map, the live track's map and
inputs, the live setup (+ its explicit capture), and chats."""
from __future__ import annotations

import logging
from dataclasses import asdict

from fastapi import Body, HTTPException

from ..analysis import analyze_lap, input_traces
from ..tiers import adjustable_from_setup, blank_setup
from .serialize import _clean, _usage_view


def register(app, deps) -> None:
    store = app.state.store
    _map_for = deps._map_for
    _map_payload = deps._map_payload
    _live_map_key = deps._live_map_key
    _ensure_rest = deps._ensure_rest
    _ensure_optimal = deps._ensure_optimal
    status = deps.status

    @app.get("/sessions")
    def sessions(track: str | None = None, car: str | None = None,
                 car_class: str | None = None, offset: int = 0, limit: int = 30):
        page = store.race_sessions_page(track or None, car or None, max(0, offset),
                                        max(1, min(limit, 200)), car_class=car_class or None)
        return {**page, **store.session_facets()}

    @app.get("/sessions/summary")
    def sessions_summary(track: str | None = None, car: str | None = None,
                         car_class: str | None = None):
        return store.session_summary(track or None, car or None, car_class or None)

    @app.get("/sessions/{sid}/laps")
    def session_laps(sid: str):
        return store.session_laps(sid)

    @app.get("/sessions/{sid}/optimal")
    def session_optimal(sid: str):
        # #176 fix round 1: split from /laps so the lap rail's own read never waits on
        # analyze_lap. This always returns instantly -- `_ensure_optimal` schedules a
        # background recompute when stale and hands back whatever is cached right now
        # (None the first time), never the caller's own compute.
        # #176 fix round 2: `pending` tells a caller with nothing cached yet (a
        # session just opened in Sessions review) that a value is on the way, so it
        # knows to poll rather than conclude there's simply no optimal.
        laps = store.session_laps(sid)
        value, pending = _ensure_optimal(sid, laps)
        return {"optimal": value, "pending": pending}

    @app.get("/sessions/{sid}/chats")
    def session_chats(sid: str):
        return store.chats_for_session(sid)

    @app.get("/sessions/{sid}/race")
    def session_race(sid: str):
        return store.race_session(sid) or {}

    @app.get("/laps/{lap_id}/analysis")
    def lap_analysis(lap_id: int):
        try:
            rec = store.load(lap_id)
        except KeyError:
            raise HTTPException(404, "lap not found")
        except FileNotFoundError:
            # The lap exists; it just has no channels to analyze (imported from a
            # result log). Saying "lap not found" sent the user hunting for a lap
            # that is sitting right there in the session list.
            raise HTTPException(404, "lap has no telemetry to analyze")
        cmap = _map_for(rec.track)
        if cmap is None:
            raise HTTPException(404, "no corner map")
        try:
            ref = store.best_lap(rec.track, rec.car, exclude_lap_id=lap_id)
            a = analyze_lap(rec, cmap, reference=ref)
        except Exception:
            raise HTTPException(422, "lap telemetry could not be analyzed")
        return _clean({
            "lap_time": a.lap_time,
            "corners": [asdict(c) for c in a.corners],
        })

    @app.get("/laps/{lap_id}/inputs")
    def lap_inputs(lap_id: int):
        # Lap-keyed input traces for the coaching screen's Inputs panel (#95):
        # selected lap vs its track/car best, both resampled the same way as
        # /track/inputs (the live-keyed version; its Pit Wall caller was retired in #151).
        try:
            rec = store.load(lap_id)
        except KeyError:
            raise HTTPException(404, "lap not found")
        except FileNotFoundError:
            raise HTTPException(404, "lap has no telemetry")
        lap = input_traces(rec)
        best_rec = store.best_lap(rec.track, rec.car, exclude_lap_id=lap_id)
        best = input_traces(best_rec) if best_rec is not None else None
        cmap = _map_for(rec.track)
        corners = [] if cmap is None else [{
            "label": c.label or str(c.number),
            "name": c.name,
            "start_dist": round(float(c.start_dist), 1),
            "apex_dist": round(float(c.apex_dist), 1),
            "end_dist": round(float(c.end_dist), 1),
        } for c in cmap.corners]
        return {"lap": lap, "best": best or None, "corners": corners,
                "length": lap.get("length")}

    @app.get("/laps/{lap_id}/map")
    def lap_map(lap_id: int, avg: int = 0):
        # The map is the track's CANONICAL layout, the same for every lap -- the
        # lap_id only selects the track (+car). avg=0: frozen file, else averaged
        # over the track's own pos laps (cached). avg=1: force a fresh re-average
        # (editor, to pick up newly-driven laps before re-freezing). Only the
        # track/car strings are needed (not the lap's own telemetry), so an older
        # session imported from a result log (no telemetry_path) still resolves
        # an already-mapped track's frozen layout.
        tc = store.lap_track_car(lap_id)
        if tc is None:
            raise HTTPException(404, "lap not found")
        track, car = tc
        return _map_payload(track, car, force=bool(avg))

    @app.get("/track/map")
    def track_map():
        # The current LIVE track's canonical map — for the live rail map, which needs the
        # layout keyed on the track itself (a practice session has no completed lap_id
        # to route through /laps/{id}/map).
        try:
            from ..lmu.smm import read_live_status
            live = read_live_status()
        except Exception:
            live = None
        if not live or not live.get("track"):
            return {}
        return _map_payload(*_live_map_key(live["track"], live["car"]))

    @app.get("/track/inputs")
    def track_inputs():
        # Driver-input traces (throttle/brake/steering), live-keyed (#21; no UI caller since #151):
        # the LAST completed lap and the current track/car BEST, both resampled onto
        # the same uniform lap_distance grid so they overlay directly.
        #
        # UI-ONLY. This is raw ~50 Hz telemetry, downsampled here in the sidecar (not
        # in the Svelte component) and returned to the chart. It never enters a coach
        # prompt -- the model only ever sees the per-corner summary.
        #
        # Keyed on the LIVE track like /track/map: a practice session has no completed
        # lap_id to route through /laps/{id}/..., and `status()` already owns the
        # "latest capture still matches the live session" staleness check.
        st = status()
        if not st["track"]:
            return {"last": None, "best": None}
        latest = app.state.latest if st["lap_id"] is not None else None

        def _trace(rec, lap_id):
            tr = input_traces(rec)
            if not tr:
                return None  # imported/result-log lap: no channels to draw
            return {**tr, "lap_id": lap_id, "lap_time": rec.lap_time}

        last = _trace(latest["record"], st["lap_id"]) if latest else None
        best_rec = store.best_lap(st["track"], st["car"])
        # `best_lap` returns a record, not its id, so identity is by capture timestamp
        # (unique per lap). Same lap -> don't draw the identical trace twice.
        same = (latest is not None and best_rec is not None
                and best_rec.timestamp == latest["record"].timestamp)
        best = None if (best_rec is None or same) else _trace(best_rec, None)
        return {"last": last, "best": best}

    @app.get("/setup")
    def setup():
        # Phase 1: raw dict from LmuRest, unfiltered. Filtering to the
        # human-meaningful subset is deferred to the Setup-panel phase (Phase 4).
        # Read-only: no store write here. A GET must stay safe to prefetch,
        # cache, retry, or replay -- persistence is the explicit POST below
        # (#51: this used to write on every GET).
        rest = _ensure_rest()
        if not rest:
            return {}
        try:
            return rest.get_setup()
        except Exception:
            return {}

    @app.get("/laps/{lap_id}/setup")
    def lap_setup(lap_id: int):
        # #188: the setup this lap was driven on; `setup` null = none recorded.
        row = store.lap_setup(lap_id)
        if row is None:
            raise HTTPException(404, "lap not found")
        return row

    @app.get("/sessions/{sid}/setup")
    def session_setup(sid: str):
        # #188: the setup on the session's fastest lap that has one; {} = none recorded.
        return store.session_fastest_setup(sid) or {}

    @app.post("/setup/capture")
    def setup_capture(body: dict = Body(...)):
        # Continuous capture: persists what the sim CURRENTLY reports via
        # LmuRest, same as the old GET-time capture did -- NOT whatever the
        # caller sent. `body` is unused except to force a non-simple request
        # (the CSRF preflight, same as every other POST here); trusting
        # caller-supplied setup content was a prompt-injection sink, since it
        # lands verbatim in get_last_setup()'s output and the coach reads that
        # as fact. Same isFreeSetting filter as the panel, same session_id
        # guard as before (skip when there's no session_id to key the row on),
        # same best-effort -- never break the caller.
        rest = _ensure_rest()
        if not rest:
            return {"saved": False}
        try:
            data = rest.get_setup()
        except Exception:
            return {"saved": False}
        if not data:
            return {"saved": False}
        try:
            st = status()
            if st["track"] and st["car"] and st["session_id"]:
                free = {k: data[k] for k in adjustable_from_setup(data)}
                if blank_setup(free):   # #188: nothing loaded in the garage yet
                    return {"saved": False}
                store.save_session_setup(st["session_id"], st["track"], st["car"], free)
                return {"saved": True}
        except Exception:
            logging.getLogger(__name__).warning("session setup capture failed", exc_info=True)
        return {"saved": False}

    @app.get("/chats")
    def chats():
        return store.list_chats()

    @app.get("/chats/{chat_id}")
    def chat_messages(chat_id: str):
        return store.chat_messages(chat_id)

    @app.get("/chats/{chat_id}/usage")
    def chat_usage(chat_id: str):
        """Billed tokens + estimated $ for a whole chat, so reopening a thread seeds
        the meter without asking a question. Same shape the `done` frame carries."""
        return _usage_view(store.chat_usage(chat_id))
