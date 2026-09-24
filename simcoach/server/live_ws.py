"""The push websockets (#113): /live (rail-map position + next corner, ~4 Hz) and /events
(status/laps/map/mapcapture/voice-setup topics, #106)."""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

from ..corners import next_corner
from .security import ALLOWED_HOSTS, _bad_origin

_EVENTS_PERIOD = 1.0  # s: /events push loop period (module constant so tests can monkeypatch it)
_MAP_RETRY_TICKS = 10  # while the pushed map is {}, re-ask this often (#155)


def register(app, deps) -> None:
    store = app.state.store
    _live_track = deps._live_track
    _fresh = deps._fresh
    _cmap_for_map = deps._cmap_for_map
    _status_view = deps._status_view
    _map_payload = deps._map_payload
    _live_map_key = deps._live_map_key
    mapcapture_status = deps.mapcapture_status

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
            # drops the cache when a map is saved (_fresh: or rewritten on disk, #158).
            track = _live_track()
            _fresh(track)
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
                _fresh(track)  # a track file rewritten outside the app bumps map_rev (#158)
                key = (track, car, app.state.map_rev)
                empty_ticks = last.get("_map_empty_ticks")
                changed = last.get("_map_key", _MISSING) != key
                if changed or (empty_ticks is not None and empty_ticks >= _MAP_RETRY_TICKS):
                    # Only on a track/car change or a map save (_invalidate_canon bumps
                    # map_rev, #155): _map_payload re-reads the track file and the
                    # store's sector bounds, which is not 1 Hz work. The exception is
                    # {} (a first drive: no outline, no position laps yet) -- nothing
                    # saves a map when the first lap commits, so re-ask every few ticks.
                    last["_map_key"] = key
                    # Armed BEFORE the read: a raising payload retries on the same
                    # cadence as an empty one, not every tick and not never.
                    last["_map_empty_ticks"] = 0 if track else None
                    payload = _map_payload(*_live_map_key(track, car)) if track else {}
                    if payload:
                        last["_map_empty_ticks"] = None
                    if changed or payload:
                        frames.append(("map", payload))
                elif empty_ticks is not None:
                    last["_map_empty_ticks"] = empty_ticks + 1
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
