"""Localhost FastAPI adapter over the synchronous core. Handlers are plain def
(FastAPI runs them in a threadpool), matching the blocking store/coach. Binds to
127.0.0.1 only, validates the Host header (DNS-rebinding defense), and disables
the docs/openapi surface. Reuses store/coach/analysis unchanged."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import sys
import tempfile
import threading
import time
from dataclasses import asdict
from pathlib import Path

import numpy as np
from fastapi import Body, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware
from starlette.websockets import WebSocketDisconnect, WebSocketState

from .. import config, logging_setup, paths, secrets
from ..analysis import analyze_lap, corner_geometry, geometry_clause
from ..car_lookup import resolve_car
from ..car_notes import load_car_notes, load_setup_concepts
from ..coach_tools import CoachContext
from ..corners import CornerMap, in_window
from ..driver import DriverProfile
from ..lmu.ingest import ingest_all
from ..model.base import TokenUsage
from ..model.factory import CoachUserError, make_model
from ..pricing import estimate_chat_cost
from ..ribbon import multi_signal_direction, reconstruct_surface, ribbon_to_screen
from ..tiers import adjustable_from_setup, legal_adjustments
from ..track_lookup import load_map_data, track_slug
from ..trackmap import average_racing_line, build_track_map, direction_from_path

# Shipped corner-map dir, resolved off the package (not cwd) so it's correct
# wherever the sidecar is launched from. simcoach/server/app.py -> simcoach/tracks.
_DEFAULT_TRACKS_DIR = Path(__file__).resolve().parent.parent / "tracks"


def _usage_view(agg: dict) -> dict:
    """Shape a LapStore.chat_usage() aggregate for the wire: the four buckets +
    total + message count, the estimated $, and whether that $ is the whole story.

    The core owns the arithmetic (pricing lives in simcoach.pricing) — the UI only
    formats what it's given. `by_model` stays server-side; the client has no use for
    the breakdown, only for the one number and the completeness flag.

    A chat with any pre-v12/NULL-usage row (`null_usage_rows`) is never "complete" —
    those rows billed real money that COALESCEs to 0, so a chat that is ALL such rows
    must not read as a genuinely-zero-cost, fully-priced chat."""
    usage = {k: agg.get(k, 0) for k in
             ("input", "output", "cache_read", "cache_write", "total", "messages")}
    cost, complete = estimate_chat_cost(agg.get("by_model") or {})
    if agg.get("null_usage_rows"):
        complete = False
    return {"usage": usage, "cost": cost, "cost_complete": complete}
_CLEAN_LAP_RATIO = 1.07  # keep laps within +7% of the track/car best (drop in/out/off laps)
_MAP_BUFFER_MAX = 8      # cap mapping-mode buffer (need only 2 edges; keep last N)

ALLOWED_HOSTS = {"127.0.0.1", "localhost"}  # port stripped before compare
REST_PROBE_TTL = 2.0  # s: liveness probe rate cap so /status polling can't hammer LMU's REST
SIDECAR_PORT = 8765  # the port simcoach/server/__main__.py serves on (it imports this)
FRONTEND_ORIGINS = ["http://localhost:5173", "http://tauri.localhost"]

# FULL origins (scheme + host + port), never bare hostnames. Comparing the
# hostname alone let ANY app on ANY localhost port through the guard (#60) --
# another local dev server, or an XSS in one, could drive every guarded route,
# which is exactly what the guard exists to stop. Three sources, no invention:
#   - FRONTEND_ORIGINS: the Vite dev origin (src-tauri/tauri.conf.json devUrl)
#     and the Windows Tauri webview origin.
#   - "tauri://localhost", the webview origin Tauri uses on macOS/Linux. NOT a
#     new permission: the old hostname-only compare accepted it (hostname
#     "localhost"), so dropping it here would lock the app's own webview out of
#     its own sidecar on those platforms. #54 tracks the wider cross-platform
#     origin question and stays untouched -- FRONTEND_ORIGINS (the CORS list) is
#     deliberately NOT changed here.
#   - the sidecar's own origin, under both names ALLOWED_HOSTS accepts as Host --
#     accepting Host "localhost:8765" but refusing Origin "http://localhost:8765"
#     would be incoherent.
# Deliberately dropped vs the old behaviour: "https://tauri.localhost" and every
# other scheme on these hostnames. Nothing serves them; the old guard only let
# them through because it never looked past the hostname.
ALLOWED_ORIGINS = set(FRONTEND_ORIGINS) | {"tauri://localhost"} | {
    f"http://{h}:{SIDECAR_PORT}" for h in ALLOWED_HOSTS
}


def _bad_origin(origin: str | None) -> bool:
    """True if a browser Origin is present AND isn't an exact ALLOWED_ORIGINS
    match. A missing Origin (native/Tauri client, or a non-browser client) is
    allowed — only a browser sets Origin, and that's the cross-site-WS/CSRF
    vector we're blocking; no browser omits Origin on a mutating request.

    Compares scheme+host+port, not just the host (#60). Exact string match after
    lowercasing: scheme and host are case-insensitive, and a browser never sends
    a path, a trailing slash, or an explicit default port on an Origin — so
    anything that isn't the literal allowed origin is refused. That also refuses
    "null", "file://" and bare "http://[::1]" for free, as before."""
    if not origin:
        return False
    return origin.lower() not in ALLOWED_ORIGINS


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


_POS_CHANNELS = ("pos_x", "pos_y", "pos_z", "lap_distance")


def _save_cloud(path, laps) -> None:
    """Persist the raw mapping point cloud (pos_x/y/z + lap_distance per lap) so a
    ribbon can be re-reconstructed after algorithm changes without re-driving.
    Atomic (tmp + os.replace) so a crash mid-save can't leave a corrupt .npz."""
    path.parent.mkdir(parents=True, exist_ok=True)
    arrs = {f"lap{i}_{c}": np.asarray(ch[c]) for i, ch in enumerate(laps)
            for c in _POS_CHANNELS if c in ch}
    tmp = path.with_suffix(".npz.tmp")
    try:
        with open(tmp, "wb") as f:      # file handle: numpy won't append its own .npz
            np.savez_compressed(f, **arrs)
        os.replace(tmp, path)
    except BaseException:
        if tmp.exists():
            tmp.unlink()
        raise


def _load_cloud(path) -> list[dict]:
    """Raw cloud back as a list of channel dicts, or [] if the file is corrupt/
    unreadable (caller treats [] as 'no usable capture' -> clean 422, not a 500)."""
    try:
        z = np.load(str(path))
        laps: dict = {}
        for k in z.files:
            i, c = k.split("_", 1)          # "lap3_lap_distance" -> ("lap3", "lap_distance")
            laps.setdefault(i, {})[c] = z[k]
        return [laps[i] for i in sorted(laps, key=lambda s: int(s[3:]))]
    except Exception:
        return []


def _capture_loop(source, store, app_state) -> None:
    """One pass over `source.laps()`: save each completed lap to `store` and
    record it on `app_state.latest` so /status and the lap list see it live.
    Mirrors simcoach.app.run_repl's capture thread body. Returns when
    `source.laps()` is exhausted or raises -- the caller (build_app) wraps
    this in a retry loop so a real LMU source reconnects after a drop."""
    for lap in source.laps():
        # Mapping mode: buffer the lap for ribbon reconstruction AND persist it,
        # tagged phase="mapping" so it gives an immediately-editable lap + shows in
        # the session/lap list, but best_lap excludes it from coaching signals.
        if getattr(app_state, "mapping", False):
            app_state.map_buffer.append(lap)
            del app_state.map_buffer[:-_MAP_BUFFER_MAX]
            lap.phase = "mapping"
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

    def _curating() -> bool:
        # True when running from source (dev): map edits go to the repo tracks dir to
        # curate the shipped set. A frozen (installed) build returns False — it edits
        # only its own app-data overlay, never the read-only/ephemeral bundle.
        return not getattr(sys, "frozen", False)

    def _map_dirs() -> tuple[Path, Path]:
        # (user_dir, bundled_dir). Curated maps ship in the install dir (read-only
        # once installed); user-created maps live in app-data (writable, like the
        # .npz captures). Reads prefer the user copy so a re-map overrides a shipped
        # one; writes go to the install dir when writable (dev curation) else app-data.
        return Path(store.data_dir) / "tracks", app.state.tracks_dir

    def _in(base: Path, slug: str) -> Path:
        # <base>/<slug>.json, rejecting any path that escapes base (defense in depth:
        # track_slug already sanitizes to [a-z0-9_], but this guards owned data).
        b = base.resolve()
        p = (b / f"{slug}.json").resolve()
        if b not in p.parents:
            raise HTTPException(400, "invalid track")
        return p

    def _write_track_json(track: str, out: dict) -> None:
        # Atomic write (unique tmp + os.replace) so a crash mid-write never leaves a
        # partial file. Dev (running from source) curates the shipped maps in the repo
        # tracks dir; an installed build writes to app-data — its bundled dir is
        # read-only/ephemeral (a PyInstaller onefile extracts it to a temp dir wiped on
        # exit, so a write there would silently vanish). allow_nan=False hard-fails.
        slug = track_slug(track)
        if not slug:
            raise HTTPException(400, "invalid track")
        user_dir, bundled = _map_dirs()
        target = _in(bundled if _curating() else user_dir, slug)
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

    def _capture_path(track: str) -> Path:
        # Raw point cloud lives in app-data (store.data_dir), NOT the shipped tracks
        # dir -- large + per-user, one file per track (latest capture wins).
        slug = track_slug(track)
        if not slug:  # guard like _write_track_json: never write captures/.npz
            raise HTTPException(400, "invalid track")
        return Path(store.data_dir) / "captures" / f"{slug}.npz"

    def _newest_capture() -> Path | None:
        d = Path(store.data_dir) / "captures"
        files = list(d.glob("*.npz")) if d.exists() else []
        return max(files, key=lambda p: p.stat().st_mtime) if files else None

    def _track_file_data(track: str) -> dict:
        # Merged map dict: authoritative shipped geometry + the user's annotation
        # overlay (corners/sections/rotation). {} if we have neither. Never raises.
        user_dir, bundled = _map_dirs()
        return load_map_data(track, str(bundled), str(user_dir))

    def _clean_sections(raw, length: float) -> list:
        # Validate/normalize a client sections array: [{name,kind,start_dist,end_dist}]
        # with 0<=start<end<=length. 422 on anything malformed.
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise HTTPException(422, "sections must be a list")
        out = []
        for s in raw:
            if not isinstance(s, dict):
                raise HTTPException(422, "each section must be an object")
            try:
                sd, ed = float(s["start_dist"]), float(s["end_dist"])
            except (KeyError, TypeError, ValueError):
                raise HTTPException(422, "section requires numeric start_dist/end_dist")
            if not (math.isfinite(sd) and math.isfinite(ed) and 0.0 <= sd < ed <= length):
                raise HTTPException(422, "section span must satisfy 0 <= start < end <= length")
            out.append({"name": str(s.get("name", ""))[:60],
                        "kind": str(s.get("kind", "section"))[:16],
                        "start_dist": round(sd, 1), "end_dist": round(ed, 1)})
        return out

    def _track_rotation(track: str) -> float:
        # Per-track display rotation (degrees), 0 if unset/invalid (back-compat).
        r = _track_file_data(track).get("rotation", 0.0)
        return float(r) if isinstance(r, (int, float)) else 0.0

    def _is_shipped(track: str) -> bool:
        # True when the track has an authoritative geometry map shipped in the install
        # dir. The client hides layout tools (regenerate/rebuild) for these — a user
        # may re-annotate a built-in track but not replace its layout.
        slug = track_slug(track)
        if not slug:
            return False
        _, bundled = _map_dirs()
        return (bundled / f"{slug}.json").exists()

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
        def frozen():
            data = _track_file_data(track)
            p, pd = data.get("path"), data.get("path_dist")
            if isinstance(p, list) and isinstance(pd, list) and p and len(p) == len(pd):
                return p, pd
            return [], []
        if not force_avg:
            p, pd = frozen()
            if p:
                return p, pd
        laps = _clean_lap_channels(track, car)
        if laps:
            return average_racing_line(laps)
        # Nothing to re-average (a result-log session has no telemetry at all, #28):
        # fall back to the frozen layout rather than returning {}, which the UI
        # reports as "no saved layout" for a track that demonstrably has one.
        return frozen()

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
        payload = _clean(build_track_map(channels, cmap, sectors=sectors,
                                         rotation=_track_rotation(track)))
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
        # Current track: prefer the live SMM read so a track change is detected
        # immediately (else mapping a new track would save under the OLD one). Fall
        # back to the latest captured lap only when SMM is unavailable (sim closed).
        try:
            from ..lmu.smm import read_session_track_car
            tc = read_session_track_car()
            if tc:
                return tc[0]
        except Exception:
            pass
        latest = app.state.latest
        return latest["record"].track if latest is not None else None

    # add_middleware inserts at index 0, so this call must come BEFORE _host_guard
    # below to make _host_guard the OUTERMOST middleware -- it sees OPTIONS
    # preflights before CORSMiddleware does. Security doesn't depend on the
    # ordering (either order refuses), but the insert-at-0 semantics are easy to
    # get backwards in a later tidy-up.
    app.add_middleware(CORSMiddleware, allow_origins=FRONTEND_ORIGINS,
                        allow_methods=["*"], allow_headers=["*"])

    @app.middleware("http")
    async def _host_guard(request: Request, call_next):
        # Also the CSRF guard for every HTTP route, and the only one that doesn't
        # depend on a library default. CORSMiddleware decides whether the browser
        # hands the RESPONSE back to script -- a CORS-simple request is sent and
        # EXECUTED regardless, so CORS can never prevent a side effect. Refusing a
        # disallowed Origin here can. Deliberately NOT filtered to mutating
        # methods: any request that CARRIES a disallowed Origin (all mutating
        # methods, plus a cors-mode GET) has no legitimate caller and is refused.
        # BUT a browser does not attach Origin to a no-cors GET -- <img>,
        # <script>, <link>, <iframe>, and top-level navigation all send none --
        # so this guard does NOT stop a same-window `<img src="/setup">`-style
        # read; _bad_origin sees no Origin and allows it through by design (a
        # MISSING Origin must stay allowed so native/Tauri and non-browser
        # clients work). That used to matter because GET /setup wrote to the DB
        # (save_session_setup); fixed by #51 -- persistence now lives behind
        # POST /setup/capture, so a no-Origin GET here is read-only and has no
        # side effect for this guard to miss.
        host = (request.headers.get("host") or "").split(":")[0].lower()
        if host not in ALLOWED_HOSTS or _bad_origin(request.headers.get("origin")):
            return JSONResponse({"detail": "forbidden host"}, status_code=403)
        return await call_next(request)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/status")
    def status():
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
            "has_map": _has_canonical(track, car),
            "lmu_connected": _ensure_rest() is not None,
            "session_id": session_id,
            "lap_id": lap_id,
        }

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
        m = _canonical_map(track, car, force=bool(avg))
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

    @app.get("/track/map")
    def track_map():
        # The current LIVE track's canonical map — for the Pit Wall, which needs the
        # layout keyed on the track itself (a practice session has no completed lap_id
        # to route through /laps/{id}/map).
        try:
            from ..lmu.smm import read_live_status
            live = read_live_status()
        except Exception:
            live = None
        if not live or not live.get("track"):
            return {}
        track, car = live["track"], live["car"]
        m = _canonical_map(track, car, force=False)
        if m:
            m = {**m, "detected_sectors": store.sector_bounds(track, car) or [],
                 "shipped": _is_shipped(track), "curating": _curating(),
                 "track_name": _track_file_data(track).get("name") or track}
        return m

    @app.get("/tracks/{slug}/map")
    def track_map_by_slug(slug: str):
        # The track's canonical map keyed on its slug -- for the Tracks screen, which
        # selects a track without any completed lap_id to route through /laps/{id}/map.
        # Track files are slug-keyed (same as the notes endpoints), and a shipped
        # layout carries a frozen path, so no car is needed to build it. {} -> the
        # pane shows "no map yet" (a user track with no driven lap).
        if track_slug(slug) != slug:
            raise HTTPException(400, "invalid slug")
        m = _canonical_map(slug, "", force=False)
        if m:
            m = {**m, "detected_sectors": store.sector_bounds(slug, "") or [],
                 "shipped": _is_shipped(slug), "curating": _curating(),
                 "track_name": _track_file_data(slug).get("name") or slug}
        return m

    def _persist_map(track: str, car: str, body: dict) -> dict:
        # SAVE the editor's corner map + sectors as shipped owned data. Direction
        # is ALWAYS re-derived from the averaged path geometry here (never trusted
        # from the client) -- the CLAUDE.md corner-direction invariant. Keyed on
        # (track, car); the lap route resolves those from a lap, the slug route
        # passes the slug with no car (a shipped track edits its frozen ribbon path).
        # If the track already has a reconstructed ribbon, its centerline IS the
        # canonical geometry -- keep it (and re-derive direction on it), don't
        # revert path to the racing-line average on a corner/label/section edit.
        existing = _track_file_data(track)
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
            name = str(c.get("name") or f"Turn {number}")[:80]  # cap: owned data + coach prompt
            label = str(c.get("label") or number)[:8]  # displayed designation ("8A")
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

        sections = _clean_sections(body.get("sections"), length)

        # Optional display-name override. Not an annotation-merge field, so an
        # end-user's app-data copy can't rename a shipped track (its read still takes
        # the bundled name); a dev/user-map edit lands in the map itself.
        new_name = body.get("name")
        name = (new_name.strip()[:80] if isinstance(new_name, str) and new_name.strip()
                else _track_file_data(track).get("name"))

        # Bake per-corner geometry: radius from the path, width/banking from the
        # ribbon (index-aligned with the frozen path) when present. Elevation needs
        # pos_y we don't have on a corner/label edit -> carry forward any baked value.
        rib_w = keep_ribbon.get("width") if isinstance(keep_ribbon, dict) else None
        rib_b = keep_ribbon.get("bank") if isinstance(keep_ribbon, dict) else None
        geo = corner_geometry(path, dist, corners, width=rib_w, bank=rib_b)
        prior = {str(pc.get("label") or pc.get("number")): pc
                 for pc in (existing.get("corners") or [])}
        for c in corners:
            lab = str(c.get("label") or c.get("number"))
            c.update(geo.get(lab, {}))
            ped = prior.get(lab, {}).get("elev_delta_m")
            if ped is not None:
                c["elev_delta_m"] = ped

        out = {
            "track": track_slug(track),
            "corners": corners,
            "path": path,
            "path_dist": dist,
            "sectors": sectors,
            "sections": sections,
            "length": length,
            "rotation": _track_rotation(track),  # preserve existing rotation
        }
        if name:
            out["name"] = name
        if keep_ribbon:
            out["ribbon"] = keep_ribbon  # keep the reconstructed surface across edits
        old_notes = existing.get("notes") or {}
        if old_notes:
            # Carry the authored notes overlay across a rebuild instead of wiping it
            # (#37) -- but per-corner notes are keyed by corner LABEL, and a rebuild
            # can re-split a chicane or relabel '2' as '2A'/'2B'. A stale key then
            # rebinds a note to a DIFFERENT corner, which get_corner states as owned
            # fact ("Authored note: ...") -- worse than losing it. So keep a per-corner
            # note only when the corner count is unchanged AND its label still exists;
            # overview/reference_times/source are not corner-keyed, always keep those.
            old_corners = {str(k): v for k, v in (old_notes.get("corners") or {}).items()}
            same_count = len(existing.get("corners") or []) == len(corners)
            labels = {c["label"] for c in corners}
            kept = {k: v for k, v in old_corners.items() if k in labels} if same_count else {}
            notes = {**old_notes, "corners": kept}
            if any(notes.values()):   # all-empty -> don't write a hollow notes block
                out["notes"] = notes
            dropped = {k: v for k, v in old_corners.items() if k not in kept}
            if dropped:
                # Loud, and with the text, so the author can recover it from the log.
                logging.getLogger(__name__).warning(
                    "track %s: dropped %d authored per-corner note(s) on map save (%s); "
                    "re-author them against the new layout. Dropped: %s",
                    track_slug(track), len(dropped),
                    "corner count changed" if not same_count else "labels no longer exist",
                    json.dumps(dropped, ensure_ascii=False))
        _write_track_json(track, out)
        # Invalidate cmap + canonical caches so the next GET returns the new
        # canonical map (now frozen into the file).
        _invalidate_canon(track)
        return _canonical_map(track, car, force=False)

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

    @app.put("/tracks/{slug}/notes")
    def save_notes(slug: str, body: dict = Body(...)):
        # Persist human-edited/reviewed notes onto the map file's overlay (shipped
        # track in dev curation, app-data otherwise -- same split as _write_track_json).
        track = body.get("track") or slug
        if track_slug(track) != slug:
            raise HTTPException(400, "slug/track mismatch")
        raw = body.get("notes") or {}
        notes = {"overview": str(raw.get("overview", "")),
                 "corners": {str(k): str(v) for k, v in (raw.get("corners") or {}).items()},
                 "reference_times": {str(k): str(v) for k, v in (raw.get("reference_times") or {}).items()},
                 "source": str(raw.get("source", ""))}
        data = dict(_track_file_data(track))       # merged shipped+overlay
        if not data:
            raise HTTPException(404, "no track file to annotate")
        data["notes"] = notes
        _write_track_json(track, data)
        _invalidate_canon(track)                    # same cache-clear as PUT /laps/{id}/map
        return {"ok": True}

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
                "source": str(raw.get("source", ""))}

    @app.get("/tracks")
    def list_tracks():
        # Coverage listing across bundled (shipped) + user-overlay track dirs, for the
        # track-management UI. Reuses _track_file_data's shipped+overlay merge per slug.
        user_dir, bundled = _map_dirs()
        slugs = set()
        for base in (bundled, user_dir):
            if base.exists():
                slugs.update(p.stem for p in base.glob("*.json"))
        out = []
        for slug in sorted(slugs):
            merged = _track_file_data(slug)
            notes = merged.get("notes") or {}
            out.append({
                "slug": slug,
                "name": merged.get("name") or slug.replace("_", " ").title(),
                "has_map": bool(merged.get("path") or merged.get("corners")),
                "has_notes": bool(notes.get("overview") or notes.get("corners")),
                "user_edited": (user_dir / f"{slug}.json").exists(),
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
        _write_track_json(track, data)
        _invalidate_canon(track)
        return _canonical_map(track, car, force=False)

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
        # Reconstruct from the ENTIRE buffer as a point cloud -- no left/right
        # designation. Body {} (Body(...) still required for the CSRF preflight).
        buf = app.state.map_buffer
        if len(buf) < 2:
            raise HTTPException(422, "need at least 2 passes across the width")
        ribbon = reconstruct_surface([r.channels for r in buf])
        if not ribbon:
            raise HTTPException(422, "mapping laps lack position telemetry")
        track = buf[0].track
        corners = _place_corners(ribbon, buf[0].channels)
        extras = _track_file_data(track)
        sectors = extras.get("sectors") or []
        sections = extras.get("sections") or []
        # Pin the track + raw laps NOW: the sim may change track between build and
        # save, and save persists the raw cloud for later rebuilds.
        raw = [{c: r.channels[c] for c in _POS_CHANNELS if c in r.channels} for r in buf]
        app.state.last_ribbon = {"world": ribbon, "corners": corners, "sectors": sectors,
                                 "sections": sections, "track": track, "laps": raw}
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
        out = {
            "track": track_slug(track),
            # Display name from the session (the live track), unless a custom name was
            # already set — so a saved layout carries its track name.
            "name": _track_file_data(track).get("name") or track,
            "corners": corners,
            "ribbon": {k: world[k] for k in ("left", "right", "center", "width", "bank")},
            "path": world["center"],
            "path_dist": world["dist"],
            "sectors": rib["sectors"],
            "sections": rib.get("sections", []),  # preserve existing named sections
            "length": world["dist"][-1] if world["dist"] else 0.0,
            "rotation": _track_rotation(track),  # preserve any existing rotation
        }
        _write_track_json(track, out)
        # Persist the raw point cloud (app-data, not the repo tracks dir) so the
        # ribbon can be rebuilt after algorithm changes with no re-drive.
        if rib.get("laps"):
            _save_cloud(_capture_path(track), rib["laps"])
        _invalidate_canon(track)
        return {"saved": True, "slug": track_slug(track)}

    @app.post("/mapcapture/save-cloud")
    def mapcapture_save_cloud(body: dict = Body(...)):
        # Persist ONLY the raw point cloud from the active buffer -- no ribbon
        # rebuild, no track-map write. Lets a track that already has a
        # driver-placed corner map (e.g. Laguna) gain a capture for later
        # cross-section extraction (width/banking/elevation) WITHOUT replacing
        # its geometry. See docs/superpowers/specs/2026-09-08-per-corner-
        # geometry-metrics-design.md.
        buf = app.state.map_buffer
        if len(buf) < 2:
            raise HTTPException(422, "need at least 2 passes across the width")
        track = buf[0].track
        raw = [{c: r.channels[c] for c in _POS_CHANNELS if c in r.channels} for r in buf]
        if not any(all(c in lap for c in _POS_CHANNELS) for lap in raw):
            raise HTTPException(422, "mapping laps lack position telemetry")
        _save_cloud(_capture_path(track), raw)
        app.state.mapping = False
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
        path = _capture_path(track) if track else _newest_capture()
        if path is None or not path.exists():
            raise HTTPException(422, "no saved capture available")
        laps = _load_cloud(path)
        if not laps:
            raise HTTPException(422, "saved capture is unreadable")
        track = track or path.stem  # offline: take the track (slug) from the file name
        ribbon = reconstruct_surface(laps)
        if not ribbon:
            raise HTTPException(422, "saved capture lacks position telemetry")
        corners = _place_corners(ribbon, laps[0])
        extras = _track_file_data(track)
        sectors = extras.get("sectors") or []
        sections = extras.get("sections") or []
        app.state.last_ribbon = {"world": ribbon, "corners": corners, "sectors": sectors,
                                 "sections": sections, "track": track, "laps": laps}
        res = _clean(ribbon_to_screen(ribbon, corners, sectors,
                                      rotation=_track_rotation(track), sections=sections))
        res["track_name"] = _track_file_data(track).get("name") or track
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
            on_track = None
            try:
                from ..lmu.smm import read_session_track_car
                live = read_session_track_car()          # (track, vehicle) | None
            except Exception:
                live = None
            if live:
                on_track = True
                track, vehicle = live
                try:
                    from .. import __main__ as _m  # resolve_context: map + car name (+ note)
                    cmap, car, _ = _m.resolve_context(
                        rest, str(app.state.tracks_dir), track, vehicle,
                        user_dir=str(Path(store.data_dir) / "tracks"))
                except Exception:
                    cmap, car = _map_for(track), vehicle
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
            # Backfill the current stint id from the latest lap when the client didn't
            # supply one (auto-anchor) — the live path above resolves track/car but not
            # the session stamp.
            if eff_session_id is None and app.state.latest is not None:
                eff_session_id = app.state.latest["record"].session_id
            # No lap captured yet (e.g. a question asked in the garage): stamp the
            # chat with the CURRENT live session so it isn't orphaned — the laps that
            # follow join the same instance and the chat stays resumable via Sessions.
            if eff_session_id is None:
                try:
                    from ..lmu.smm import read_live_session_id
                    base = read_live_session_id()
                    if base:
                        eff_session_id = store.resolve_current_instance(base)
                except Exception:
                    pass
            cfg = config.load_config()
            ctx = CoachContext(app.state.store, record=None, lap_id=None, cmap=cmap,
                               units=cfg.get("units", "metric"),
                               setup=setup, track=track, car=car, on_track=on_track,
                               allowed_adjustments=(legal_adjustments(setup) if setup else None),
                               session_id=eff_session_id,
                               car_notes=load_car_notes(
                                   car, user_dir=str(Path(store.data_dir) / "cars")),
                               setup_concepts=load_setup_concepts())
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
            # What this turn billed. One usage event arrives per model call, so a
            # tool-using question contributes several — summing them here is what
            # makes the ×N tool-loop re-send visible instead of invisible.
            turn_usage = TokenUsage()
            used_model: str | None = None
            try:
                for ev in app.state.coach.ask_agentic_stream(
                        ctx, question, car=car, input_device=req.get("input_device"),
                        history=history, units=ctx.units, driver_profile=driver_profile):
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
                                              question, text, ctx.tool_calls,
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
        arc = app.state.store.session_position_arc(session_id)
        if arc:
            digest.append(f"Race arc: started P{arc['start']}, best P{arc['best']}, "
                          f"worst P{arc['worst']}, finished P{arc['last']}.")

        cmap = _cmap_for_map(track)
        if cmap and cmap.corners:
            best = app.state.store.best_lap(track, car, session_id=session_id)
            weak_counts: dict = {}
            for row in laps:
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
                    digest.append(f"Coach answered: {m['answer'][:200]}")

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

    _LIVE_PERIOD = 0.25  # ~4 Hz; the map/tower don't need capture-rate updates

    @app.websocket("/live")
    async def live_ws(ws: WebSocket):
        # HTTP Host-guard middleware does NOT run for websockets — check here
        # (same preamble as /coach).
        host = (ws.headers.get("host") or "").split(":")[0].lower()
        if host not in ALLOWED_HOSTS or _bad_origin(ws.headers.get("origin")):
            await ws.close(code=1008)
            return
        await ws.accept()

        from ..lmu.live import read_field_snapshot, snapshot_to_dict
        loop = asyncio.get_running_loop()
        try:
            while True:
                # Blocking mmap read off the event loop.
                snap = await loop.run_in_executor(None, read_field_snapshot)
                payload = snapshot_to_dict(snap)
                # Live scoring only carries the team/entry name; map it to the car
                # MODEL the same way /status does (raw name stays the resolve key).
                for c in payload["cars"]:
                    c["car"] = resolve_car(c["car"], c["cls"])
                await ws.send_json(payload)
                await asyncio.sleep(_LIVE_PERIOD)
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
