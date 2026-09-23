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
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

from .. import config
from ..car_lookup import resolve_car
from ..car_notes import load_car_notes, load_setup_concepts
from ..coach_tools import (
    _PARTIAL_LIST_WORD,
    CoachContext,
    _lap_age,
    current_race_fact,
    race_turn_note,
    scrub_stored_answer,
)
from ..corners import next_corner
from ..driver import DriverProfile
from ..format import format_laptime
from ..model.base import TokenUsage
from ..model.factory import CoachUserError
from ..race import RaceTracker, live_race
from ..tiers import legal_adjustments
from . import (
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
from .serialize import _usage_view
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
    _map_for = deps._map_for
    _invalidate_canon = deps._invalidate_canon
    _ensure_rest = deps._ensure_rest
    _live_track = deps._live_track
    _live_session_id = deps._live_session_id
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
    profile.register(app, deps)

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
