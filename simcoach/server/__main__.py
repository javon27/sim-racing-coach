"""Sidecar entry point (Tauri spawns this): build the FastAPI app against the
real app-data paths and serve it on localhost. Uses LazyModel (not make_model)
so a fresh install with no Anthropic key still boots and can reach /settings
to enter one."""
from __future__ import annotations

import threading
import time
from pathlib import Path

from ..car_lookup import warm_entry_table_async
from ..coach import Coach
from ..config import load_config
from ..corners import CornerMap
from ..lmu.ingest import poll_results, scan_now
from ..lmu.rest import LmuRest
from ..lmu.smm import LmuSource
from ..logging_setup import configure_logging
from ..model.factory import LazyModel, make_classifier
from ..paths import db_path, telemetry_dir
from ..store import LapStore
from ..track_lookup import TRACKS_DIR, load_map_data
from .app import SIDECAR_PORT, _capture_loop, create_app


def build_app():
    configure_logging()
    cfg = load_config()
    store = LapStore(str(db_path()), str(telemetry_dir()))
    coach = Coach(LazyModel(model=cfg["model"]), classifier_for=make_classifier)

    user_tracks_dir = str(Path(store.data_dir) / "tracks")

    def _map_resolver(track):
        # Resolve+load the corner map for a track name, or None if unmapped. Called
        # per-lap so a track change follows the live car. Shipped geometry + the
        # user's annotation overlay (names/labels/etc.) so the coach uses the
        # driver's own corner names.
        try:
            data = load_map_data(track, str(TRACKS_DIR), user_dir=user_tracks_dir)
            if data.get("corners"):
                return CornerMap.from_data(data)
        except Exception:
            pass
        return None

    app = create_app(store, coach, cmap=None, rest=None,
                     map_resolver=_map_resolver, rest_factory=LmuRest,
                     tracks_dir=TRACKS_DIR)

    def _capture_forever():
        # LMU may not be running yet (or drops mid-session) -- wait and reopen
        # rather than dying, and never let a failure here block/crash the sidecar.
        while True:
            try:
                _capture_loop(LmuSource(), store, app.state)
            except Exception:
                pass
            time.sleep(3)

    app.state.capture_thread = threading.Thread(target=_capture_forever, daemon=True)
    app.state.capture_thread.start()
    threading.Thread(target=app.state.live_lap.run, name="live-lap", daemon=True).start()

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
    app.state.scan_results = scan_now   # #209: a race review wakes it (CoachContext)
    # Warm car_lookup's entry table (#179) so the first cold Results-dir scan (~5 s
    # on a real install) happens here, not on the first /status tick. Same reasoning
    # as poll_results: real install only, never started from build_app/create_app.
    warm_entry_table_async()
    # Port comes from app.py so the origin allowlist can't silently drift from the
    # port we actually serve on (#60) -- change it in one place, both follow.
    uvicorn.run(app, host="127.0.0.1", port=SIDECAR_PORT, log_config=None)


if __name__ == "__main__":
    main()
