import threading

from simcoach.analysis import analyze_lap
from simcoach.coach import Coach
from simcoach.corners import CornerMap
from simcoach.source import TelemetrySource
from simcoach.store import LapStore


def run_repl(source: TelemetrySource, store: LapStore, cmap: CornerMap, coach: Coach, car: str) -> None:
    """Background thread captures laps from `source` into `store` (silently, no
    per-lap print). Foreground loop answers questions about the latest captured
    lap, using the stored best lap as reference."""
    latest = {"record": None}

    def capture():
        for lap in source.laps():
            store.save(lap)
            latest["record"] = lap

    threading.Thread(target=capture, daemon=True).start()

    print("Sim-racing coach ready. Type a question (or 'quit' to exit).")
    while True:
        try:
            question = input("> ").strip()
        except EOFError:
            break
        if not question or question.lower() in ("quit", "exit"):
            break

        record = latest["record"]
        if record is None:
            print("No laps captured yet — drive a lap first (or seed the replay store).")
            continue

        reference = store.best_lap(record.track, car)
        analysis = analyze_lap(record, cmap, reference=reference)
        print(coach.ask(analysis, car, question))
