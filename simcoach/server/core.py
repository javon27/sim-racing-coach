"""Per-app core helpers every route module shares (#113): corner-map resolution, map/capture
paths, cache invalidation, the rate-capped LMU REST probe, the live track/session reads."""
from __future__ import annotations

import sys
import time
from pathlib import Path

from fastapi import HTTPException

from ..coach_tools import CoachContext
from ..track_lookup import track_slug

REST_PROBE_TTL = 2.0  # s: liveness probe rate cap so /status polling can't hammer LMU's REST
# The liveness probe itself must finish well inside REST_PROBE_TTL, or a hung LMU
# re-blocks the next tick before this one's failure is even cached (#1). LmuRest's
# normal request timeout (4.0s) is for real setup reads, not this probe.
REST_PROBE_TIMEOUT = 1.5


def _in(base: Path, slug: str) -> Path:
    # <base>/<slug>.json, rejecting any path that escapes base (defense in depth:
    # track_slug already sanitizes to [a-z0-9_], but this guards owned data).
    # `b in p.parents` alone accepts a slug containing a separator (e.g.
    # "sub/evil") -- it resolves INSIDE base, so b is one of its parents, just
    # not the immediate one. Also require the immediate parent be base itself
    # (#18), so the result is always a direct child, never a subdirectory.
    # Module-level (no closure over app/store) so it's directly unit-testable.
    b = base.resolve()
    p = (b / f"{slug}.json").resolve()
    if b not in p.parents or p.parent != b:
        raise HTTPException(400, "invalid track")
    return p


def export(deps, *fns) -> None:
    # Publish closures on the shared namespace under their own names, so a consumer binds
    # `_x = deps._x` and its moved body reads exactly as it did inside create_app.
    for f in fns:
        setattr(deps, f.__name__, f)


def register(app, deps) -> None:
    store = app.state.store

    def _map_for(track):
        # Resolve a corner map for a specific track, following track changes.
        # Only non-None results are cached, so a still-missing map is re-checked
        # cheaply on the next call (it may appear once a lap completes).
        if not track:
            return None
        _fresh(track)
        cached = app.state.cmaps.get(track)
        if cached is not None:
            return cached
        if app.state.map_resolver is not None:
            m = app.state.map_resolver(track)
            if m is not None:
                app.state.cmaps[track] = m
            return m
        return app.state.cmap
    app.state.map_for = _map_for   # #152: the race worker resolves a lap's corner map

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
        app.state.live_cmap = None  # /live re-resolves the corner map on its next tick
        app.state.map_rev += 1      # /events re-pushes the live map on its next tick (#155)

    def _fresh(track) -> None:
        # #158: a track file changed on disk OUTSIDE the sidecar (a curation script,
        # promote_map, a hand edit) invalidates like a save does -- including map_rev, so
        # /events re-pushes the live map. Every per-track cache read calls this BEFORE it
        # reads, so a rewrite that lands between the stat and the read only costs one
        # spare invalidation. One stat() per file: cheap enough for /live's 4 Hz tick.
        slug = track_slug(track) if track else ""
        if not slug:
            return
        user_dir, bundled = _map_dirs()
        sig = []
        for d in (bundled, user_dir):
            try:
                st = (Path(d) / f"{slug}.json").stat()
                sig.append((st.st_mtime_ns, st.st_size))
            except OSError:
                sig.append(None)
        seen = app.state.track_sigs.get(slug)
        app.state.track_sigs[slug] = sig
        if seen is not None and seen != sig:
            _invalidate_canon(track)
            app.state.track_sigs[slug] = sig

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
            # Bound this call to REST_PROBE_TIMEOUT (< REST_PROBE_TTL) so a port that
            # accepts but never answers can't cost the full 4s request timeout (#1).
            # Falls back to the plain call for test doubles that don't accept the
            # kwarg -- get_setup(probe_timeout=...) raises TypeError before running
            # any of the fake's body, so the fallback call is the only real one.
            try:
                rest.get_setup(probe_timeout=REST_PROBE_TIMEOUT)
            except TypeError:
                rest.get_setup()
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

    def _live_session_id() -> str | None:
        # The store instance id for the CURRENT live track/car (what the next
        # captured lap -- or a garage chat started before one -- gets stamped
        # with), or None if LMU isn't running / no player. Not cached: SMM reads
        # are cheap and each call site wants a fresh read.
        try:
            from ..lmu.smm import read_live_session_id
            base = read_live_session_id()
        except Exception:
            base = None
        return store.resolve_current_instance(base) if base else None

    def _session_optimal(sid: str | None, laps: list[dict]) -> float | None:
        """#176: the lap rail's Optimal figure for a session -- the same headline
        `get_time_on_table` states for its best lap, computed the same way
        (`CoachContext.get_optimal_seconds`, sharing every rule with the coach tool
        rather than re-deriving them). Cached per session, keyed on the same
        (count, last id) signal /events' laps frame already uses to invalidate --
        analyze_lap runs once per comparable lap, so this is only redone when a lap
        actually lands, never on every poll."""
        if not sid or not laps:
            return None
        sig = (len(laps), laps[-1]["id"])
        cached = app.state.optimal_cache.get(sid)
        if cached is not None and cached[0] == sig:
            return cached[1]
        track = next((r["track"] for r in laps if r.get("track")), None)
        car = next((r["car"] for r in laps if r.get("car")), None)
        cmap = _map_for(track) if track else None
        value = None
        if track and car and cmap is not None:
            value = CoachContext(store, cmap=cmap, track=track, car=car,
                                 session_id=sid).get_optimal_seconds()
        app.state.optimal_cache[sid] = (sig, value)
        return value

    export(deps, _map_for, _curating, _map_dirs, _capture_path, _newest_capture,
           _invalidate_canon, _fresh, _ensure_rest, _live_track, _live_session_id,
           _session_optimal)
