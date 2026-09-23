"""Localhost FastAPI adapter over the synchronous core. Handlers are plain def
(FastAPI runs them in a threadpool), matching the blocking store/coach. Binds to
127.0.0.1 only, validates the Host header (DNS-rebinding defense), and disables
the docs/openapi surface. Reuses store/coach/analysis unchanged."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

from fastapi import Body, FastAPI, HTTPException, Request, Response, WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

from .. import config, logging_setup, paths, secrets
from ..analysis import (
    CORNER_GEOMETRY_KEYS,
    analyze_lap,
    corner_geometry,
    geometry_clause,
    input_traces,
    measurable_geometry,
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
from ..corners import in_window, next_corner
from ..driver import DriverProfile
from ..format import format_laptime
from ..lmu.ingest import ingest_all
from ..model.base import TokenUsage
from ..model.factory import CoachUserError, make_model
from ..model_types import LapRecord
from ..pronunciations import table_for
from ..race import RaceTracker, live_race
from ..ribbon import multi_signal_direction, reconstruct_surface, ribbon_to_screen
from ..tiers import adjustable_from_setup, legal_adjustments
from ..track_lookup import (
    corner_apex_key,
    fill_blank_labels,
    track_slug,
)
from ..trackmap import average_racing_line, direction_from_path
from . import core, maps, security, trackfile
from .captures import (
    _POS_CHANNELS,
    _append_pending_lap,
    _drop_pending,
    _load_cloud,
    _one_track,
    _pending_dir,
    _pending_files,
    _pending_laps,
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
    _curating = deps._curating
    _map_dirs = deps._map_dirs
    _capture_path = deps._capture_path
    _newest_capture = deps._newest_capture
    _invalidate_canon = deps._invalidate_canon
    _ensure_rest = deps._ensure_rest
    _live_track = deps._live_track
    _live_session_id = deps._live_session_id
    trackfile.register(app, deps)
    _write_track_json = deps._write_track_json
    _track_file_data = deps._track_file_data
    _track_rotation = deps._track_rotation
    _is_shipped = deps._is_shipped
    _shipped_extras = deps._shipped_extras
    maps.register(app, deps)
    _live_lap_paths = deps._live_lap_paths
    _rebuild_clouds = deps._rebuild_clouds
    _clean_lap_channels = deps._clean_lap_channels
    _cmap_for_map = deps._cmap_for_map
    _frozen_outline = deps._frozen_outline
    _canonical_map = deps._canonical_map
    _has_canonical = deps._has_canonical
    _map_payload = deps._map_payload
    _live_map_key = deps._live_map_key
    _slug_map_key = deps._slug_map_key

    security.install(app)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    def _status_view() -> dict:
        # Live track/car from shared memory FIRST, so a track/car change is detected
        # right away. The latest captured lap adds rich context (on_track + session/
        # lap ids) but ONLY while it still matches the live session; once the driver
        # moves to another track/car it's stale, so drop it (else we'd report the old
        # track and, worse, map the new track under the old slug).
        try:
            from ..lmu.smm import read_live_status
            live = read_live_status()  # {track, car, state} | None
        except Exception:
            live = None
        live_tc = (live["track"], live["car"]) if live else None
        latest = app.state.latest
        matches_live = (
            latest is not None
            and (live_tc is None
                 or (latest["record"].track == live_tc[0] and latest["record"].car == live_tc[1]))
        )
        session_id = lap_id = None
        if matches_live:
            record = latest["record"]
            track, car = record.track, record.car
            session_id, lap_id = record.session_id, latest["lap_id"]
        elif live:
            track, car = live["track"], live["car"]
            # The live track/car doesn't match the latest stored lap (a track
            # change): report the id the first lap on the new track will be
            # stamped with, so the strip and chat pin move right away instead
            # of waiting on that lap (#127). lap_id stays None -- no lap yet.
            session_id = _live_session_id()
        else:
            # #137: no live SMM and nothing captured this process (a sidecar
            # restart with LMU in the lobby) -- fall back to the newest stored
            # lap so the strip isn't blank. on_track/activity stay False/None
            # below (live is None here), which is correct: we don't know the
            # driver is on track, only what they last drove.
            row = store.latest_lap_row()
            if row is not None:
                track, car, session_id = row["track"], row["car"], row["session_id"]
            else:
                track, car = None, None
        # `activity` is the live driving state (track/pits/garage), detected from
        # scoring immediately — not gated on a captured lap. on_track mirrors it.
        activity = live["state"] if live else None
        return {
            "track": track, "car": car,
            "car_model": resolve_car(car) if car else None,
            "on_track": activity == "track",
            "activity": activity,
            "has_map": _has_canonical(*_live_map_key(track, car)),
            "lmu_connected": _ensure_rest() is not None,
            "session_id": session_id,
            "lap_id": lap_id,
        }

    @app.get("/status")
    def status():
        return _status_view()

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

    def _notes_rev(track: str) -> str:
        # Revision of what a client holding the notes was looking at: the stored notes
        # AND the corner labels they are keyed to. PUT /notes writes its body verbatim --
        # it has to, it is the author typing -- so a copy loaded before a map save that
        # re-keyed the notes (#64) would put every old key back, and the old keys match
        # the new labels, so nothing even flags them as orphans. A relabel that moved no
        # note still changes what a key means, hence the labels.
        data = _track_file_data(track)
        corners = data.get("corners")
        labels = [str(c.get("label") or c.get("number")) for c in corners
                  if isinstance(c, dict)] if isinstance(corners, list) else []
        blob = json.dumps({"notes": data.get("notes"), "labels": labels},
                          sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

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
        if rev != _notes_rev(track):
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
        return {"ok": True, "rev": _notes_rev(track), "dropped_notes": dropped}

    @app.get("/tracks/{slug}/notes")
    def get_notes(slug: str):
        # Merged (shipped+overlay) notes for the editor to pre-fill, normalized to
        # the same 4-field shape PUT persists. No notes (or no track) yet -> all-empty
        # defaults, NOT 404 -- the editor needs a blank-but-valid shape to edit.
        if track_slug(slug) != slug:
            raise HTTPException(400, "invalid slug")
        raw = _track_file_data(slug).get("notes") or {}
        return {"overview": str(raw.get("overview", "")),
                "corners": {str(k): str(v) for k, v in (raw.get("corners") or {}).items()},
                "reference_times": {str(k): str(v) for k, v in (raw.get("reference_times") or {}).items()},
                "source": str(raw.get("source", "")),
                "rev": _notes_rev(slug)}   # send it back with PUT; see _notes_rev

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
        app.state.mapping = True
        return {"active": True}

    @app.post("/mapcapture/cancel")
    def mapcapture_cancel(body: dict = Body(...)):
        # Leaves the pending capture on disk (#150): the editor calls this on unmount
        # too, so a cancel must not be a way to lose driven laps. Next start resumes it.
        # {"discard": true} (the editor's explicit "Discard capture") deletes it: every
        # track in the buffer (the sim may change track mid-session), else the live one.
        # Nothing to resolve is a 409, never a silent no-op the UI reads as success.
        discarded = 0
        discard = body.get("discard") is True
        if discard:
            buf = list(app.state.map_buffer)
            names = {r.track for r in buf} if buf else {_live_track()}
            slugs = {track_slug(n) for n in names if n} - {""}
            if not slugs:
                raise HTTPException(409, "no track to discard a capture for; is the "
                                         "sim running?")
            app.state.mapping = False
            for slug in slugs:
                d = _pending_dir(store.data_dir, slug)
                discarded += _drop_pending(sorted(d.glob("*.npz")) if d.is_dir() else [])
        app.state.mapping = False
        app.state.map_buffer = []
        app.state.last_ribbon = None
        # A plain cancel's reply is unchanged; only a discard reports its count.
        return {"active": False, "discarded": discarded} if discard else {"active": False}

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
        out = {
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
        }
        # Mark maps this app BUILT (from a capture or from driven laps). A live-lap rebuild
        # may replace only a marked map's corners -- an unmarked map with corners is a
        # curated layout (see mapcapture_rebuild).
        if rib.get("built_from"):
            out["built_from"] = rib["built_from"]
        # No `notes` key, deliberately -- see _persist_map. _write_track_json REPLACES
        # the file, and this route builds its payload from scratch, which is how "Save
        # ribbon" used to delete every authored note for the track (#65). The one writer
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
        if rib.get("laps"):
            # Promoted: the Build's laps are in captures/<slug>.npz now. Leave mapping
            # mode FIRST (shrinks the window for a lap landing mid-promote), then drop
            # only those laps' files: a lap driven after Build stays pending, and in
            # the buffer, for the next Generate map layout.
            app.state.mapping = False
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
                                 "built_from": "capture" if from_capture else "live_laps"}
        res = _clean(ribbon_to_screen(ribbon, corners, sectors,
                                      rotation=_track_rotation(track), sections=sections))
        res["track_name"] = _track_file_data(track).get("name") or track
        # How many laps back this geometry, so the driver knows before committing it:
        # a map off 1-3 clouds is thin (#41's Imola sector splits rest on a single lap).
        res["lap_count"] = len(laps)
        return res

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
