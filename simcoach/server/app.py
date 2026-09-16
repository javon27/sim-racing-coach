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
from ..coach import _SPOKEN_RE, first_paragraph, split_spoken
from ..coach_tools import CoachContext, race_result_text, scrub_stored_answer
from ..corners import CornerMap, in_window, validated_sections, validated_sectors
from ..driver import DriverProfile
from ..lmu.ingest import ingest_all
from ..model.base import TokenUsage
from ..model.factory import CoachUserError, make_model
from ..pricing import estimate_chat_cost
from ..ribbon import multi_signal_direction, reconstruct_surface, ribbon_to_screen
from ..tiers import adjustable_from_setup, legal_adjustments
from ..track_lookup import (
    corner_apex_key,
    fill_blank_labels,
    guard_corner_notes,
    load_map_data,
    track_slug,
)
from ..trackmap import average_racing_line, build_track_map, direction_from_path

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
# BYTES of `json.dumps(notes)` -- the same serializer, with the same ensure_ascii=True,
# that _write_track_json uses, so the quantity enforced here is the quantity that lands on
# disk. Measuring the unescaped string instead would bound CODE POINTS, and an astral char
# escapes to 12 bytes: an all-emoji payload measured 62,367 code points, passed, and wrote
# 744,367 bytes. Escaped JSON is pure ASCII, so one char is one byte and the cap holds
# whatever the encoding -- at the limit the notes block on disk is ~64.5 KB of ASCII prose
# and ~64.2 KB of emoji (the extra over the cap is indent=2 whitespace).
#
# The number is the intended ceiling MEASURED, not rounded: the largest shipped map has 30
# corners, so 30 notes at the full 2000-char per-note cap plus overview and source at
# theirs serialises to exactly 64,357 bytes. (The raw content is 64,000 -- quoting, keys,
# colons and commas are the other 357, which is why the constant is not a round number.
# Stating 64,000 would have refused the very document the derivation is built from.)
# For scale, the biggest `notes` block in simcoach/tracks/*.json today is 4,412 bytes
# (longest single note 289, longest overview 706, most corner keys 24; shipped notes are
# pure ASCII, so escaping does not move that number), making this ~14.6x the real worst
# case. The per-FIELD caps still count code points, which is what the editor shows the
# author -- on astral text the UI's UTF-16 count and those caps disagree, accepted, and
# this cap is what bounds the consequence.
_MAX_NOTES_DOC_BYTES = 64_357


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

