"""GET /health and GET /status: the live track/car/session strip (#113)."""
from __future__ import annotations

from ..car_lookup import resolve_car
from .core import export


def register(app, deps) -> None:
    store = app.state.store
    _has_canonical = deps._has_canonical
    _live_map_key = deps._live_map_key
    _ensure_rest = deps._ensure_rest
    _live_session_id = deps._live_session_id
    _latest_is_live = deps._latest_is_live
    _read_live_base = deps._read_live_base

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
        base = _read_live_base() if live else None  # one SMM read for both uses below
        matches_live = latest is not None and _latest_is_live(latest["record"], live_tc, base)
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
            session_id = _live_session_id(base)
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

    export(deps, _status_view, status)
