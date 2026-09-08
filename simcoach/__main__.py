"""Live entry point: detect the current LMU session (track + car), load — or auto-build
from your first lap — the matching corner map, and run the capture+coach REPL against
live shared memory.

Requires LMU running and the driver on track. The model provider comes from
SIMCOACH_MODEL_PROVIDER (default anthropic, which reads ANTHROPIC_API_KEY from env)."""
import json
import time
from pathlib import Path

from simcoach.app import run_repl
from simcoach.car_lookup import resolve_car
from simcoach.coach import Coach
from simcoach.config import load_config
from simcoach.corners import CornerMap
from simcoach.driver import DriverProfile
from simcoach.lmu.rest import LmuRest
from simcoach.lmu.smm import LmuSource, read_session_track_car
from simcoach.mapbuild import build_map
from simcoach.model.factory import make_model
from simcoach.store import LapStore
from simcoach.track_lookup import load_map_data, resolve_track_map, track_slug

PKG_DIR = Path(__file__).parent
TRACKS_DIR = PKG_DIR / "tracks"


def resolve_context(rest, tracks_dir, track_name: str, vehicle: str, channels=None,
                    user_dir=None):
    """Resolve the coaching context (corner map, car display name, track-note slug)
    for a live (track, car). Builds+saves a numbered map (named by the track slug, so
    it resolves next time) from `channels` if none exists yet. Returns
    (CornerMap | None, car, note_slug); cmap is None when no map exists and no lap is
    available to build one from."""
    car_class = None
    if rest is not None:
        try:
            car_class = rest.get_setup().get("carClass")
        except Exception:
            car_class = None
    car = resolve_car(vehicle, car_class)
    slug = track_slug(track_name)
    data = load_map_data(track_name, str(tracks_dir), user_dir=user_dir)
    if not data.get("corners") and channels is not None:
        data = build_map(slug, channels)
        (Path(tracks_dir) / f"{slug}.json").write_text(json.dumps(data, indent=2))
    if not data.get("corners"):
        return None, car, slug
    return CornerMap.from_data(data), car, slug


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
        slug = track_slug(track_name)
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
    config = load_config()
    coach = Coach(make_model(model=config["model"]), str(PKG_DIR / "knowledge"))
    print(f"Units: {config['units']}")

    # The context (map/car/note) resolved above is a startup snapshot; it can be stale
    # (LMU on the menu showing the previous track) or go out of date when the driver
    # switches car/track mid-run. Hand run_repl a resolver + a live session peek so the
    # coaching context follows whatever the driver is actually in.
    def resolver(track_name, vehicle, channels):
        return resolve_context(rest, TRACKS_DIR, track_name, vehicle, channels)

    # Concise always-on profile tier: static prefs from settings + the active
    # driver_summary only — NOT the full memory list (that's a pull tool, later task).
    # Best-effort: a missing/empty summary just means no learned_notes.
    d = config.get("driver") or {}
    try:
        summary = store.get_driver_summary()
    except Exception:
        summary = {}
    notes = [summary["text"]] if summary.get("status") == "active" and summary.get("text") else []
    driver_profile = DriverProfile(
        braking_style=d.get("braking_style") or None,
        balance_preference=d.get("balance_preference") or None,
        risk_tolerance=d.get("risk_tolerance") or None,
        learned_notes=notes,
    )

    run_repl(LmuSource(), store, cmap, coach, car, rest=rest, track_note=map_path.stem,
             input_device=config["input_device"], units=config["units"],
             resolver=resolver, peek=read_session_track_car, initial_key=(track_name, vehicle),
             driver_profile=driver_profile)


if __name__ == "__main__":
    main()
