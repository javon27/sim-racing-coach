from pathlib import Path

from simcoach.app import run_repl
from simcoach.coach import Coach
from simcoach.corners import CornerMap
from simcoach.model.claude import ClaudeModel
from simcoach.source import ReplaySource
from simcoach.store import LapStore

PKG_DIR = Path(__file__).parent
TRACK = "test_track"
CAR = "test_car"


def main():
    store = LapStore("simcoach.sqlite", "telemetry")
    cmap = CornerMap.load(str(PKG_DIR / "tracks" / f"{TRACK}.json"))
    coach = Coach(ClaudeModel(), str(PKG_DIR / "knowledge"))
    source = ReplaySource(store, TRACK, CAR)

    if not list(source.laps()):
        print(f"No laps in the store yet for {TRACK}/{CAR}. Capture a lap first.")
        return

    run_repl(source, store, cmap, coach, CAR)


if __name__ == "__main__":
    main()
