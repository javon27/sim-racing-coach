"""Sidecar entry point (Tauri spawns this): build the FastAPI app against the
real app-data paths and serve it on localhost. Uses LazyModel (not make_model)
so a fresh install with no Anthropic key still boots and can reach /settings
to enter one."""
from __future__ import annotations

from .. import __main__ as m  # reuse track/car/corner-map resolution helpers
from ..coach import Coach
from ..config import load_config
from ..model.factory import LazyModel
from ..paths import db_path, telemetry_dir
from ..store import LapStore
from .app import create_app


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
            map_path = m.resolve_track_map(track_name, str(m.TRACKS_DIR))
            if map_path is not None:
                cmap = m.CornerMap.load(str(map_path))
    except Exception:
        cmap = rest = None  # offline: never let resolution failures block boot

    return create_app(store, coach, cmap, rest)


def main():
    import uvicorn
    uvicorn.run(build_app(), host="127.0.0.1", port=8765)


if __name__ == "__main__":
    main()
