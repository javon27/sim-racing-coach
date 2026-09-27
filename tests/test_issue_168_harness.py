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
    # #181: the cool-down rule rides with a logged race too, keyed on the lap begun
    # after the flag -- never on a lap merely lacking a time.
    assert "the lap begun after the chequered flag is the cool-down" in out


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


# --- red-team round 2: the stream never reshapes an answer it didn't cut ---------

# Stored-answer shapes (#210, #247, #254 of the real chats): paragraphs, bullets,
# bold, a triple break, list markers, figures.
ANSWERS = [
    ("That lap 21 was a pit stop lap — you rolled in and parked it, so that 1:53 is the "
    "stop itself.\n\nZooming out on the whole run though, you should feel good.\n\n\n"
    "There were a couple of moments worth knowing about — an off at Turn 4/Turn 5 back "
    "on lap 6, and a spin on lap 20."),
    ("That's a strong result — you started **P2 overall** and finished **P2 overall**.\n\n"
    "Here's the fuller picture though:\n\n**The incidents.** There were 12 logged — "
    "contact with Momo on laps 3 and 6.\n- Turn 3 (Lagos): oversteer.\n- Turn 11: "
    "oversteer.\n\n1. Brake balance forward a click.\n2. TC slip down one."),
    ("Given your best lap, the car's showing oversteer at **Turn 3 (Lagos)**.\n"
    "\nI'd leave the ABS and TC maps alone for now... they're mid-range."),
]


def _joined(tmp_path, answer, deltas, voice):
    from simcoach.coach import Coach
    from simcoach.coach_tools import CoachContext
    from simcoach.model.fake import FakeModel
    from simcoach.store import LapStore
    ctx = CoachContext(LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data")),
                       track="T", car="C")
    script = [("tool", "get_lap_summary", {})] + [("text", d) for d in deltas]
    # The answer as the question: every name in it is "known", so nothing is cut.
    events = Coach(FakeModel(stream_script=script)).ask_agentic_stream(
        ctx, answer, car="C", voice=voice)
    return "".join(e["text"] for e in events if e["type"] == "text")


def test_uncut_answers_survive_every_chunking(tmp_path):
    import random
    rng = random.Random(168)
    bad = []
    for voice in (None, "conversation", "ptt"):
        for answer in ANSWERS:
            splits = [[answer[:i], answer[i:]] for i in range(1, len(answer))]
            for _ in range(20):
                deltas, i = [], 0
                while i < len(answer):
                    n = rng.randint(1, 12)
                    deltas.append(answer[i:i + n])
                    i += n
                splits.append(deltas)
            for deltas in splits:
                if (got := _joined(tmp_path, answer, deltas, voice)) != answer.strip():
                    bad.append((voice, [d for d in deltas][:3], got[:80]))
    assert not bad, bad[:3]
