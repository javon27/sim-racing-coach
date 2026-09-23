"""Localhost FastAPI adapter over the synchronous core. Handlers are plain def
(FastAPI runs them in a threadpool), matching the blocking store/coach. Binds to
127.0.0.1 only, validates the Host header (DNS-rebinding defense), and disables
the docs/openapi surface. Reuses store/coach/analysis unchanged."""
from __future__ import annotations

import asyncio
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

from .. import config
from ..corners import next_corner
from ..race import RaceTracker
from . import (
    coach_ws,
    core,
    mapcapture,
    maps,
    profile,
    reads,
    security,
    settings_routes,
    status_routes,
    trackfile,
    tracks,
)
from .captures import (
    _append_pending_lap,
    _drop_pending,
    _load_cloud,
    _save_cloud,
)
from .core import _in
from .security import ALLOWED_HOSTS, SIDECAR_PORT, _bad_origin
from .settings_routes import _clamp_tts_cache_mb

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
    from ..tts_kokoro import KokoroEngine
    from ..voice import (
        KokoroInstaller,
        ModelInstaller,
        WhisperTranscriber,
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
    _invalidate_canon = deps._invalidate_canon
    _live_track = deps._live_track
    trackfile.register(app, deps)
    maps.register(app, deps)
    _rebuild_clouds = deps._rebuild_clouds
    _cmap_for_map = deps._cmap_for_map
    _map_payload = deps._map_payload
    _live_map_key = deps._live_map_key

    security.install(app)
    status_routes.register(app, deps)
    _status_view = deps._status_view
    reads.register(app, deps)
    tracks.register(app, deps)
    mapcapture.register(app, deps)
    mapcapture_status = deps.mapcapture_status
    coach_ws.register(app, deps)
    profile.register(app, deps)

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

    settings_routes.register(app, deps)

    return app
