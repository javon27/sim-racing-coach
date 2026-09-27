"""Per-app core helpers every route module shares (#113): corner-map resolution, map/capture
paths, cache invalidation, the rate-capped LMU REST probe, the live track/session reads."""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

from fastapi import HTTPException

from ..coach_tools import CoachContext
from ..store import _session_base
from ..track_lookup import track_slug

REST_PROBE_TTL = 2.0  # s: liveness probe rate cap so /status polling can't hammer LMU's REST
# The liveness probe itself must finish well inside REST_PROBE_TTL, or a hung LMU
# re-blocks the next tick before this one's failure is even cached (#1). LmuRest's
# normal request timeout (4.0s) is for real setup reads, not this probe.
REST_PROBE_TIMEOUT = 1.5
# #176 fix round 1: bounded LRU for the background optimal-lap cache (one entry per
# session id ever asked about), so a long-running sidecar reviewing many past
# sessions doesn't grow this unbounded.
_OPTIMAL_CACHE_MAX = 32


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

    app.state.ensure_rest = _ensure_rest   # #188: the setup worker reads each lap's setup

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

    def _read_live_base() -> str | None:
        # The live BASE session id (`track--car--online|offline`), or None if LMU isn't
        # running / no player / the read fails.
        try:
            from ..lmu.smm import read_live_session_id
            return read_live_session_id()
        except Exception:
            return None

    def _live_session_id(base=...) -> str | None:
        # The store instance id for the CURRENT live track/car (what the next
        # captured lap -- or a garage chat started before one -- gets stamped
        # with), or None if LMU isn't running / no player. Not cached: SMM reads
        # are cheap and each call site wants a fresh read. `base`: an already-read
        # _read_live_base() result, so one request reads SMM once.
        if base is ...:
            base = _read_live_base()
        return store.resolve_current_instance(base) if base else None

    def _latest_is_live(record, live, base=...) -> bool:
        # Is the latest captured lap from the LIVE session? `live` is the live
        # (track, car), None when SMM is down (then the latest lap stands). Compared on
        # the base session id, which carries online|offline too: a question in the
        # online garage before the first online lap must not take the last OFFLINE
        # lap's id (#170). (track, car) only if the live id can't be read.
        if live is None:
            return True
        if base is ...:
            base = _read_live_base()
        if base:
            return _session_base(record.session_id or "") == base
        return (record.track, record.car) == tuple(live)

    def _optimal_sig(laps: list[dict]) -> tuple:
        """The cache key for #176's background optimal compute. Recomputes on: a lap
        landing (count + last id), a corner map appearing or being re-saved (map_rev
        -- the same signal #155 already bumps for /events' map frame), and a
        result-log import backfilling sectors onto EXISTING rows without adding one
        (the count of laps with all three sectors set -- `save_result_session`'s
        `_BACKFILL_SECTORS_SQL` only ever fills previously-NULL sectors, never
        overwrites a set one, so this count strictly increases on a real backfill and
        is cheap: `laps` is already fetched, no extra query).

        Not covered, undocumented gap accepted (#176 fix round 1): a stored sector
        edited or cleared some OTHER way. Nothing in this codebase does that today --
        every write path only ever inserts a lap or fills a previously-NULL sector."""
        sectors_filled = sum(1 for r in laps if r.get("s1") is not None
                             and r.get("s2") is not None and r.get("s3") is not None)
        return (len(laps), laps[-1]["id"], app.state.map_rev, sectors_filled)

    def _compute_optimal(sid: str, sig: tuple, laps: list[dict]) -> None:
        # Runs on app.state.optimal_pool's single worker thread -- never the request
        # thread, never the /events tick. Any failure here must surface as a log line
        # only: a bad lap or an analysis bug must never take down the lap rail or the
        # /events loop that carries it (#176 fix round 1's raise probe).
        value = None
        try:
            track = next((r["track"] for r in laps if r.get("track")), None)
            car = next((r["car"] for r in laps if r.get("car")), None)
            cmap = _map_for(track) if track else None
            if track and car and cmap is not None:
                value = CoachContext(store, cmap=cmap, track=track, car=car,
                                     session_id=sid).get_optimal_seconds()
        except Exception:
            logging.getLogger(__name__).warning(
                "#176 optimal compute failed for session %r", sid, exc_info=True)
            value = None
        with app.state.optimal_lock:
            app.state.optimal_cache[sid] = (sig, value)
            app.state.optimal_cache.move_to_end(sid)
            while len(app.state.optimal_cache) > _OPTIMAL_CACHE_MAX:
                app.state.optimal_cache.popitem(last=False)
            app.state.optimal_inflight.pop(sid, None)

    def _ensure_optimal(sid: str | None, laps: list[dict]) -> tuple[float | None, bool]:
        """#176 fix round 1: the lap rail's Optimal figure for a session, computed the
        same way the coach's get_time_on_table does (`CoachContext.get_optimal_seconds`,
        sharing every rule -- see `_compute_optimal`) but NEVER on the caller's thread.

        Schedules a background recompute when the cache is stale for this session
        (single-flight: at most one in-flight compute per session id, and at most one
        compute running at all at any moment, since `optimal_pool` has exactly one
        worker -- so a driver's PTT question and a lap landing never contend for more
        than one extra core between them), then returns
        `(value, pending)` -- `pending` is True while a compute for this session is
        scheduled or still running (#176 fix round 2: lets a caller that just opened
        a session, with nothing cached yet, know to check back rather than conclude
        there's simply no optimal). `value` is None whenever there's nothing FRESH to
        report: no session asked about yet, OR the cached value's signature is stale
        (#176 fix round 3 should-fix: a new lap landing must never keep showing the
        PREVIOUS lap's optimal against the new best -- that's a false gap on the
        rail, worth 2-4 s of a wrong-looking number every time a lap lands. Hiding the
        row until the fresh value lands is the honest choice, matching this repo's
        rule that an unverified/stale value is cut, not hedged.) Never blocks: the
        REST endpoint that calls this returns instantly, and the /events tick that
        also calls this (to decide whether to push a fresh 'optimal' frame once the
        compute lands) never awaits it either."""
        if not sid or not laps:
            return None, False
        sig = _optimal_sig(laps)
        with app.state.optimal_lock:
            cached = app.state.optimal_cache.get(sid)
            fresh = cached is not None and cached[0] == sig
            if not fresh and sid not in app.state.optimal_inflight:
                fut = app.state.optimal_pool.submit(_compute_optimal, sid, sig, laps)
                app.state.optimal_inflight[sid] = fut
            value = cached[1] if fresh else None
            pending = sid in app.state.optimal_inflight
            return value, pending

    export(deps, _map_for, _curating, _map_dirs, _capture_path, _newest_capture,
           _invalidate_canon, _fresh, _ensure_rest, _live_track, _live_session_id,
           _read_live_base, _latest_is_live, _ensure_optimal)