# Host-header allowlist (port stripped before compare) -- AND an input to
# ALLOWED_ORIGINS below, which is a cross product of these hostnames with our two
# local ports. Adding a hostname here to accept a new Host therefore also grants
# it two ORIGINS, widening the CSRF guard without anyone editing it: adding
# "tauri.localhost" (plausible -- it is already an allowed origin host) would
# auto-admit http://tauri.localhost:5173 and :8765. Widen this set deliberately.
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}
REST_PROBE_TTL = 2.0  # s: liveness probe rate cap so /status polling can't hammer LMU's REST
SIDECAR_PORT = 8765   # the port simcoach/server/__main__.py serves on (it imports this)
VITE_DEV_PORT = 5173  # `npm run dev` / src-tauri/tauri.conf.json devUrl
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
#   - our two local ports (sidecar + Vite dev) under BOTH names ALLOWED_HOSTS
#     accepts as a Host. 127.0.0.1 and localhost are the same machine and the
#     same server: accepting Host "localhost:8765" while refusing Origin
#     "http://localhost:8765" would be incoherent, and the same argument applies
#     to the dev port -- hence the cross product rather than four literals, so
#     the host list and the port list cannot drift apart.
# Deliberately dropped vs the old behaviour: "https://tauri.localhost" and every
# other scheme on these hostnames. Nothing serves them; the old guard only let
# them through because it never looked past the hostname.
# NOT auto-widened: if Vite falls back to 5174 (5173 already taken) or you use
# `vite preview` on 4173, that origin is refused with a 403 -- add the port here
# deliberately. Allowing a range would hand the guard back to any local app.
ALLOWED_ORIGINS = set(FRONTEND_ORIGINS) | {"tauri://localhost"} | {
    f"http://{h}:{p}" for h in ALLOWED_HOSTS for p in (SIDECAR_PORT, VITE_DEV_PORT)
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
# What reconstruct_surface actually requires of a lap. pos_y is NOT in here: it is
# optional there (it only fits BANK, which ribbon.py writes as unknown wherever the
# samples lack lateral spread), so demanding it would reject usable laps.
_REBUILD_CHANNELS = ("pos_x", "pos_z", "lap_distance")


def _usable_cloud(ch: dict) -> bool:
    """Will reconstruct_surface get >= 2 finite (x, z, d) samples out of this lap?

    The ONE test behind both the /tracks count and what a rebuild consumes (#69), so a
    badge can neither promise a lap the build then drops nor count one it should not:
    a zero-length or all-NaN channel is refused here, not counted AND fed to the build.
    """
    try:
        cols = [np.asarray(ch[c], dtype=float) for c in _REBUILD_CHANNELS]
    except (KeyError, TypeError, ValueError):
        return False
    if any(a.ndim != 1 for a in cols):
        return False
    n = min(len(a) for a in cols)
    x, z, d = (a[:n] for a in cols)
    return int((np.isfinite(x) & np.isfinite(z) & np.isfinite(d)).sum()) >= 2


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
    Returns when `source.laps()` is exhausted or raises -- the caller
    (build_app) wraps this in a retry loop so a real LMU source reconnects
    after a drop."""
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
               tracks_dir=None, *, transcriber=None, installer=None) -> FastAPI:
    app = FastAPI(title="SimCoach", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    app.state.coach = coach
    from ..voice import (
        ModelInstaller,
        NotInstalled,
        VoiceInputError,
        WhisperTranscriber,
        decode_wav,
    )
    # The whisper model lives at the app-data ROOT (next to the config), not in the
    # telemetry dir the store owns; move an install from the first release across.
    from .. import paths as _paths
    from ..voice import migrate_model_dir
    voice_root = str(_paths.app_data_dir())
    migrate_model_dir(store.data_dir, voice_root)
    app.state.transcriber = transcriber or WhisperTranscriber(voice_root)
    app.state.voice_installer = installer or ModelInstaller(voice_root)
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

    def _write_track_json(track: str, out: dict) -> dict:
        # Returns the authored per-corner notes this write dropped (key -> text), which
        # every route hands back to the client: a drop that only reaches the log is a drop
        # the driver never hears about.
        #
        # THE one writer of a track JSON file, and therefore the one place the rules for
        # what may land in one live (#65). There used to be four writers and three
        # validators each wired to one of them: `_persist_map` validated sections,
        # sectors and the notes overlay, and the other three wrote whatever they were
        # handed -- which is how POST /mapcapture/save silently deleted every authored
        # note for a track (30,445 bytes / 135 notes across the shipped maps were one
        # "Save ribbon" away), and how an unvalidated `sections` span could commit a map
        # that CornerMap.from_data refuses to open, killing the track for every client.
        # A shared validator only two of four writers call is a half-done
        # centralisation; validate HERE, where nothing can write around it.
        #
        # Atomic write (unique tmp + os.replace) so a crash mid-write never leaves a
        # partial file. Dev (running from source) curates the shipped maps in the repo
        # tracks dir; an installed build writes to app-data — its bundled dir is
        # read-only/ephemeral (a PyInstaller onefile extracts it to a temp dir wiped on
        # exit, so a write there would silently vanish). allow_nan=False hard-fails.
        slug = track_slug(track)
        if not slug:
            raise HTTPException(400, "invalid track")
        dropped: dict = {}
        out = _validated_track_data(track, slug, out, dropped)
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
        return dropped

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

    def _live_lap_paths() -> list[tuple[str, str, str]]:
        # (slug, raw live name, telemetry_path) for every LIVE-captured lap, newest last.
        #
        # telemetry_path IS NOT NULL is the whole distinction: save_result_session rows
        # are summary-only with no file, so a track with ~991 imported laps and no
        # capture is correctly not buildable (#66). Slugged here because laps store the
        # raw live name ("Paul Ricard - ELMS") while a map file is keyed by slug.
        with store._lock:   # store.py shares one connection with the capture thread
            rows = store.conn.execute(
                "SELECT track, telemetry_path FROM laps "
                "WHERE telemetry_path IS NOT NULL AND track IS NOT NULL "
                "ORDER BY id").fetchall()
        return [(track_slug(n), n, p) for n, p in rows if track_slug(n)]

    def _rebuild_clouds(track: str, rows=None, *, capture: bool = True,
                        cache: dict | None = None) -> tuple[list[dict], bool]:
        # (laps, from_capture): EXACTLY the usable clouds a rebuild of `track` consumes.
        # /tracks counts this and /mapcapture/rebuild builds from it, so the badge and
        # the build cannot disagree (#69) -- e.g. a save-cloud capture of 2 laps beside 7
        # live laps is "2", because the capture is what gets built.
        #
        # A purpose-built mapping capture WINS whenever there is one: its boundary passes
        # cover the full width; racing laps only trace the line driven. Otherwise the
        # position channels off the track's own LIVE laps' .npz files, in the same shape
        # _load_cloud returns. Laps recorded before position capture landed (47 channels,
        # no pos_*) are not usable and so are neither counted nor built from.
        #
        # ([], True) is its own state: a capture EXISTS and yields nothing (corrupt, or no
        # usable lap in it). It still wins -- never a silent fall-back to other data (#69)
        # -- so callers must name it, not read it as "no laps with position data" (#84).
        # capture=False skips the capture, to count what deleting it would leave.
        # cache (#105): an optional {(telemetry_path, mtime): ch} dict, checked/filled
        # per live lap so a re-open only reads files that changed. Only /tracks passes
        # one; the build path leaves it None and is byte-for-byte unaffected.
        slug = track_slug(track)
        if not slug:
            # A map file name with nothing to slug ("---.json" dropped into app-data
            # tracks/) has no capture and no laps. Count it as none -- _capture_path's
            # writer guard would 400 the whole GET /tracks, the same one-bad-file-blanks-
            # the-screen failure R1/R2 fixed for lap files.
            return [], False
        path = _capture_path(track)
        if capture and path.exists():
            return [ch for ch in _load_cloud(path) if _usable_cloud(ch)], True
        laps = []
        for lap_slug, _name, p in (_live_lap_paths() if rows is None else rows):
            if lap_slug != slug:
                continue
            key = None
            if cache is not None:
                try:
                    key = (p, os.stat(p).st_mtime)
                except OSError:
                    key = None
            if key is not None and key in cache:
                ch = cache[key]
            else:
                try:
                    with np.load(p) as z:
                        ch = {c: z[c] for c in _POS_CHANNELS if c in z.files}
                except Exception as e:
                    # Skip THIS lap, never the listing or the rebuild. Not a narrow tuple:
                    # a zero-byte file raises EOFError and a corrupt member zlib.error, and
                    # either one used to 500 all of GET /tracks. Same stance as _load_cloud.
                    logging.getLogger(__name__).warning("skipping unreadable lap %s: %r", p, e)
                    continue
                if key is not None:
                    cache[key] = ch
            if _usable_cloud(ch):
                laps.append(ch)
        return laps, False

    def _track_file_data(track: str) -> dict:
        # Merged map dict: authoritative shipped geometry + the user's annotation
        # overlay (corners/sections/rotation). {} if we have neither. Never raises.
        user_dir, bundled = _map_dirs()
        return load_map_data(track, str(bundled), str(user_dir))

    def _validated_track_data(track: str, slug: str, out: dict, dropped: dict) -> dict:
        """Everything that must be true of a track JSON, applied to what a writer is
        about to persist. Called only from `_write_track_json`, so every writer gets it
        (#65) -- do not call it from a route, and do not write around it.

        Refuses (422) what would commit a map the app cannot open: `CornerMap.from_data`
        raises on a bad `sections` span, so an unvalidated one kills the track for every
        client, and `sectors` reach the client as-is (strings and `true` both used to
        land in a shipped file). Per-corner geometry is DROPPED rather than refused:
        it is derived, re-bakeable data, and a string in `radius_m` is a crash in
        `geometry_clause` on the coach's next answer -- refusing would make the editor
        the only way to fix a file the editor then cannot save.

        The authored `notes` overlay is carried across the rewrite unless the caller
        passes its own (only `PUT /tracks/{slug}/notes` does -- it IS the author typing,
        including keys that name no corner yet, which the Tracks screen surfaces). So a
        writer that rebuilds a track file gets the guard for free, and one that seeds its
        payload from `_track_file_data` keeps what was already there.
        """
        existing = _track_file_data(track)
        out = dict(out)
        length = out.get("length") or out.get("length_m")
        if not measurable_geometry("length", length):
            # No usable length to bound a span against (a corner-less ribbon save writes
            # 0.0). Shape, type and ordering still hold; only the upper bound relaxes.
            length = math.inf
        try:
            sectors = validated_sectors(out.get("sectors") or [], length)
            sections = validated_sections(out.get("sections"), length)
        except (TypeError, ValueError) as e:
            raise HTTPException(422, str(e)) from None
        # Only write the key back if it was there or has content: a validated empty list
        # is not a reason to add `"sections": []` to a file that never had one.
        for key, value in (("sectors", sectors), ("sections", sections)):
            if key in out or value:
                out[key] = value
        if isinstance(out.get("corners"), list):
            out["corners"] = [_clean_corner_geometry(slug, c) for c in out["corners"]]
        if "notes" not in out:
            notes = guard_corner_notes(slug, existing.get("notes"),
                                       existing.get("corners"), out.get("corners"), dropped)
            if notes:
                out["notes"] = notes
        return out

    def _clean_corner_geometry(slug: str, corner: dict) -> dict:
        # Strip a baked per-corner metric that isn't a finite number of the right sign.
        # `measurable_geometry` is the same owner the save path uses to decide whether a
        # stored metric is a measurement (#62), so "what may be written" and "what may be
        # carried forward" cannot disagree. A string radius loads fine and then raises
        # `Unknown format code 'f'` inside geometry_clause -- on the coach's answer, far
        # from the save that wrote it, and no test over the shipped maps caught it.
        if not isinstance(corner, dict):
            return corner
        bad = [k for k in CORNER_GEOMETRY_KEYS
               if k in corner and not measurable_geometry(k, corner[k])]
        if not bad:
            return corner
        # Types, not values: the value is junk by definition, and a str() of it is
        # unbounded input on its way into a log line.
        logging.getLogger(__name__).warning(
            "track %s corner %.40s: dropped unusable baked geometry %s; each must be a "
            "finite number (a length also > 0).",
            slug, str(corner.get("label") or corner.get("number")),
            {k: type(corner[k]).__name__ for k in bad})
        return {k: v for k, v in corner.items() if k not in bad}

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

    def _frozen_outline(track: str):
        # The outline stored in the track file as (path, path_dist), else ([], []).
        data = _track_file_data(track)
        p, pd = data.get("path"), data.get("path_dist")
        if isinstance(p, list) and isinstance(pd, list) and p and len(p) == len(pd):
            return p, pd
        return [], []

    def _canonical_outline(track: str, car: str, force_avg: bool):
        # The track's canonical (path_world, dist): frozen file path first (unless
        # forcing a re-average), else averaged over the track+car's own pos laps,
        # else ([], []). The map is per-track owned data, NOT the selected lap.
        if not force_avg:
            p, pd = _frozen_outline(track)
            if p:
                return p, pd
        laps = _clean_lap_channels(track, car)
        if laps:
            return average_racing_line(laps)
        # Nothing to re-average (a result-log session has no telemetry at all, #28):
        # fall back to the frozen layout rather than returning {}, which the UI
        # reports as "no saved layout" for a track that demonstrably has one.
        return _frozen_outline(track)

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

    def _map_payload(track: str, car: str, force: bool = False) -> dict:
        # What every map GET returns: the canonical map plus what the editor needs around
        # it. {} stays {} (the pane's "no map yet").
        m = _canonical_map(track, car, force)
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

    def _slug_map_key(slug: str) -> tuple[str, str]:
        # The (track, car) a slug's map is drawn from. Normally (slug, ""): a stored outline
        # needs no car. A track file with corners and NO stored outline (shipped Spa
        # predates them, #82) has nothing to draw by slug -- laps are stored under the live
        # name + car -- so borrow exactly what /laps/{id}/map draws for that track: the
        # clean laps of the car driven there MOST RECENTLY (highest lap id), or of the next
        # most recent car if that one has no clean position lap. Corners still come from
        # the file, and nothing is written.
        # No corners, no fallback: a driven-but-unmapped track keeps {} so the Tracks
        # screen still offers "Build map" (#69) rather than a corner-less outline.
        if _frozen_outline(slug)[0] or not _track_file_data(slug).get("corners"):
            return slug, ""
        with store._lock:   # store.py shares one connection with the capture thread
            rows = store.conn.execute(
                "SELECT track, car FROM laps WHERE telemetry_path IS NOT NULL "
                "AND track IS NOT NULL AND car IS NOT NULL "
                "GROUP BY track, car ORDER BY MAX(id) DESC").fetchall()
        for track, car in rows:
            if track_slug(track) == slug and _canonical_map(track, car, force=False):
                return track, car
        return slug, ""

    def _invalidate_canon(track: str) -> None:
        # By SLUG, not by the string the caller holds: one track is cached under its live
        # name (lap routes, the coach) AND under its slug (Tracks screen), and a write
        # through either route has to clear both -- the slug route's map can now be the
        # live-name-keyed entry (#82), which a slug-only pop left stale until restart.
        slug = track_slug(track)
        for key in [k for k in app.state.cmaps if track_slug(k) == slug]:
            app.state.cmaps.pop(key, None)
        for key in [k for k in app.state.canon if track_slug(k[0]) == slug]:
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
        # The cap applies to a FAILED probe too. While LMU's REST port accepts the
        # connection but never answers (seen at the rig 2026-09-15: every /status
        # took the full 4 s timeout, three pollers filled the browser's connection
        # pool, and the whole UI queued behind them), a disconnected state must not
        # re-probe on every poll -- remember the failure for one TTL as well.
        if (now - app.state.rest_probed_at) < REST_PROBE_TTL:
            return rest
        app.state.rest_probed_at = now
        if rest is None:
            if app.state.rest_factory is None:
                return None
            try:
                rest = app.state.rest_factory()
            except Exception:
                return None
        try:
            # Fast TCP check first (300 ms bound) so a closed LMU never costs the
            # full HTTP timeout; then the real liveness probe.
            reachable = getattr(rest, "reachable", None)
            if reachable is not None and not reachable():
                raise ConnectionError("LMU REST port not accepting connections")
            rest.get_setup()  # liveness probe
        except Exception:
            app.state.rest = None
            return None
        app.state.rest = rest
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

    @app.get("/laps/{lap_id}/inputs")
    def lap_inputs(lap_id: int):
        # Lap-keyed input traces for the coaching screen's Inputs panel (#95):
        # selected lap vs its track/car best, both resampled the same way as
        # /track/inputs (which stays the Pit Wall's live-keyed version).
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
        return _map_payload(live["track"], live["car"])

    @app.get("/track/inputs")
    def track_inputs():
        # Driver-input traces (throttle/brake/steering) for the Pit Wall graphs (#21):
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
            name = str(c.get("name") or f"Turn {number}")[:80]  # cap: owned data + coach prompt
            label = str(c.get("label") or "")[:8]  # displayed designation ("8A"); blank filled below
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
        name = (new_name.strip()[:80] if isinstance(new_name, str) and new_name.strip()
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
            return s

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
                out[k] = _text(v, f"{field} note")
            return out

        notes = {"overview": _text(raw.get("overview", ""), "overview"),
                 "corners": _text_map(raw.get("corners"), "corner notes"),
                 "reference_times": _text_map(raw.get("reference_times"), "reference times"),
                 "source": _text(raw.get("source", ""), "source")}
        # Serialised exactly as _write_track_json will serialise it, so this measures the
        # bytes that land on disk. Checked after the per-field caps so an over-limit field
        # still gets its own named error.
        if len(json.dumps(notes)) > _MAX_NOTES_DOC_BYTES:
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
                                 "sections": sections, "track": track, "laps": raw,
                                 "built_from": "capture"}
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
        dropped = _write_track_json(track, out)
        # Persist the raw point cloud (app-data, not the repo tracks dir) so the
        # ribbon can be rebuilt after algorithm changes with no re-drive.
        if rib.get("laps"):
            _save_cloud(_capture_path(track), rib["laps"])
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
                    from ..track_lookup import resolve_context
                    cmap, car, _ = resolve_context(
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
                                                "content": scrub_stored_answer(row["answer"],
                                                                               row["ts"],
                                                                               row.get("track"))})
                    except Exception:
                        history = []

            text = ""
            # What this turn billed. One usage event arrives per model call, so a
            # tool-using question contributes several — summing them here is what
            # makes the ×N tool-loop re-send visible instead of invisible.
            turn_usage = TokenUsage()
            used_model: str | None = None
            # PTT: the [spoken] cue is the FIRST line (SPOKEN_SENTENCE). Buffer text
            # deltas until the first newline arrives, so that line can be emitted as
            # its own `spoken` event (and spoken immediately client-side) instead of
            # waiting for the whole answer. Not buffered outside PTT.
            spoken_buf = "" if voice == "ptt" else None
            spoken_resolved = voice != "ptt"
            spoken = None   # defined even if the stream raises before it's resolved below
            try:
                for ev in app.state.coach.ask_agentic_stream(
                        ctx, question, car=car, input_device=req.get("input_device"),
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
                        if not spoken_resolved:
                            spoken_buf += ev["text"]
                            if "\n" not in spoken_buf:
                                continue          # first line not complete yet — keep buffering
                            first_line, rest = spoken_buf.split("\n", 1)
                            spoken_resolved = True
                            m = _SPOKEN_RE.match(first_line)
                            if m and m.group(1):
                                _emit({"type": "spoken", "text": m.group(1)})
                                rest = rest.removeprefix("\n")   # also drop the blank line after the tag
                                if rest:
                                    _emit({"type": "text", "text": rest})
                            elif spoken_buf:
                                _emit({"type": "text", "text": spoken_buf})
                            continue
                    _emit(ev)
                if not spoken_resolved and spoken_buf:
                    # Stream ended before a newline ever arrived (single-line reply).
                    m = _SPOKEN_RE.match(spoken_buf)
                    if m and m.group(1):
                        _emit({"type": "spoken", "text": m.group(1)})
                    else:
                        _emit({"type": "text", "text": spoken_buf})
                if voice == "ptt":
                    text, spoken = split_spoken(text)
                    spoken = spoken or first_paragraph(text)
                terminal = {"type": "done", "answer": text}
                if voice == "ptt":
                    terminal["spoken"] = spoken
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
                                              usage=turn_usage.as_dict(), model=used_model,
                                              spoken=spoken)
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

    VOICE_MAX_BODY = 8_000_000   # bytes; a 30 s 16 kHz mono WAV is ~1 MB

    @app.post("/voice/transcribe")
    async def voice_transcribe(request: Request):
        # Raw WAV body (not JSON). Localhost-only + the Host/Origin middleware already
        # gate this; a cross-origin no-cors POST could only cost local CPU.
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
