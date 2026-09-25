"""#168: "it still feels like I'm talking to a brick wall" -- Long Beach/Algarve,
2026-09-24 (chat-20260924T223706-cca7c5, chat-20260924T232917-c69dd5)."""
from simcoach.coach import AGENTIC_SYSTEM_PROMPT


def test_prompt_takes_the_drivers_word_as_given():
    # #237/#240 corrected his lap numbering; #211/#212 (09-23) refused to "just agree"
    # with where he finished.
    p = AGENTIC_SYSTEM_PROMPT
    assert "TAKEN AS GIVEN" in p
    assert "never correct the driver's lap numbering" in p
    assert "The driver's own figures" in p
    assert "If the driver says it was a race, it was" in p
    # ...while the coach still never invents its own figures.
    assert "YOU introduce comes from a tool you called THIS turn -- never invent one" in p


def test_empty_track_notes_license_framed_general_knowledge(tmp_path):
    # #236: "first time at Long Beach, any tips?" -> "I've got nothing loaded".
    from simcoach.coach_tools import CoachContext
    from simcoach.store import LapStore
    ctx = CoachContext(LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data")),
                       track="Nowhere Street Circuit", car="C")
    notes = ctx.dispatch("get_track_notes", {})
    assert notes.startswith("No notes on file for Nowhere Street Circuit")
    assert "general knowledge of the track is fine" in notes
    assert "no turn numbers, names or directions from memory" in notes
    assert "General knowledge of the track's character is fine" in ctx.dispatch(
        "list_track_corners", {})
    p = AGENTIC_SYSTEM_PROMPT
    assert "you MAY give general, well-known knowledge of the track" in p
    assert "NEVER recall corner names, numbers, or directions" in p   # still owned data
    assert "(not its named corners)" in p and "famous" not in p


def _race(tmp_path, synthetic_lap, logged):
    from simcoach.coach_tools import CoachContext
    from simcoach.lmu.results import ResultLap, ResultSession
    from simcoach.model_types import LapRecord
    from simcoach.store import LapStore
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    for i in range(3):
        store.save(LapRecord(**{**synthetic_lap.__dict__, "session_id": "alg",
                                "timestamp": float(i), "position": 2, "phase": "race"}))
    if logged:
        store.save_result_session(ResultSession(
            sim="lmu", track=synthetic_lap.track, car=synthetic_lap.car, car_class="GT3",
            date_ts=0.0, session_type="race", position=2, class_position=2, best_lap=90.0,
            finish_status="Finished Normally", incidents=[], penalties=[],
            laps=[ResultLap(lap_no=1, lap_time=90.0, sectors=[], valid=True)]),
            session_id="alg")
    return lambda **kw: CoachContext(store, track=synthetic_lap.track, car="C",
                                     session_id="alg", **kw)


FINISHED = "talk about it as a finished race"


def test_race_result_says_a_logged_race_is_finished(tmp_path, synthetic_lap):
    # #247's state: the result log is in.
    out = _race(tmp_path, synthetic_lap, logged=True)(on_track=True).get_race_result()
    assert "This race is FINISHED" in out and FINISHED in out


def test_race_result_after_the_flag_before_the_log_is_finished_too(tmp_path, synthetic_lap):
    # #245's state (replayed on the real Algarve DB): 13 race laps, no log yet, no live
    # race -- the model read the in-lap as a pit stop and offered strategy.
    out = _race(tmp_path, synthetic_lap, logged=False)(on_track=False).get_race_result()
    assert "No race is live now" in out and FINISHED in out
    assert "not a pit stop" in out


def test_race_result_during_a_live_race_is_not_called_finished(tmp_path, synthetic_lap):
    # Red-team round 1: a live race-phase session with no fresh snapshot (before the
    # first lap commits, or a sidecar restart mid-race) has race=None + race_note.
    from simcoach.race import NO_RACE_LAP_YET
    ctx = _race(tmp_path, synthetic_lap, logged=False)(on_track=True, race=None,
                                                         race_note=NO_RACE_LAP_YET)
    assert FINISHED not in ctx.get_race_result()
    # The same session with no note and no snapshot (not a race phase) is over.
    ctx = _race(tmp_path, synthetic_lap, logged=False)(on_track=True, race=None)
    assert FINISHED in ctx.get_race_result()


def test_prompt_answers_a_finished_race_as_finished():
    assert "never 'now that you've pitted'" in AGENTIC_SYSTEM_PROMPT
