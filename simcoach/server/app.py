"""Localhost FastAPI adapter over the synchronous core. Handlers are plain def
(FastAPI runs them in a threadpool), matching the blocking store/coach. Binds to
127.0.0.1 only, validates the Host header (DNS-rebinding defense), and disables
the docs/openapi surface. Reuses store/coach/analysis unchanged."""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

from fastapi import Body, FastAPI, HTTPException, Request, Response, WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

from .. import config, logging_setup, paths, secrets
from ..analysis import (
    analyze_lap,
    input_traces,
)
from ..car_lookup import resolve_car
from ..car_notes import load_car_notes, load_setup_concepts
from ..coach_tools import (
    _PARTIAL_LIST_WORD,
    CoachContext,
    _lap_age,
    current_race_fact,
    race_result_text,
    race_turn_note,
    scrub_stored_answer,
)
from ..corners import next_corner
from ..driver import DriverProfile
from ..format import format_laptime
from ..lmu.ingest import ingest_all
from ..model.base import TokenUsage
from ..model.factory import CoachUserError, make_model
from ..pronunciations import table_for
from ..race import RaceTracker, live_race
from ..tiers import adjustable_from_setup, legal_adjustments
from ..track_lookup import (
    track_slug,
)
from . import core, mapcapture, maps, security, status_routes, trackfile, tracks
from .captures import (
    _append_pending_lap,
    _drop_pending,
    _load_cloud,
    _save_cloud,
)
from .core import _in
from .security import ALLOWED_HOSTS, SIDECAR_PORT, _bad_origin
from .serialize import _clean, _usage_view

# Imported from here by __main__, tests and scripts; defined in the sibling modules (#113).
__all__ = [
    "SIDECAR_PORT",
    "_append_pending_lap",
    "_capture_loop",
    "_drop_pending",
    "_in",
    "_load_cloud",
    "_save_cloud",
    "create_app",
]

# Shipped corner-map dir, resolved off the package (not cwd) so it's correct
# wherever the sidecar is launched from. simcoach/server/app.py -> simcoach/tracks.
_DEFAULT_TRACKS_DIR = Path(__file__).resolve().parent.parent / "tracks"

_EVENTS_PERIOD = 1.0  # s: /events push loop period (module constant so tests can monkeypatch it)


def _clamp_tts_cache_mb(voice_cfg: dict) -> int:
    # A hand-edited config file must never break speech (#135): fall back to
    # the default on anything non-numeric, then clamp to a sane range.
    try:
        mb = int(voice_cfg.get("tts_cache_mb", 200))
    except (TypeError, ValueError):
        mb = 200
    return max(0, min(mb, 5000))


def _race_commit(app_state, store, lap, lap_id) -> None:
    """Refresh the race tools' cached snapshot for a stored lap and keep its battle rows
    (#152). A failure costs the race tools one lap of freshness, never a lap of capture
    -- logged, not raised (an exception inside a pool future would vanish silently)."""
    try:
        map_for = getattr(app_state, "map_for", None)
        snap = app_state.race.commit(lap, map_for(lap.track) if map_for else None)
        if snap is not None and snap.battle is not None:
            store.save_battle(lap_id, snap.battle.laps)
    except Exception:
        logging.getLogger(__name__).warning("race snapshot not refreshed", exc_info=True)


def _capture_loop(source, store, app_state) -> None:
    """One pass over `source.laps()`: save each completed lap to `store` and
    record it on `app_state.latest` so /status and the lap list see it live.
    Returns when `source.laps()` is exhausted or raises -- the caller
    (build_app) wraps this in a retry loop so a real LMU source reconnects
    after a drop."""
    for lap in source.laps():
        # Mapping mode: buffer the lap for ribbon reconstruction AND persist it,
        # tagged phase="mapping" so it gives an immediately-editable lap + shows in
        # the session/lap list, but best_lap excludes it from coaching signals.
        if getattr(app_state, "mapping", False):
            # A partial lap (#77) is still persisted and still numbered, but never
            # buffered for ribbon reconstruction -- mapping wants full boundary passes.
            # Uncapped (#150): the old 8-lap cap silently dropped driven passes.
            # ponytail: RAM grows ~4.4 MB/lap (104 channels x 5284 samples at Spa;
            # _place_corners reads lap 0's); trim to _POS_CHANNELS past lap 0 if marathon
            # mapping sessions matter.
            if lap.partial is None:
                try:
                    # Which file backs this lap, so Save promotes exactly the laps it
                    # saved (a lap driven after Build stays pending).
                    lap.pending_path = _append_pending_lap(store.data_dir, lap)
                except Exception:
                    # Never kill capture over it: the lap is still in RAM and the store.
                    logging.getLogger(__name__).warning(
                        "couldn't write mapping lap to the pending capture", exc_info=True)
                app_state.map_buffer.append(lap)
            lap.phase = "mapping"
        lap_id = store.save(lap)
        app_state.latest = {"record": lap, "lap_id": lap_id}
        if getattr(app_state, "race", None) is not None:
            # #152: refresh the race tools' snapshot OFF this thread -- source.laps() is
            # suspended while this body runs, so work here would delay the next poll.
            try:
                app_state.race_pool.submit(_race_commit, app_state, store, lap, lap_id)
            except RuntimeError:
                # The pool is shut down (app/interpreter exit): skip the snapshot,
                # never stop capture over it.
                logging.getLogger(__name__).warning("race pool closed; snapshot skipped")


