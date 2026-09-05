"""Localhost FastAPI adapter over the synchronous core. Handlers are plain def
(FastAPI runs them in a threadpool), matching the blocking store/coach. Binds to
127.0.0.1 only, validates the Host header (DNS-rebinding defense), and disables
the docs/openapi surface. Reuses store/coach/analysis unchanged."""
from __future__ import annotations

import asyncio
import json
import math
import os
import tempfile
import threading
import time
import urllib.parse
from dataclasses import asdict
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware
from starlette.websockets import WebSocketDisconnect, WebSocketState

from .. import config, logging_setup, paths, secrets
from ..analysis import analyze_lap
from ..coach_tools import CoachContext
from ..lmu.ingest import ingest_all
from ..model.factory import CoachUserError, make_model
from ..tiers import legal_adjustments
from ..corners import CornerMap
from ..ribbon import multi_signal_direction, reconstruct_ribbon, ribbon_to_screen
from ..track_lookup import track_slug
from ..trackmap import average_racing_line, build_track_map, direction_from_path

# Shipped corner-map dir, resolved off the package (not cwd) so it's correct
# wherever the sidecar is launched from. simcoach/server/app.py -> simcoach/tracks.
_DEFAULT_TRACKS_DIR = Path(__file__).resolve().parent.parent / "tracks"
_CLEAN_LAP_RATIO = 1.07  # keep laps within +7% of the track/car best (drop in/out/off laps)
_MAP_BUFFER_MAX = 8      # cap mapping-mode buffer (need only 2 edges; keep last N)

ALLOWED_HOSTS = {"127.0.0.1", "localhost"}  # port stripped before compare
REST_PROBE_TTL = 2.0  # s: liveness probe rate cap so /status polling can't hammer LMU's REST
FRONTEND_ORIGINS = ["http://localhost:5173", "http://tauri.localhost"]
ALLOWED_ORIGIN_HOSTS = ALLOWED_HOSTS | {"tauri.localhost"}


def _bad_origin(origin: str | None) -> bool:
    """True if a browser Origin is present AND its host isn't allowed. A missing
    Origin (native/Tauri client, or a non-browser client) is allowed — only a
    browser sets Origin, and that's the cross-site-WS vector we're blocking.
    Port-blind by design (any localhost port is fine for a local desktop app)."""
    if not origin:
        return False
    host = (urllib.parse.urlparse(origin).hostname or "").lower()
    return host not in ALLOWED_ORIGIN_HOSTS


def _clean(v):
    # CornerMetrics.min_spd et al. can be float('nan') on real data;
    # NaN/Inf are invalid JSON, so map non-finite floats to None.
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_clean(x) for x in v]
    return v


def _capture_loop(source, store, app_state) -> None:
    """One pass over `source.laps()`: save each completed lap to `store` and
    record it on `app_state.latest` so /status and the lap list see it live.
    Mirrors simcoach.app.run_repl's capture thread body. Returns when
    `source.laps()` is exhausted or raises -- the caller (build_app) wraps
    this in a retry loop so a real LMU source reconnects after a drop."""
    for lap in source.laps():
        # Mapping mode: buffer boundary laps for ribbon reconstruction and NEVER
        # persist them (no DB / sessions / lap-list / coach pollution). Cap the
        # buffer so mapping mode left on can't grow it unbounded (keep last N).
        if getattr(app_state, "mapping", False):
            app_state.map_buffer.append(lap)
            del app_state.map_buffer[:-_MAP_BUFFER_MAX]
            continue
        lap_id = store.save(lap)
        app_state.latest = {"record": lap, "lap_id": lap_id}


