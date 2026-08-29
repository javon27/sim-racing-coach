"""Live entry point: detect the current LMU session (track + car), load — or auto-build
from your first lap — the matching corner map, and run the capture+coach REPL against
live shared memory.

Requires LMU running and the driver on track. The model provider comes from
SIMCOACH_MODEL_PROVIDER (default anthropic, which reads ANTHROPIC_API_KEY from env)."""
import json
import os
import re
import time
from pathlib import Path

from simcoach.app import run_repl
from simcoach.car_lookup import resolve_car
from simcoach.coach import Coach
from simcoach.corners import CornerMap
from simcoach.lmu.rest import LmuRest
from simcoach.lmu.smm import LmuSource, read_session_track_car
from simcoach.mapbuild import build_map
from simcoach.model.factory import make_model
from simcoach.store import LapStore
from simcoach.track_lookup import resolve_track_map

PKG_DIR = Path(__file__).parent
TRACKS_DIR = PKG_DIR / "tracks"


def _slug(track_name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", track_name.lower()).strip("_") or "unknown_track"


def wait_for_session(poll: float = 3.0):
    """Block until an LMU session (player on track) is detected, then return
    (track, car). Lets you start the coach before/alongside LMU. Ctrl-C to quit."""
    info = read_session_track_car()
    if info is not None:
        return info
    print("Waiting for an LMU session — start LMU and get on track "
          "(Ctrl-C to quit)...", flush=True)
    waited = 0.0
    while True:
        time.sleep(poll)
        info = read_session_track_car()
        if info is not None:
            return info
        waited += poll
        if waited % 30 < poll:  # a heartbeat every ~30s so it's clearly alive
            print("  ...still waiting for LMU.", flush=True)


def main():
    try:
        track_name, vehicle = wait_for_session()
    except KeyboardInterrupt:
        print("\nExiting.")
        return

    # Resolve the entry name to a car model (via REST class + alias table) so the
    # coach loads the right car note and gives model-specific setup advice.
    rest = LmuRest()
    car_class = None
    try:
        car_class = rest.get_setup().get("carClass")
    except Exception:
        rest = None  # REST unavailable — run without setup/class
    car = resolve_car(vehicle, car_class)
    print(f"Track: {track_name}\nCar:   {vehicle}  ->  {car}")

    store = LapStore("simcoach.sqlite", "telemetry")
    map_path = resolve_track_map(track_name, str(TRACKS_DIR))

    if map_path is None:
        # No map for this track/layout yet — build a numbered-geometry one from the
        # first completed lap, then continue. Names stay "Turn N" until enriched.
        slug = _slug(track_name)
        print(f"No corner map for this track — drive one clean lap and I'll build "
              f"'{slug}.json' from it...")
        first = next(LmuSource().laps())  # blocks until the first completed lap
        store.save(first)
        obj = build_map(slug, first.channels)
        map_path = TRACKS_DIR / f"{slug}.json"
        map_path.write_text(json.dumps(obj, indent=2))
        print(f"Built {map_path.name}: {len(obj['corners'])} corners "
              f"(numbered — fill in real names later).")

    print(f"Corner map: {map_path.stem}")
    cmap = CornerMap.load(str(map_path))
    coach = Coach(make_model(), str(PKG_DIR / "knowledge"))
    run_repl(LmuSource(), store, cmap, coach, car, rest=rest, track_note=map_path.stem,
             input_device=os.environ.get("SIMCOACH_INPUT_DEVICE"))


if __name__ == "__main__":
    main()
