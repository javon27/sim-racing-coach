"""Sidecar entry point (Tauri spawns this): build the FastAPI app against the
real app-data paths and serve it on localhost. Uses LazyModel (not make_model)
so a fresh install with no Anthropic key still boots and can reach /settings
to enter one."""
from __future__ import annotations

import threading
import time

from .. import __main__ as m  # reuse track/car/corner-map resolution helpers
from ..coach import Coach
from ..config import load_config
from ..model.factory import LazyModel
from ..paths import db_path, telemetry_dir
from ..store import LapStore
from .app import create_app, _capture_loop


def build_app():
    cfg = load_config()
    store = LapStore(str(db_path()), str(telemetry_dir()))
    coach = Coach(LazyModel(model=cfg["model"]), str(m.PKG_DIR / "knowledge"))

    cmap = rest = None
    try:
        # ponytail: cmap fixed at boot; live re-resolution arrives with WS /telemetry (Phase 2/3)
        tc = m.read_session_track_car()  # None when LMU is closed / no player on track
        if tc is not None:
            track_name, _vehicle = tc
            rest = m.LmuRest()
            try:
                rest.get_setup()
            except Exception:
                rest = None  # REST unavailable — degrade, don't block boot
            # ponytail: no auto-build of a missing corner map at boot (needs a completed lap); /laps analysis 404s for an unmapped track until Phase 2/3
            map_path = m.resolve_track_map(track_name, str(m.TRACKS_DIR))
            if map_path is not None:
                cmap = m.CornerMap.load(str(map_path))
    except Exception:
        cmap = rest = None  # offline: never let resolution failures block boot

    app = create_app(store, coach, cmap, rest)

    def _capture_forever():
        # Retries like simcoach.app.run_repl's capture thread: LMU may not be
        # running yet (or drops mid-session) -- wait and reopen rather than
        # dying, and never let a failure here block/crash the sidecar.
        while True:
            try:
                _capture_loop(m.LmuSource(), store, app.state)
            except Exception:
                pass
            time.sleep(3)

    app.state.capture_thread = threading.Thread(target=_capture_forever, daemon=True)
    app.state.capture_thread.start()

    return app


def main():
    import uvicorn
    uvicorn.run(build_app(), host="127.0.0.1", port=8765)


if __name__ == "__main__":
    main()
