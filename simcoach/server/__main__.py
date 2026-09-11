"""Sidecar entry point (Tauri spawns this): build the FastAPI app against the
real app-data paths and serve it on localhost. Uses LazyModel (not make_model)
so a fresh install with no Anthropic key still boots and can reach /settings
to enter one."""
from __future__ import annotations

import threading
import time
from pathlib import Path

from .. import __main__ as m  # reuse track/car/corner-map resolution helpers
from ..coach import Coach
from ..config import load_config
from ..lmu.ingest import poll_results
from ..logging_setup import configure_logging
from ..model.factory import LazyModel
from ..paths import db_path, telemetry_dir
from ..store import LapStore
from .app import SIDECAR_PORT, create_app, _capture_loop


def build_app():
    configure_logging()
    cfg = load_config()
    store = LapStore(str(db_path()), str(telemetry_dir()))
    coach = Coach(LazyModel(model=cfg["model"]))

    user_tracks_dir = str(Path(store.data_dir) / "tracks")

    def _map_resolver(track):
        # Resolve+load the corner map for a track name, or None if unmapped. Called
        # per-lap so a track change follows the live car. Shipped geometry + the
        # user's annotation overlay (names/labels/etc.) so the coach uses the
        # driver's own corner names.
        try:
            data = m.load_map_data(track, str(m.TRACKS_DIR), user_dir=user_tracks_dir)
            if data.get("corners"):
                return m.CornerMap.from_data(data)
        except Exception:
            pass
        return None

    app = create_app(store, coach, cmap=None, rest=None,
                     map_resolver=_map_resolver, rest_factory=m.LmuRest,
                     tracks_dir=m.TRACKS_DIR)

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
    configure_logging()
    app = build_app()
    # Import LMU result logs now and as LMU writes new ones (#81). Started here, not in
    # build_app or create_app: tests build both, and this reads the REAL LMU install.
    # build_app has returned, so LapStore.__init__ (every migration) is finished first.
    threading.Thread(target=poll_results, args=(app.state.store,), name="result-import",
                     daemon=True).start()
    # Port comes from app.py so the origin allowlist can't silently drift from the
    # port we actually serve on (#60) -- change it in one place, both follow.
    uvicorn.run(app, host="127.0.0.1", port=SIDECAR_PORT, log_config=None)


if __name__ == "__main__":
    main()