def create_app(store, coach, cmap=None, rest=None, map_resolver=None, rest_factory=None,
               tracks_dir=None, *, transcriber=None, installer=None,
               kokoro_installer=None, kokoro_engine=None) -> FastAPI:
    app = FastAPI(title="SimCoach", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    app.state.coach = coach
    # The whisper model lives at the app-data ROOT (next to the config), not in the
    # telemetry dir the store owns; move an install from the first release across.
    from .. import paths as _paths
    from ..tts_cache import TtsCache
    from ..tts_kokoro import EngineUnavailable, KokoroEngine, Unpronounceable, gpl_free
    from ..voice import (
        KokoroInstaller,
        ModelInstaller,
        NotInstalled,
        VoiceInputError,
        WhisperTranscriber,
        decode_wav,
        kokoro_dir,
        migrate_model_dir,
    )
    voice_root = str(_paths.app_data_dir())
    migrate_model_dir(store.data_dir, voice_root)
    app.state.transcriber = transcriber or WhisperTranscriber(voice_root)
    app.state.voice_installer = installer or ModelInstaller(voice_root)
    app.state.kokoro_installer = kokoro_installer or KokoroInstaller(voice_root)
    app.state.kokoro_engine = kokoro_engine or KokoroEngine(kokoro_dir(voice_root))
    app.state.kokoro_installer.voices_fn = app.state.kokoro_engine.voices
    app.state.tts_cache = TtsCache(
        Path(voice_root) / "tts-cache",
        _clamp_tts_cache_mb(config.load_config()["voice"]) * 1024 * 1024,
    )
    app.state.tracks_dir = Path(tracks_dir) if tracks_dir else _DEFAULT_TRACKS_DIR
    # #105: GET /tracks' per-lap usable-cloud check, keyed on (telemetry_path, mtime).
    # Only /tracks reads this (via _rebuild_clouds(..., cache=...)) -- the build path
    # calls _rebuild_clouds with no cache and is unaffected.
    app.state.cloud_check_cache: dict[tuple[str, float], dict] = {}
    app.state.cmap = cmap            # fallback map when no resolver is wired
    app.state.rest = rest            # cached live REST client once connected
    app.state.rest_probed_at = 0.0   # time.monotonic() of the last liveness probe
    app.state.map_resolver = map_resolver  # (track) -> CornerMap | None
    app.state.rest_factory = rest_factory  # () -> LmuRest (probed on first use)
    app.state.cmaps = {}             # per-track CornerMap cache (only non-None cached)
    app.state.canon = {}             # per-(track,car) canonical map payload cache
    app.state.live_cmap = None       # (live track, CornerMap|None) for /live's next corner
    app.state.latest = None  # set by the capture thread (build_app): {"record", "lap_id"} | None
    app.state.race = RaceTracker()   # #152: the race tools' snapshot, one per lap commit
    # One worker: commits are processed in lap order, and never on the capture thread.
    app.state.race_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="race-state")
    app.state.mapping = False        # mapping-capture mode: buffer laps, don't persist
    app.state.map_buffer = []        # LapRecords captured while mapping (transient)
    app.state.last_ribbon = None     # last built ribbon {world, corners, sectors} for save

    deps = SimpleNamespace()   # helpers each module publishes for the next (#113)
    core.register(app, deps)
    _map_for = deps._map_for
    _invalidate_canon = deps._invalidate_canon
    _ensure_rest = deps._ensure_rest
    _live_track = deps._live_track
    _live_session_id = deps._live_session_id
    trackfile.register(app, deps)
    _track_file_data = deps._track_file_data
    maps.register(app, deps)
    _rebuild_clouds = deps._rebuild_clouds
    _cmap_for_map = deps._cmap_for_map
    _map_payload = deps._map_payload
    _live_map_key = deps._live_map_key

    security.install(app)
    status_routes.register(app, deps)
    _status_view = deps._status_view
    status = deps.status
    tracks.register(app, deps)
    mapcapture.register(app, deps)
    mapcapture_status = deps.mapcapture_status

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

    @app.websocket("/coach")
    async def coach_ws(ws: WebSocket):
        # HTTP Host-guard middleware does NOT run for websockets — check here.
        host = (ws.headers.get("host") or "").split(":")[0].lower()
        if host not in ALLOWED_HOSTS or _bad_origin(ws.headers.get("origin")):
            await ws.close(code=1008)      # reject before accept
            return
        await ws.accept()

        # Read + validate the one request message. receive_json raises ValueError on
        # non-JSON; a non-dict body has no .get — guard both, don't crash the socket.
        try:
            req = await ws.receive_json()
        except WebSocketDisconnect:
            return
        except ValueError:
            await ws.send_json({"type": "error", "message": "invalid JSON"})
            await ws.close(code=1003);  return
        if not isinstance(req, dict):
            await ws.send_json({"type": "error", "message": "expected a JSON object"})
            await ws.close(code=1003);  return
        question = (req.get("question") or "").strip()
        if not question:
            await ws.send_json({"type": "error", "message": "empty question"})
            await ws.close(code=1003);  return

        voice = req.get("voice")
        voice = voice if voice in ("ptt", "conversation") else None

        # Coerce lap_id ONCE (used for load AND persisted context stamp).
        lap_id = req.get("lap_id")
        try: lap_id = int(lap_id) if lap_id is not None else None
        except (TypeError, ValueError): lap_id = None
        session_id = req.get("session_id")

        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()   # unbounded; localhost token rate is the ceiling
        _SENTINEL = object()

        def _emit(ev):
            # Worker may outlive a client disconnect / the test loop; swallow a
            # closed-loop RuntimeError so it dies quietly instead of tracebacking.
            try: loop.call_soon_threadsafe(q.put_nowait, ev)
            except RuntimeError: pass

        def worker():
            # ponytail: no cancellation on client disconnect — worker runs the
            # (billed) call to completion regardless. Fine for a localhost
            # single-user app; a stop-flag through the tool loop is deferred.
            #
            # Blocking I/O (store.load, rest.get_setup) lives here, off the event
            # loop, so a hung LMU REST call can't freeze the whole server.
            eff_session_id = session_id
            setup = {}
            rest = _ensure_rest()
            if rest:
                try: setup = rest.get_setup()
                except Exception: setup = {}
            # Orient the coach to the CURRENT track/car/session/map WITHOUT handing it a
            # lap. A pinned lap made the coach fixate on the UI selection; instead it
            # fetches laps itself via list_recent_laps / get_lap_summary as the
            # conversation needs. We still resolve the environment so corner tools and
            # the reference lap work. Priority: live session -> most-recent lap ->
            # the lap the client is viewing (for track/car only, never pinned).
            track = car = cmap = None
            car_class = None
            on_track = None
            try:
                from ..lmu.smm import read_session_track_car
                live = read_session_track_car()          # (track, vehicle) | None
            except Exception:
                live = None
            race_snap = race_note = None
            if live:
                on_track = True
                track, vehicle = live
                try:
                    # #152: only THIS race's snapshot -- never the last race's, from the
                    # garage or the next race's formation lap.
                    from ..lmu.live import read_field_snapshot
                    sess = read_field_snapshot().session
                    race_snap, race_note = live_race(
                        app.state.race.snapshot, track=track, car=vehicle,
                        phase=sess.phase, current_lap=sess.current_lap)
                except Exception:
                    race_snap = race_note = None
                try:
                    from ..track_lookup import resolve_context
                    cmap, car, _, car_class = resolve_context(
                        rest, str(app.state.tracks_dir), track, vehicle,
                        user_dir=str(Path(store.data_dir) / "tracks"))
                except Exception:
                    cmap, car = _map_for(track), vehicle
                # #108 item 5: REST didn't have a class (no REST, or no carClass on
                # the payload) -- fall back to the scraped result-log table, still
                # without a second REST call.
                if car_class is None:
                    try:
                        from ..car_lookup import resolve_car_class
                        car_class = resolve_car_class(vehicle)
                    except Exception:
                        car_class = None
            if track is None and app.state.latest is not None:
                lr = app.state.latest["record"]
                track, car = lr.track, resolve_car(lr.car)
                eff_session_id = eff_session_id or lr.session_id
                cmap, on_track = _map_for(track), False
            if track is None and lap_id is not None:
                try:
                    sr = app.state.store.load(lap_id)
                    track, car = sr.track, resolve_car(sr.car)
                    eff_session_id = eff_session_id or sr.session_id
                    cmap, on_track = _map_for(track), False
                except Exception:
                    pass
            # #137: no live SMM, no in-process latest lap, no client-supplied lap_id
            # either -- e.g. LMU sitting in the lobby after a sidecar restart, asked
            # for post-race analysis. Fall back to the store: the client's viewed
            # session first, then whatever lap is newest overall.
            if track is None:
                row = store.latest_lap_row(session_id) or store.latest_lap_row()
                if row is not None:
                    track, car = row["track"], resolve_car(row["car"])
                    eff_session_id = eff_session_id or row["session_id"]
                    cmap, on_track = _map_for(track), False
            # Backfill the current stint id from the latest lap when the client didn't
            # supply one (auto-anchor) — the live path above resolves track/car but not
            # the session stamp. Only when that lap still matches the live track/car
            # (same rule as _status_view's matches_live); on a track change the latest
            # lap is from the OLD track, so use the live session instead (#127).
            if eff_session_id is None and app.state.latest is not None:
                lr = app.state.latest["record"]
                if live is None or (lr.track, lr.car) == live:
                    eff_session_id = lr.session_id
                else:
                    eff_session_id = _live_session_id()
            # No lap captured yet (e.g. a question asked in the garage): stamp the
            # chat with the CURRENT live session so it isn't orphaned — the laps that
            # follow join the same instance and the chat stays resumable via Sessions.
            if eff_session_id is None:
                eff_session_id = _live_session_id()
            cfg = config.load_config()
            ctx = CoachContext(app.state.store, record=None, lap_id=None, cmap=cmap,
                               units=cfg.get("units", "metric"),
                               setup=setup, track=track, car=car, on_track=on_track,
                               allowed_adjustments=(legal_adjustments(setup) if setup else None),
                               session_id=eff_session_id,
                               car_notes=load_car_notes(
                                   car, user_dir=str(Path(store.data_dir) / "cars")),
                               setup_concepts=load_setup_concepts(),
                               car_class=car_class,
                               # #152: only the race being driven now (live_race).
                               race=race_snap, race_note=race_note)
            car = ctx.car or "car"

            # Concise always-on profile tier: static prefs from settings + the active
            # driver_summary only — NOT the full memory list (that's a pull tool, later
            # task). Best-effort: a missing/empty summary just means no learned_notes.
            d = cfg.get("driver") or {}
            try:
                summary = app.state.store.get_driver_summary()
            except Exception:
                summary = {}
            notes = [summary["text"]] if summary.get("status") == "active" and summary.get("text") else []
            driver_profile = DriverProfile(
                braking_style=d.get("braking_style") or None,
                balance_preference=d.get("balance_preference") or None,
                risk_tolerance=d.get("risk_tolerance") or None,
                learned_notes=notes,
            )

            # Thread the chat's prior turns so the coach continues the conversation
            # instead of treating each question standalone (the system prompt tells it
            # "the messages above are what you and the driver already said").
            history: list[dict] = []
            last_chat_ts: float | None = None   # #131: this chat's last-turn timestamp
            chat_id_in = req.get("chat_id")
            if chat_id_in:
                try:
                    messages = app.state.store.chat_messages(chat_id_in)
                except Exception:
                    messages = []
                last_session = messages[-1]["session_id"] if messages else None
                if last_session and eff_session_id and last_session != eff_session_id:
                    # This chat's last turn belongs to a different race session --
                    # continuing it here would let one chat span sessions (#97, the
                    # Monza WRT bug). Start a fresh chat instead, same as a request
                    # that sent no chat_id at all -- history stays empty, and
                    # save_chat below persists under the new id.
                    chat_id_in = app.state.store.create_chat()
                else:
                    try:
                        # #136: which race each earlier turn was about. A chat is meant
                        # to span the races of a sitting (they share one session id), so
                        # without this an answer about race 1 re-enters the context as
                        # the model's own prior reasoning about the race being driven now.
                        race_windows = ctx._race_windows()
                        for row in messages:
                            if row.get("question"):
                                history.append({"role": "user", "content": row["question"]})
                            if row.get("answer"):
                                # scrub_stored_answer, not the raw row: an older answer can
                                # assert a fabricated grip reading (#72) or a race start the
                                # coach could not know (#67) as established fact, and
                                # resuming the chat would re-feed it to the model as its own
                                # prior reasoning.
                                history.append({"role": "assistant",
                                                "content": race_turn_note(row["ts"], race_windows)
                                                + scrub_stored_answer(row["answer"],
                                                                      row["ts"],
                                                                      row.get("track"))})
                        last_chat_ts = messages[-1]["ts"] if messages else None
                    except Exception:
                        history = []

            # #131: "that lap"/"how was that" should resolve to a lap that landed
            # after this chat's last turn without the coach asking which one. Only
            # meaningful when there WAS a last turn (last_chat_ts set above) --
            # compares stored ts, no LLM guess.
            new_since_last_turn: str | None = None
            if eff_session_id and last_chat_ts is not None:
                sess_laps = app.state.store.session_laps(eff_session_id)
                if sess_laps:
                    latest = sess_laps[-1]
                    if latest["ts"] > last_chat_ts:
                        label_by_id, _ = ctx._lap_numbering()
                        label = label_by_id.get(latest["id"]) or f"lap {latest['id']}"
                        # A partial/out/in lap has no time: name its kind instead.
                        detail = (format_laptime(latest["lap_time"]) if latest.get("lap_time")
                                  else _PARTIAL_LIST_WORD.get(latest.get("partial"), "no time"))
                        new_since_last_turn = (
                            f"{label} ({detail}) landed {_lap_age(latest['ts'])}.")
            # The facts ride in the USER turn, not the system prompt: the system block is
            # prompt-cached and a per-turn value there would bust the cache every question.
            # The stored question (below) stays the driver's own words.
            # #136's CURRENT RACE line leads: it frames every number in the turn, including
            # the lap the NEW SINCE line names. Both are silent when there is nothing to say.
            facts = [f for f in (current_race_fact(ctx._race_windows()),
                                 f"NEW SINCE YOUR LAST ANSWER: {new_since_last_turn}"
                                 if new_since_last_turn else "") if f]
            model_question = "\n\n".join([*facts, question])

            text = ""
            # What this turn billed. One usage event arrives per model call, so a
            # tool-using question contributes several — summing them here is what
            # makes the ×N tool-loop re-send visible instead of invisible.
            turn_usage = TokenUsage()
            used_model: str | None = None
            try:
                for ev in app.state.coach.ask_agentic_stream(
                        ctx, model_question, car=car, input_device=req.get("input_device"),
                        history=history, units=ctx.units, driver_profile=driver_profile,
                        voice=voice):
                    if ev["type"] == "usage":
                        # Keep the per-turn breakdown server-side (noise on the wire);
                        # the client gets one cumulative figure on `done`. The model id
                        # comes from the event, i.e. what actually billed — NOT from
                        # config, which may have changed since the model was resolved.
                        turn_usage = turn_usage + TokenUsage(**ev["usage"])
                        used_model = ev.get("model") or used_model
                        continue
                    if ev["type"] == "text":
                        text += ev["text"]       # accumulate streamed deltas into the full answer
                    _emit(ev)
                terminal = {"type": "done", "answer": text}
            except Exception as e:
                # Surface our own curated user-facing errors verbatim; hide any
                # other internal error text (and generic RuntimeError/ValueError)
                # from the client.
                msg = str(e) if isinstance(e, CoachUserError) else "coach error"
                terminal = {"type": "error", "message": msg}
            # Persist best-effort, but only when there's a real turn to keep —
            # skip the row when an error produced no text, so failed turns don't
            # pollute history with an empty answer.
            if terminal["type"] == "done" or text:
                try:
                    chat_id = chat_id_in or app.state.store.create_chat()
                    stored_question = ("🎤 " + question) if voice else question
                    app.state.store.save_chat(chat_id, ctx.session_id, ctx.track, ctx.car, ctx.lap_id,
                                              stored_question, text, ctx.tool_calls,
                                              usage=turn_usage.as_dict(), model=used_model)
                    terminal["chat_id"] = chat_id
                    # Report the CHAT cumulative total, read back from the store after
                    # the insert: the number the driver sees is then the persisted
                    # truth rather than a drifting in-memory counter, and it survives
                    # a sidecar restart. Still inside the swallowing try — a pricing
                    # or aggregate failure costs the meter, never the answer.
                    terminal.update(_usage_view(app.state.store.chat_usage(chat_id)))
                    terminal["model"] = used_model
                    # Soft long-chat nudge: the core decides, the UI only renders it.
                    # A SOFT signal — nothing is summarized, windowed or truncated;
                    # the driver chooses whether to start a New chat. 0 disables it.
                    soft = cfg.get("chat_token_soft_limit") or 0
                    terminal["long_chat"] = bool(soft) and terminal["usage"]["total"] >= soft
                except Exception as e:
                    # Still best-effort (the answer must reach the driver either way),
                    # but no longer silent: a failure here loses the turn from history
                    # AND leaves the meter showing the pre-turn total with no sign
                    # anything went wrong (#5). Log it, and tell the client so the UI
                    # can show "usage unavailable" instead of a stale number.
                    logging.getLogger(__name__).warning("coach turn not persisted", exc_info=True)
                    terminal["usage_error"] = str(e)
            _emit(terminal)
            _emit(_SENTINEL)

        threading.Thread(target=worker, daemon=True).start()
        try:
            while True:
                ev = await q.get()
                if ev is _SENTINEL:
                    break
                await ws.send_json(ev)
        except (WebSocketDisconnect, RuntimeError):
            pass          # client left mid-stream; worker still finishes + persists (known ceiling)
        finally:
            if ws.client_state == WebSocketState.CONNECTED:
                try: await ws.close()
                except Exception: pass

    _PROFILE_PREF_KEYS = ("braking_style", "balance_preference", "risk_tolerance")
    _LEARNING_STATUSES = ("active", "pending", "retired")

    def _profile_view() -> dict:
        d = config.load_config().get("driver") or {}
        return {
            "prefs": {k: d.get(k) for k in _PROFILE_PREF_KEYS},
            "summary": app.state.store.get_driver_summary(),
            "memories": {
                "active": app.state.store.list_learnings(status="active"),
                "pending": app.state.store.list_learnings(status="pending"),
            },
        }

    @app.get("/profile")
    def get_profile():
        return _profile_view()

    @app.put("/profile")
    def put_profile(body: dict = Body(...)):
        prefs = body.get("prefs") or {}
        driver = {k: prefs[k] for k in _PROFILE_PREF_KEYS if k in prefs}
        config.save_config({"driver": driver})  # merges over existing driver keys (see config.save_config)
        return _profile_view()

    @app.put("/profile/summary")
    def put_profile_summary(body: dict = Body(...)):
        status = body.get("status") or "active"
        if status not in _LEARNING_STATUSES:
            raise HTTPException(422, "invalid status")
        app.state.store.set_driver_summary(str(body.get("text", "")).strip(), status)
        return app.state.store.get_driver_summary()

    @app.post("/profile/memories")
    def add_profile_memory(body: dict = Body(...)):
        text = str(body.get("text", "")).strip()
        if not text:
            raise HTTPException(422, "text is required")
        mid = app.state.store.add_learning(text, status="active", source="manual",
                                            track=body.get("track"), car=body.get("car"))
        return {"id": mid, "text": text, "status": "active",
                "track": body.get("track"), "car": body.get("car")}

    @app.put("/profile/memories/{learning_id}")
    def update_profile_memory(learning_id: int, body: dict = Body(...)):
        if "text" in body:
            text = str(body["text"]).strip()
            if not text:
                raise HTTPException(422, "text must not be empty")
            app.state.store.update_learning(learning_id, text)
        if "status" in body:
            status = body["status"]
            if status not in _LEARNING_STATUSES:
                raise HTTPException(422, "invalid status")
            app.state.store.set_learning_status(learning_id, status)
        return {"ok": True}

    @app.post("/profile/distill")
    def distill_profile(body: dict = Body(...)):
        # On-demand only, never on the hot chat path: assembles a deterministic
        # digest from this session's laps/arc/chat, then asks the model to PROPOSE
        # memories + an updated summary (mirrors /tracks/{slug}/draft-notes). Every
        # proposal lands as status='pending' -- approval is a separate PUT, and the
        # single-row driver_summary is never touched here (that would clobber the
        # driver's currently-active summary), so the proposed summary text is
        # returned only, not persisted.
        session_id = body.get("session_id")
        if not session_id:
            raise HTTPException(422, "session_id is required")
        laps = app.state.store.session_laps(session_id)
        if not laps:
            raise HTTPException(404, "no laps for that session")
        track, car = laps[0]["track"], laps[0]["car"]

        digest = [f"Session {session_id} -- track {track}, car {car}, {len(laps)} lap(s)."]
        # Via the shared owner, never a local arc read: an unscoped session_position_arc
        # here called the first captured lap of ANY phase a starting position, which is
        # the #67 defect the coach tool was fixed for. One wording, one place.
        race = race_result_text(app.state.store, session_id)
        digest.append(race)

        cmap = _cmap_for_map(track)
        if cmap and cmap.corners:
            best = app.state.store.best_lap(track, car, session_id=session_id)
            weak_counts: dict = {}
            for row in laps:
                if row.get("partial"):
                    continue   # an out/in/spin lap is not evidence of a weak corner (#77)
                try:
                    record = app.state.store.load(row["id"])
                except Exception:
                    continue
                analysis = analyze_lap(record, cmap, reference=best)
                for cm in analysis.corners:
                    if cm.min_spd_delta is not None and cm.min_spd_delta < -1.0:
                        weak_counts[cm.corner_no] = weak_counts.get(cm.corner_no, 0) + 1
            recurring = [f"T{k} slower than best in {v} lap(s)"
                        for k, v in sorted(weak_counts.items(), key=lambda kv: -kv[1]) if v >= 2]
            if recurring:
                digest.append("Recurring weak corners: " + "; ".join(recurring))

        chats = app.state.store.chats_for_session(session_id)
        if chats:
            for m in app.state.store.chat_messages(chats[0]["chat_id"])[-6:]:
                if m.get("question"):
                    digest.append(f"Driver asked: {m['question']}")
                if m.get("answer"):
                    # Scrub the SLICE, not the answer: the retraction appends at the
                    # end, so scrubbing first then truncating would drop the correction
                    # and keep the fabricated claim. Scrubbing what actually reaches the
                    # model is also exactly right -- a claim past char 200 never gets
                    # there, so it needs no retraction (#72).
                    digest.append("Coach answered: "
                                  + scrub_stored_answer(m["answer"][:200], m["ts"],
                                                        m.get("track")))

        system = (
            "You are distilling durable driver-profile observations from one session's "
            "data. Return STRICT JSON: {\"memories\": [str, ...], \"summary\": str}. Each "
            "memory is one short, durable observation about THIS DRIVER (not the car or "
            "track), grounded only in the digest below -- do not invent facts. summary is "
            "an updated 1-2 sentence always-on profile blurb.")
        raw = app.state.coach.model.complete(system, [{"role": "user", "content": "\n".join(digest)}])
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            parsed = None
        if not isinstance(parsed, dict):
            raise HTTPException(502, "model did not return valid distill JSON")

        memories = [str(m).strip() for m in (parsed.get("memories") or []) if str(m).strip()]
        created = []
        for text in memories:
            mid = app.state.store.add_learning(text, status="pending", source="distill",
                                                track=track, car=car, session_id=session_id)
            created.append({"id": mid, "text": text, "status": "pending", "track": track, "car": car})
        return {"memories": created, "summary": str(parsed.get("summary", "")).strip()}

    _LIVE_PERIOD = 0.25  # ~4 Hz; the rail map doesn't need capture-rate updates

    @app.websocket("/live")
    async def live_ws(ws: WebSocket):
        # HTTP Host-guard middleware does NOT run for websockets — check here
        # (same preamble as /coach).
        host = (ws.headers.get("host") or "").split(":")[0].lower()
        if host not in ALLOWED_HOSTS or _bad_origin(ws.headers.get("origin")):
            await ws.close(code=1008)
            return
        await ws.accept()

        from ..lmu.live import read_field_snapshot

        def _next_corner_view(dist: float) -> dict | None:
            # #151: the rail map's "Next: T10 Pouhon (L)". Corner identity is owned
            # data, so it is resolved HERE from the live track's corner map, never in
            # the UI. Resolved once per track (this runs at 4 Hz); _invalidate_canon
            # drops the cache when a map is saved.
            track = _live_track()
            cached = app.state.live_cmap
            if cached is None or cached[0] != track:
                cached = app.state.live_cmap = (track, _cmap_for_map(track) if track else None)
            c = next_corner(cached[1].corners, dist) if cached[1] else None
            if c is None:
                return None
            # A generic "Turn 10" name only repeats the label ("Next: T10 Turn 10").
            name = "" if c.name.strip().lower() == f"turn {c.label}".lower() else c.name
            return {"label": c.label, "name": name, "direction": c.direction}

        def _live_tick() -> dict:
            # Blocking mmap + track-file reads: run off the event loop. Trimmed to what
            # the Coach rail map draws (#151): the player's position and next corner.
            # The full field stays available to the core via read_field_snapshot()
            # (the race-state tools, #152) -- it just isn't streamed to the UI.
            snap = read_field_snapshot()
            me = next((c for c in snap.cars if c.is_player), None)
            if not snap.session.live or me is None:
                return {"live": False, "player": None, "next_corner": None}
            return {"live": True, "player": {"lap_dist": me.lap_dist, "cls": me.cls},
                    "next_corner": _next_corner_view(me.lap_dist)}

        loop = asyncio.get_running_loop()
        try:
            while True:
                await ws.send_json(await loop.run_in_executor(None, _live_tick))
                await asyncio.sleep(_LIVE_PERIOD)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            if ws.client_state == WebSocketState.CONNECTED:
                try: await ws.close()
                except Exception: pass

    @app.websocket("/events")
    async def events_ws(ws: WebSocket):
        # Replaces the frontend's status/laps/map/mapcapture/voice-status pollers
        # (#106) with one push. Same host/origin preamble as /coach and /live.
        host = (ws.headers.get("host") or "").split(":")[0].lower()
        if host not in ALLOWED_HOSTS or _bad_origin(ws.headers.get("origin")):
            await ws.close(code=1008)
            return
        await ws.accept()

        log = logging.getLogger(__name__)
        _MISSING = object()
        last: dict = {}  # per-connection change-detection state

        def _events_tick() -> list[tuple[str, dict]]:
            # Runs off the event loop (blocking store/smm/disk reads). Each topic is
            # independently guarded: a broken one is logged and skipped, never taking
            # `status` (the client's liveness signal) down with it.
            frames: list[tuple[str, dict]] = []
            status_data = _status_view()
            frames.append(("status", status_data))  # every tick

            try:
                sid = status_data["session_id"]
                laps = store.session_laps(sid) if sid else []
                sig = (sid, len(laps), laps[-1]["id"] if laps else None)
                if last.get("_laps_sig", _MISSING) != sig:
                    last["_laps_sig"] = sig
                    frames.append(("laps", {"session_id": sid, "laps": laps}))
            except Exception:
                log.warning("events: laps tick failed", exc_info=True)

            try:
                track, car = status_data["track"], status_data["car"]
                key = (track, car)
                if last.get("_map_key", _MISSING) != key:
                    # Only on a track/car change: _map_payload re-reads the track file
                    # and the store's sector bounds, which is not 1 Hz work.
                    last["_map_key"] = key
                    frames.append(("map", _map_payload(*_live_map_key(track, car)) if track else {}))
            except Exception:
                log.warning("events: map tick failed", exc_info=True)

            try:
                was_mapping = last.get("_mapcapture_active", False)
                if app.state.mapping or was_mapping:
                    mc_data = mapcapture_status()
                    sig = json.dumps(mc_data, sort_keys=True, default=str)
                    if last.get("_mapcapture_sig") != sig:
                        last["_mapcapture_sig"] = sig
                        frames.append(("mapcapture", mc_data))
                    last["_mapcapture_active"] = app.state.mapping
            except Exception:
                log.warning("events: mapcapture tick failed", exc_info=True)

            for inst in (app.state.voice_installer, app.state.kokoro_installer):
                try:
                    vs_data = inst.status()
                    engine = vs_data.get("engine", "whisper")
                    installing_key, sig_key = f"_voice_installing_{engine}", f"_voice_sig_{engine}"
                    was_installing = last.get(installing_key, False)
                    if vs_data.get("installing") or was_installing:
                        sig = json.dumps(vs_data, sort_keys=True, default=str)
                        if last.get(sig_key) != sig:
                            last[sig_key] = sig
                            frames.append(("voice_setup", vs_data))
                        last[installing_key] = vs_data.get("installing")
                except Exception:
                    log.warning("events: voice_setup tick failed", exc_info=True)

            return frames

        loop = asyncio.get_running_loop()
        try:
            while True:
                frames = await loop.run_in_executor(None, _events_tick)
                for topic, data in frames:
                    await ws.send_json({"topic": topic, "data": data})
                await asyncio.sleep(_EVENTS_PERIOD)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            if ws.client_state == WebSocketState.CONNECTED:
                try: await ws.close()
                except Exception: pass

    def _settings_view() -> dict:
        try:
            from importlib.metadata import version
            app_version = version("simcoach")
        except Exception:
            app_version = "dev"
        cfg = config.load_config()
        # LMU build: read live from the Steam appmanifest; persist the last-seen value
        # so it still shows when Steam/the game dir isn't readable at this moment.
        from ..lmu import install as lmu_install
        live_build = lmu_install.lmu_build()
        if live_build and live_build != cfg.get("lmu_build"):
            config.save_config({"lmu_build": live_build})
            cfg["lmu_build"] = live_build
        cfg.update({
            "api_key_set": secrets.has_api_key(),
            "data_dir": str(paths.app_data_dir()),
            "app_version": app_version,
            "lmu_build": cfg.get("lmu_build"),
        })
        return cfg

    @app.get("/settings")
    def get_settings():
        return _settings_view()

    _MAX_SETTINGS_BODY_BYTES = 64 * 1024  # #10: no legitimate settings body is anywhere close

    def _whitelist_settings(body: dict) -> dict:
        # Whitelist to config.DEFAULTS keys before saving — this is the
        # secret-leak guard: an injected "key"/"api_key" field can never
        # reach the JSON file on disk. A DEFAULTS value that's itself a dict
        # (voice, driver) is whitelisted the SAME way one level down, or
        # {"voice": {"api_key": "..."}} would pass straight through whole and
        # land in the file via config.save_config's nested merge (#10).
        out = {}
        for key, default in config.DEFAULTS.items():
            if key not in body:
                continue
            v = body[key]
            if isinstance(default, dict):
                if isinstance(v, dict):
                    out[key] = {sk: v[sk] for sk in default if sk in v}
            else:
                out[key] = v
        return out

    @app.put("/settings")
    def put_settings(body: dict = Body(...)):
        if len(json.dumps(body).encode("utf-8")) > _MAX_SETTINGS_BODY_BYTES:
            raise HTTPException(413, "settings body too large")
        whitelisted = _whitelist_settings(body)
        config.save_config(whitelisted)
        if "log_level" in whitelisted:
            logging_setup.apply_log_level(whitelisted["log_level"])
        return _settings_view()

    @app.post("/settings/api-key")
    def set_api_key(body: dict = Body(...)):
        key = (body.get("key") or "").strip()
        if not key:
            raise HTTPException(400, "key is required")
        secrets.set_api_key(key)
        return {"api_key_set": True}

    @app.post("/settings/test-key")
    def test_key(body: dict = Body(...)):
        # Body(...) required as a CSRF guard (same mechanism as /mapcapture/*,
        # see comment above them). Sharper here than elsewhere: this handler
        # makes a real, BILLED Anthropic call, so a cross-origin page could loop
        # it with mode:"no-cors" and burn the user's API credit without ever
        # needing to read a response.
        try:
            cfg = config.load_config()
            model = make_model(model=cfg.get("model"))
            model.complete("ping", [{"role": "user", "content": "ping"}])
            return {"ok": True, "error": None}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    VOICE_MAX_BODY = 8_000_000   # bytes; a 30 s 16 kHz mono WAV is ~1 MB

    @app.post("/voice/transcribe")
    async def voice_transcribe(request: Request):
        # Raw WAV body (not JSON). Localhost-only + the Host/Origin middleware already
        # gate this; a cross-origin no-cors POST could only cost local CPU.
        # Reject on the DECLARED size before buffering it (#12) -- the post-read
        # check below stays too, for a chunked body with no Content-Length.
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > VOICE_MAX_BODY:
                    raise HTTPException(413, "audio body too large")
            except ValueError:
                pass  # not a valid integer -- let the post-read check catch it
        data = await request.body()
        if len(data) > VOICE_MAX_BODY:
            raise HTTPException(413, "audio body too large")
        try:
            pcm, seconds = decode_wav(data)
        except VoiceInputError as e:
            raise HTTPException(422, str(e))
        loop = asyncio.get_running_loop()
        try:
            # whisper is CPU-bound for ~1-3 s: keep the event loop (and /status polls) free
            text = await loop.run_in_executor(None, app.state.transcriber.transcribe, pcm)
        except NotInstalled as e:
            raise HTTPException(409, str(e))
        return {"text": text, "seconds": seconds}

    @app.post("/voice/setup")
    def voice_setup(body: dict = Body(...)):
        # Body(...) forces a JSON content-type -- CSRF guard, same mechanism as /ingest.
        started = app.state.voice_installer.start()
        return {"started": started, **app.state.voice_installer.status()}

    @app.get("/voice/status")
    def voice_status():
        return app.state.voice_installer.status()

    @app.post("/voice/kokoro/setup")
    def kokoro_setup(body: dict = Body(...)):
        started = app.state.kokoro_installer.start()
        return {"started": started, **app.state.kokoro_installer.status()}

    @app.get("/voice/kokoro/status")
    def kokoro_status():
        return {**app.state.kokoro_installer.status(), "gpl_free": gpl_free()}

    @app.post("/voice/speak")
    async def voice_speak(body: dict = Body(...)):
        text = str(body.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "text is required")
        eng = app.state.kokoro_engine
        if not eng.available():
            raise HTTPException(409, "Kokoro voice isn't installed — enable it in Settings")
        voice_in, speed_in, track_in = body.get("voice"), body.get("speed"), \
            body.get("track_slug") or body.get("track")

        def _synthesize() -> bytes:
            # Prelude (disk config read, blocking SMM read, disk track reads) AND
            # synthesis all run in the executor together, off the event loop (#13
            # -- this fires once per spoken utterance while /live pushes at 4 Hz).
            cfg = config.load_config()["voice"]
            voice = str(voice_in or cfg.get("kokoro_voice") or "af_heart")
            speed_raw = speed_in or cfg.get("tts_rate") or 1.0
            try:
                speed = float(speed_raw)
            except (TypeError, ValueError):
                raise ValueError(f"speed must be a number, got {speed_raw!r}") from None
            raw_slug = track_in or _live_track() or ""
            slug = track_slug(raw_slug) if raw_slug else ""
            # table_for always runs, even with no track resolved: the global lexicon
            # (#118) must still apply -- the coach can talk about a track before a lap
            # exists (the Imola bug: "Autodromo"/"Enzo"/"Ferrari" spelled out pre-lap).
            table = table_for(_track_file_data(slug) if slug else {})
            cache_mb = _clamp_tts_cache_mb(cfg)
            if not cache_mb:
                return eng.synthesize(text, voice, speed, table)
            cache = app.state.tts_cache
            cache.cap_bytes = cache_mb * 1024 * 1024
            key = cache.key(text, voice, speed, table)
            cached = cache.get(key)
            if cached is not None:
                return cached
            wav = eng.synthesize(text, voice, speed, table)
            cache.put(key, wav)
            return wav

        loop = asyncio.get_running_loop()
        try:
            wav = await loop.run_in_executor(None, _synthesize)
        except ValueError as e:
            raise HTTPException(422, str(e))
        except Unpronounceable as e:
            # Dict detail (vs. the bad-speed 422's plain string above) so the client can
            # tell the two apart and fall back to Windows speech instead of erroring.
            raise HTTPException(422, {"error": "unpronounceable", "words": e.words})
        except EngineUnavailable as e:
            raise HTTPException(503, str(e))
        return Response(content=wav, media_type="audio/wav")

    @app.get("/voice/tts-cache")
    def tts_cache_stats():
        return app.state.tts_cache.stats()

    @app.delete("/voice/tts-cache")
    def tts_cache_clear(body: dict = Body(default={})):
        if not body.get("confirm"):
            raise HTTPException(400, "confirmation required")
        return {"removed": app.state.tts_cache.clear()}

    @app.post("/ingest")
    def ingest(body: dict = Body(...)):
        # Body(...) forces a JSON content-type -- CSRF guard, same mechanism as
        # /mapcapture/* (see comment above them).
        return ingest_all(store)

    @app.delete("/data")
    def delete_data(body: dict = Body(default={})):
        if not body.get("confirm"):
            raise HTTPException(400, "confirmation required")
        store.clear_all()
        return {"cleared": True}

    return app
