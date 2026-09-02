import threading
import time

from simcoach.coach import Coach
from simcoach.coach_tools import CoachContext
from simcoach.corners import CornerMap
from simcoach.session import CoachSession
from simcoach.source import TelemetrySource
from simcoach.store import LapStore
from simcoach.tiers import legal_adjustments


def run_repl(source: TelemetrySource, store: LapStore, cmap: CornerMap, coach: Coach,
             car: str, rest=None, track_note: str | None = None,
             input_device: str | None = None, units: str = "metric",
             resolver=None, peek=None, initial_key: tuple[str, str] | None = None) -> None:
    """Background thread captures laps from `source` into `store` (silently, no
    per-lap print). Foreground loop answers questions — including BEFORE any lap is
    captured (general track/car/setup guidance), and lap-specific once you've driven.

    `car` is the resolved model name (coach + its knowledge note). `rest`, if given
    (an LmuRest), supplies the current setup to each answer. `track_note` is the map
    slug used to load the track's knowledge note.

    The coaching context (corner map, car, track note) FOLLOWS the live session: when
    the current lap's or live-peeked (track, car) no longer matches what the context
    was resolved for, `resolver(track, vehicle, channels) -> (cmap, car, note)` is
    called to re-resolve it. This corrects a stale startup snapshot (e.g. the coach
    launched on LMU's menu showing the previous track) and a mid-run track/car change.
    `peek() -> (track, vehicle) | None` reads the live session so context can switch
    even before the first lap is captured. `initial_key` is what the passed-in context
    was resolved for, so an already-correct startup doesn't re-resolve or print a notice.

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
            except Exception as e:
                # LMU likely closed / shared memory vanished — wait for it to come
                # back, then re-open and resume. Surface it (was silently swallowed)
                # so a real capture fault is visible, not just missing laps.
                print(f"\n  [capture paused: {type(e).__name__}: {e} — retrying in 3s]",
                      flush=True)
                time.sleep(3)

    threading.Thread(target=capture, daemon=True).start()

    # One conversation for the whole run: threads prior Q&A so follow-ups have
    # context, and persists every exchange to the chat log. session_id is a
    # sortable timestamp so `store.recent_session_ids()` reads newest-first.
    session_id = time.strftime("%Y-%m-%dT%H%M%S")
    chat_id = store.create_chat()
    session = CoachSession(coach, car, track_note=track_note, input_device=input_device,
                           store=store, session_id=session_id, units=units, chat_id=chat_id)

    ctx_key = (initial_key[0].strip(), initial_key[1].strip()) if initial_key else None

    def maybe_reresolve():
        """Re-point the coaching context at the live (track, car) when it changes.
        Driven by the current lap when there is one, else a live session peek so the
        context is right even before a lap is captured."""
        nonlocal cmap, ctx_key
        if resolver is None:
            return
        rec = latest["record"]
        if rec is not None:
            cur, channels = (rec.track.strip(), rec.car.strip()), rec.channels
        elif peek is not None:
            p = peek()
            if p is None:
                return
            cur, channels = (p[0].strip(), p[1].strip()), None
        else:
            return
        if cur == ctx_key:
            return
        try:
            new_cmap, new_car, new_note = resolver(cur[0], cur[1], channels)
        except Exception:
            return  # keep the current context rather than blanking it on a transient error
        track_changed = ctx_key is None or cur[0] != ctx_key[0]
        cmap, session.car, session.track_note = new_cmap, new_car, new_note
        if ctx_key is not None:
            print(f"\n  ↻ session changed → {cur[0]} / {new_car}")
            if track_changed:
                session.reset()  # don't let the previous track's conversation bleed in
        ctx_key = cur

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

        maybe_reresolve()

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
        # track/car for store lookups come from the live lap when present, else the
        # resolved context key. car here is the RAW name laps are stored under (the
        # tools query the store), distinct from the coach's resolved display name.
        track = record.track if record else (ctx_key[0] if ctx_key else None)
        car_raw = record.car if record else (ctx_key[1] if ctx_key else None)
        context = CoachContext(
            store, units=units, record=record, lap_id=latest["lap_id"], cmap=cmap,
            setup=setup, track=track, car=car_raw, on_track=record is not None,
            allowed_adjustments=allowed,
        )
        try:
            answer = session.ask(context, question)
        except Exception as e:
            answer = f"(couldn't answer that one: {type(e).__name__}: {e})"

        # Clear visual separation between question and the coach's reply.
        print(f"\n{rule}\ncoach ▸\n\n{answer}\n{rule}")
