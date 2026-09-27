"""Localhost FastAPI adapter over the synchronous core. Handlers are plain def
(FastAPI runs them in a threadpool), matching the blocking store/coach. Binds to
127.0.0.1 only, validates the Host header (DNS-rebinding defense), and disables
the docs/openapi surface. Reuses store/coach/analysis unchanged. Routes and helpers live
in the sibling modules; create_app wires them (#113)."""
from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI

from .. import config
from ..livelap import LiveLap
from ..race import RaceTracker
from ..tiers import adjustable_from_setup, blank_setup
from . import (
    coach_ws,
    core,
    live_ws,
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
    _session_key,
)
from .core import _in
from .security import SIDECAR_PORT
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


def _setup_commit(app_state, store, lap_id) -> None:
    """Link a stored lap to the setup LMU REST reports now (#188), with the same
    isFreeSetting filter as POST /setup/capture. Runs on setup_pool, never the capture
    thread; REST down (or any failure) leaves the lap with no setup, logged not raised."""
    try:
        ensure = getattr(app_state, "ensure_rest", None)
        rest = ensure() if ensure else None
        data = rest.get_setup() if rest else None
        free = {k: data[k] for k in adjustable_from_setup(data)} if data else {}
        if free and not blank_setup(free):
            store.link_lap_setup(lap_id, free)
    except Exception:
        logging.getLogger(__name__).warning("lap setup not recorded", exc_info=True)


def _capture_loop(source, store, app_state) -> None:
    """One pass over `source.laps()`: save each completed lap to `store` and
    record it on `app_state.latest` so /status and the lap list see it live.
    Returns when `source.laps()` is exhausted or raises -- the caller
    (build_app) wraps this in a retry loop so a real LMU source reconnects
    after a drop."""
    live = getattr(app_state, "live_lap", None)
    if live is not None:
        # #160: the worker reads this source's in-progress lap; capture never calls it.
        live.source = source
    for lap in source.laps():
        if getattr(app_state, "mapping", False):
            # #154 safety net: mapping belongs to the session it was started in. A phase
            # (practice -> qualifying -> race), track, car or session change ends it, and
            # this lap is an ordinary lap. /mapcapture/start leaves it unpinned: the
            # first mapping lap pins it.
            key = _session_key(lap)
            pinned = getattr(app_state, "mapping_key", None)
            if pinned is None:
                app_state.mapping_key = key
            elif key != pinned:
                app_state.mapping = False
                logging.getLogger(__name__).info(
                    "mapping mode ended: live session changed %s -> %s", pinned, key)
        # Mapping mode: buffer the lap for ribbon reconstruction AND persist it,
        # flagged mapping=True so it gives an immediately-editable lap + shows in the
        # session/lap list, but best_lap excludes it from coaching signals. The lap
        # KEEPS its real phase (#154): overwriting it with "mapping" is how a stuck
        # mapping mode erased a whole qualifying + race.
        if getattr(app_state, "mapping", False):
            # A partial lap (#77) is still persisted and still numbered, but never
            # buffered for ribbon reconstruction -- mapping wants full boundary passes.
            # Uncapped (#150): the old 8-lap cap silently dropped driven passes.
            # ponytail: RAM grows ~4.4 MB/lap (104 channels x 5284 samples at Spa;
            # _place_corners reads lap 0's); trim to _POS_CHANNELS past lap 0 if marathon
            # mapping sessions matter.
            # An untimed whole lap (#181) is gated too: the off LMU invalidated it for
            # would widen the ribbon, exactly as a 'partial' one used to.
            if lap.partial is None and lap.lap_time:
                try:
                    # Which file backs this lap, so Save promotes exactly the laps it
                    # saved (a lap driven after Build stays pending).
                    lap.pending_path = _append_pending_lap(store.data_dir, lap)
                except Exception:
                    # Never kill capture over it: the lap is still in RAM and the store.
                    logging.getLogger(__name__).warning(
                        "couldn't write mapping lap to the pending capture", exc_info=True)
                app_state.map_buffer.append(lap)
            lap.mapping = True
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
        pool = getattr(app_state, "setup_pool", None)
        if pool is not None:
            try:
                pool.submit(_setup_commit, app_state, store, lap_id)
            except RuntimeError:
                logging.getLogger(__name__).warning("setup pool closed; lap setup skipped")


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
    # #176 fix round 1: the lap rail's Optimal figure is computed off the request/
    # /events thread entirely, on its own single-worker pool (never contends with
    # more than one extra core, even under a PTT question). `optimal_cache` is an
    # LRU (OrderedDict, bounded by core._OPTIMAL_CACHE_MAX) of
    # sid -> (sig, optimal seconds|None); `optimal_inflight` is sid -> Future, for
    # single-flight de-duplication; `optimal_lock` guards both together.
    app.state.optimal_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="optimal-calc")
    app.state.optimal_cache: OrderedDict = OrderedDict()
    app.state.optimal_inflight: dict = {}
    app.state.optimal_lock = threading.Lock()
    app.state.canon = {}             # per-(track,car) canonical map payload cache
    app.state.live_cmap = None       # (live track, CornerMap|None) for /live's next corner
    app.state.map_rev = 0            # bumped on every map save; /events re-pushes the map (#155)
    app.state.track_sigs = {}        # slug -> track files' (mtime_ns, size); see core._fresh (#158)
    app.state.latest = None  # set by the capture thread (build_app): {"record", "lap_id"} | None
    app.state.race = RaceTracker()   # #152: the race tools' snapshot, one per lap commit
    # One worker: commits are processed in lap order, and never on the capture thread.
    app.state.race_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="race-state")
    # #188: each lap's setup read (a REST call, up to its timeout). Its own single
    # worker, in lap order, so a hung LMU never delays capture or the race snapshot.
    app.state.setup_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lap-setup")
    app.state.mapping = False        # mapping-capture mode: buffer laps, don't persist
    app.state.map_buffer = []        # LapRecords captured while mapping (transient)
    app.state.last_ribbon = None     # last built ribbon {world, corners, sectors} for save
    app.state.mapping_key = None     # _session_key mapping was started in (#154)

    deps = SimpleNamespace()   # helpers each module publishes for the next (#113)
    core.register(app, deps)
    trackfile.register(app, deps)
    maps.register(app, deps)
    # #160: the lap in progress, corner by corner (its thread is started by __main__).
    app.state.live_lap = LiveLap(store, deps._cmap_for_map)
    security.install(app)      # before any route; see the order note inside install()
    status_routes.register(app, deps)
    reads.register(app, deps)
    tracks.register(app, deps)
    mapcapture.register(app, deps)
    coach_ws.register(app, deps)
    profile.register(app, deps)
    live_ws.register(app, deps)
    settings_routes.register(app, deps)
    return app
