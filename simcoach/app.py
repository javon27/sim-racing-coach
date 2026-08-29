import threading
import time

from simcoach.analysis import analyze_lap
from simcoach.coach import Coach
from simcoach.corners import CornerMap
from simcoach.source import TelemetrySource
from simcoach.store import LapStore
from simcoach.tiers import legal_adjustments


def run_repl(source: TelemetrySource, store: LapStore, cmap: CornerMap, coach: Coach,
             car: str, rest=None, track_note: str | None = None,
             input_device: str | None = None) -> None:
    """Background thread captures laps from `source` into `store` (silently, no
    per-lap print). Foreground loop answers questions — including BEFORE any lap is
    captured (general track/car/setup guidance), and lap-specific once you've driven.

    `car` is the resolved model name (coach + its knowledge note). `rest`, if given
    (an LmuRest), supplies the current setup to each answer. `track_note` is the map
    slug used to load the track's knowledge note.

    Resilience: the capture thread survives LMU dropping mid-session (it waits and
    retries rather than dying), and an error answering one question is reported
    without killing the REPL."""
    latest = {"record": None, "lap_id": None}

    def capture():
        while True:
            try:
                for lap in source.laps():
                    lap_id = store.save(lap)
                    latest["lap_id"] = lap_id
                    latest["record"] = lap
            except Exception:
                # LMU likely closed / shared memory vanished — wait for it to
                # come back, then re-open and resume. (daemon thread; loops forever)
                time.sleep(3)

    threading.Thread(target=capture, daemon=True).start()

    rule = "─" * 60
    print(f"\nSim-racing coach ready. Ask a question any time (or 'quit' to exit).\n{rule}")
    while True:
        try:
            question = input("\nyou ▸ ").strip()
        except EOFError:
            break
        if not question or question.lower() in ("quit", "exit"):
            break

        # Immediate feedback that the request was received (the model call blocks a
        # few seconds with no output otherwise).
        print("\n  …coaching…", flush=True)

        setup = None
        if rest is not None:
            try:
                setup = rest.get_setup()
            except Exception:
                setup = None

        # Only constrain setup advice when the session is actually restricted (fixed/
        # tier races); an open setup returns None here (no constraint).
        allowed = legal_adjustments(setup) if setup else None

        record = latest["record"]
        try:
            if record is None:
                # No lap yet — general guidance from track/car knowledge + setup.
                answer = coach.ask(None, car, question, setup=setup, track_note=track_note,
                                   input_device=input_device, allowed_adjustments=allowed)
            else:
                # Query history by the raw car the laps were stored under (record.car),
                # not the resolved model display name passed for the coach.
                reference = store.best_lap(record.track, record.car,
                                           exclude_lap_id=latest["lap_id"])
                analysis = analyze_lap(record, cmap, reference=reference)
                answer = coach.ask(analysis, car, question, setup=setup, track_note=track_note,
                                   input_device=input_device, allowed_adjustments=allowed)
        except Exception as e:
            answer = f"(couldn't answer that one: {type(e).__name__}: {e})"

        # Clear visual separation between question and the coach's reply.
        print(f"\n{rule}\ncoach ▸\n\n{answer}\n{rule}")