def create_app(store, coach, cmap=None, rest=None, map_resolver=None, rest_factory=None,
               tracks_dir=None) -> FastAPI:
    app = FastAPI(title="SimCoach", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    app.state.coach = coach
    app.state.tracks_dir = Path(tracks_dir) if tracks_dir else _DEFAULT_TRACKS_DIR
    app.state.cmap = cmap            # fallback map when no resolver is wired
    app.state.rest = rest            # cached live REST client once connected
    app.state.rest_probed_at = 0.0   # time.monotonic() of the last liveness probe
    app.state.map_resolver = map_resolver  # (track) -> CornerMap | None
    app.state.rest_factory = rest_factory  # () -> LmuRest (probed on first use)
    app.state.cmaps = {}             # per-track CornerMap cache (only non-None cached)
    app.state.canon = {}             # per-(track,car) canonical map payload cache
    app.state.latest = None  # set by the capture thread (build_app): {"record", "lap_id"} | None
    app.state.mapping = False        # mapping-capture mode: buffer laps, don't persist
    app.state.map_buffer = []        # LapRecords captured while mapping (transient)
    app.state.last_ribbon = None     # last built ribbon {world, corners, sectors} for save

    def _map_for(track):
        # Resolve a corner map for a specific track, following track changes.
        # Only non-None results are cached, so a still-missing map is re-checked
        # cheaply on the next call (it may appear once a lap completes).
        if not track:
            return None
        cached = app.state.cmaps.get(track)
        if cached is not None:
            return cached
        if app.state.map_resolver is not None:
            m = app.state.map_resolver(track)
            if m is not None:
                app.state.cmaps[track] = m
            return m
        return app.state.cmap

    def _track_file(track: str) -> Path:
        # Resolve a track name to its shipped JSON path, and REJECT any path that
        # escapes the tracks dir (defense in depth: track_slug already sanitizes
        # to [a-z0-9_], but a save writes owned data, so verify containment).
        slug = track_slug(track)
        tracks_dir = app.state.tracks_dir.resolve()
        p = (tracks_dir / f"{slug}.json").resolve()
        if not slug or tracks_dir not in p.parents:
            raise HTTPException(400, "invalid track")
        return p

    def _write_track_json(track: str, out: dict) -> None:
        # Atomic write (unique tmp + os.replace) so a crash mid-write never leaves
        # a partial file and concurrent same-track writes don't clobber one tmp
        # path. allow_nan=False hard-fails rather than emit invalid JSON.
        # _track_file rejects traversal outside the tracks dir.
        target = _track_file(track)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(out, indent=2, allow_nan=False)
        fd, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".json.tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(payload)
            os.replace(tmp, target)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def _track_file_data(track: str) -> dict:
        # Parsed shipped track file, or {} if missing/unreadable. Never raises.
        try:
            data = json.loads(_track_file(track).read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError, HTTPException):
            return {}

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

    def _clean_lap_channels(track: str, car: str) -> list[dict]:
        # Channel dicts for full, clean laps of this track+car: skip laps with no
        # telemetry (FileNotFoundError) or no position, drop in/out/off laps
        # (lap_time > best * _CLEAN_LAP_RATIO) and partials (short lap_distance).
        best = store.best_lap(track, car)
        best_t = best.lap_time if best is not None else None
        spans, recs = [], []
        for lap_id in store.list_lap_ids(track, car):
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
        try:
            return CornerMap.load(str(_track_file(track)))
        except (OSError, ValueError, KeyError, HTTPException):
            return None

    def _any_pos_lap(track: str, car: str) -> bool:
        # Cheap "does this track+car have ANY position-captured lap" (early-exit,
        # for /status has_map) -- avoids averaging every clean lap just to poll.
        for lap_id in store.list_lap_ids(track, car):
            try:
                ch = store.load(lap_id).channels
            except (KeyError, FileNotFoundError):
                continue
            if "pos_x" in ch and "pos_z" in ch and "lap_distance" in ch:
                return True
        return False

    def _canonical_outline(track: str, car: str, force_avg: bool):
        # The track's canonical (path_world, dist): frozen file path first (unless
        # forcing a re-average), else averaged over the track+car's own pos laps,
        # else ([], []). The map is per-track owned data, NOT the selected lap.
        if not force_avg:
            data = _track_file_data(track)
            p, pd = data.get("path"), data.get("path_dist")
            if isinstance(p, list) and isinstance(pd, list) and p and len(p) == len(pd):
                return p, pd
        laps = _clean_lap_channels(track, car)
        return average_racing_line(laps) if laps else ([], [])

    def _has_canonical(track: str, car: str) -> bool:
        if not track:
            return False
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
        if not force:
            cached = app.state.canon.get((track, car))
            if cached is not None:
                return cached
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
        payload = _clean(build_track_map(channels, cmap, sectors=sectors))
        if not force:
            app.state.canon[(track, car)] = payload
        return payload

    def _invalidate_canon(track: str) -> None:
        app.state.cmaps.pop(track, None)
        for key in [k for k in app.state.canon if k[0] == track]:
            app.state.canon.pop(key, None)

    def _ensure_rest():
        # Return a live LMU REST client, or None. Probes get_setup() to reflect a
        # mid-session DISCONNECT (LMU closed), not just the first connect, and clears
        # the cached client on failure so a later reconnect re-probes. The probe is
        # rate-capped to once per REST_PROBE_TTL so live /status polling can't turn
        # into a per-poll (4s-timeout) REST call that saturates the threadpool if
        # LMU hangs — a recently-probed connected client is trusted between windows.
        now = time.monotonic()
        rest = app.state.rest
        if rest is not None and (now - app.state.rest_probed_at) < REST_PROBE_TTL:
            return rest
        if rest is None:
            if app.state.rest_factory is None:
                return None
            try:
                rest = app.state.rest_factory()
            except Exception:
                return None
        try:
            rest.get_setup()  # liveness probe
        except Exception:
            app.state.rest = None
            return None
        app.state.rest = rest
        app.state.rest_probed_at = now
        return rest

    def _live_track():
        # Current track: the latest captured lap's track, else the live SMM read.
        latest = app.state.latest
        if latest is not None:
            return latest["record"].track
        try:
            from ..lmu.smm import read_session_track_car
            tc = read_session_track_car()
            if tc:
                return tc[0]
        except Exception:
            pass
        return None

    app.add_middleware(CORSMiddleware, allow_origins=FRONTEND_ORIGINS,
                        allow_methods=["*"], allow_headers=["*"])

    @app.middleware("http")
    async def _host_guard(request: Request, call_next):
        host = (request.headers.get("host") or "").split(":")[0].lower()
        if host not in ALLOWED_HOSTS:
            return JSONResponse({"detail": "forbidden host"}, status_code=403)
        return await call_next(request)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/status")
    def status():
        latest = app.state.latest
        session_id = lap_id = None
        if latest is not None:
            record = latest["record"]
            track, car, on_track = record.track, record.car, True
            session_id, lap_id = record.session_id, latest["lap_id"]
        else:
            on_track = False
            track = car = None
            try:
                from ..lmu.smm import read_session_track_car
                tc = read_session_track_car()
                if tc:
                    track, car = tc
            except Exception:
                track = car = None
        return {
            "track": track, "car": car,
            "on_track": on_track,
            "has_map": _has_canonical(track, car),
            "lmu_connected": _ensure_rest() is not None,
            "session_id": session_id,
            "lap_id": lap_id,
        }

    @app.get("/sessions")
    def sessions():
        return store.recent_race_sessions()

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
        except (KeyError, FileNotFoundError):
            raise HTTPException(404, "lap not found")
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

    @app.get("/laps/{lap_id}/map")
    def lap_map(lap_id: int, avg: int = 0):
        # The map is the track's CANONICAL layout, the same for every lap -- the
        # lap_id only selects the track (+car). avg=0: frozen file, else averaged
        # over the track's own pos laps (cached). avg=1: force a fresh re-average
        # (editor, to pick up newly-driven laps before re-freezing).
        try:
            rec = store.load(lap_id)
        except (KeyError, FileNotFoundError):
            raise HTTPException(404, "lap not found")
        return _canonical_map(rec.track, rec.car, force=bool(avg))

    @app.put("/laps/{lap_id}/map")
    def save_lap_map(lap_id: int, body: dict = Body(...)):
        # SAVE the editor's corner map + sectors as shipped owned data. Direction
        # is ALWAYS re-derived from the averaged path geometry here (never trusted
        # from the client) -- the CLAUDE.md corner-direction invariant.
        try:
            rec = store.load(lap_id)
        except (KeyError, FileNotFoundError):
            raise HTTPException(404, "lap not found")

        laps = _clean_lap_channels(rec.track, rec.car)
        path, dist = average_racing_line(laps) if laps else ([], [])
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
            name = str(c.get("name") or f"Turn {number}")[:80]  # cap: owned data + coach prompt
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
            if not (start < apex < end):
                raise HTTPException(422, f"corner {number}: require start_dist < apex_dist < end_dist")
            direction = direction_from_path(path, dist, apex)  # server-derived, never client
            corners.append({
                "number": number, "name": name, "direction": direction,
                "start_dist": round(start, 1), "apex_dist": round(apex, 1),
                "end_dist": round(end, 1),
            })
        corners.sort(key=lambda c: c["apex_dist"])

        sectors = body.get("sectors") or []
        if not isinstance(sectors, list):
            raise HTTPException(422, "sectors must be a list")
        try:
            sectors = [float(s) for s in sectors]
        except (TypeError, ValueError):
            raise HTTPException(422, "sectors must be numbers")
        if any(not math.isfinite(s) or not (0.0 < s < length) for s in sectors):
            raise HTTPException(422, "sector splits must be within (0, length)")
        if any(a >= b for a, b in zip(sectors, sectors[1:])):
            raise HTTPException(422, "sector splits must be strictly increasing")

        out = {
            "track": track_slug(rec.track),
            "corners": corners,
            "path": path,
            "path_dist": dist,
            "sectors": sectors,
            "length": length,
        }
        _write_track_json(rec.track, out)
        # Invalidate cmap + canonical caches so the next GET returns the new
        # canonical map (now frozen into the file).
        _invalidate_canon(rec.track)
        return _canonical_map(rec.track, rec.car, force=False)

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
        for i, c in enumerate(solved):
            ai = int(c["apex_idx"]) % n
            si = int(c["start_idx"]) % n
            ei = int(c["end_idx"]) % n
            apex = dist[ai]
            direction, confidence = multi_signal_direction(
                ribbon["center"], dist, apex,
                edge_channels=edge_channels, lap_length=length,
            )
            out.append({
                "number": i + 1, "name": f"Turn {i + 1}", "direction": direction,
                "apex_dist": round(apex, 1),
                "start_dist": round(dist[si], 1),
                "end_dist": round(dist[ei], 1),
                "confidence": confidence,
            })
        return out

    # Body(...) forces an application/json content-type, so these bodyless POSTs
    # stop being CORS "simple requests" -- the browser must preflight, which the
    # origin allowlist blocks (CSRF guard for the mapping-mode toggle).
    @app.post("/mapcapture/start")
    def mapcapture_start(body: dict = Body(...)):
        app.state.map_buffer = []
        app.state.last_ribbon = None
        app.state.mapping = True
        return {"active": True}

    @app.post("/mapcapture/cancel")
    def mapcapture_cancel(body: dict = Body(...)):
        app.state.mapping = False
        app.state.map_buffer = []
        app.state.last_ribbon = None
        return {"active": False}

    @app.get("/mapcapture/status")
    def mapcapture_status():
        laps = []
        for i, r in enumerate(app.state.map_buffer):
            ld = r.channels.get("lap_distance")
            n = len(ld) if ld is not None else 0
            length = float(max(ld)) if n else 0.0
            laps.append({"idx": i, "samples": n, "length": round(length, 1)})
        return {"active": app.state.mapping, "live_track": _live_track(), "laps": laps}

    @app.post("/mapcapture/build")
    def mapcapture_build(body: dict = Body(...)):
        buf = app.state.map_buffer
        try:
            li, ri = int(body["left"]), int(body["right"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(422, "left/right must be integers")
        # Reject negatives (Python would wrap buf[-1]) and left==right (degenerate
        # zero-width ribbon).
        if not (0 <= li < len(buf) and 0 <= ri < len(buf)) or li == ri:
            raise HTTPException(422, "left/right must be distinct valid buffer indices")
        left_rec, right_rec = buf[li], buf[ri]
        ribbon = reconstruct_ribbon(left_rec.channels, right_rec.channels)
        if not ribbon:
            raise HTTPException(422, "boundary laps lack position telemetry")
        corners = _place_corners(ribbon, left_rec.channels)
        sectors = _shipped_extras(left_rec.track).get("sectors") or []
        # Pin the track from the buffered lap NOW: the sim may change track between
        # build and save, and the geometry belongs to this lap's track.
        app.state.last_ribbon = {"world": ribbon, "corners": corners,
                                 "sectors": sectors, "track": left_rec.track}
        return _clean(ribbon_to_screen(ribbon, corners, sectors))

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
        keys = ("number", "name", "direction", "start_dist", "apex_dist", "end_dist")
        corners = [{k: c[k] for k in keys} for c in rib["corners"]]
        out = {
            "track": track_slug(track),
            "corners": corners,
            "ribbon": {k: world[k] for k in ("left", "right", "center", "width", "bank")},
            "path": world["center"],
            "path_dist": world["dist"],
            "sectors": rib["sectors"],
            "length": world["dist"][-1] if world["dist"] else 0.0,
        }
        _write_track_json(track, out)
        _invalidate_canon(track)
        return {"saved": True, "slug": track_slug(track)}

    @app.get("/setup")
    def setup():
        # Phase 1: raw dict from LmuRest, unfiltered. Filtering to the
        # human-meaningful subset is deferred to the Setup-panel phase (Phase 4).
        rest = _ensure_rest()
        if not rest:
            return {}
        try:
            return rest.get_setup()
        except Exception:
            return {}

    @app.get("/chats")
    def chats():
        return store.list_chats()

    @app.get("/chats/{chat_id}")
    def chat_messages(chat_id: str):
        return store.chat_messages(chat_id)

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
            record = None
            eff_lap_id = lap_id
            eff_session_id = session_id
            if eff_lap_id is not None:
                try: record = app.state.store.load(eff_lap_id)
                except Exception: record = None
            # Auto-anchor to the LIVE session when the client didn't pick a lap or
            # session, so the coach already knows the current session + its latest
            # lap without the driver having to click one (the auto-context goal).
            latest = app.state.latest
            if record is None and latest is not None:
                record = latest["record"]
                eff_lap_id = latest["lap_id"]
            if eff_session_id is None and latest is not None:
                eff_session_id = latest["record"].session_id
            setup = {}
            rest = _ensure_rest()
            if rest:
                try: setup = rest.get_setup()
                except Exception: setup = {}
            cmap = _map_for(record.track) if record is not None else _map_for(_live_track())
            ctx = CoachContext(app.state.store, record=record, lap_id=eff_lap_id, cmap=cmap,
                               setup=setup, on_track=record is not None,
                               allowed_adjustments=(legal_adjustments(setup) if setup else None),
                               session_id=eff_session_id)
            car = ctx.car or "car"

            # Thread the chat's prior turns so the coach continues the conversation
            # instead of treating each question standalone (the system prompt tells it
            # "the messages above are what you and the driver already said").
            history: list[dict] = []
            chat_id_in = req.get("chat_id")
            if chat_id_in:
                try:
                    for row in app.state.store.chat_messages(chat_id_in):
                        if row.get("question"):
                            history.append({"role": "user", "content": row["question"]})
                        if row.get("answer"):
                            history.append({"role": "assistant", "content": row["answer"]})
                except Exception:
                    history = []

            text = ""
            try:
                for ev in app.state.coach.ask_agentic_stream(
                        ctx, question, car=car, input_device=req.get("input_device"),
                        history=history):
                    if ev["type"] == "text":
                        text += ev["text"]       # accumulate streamed deltas into the full answer
                    _emit(ev)
                terminal = {"type": "done"}
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
                    chat_id = req.get("chat_id") or app.state.store.create_chat()
                    app.state.store.save_chat(chat_id, ctx.session_id, ctx.track, ctx.car, ctx.lap_id,
                                              question, text, ctx.tool_calls)
                    terminal["chat_id"] = chat_id
                except Exception:
                    pass
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

    def _settings_view() -> dict:
        try:
            from importlib.metadata import version
            app_version = version("simcoach")
        except Exception:
            app_version = "dev"
        cfg = config.load_config()
        cfg.update({
            "api_key_set": secrets.has_api_key(),
            "data_dir": str(paths.app_data_dir()),
            "app_version": app_version,
            "lmu_build": None,
        })
        return cfg

    @app.get("/settings")
    def get_settings():
        return _settings_view()

    @app.put("/settings")
    def put_settings(body: dict = Body(...)):
        # Whitelist to config.DEFAULTS keys before saving — this is the
        # secret-leak guard: an injected "key"/"api_key" field can never
        # reach the JSON file on disk.
        whitelisted = {k: body[k] for k in config.DEFAULTS if k in body}
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
    def test_key():
        try:
            cfg = config.load_config()
            model = make_model(model=cfg.get("model"))
            model.complete("ping", [{"role": "user", "content": "ping"}])
            return {"ok": True, "error": None}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @app.post("/ingest")
    def ingest():
        return ingest_all(store)

    @app.delete("/data")
    def delete_data(body: dict = Body(default={})):
        if not body.get("confirm"):
            raise HTTPException(400, "confirmation required")
        store.clear_all()
        return {"cleared": True}

    return app
