"""#162: Road Atlanta, 2026-09-23 (chat-20260923T205800-210b5a). Turn #215 said no
reference time was loaded without calling get_track_notes; turns #223-#225 were PTT
answers of several paragraphs."""
import pytest

from simcoach.coach import Coach, states_data
from simcoach.coach_tools import CoachContext
from simcoach.model.fake import FakeModel
from simcoach.store import LapStore

# Turn #215, verbatim: no tool was called.
NO_REFERENCE = ("Comparing to a community benchmark isn't something I have loaded for this "
                "track/car — that's outside my data. What I can do is compare your pace to "
                "your own personal best or session best if you want a concrete target. Want "
                "me to pull that up?")


def _ctx(tmp_path):
    return CoachContext(LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data")),
                        track="T", car="C")


def test_a_claim_that_data_is_missing_needs_a_tool():
    assert states_data(NO_REFERENCE, known="")
    for text in ("There's no reference time for GT3 here.",
                 "I don't have a benchmark lap on file for this class.",
                 "No reference lap is loaded for this track."):
        assert states_data(text, known=""), text


def test_advice_with_a_negation_is_not_an_absence_claim():
    for text in ("Don't brake so late into Turn 1.",
                 "No need to change the setup yet.",
                 "It's a corner that's entry-loaded, not exit-loaded.",   # turn #223
                 "Give me a direction and I'll pull the data.",
                 # red-team round 1: imperative advice
                 "Don't turn in with the car loaded on the brakes.",
                 "Don't chase the benchmarks yet.",
                 "You don't need the reference lap to see it."):
        assert not states_data(text, known=""), text


def test_turn_215_is_discarded_and_a_tool_forced(tmp_path):
    ctx = _ctx(tmp_path)
    model = FakeModel(stream_script=[("text", NO_REFERENCE)],
                      forced_stream_script=[("tool", "get_track_notes", {}),
                                            ("text", "The track notes list no GT3 time.")])
    events = list(Coach(model).ask_agentic_stream(
        ctx, "What's the reference lap for this class?", car="C", voice="ptt"))
    text = "".join(e["text"] for e in events if e["type"] == "text")
    assert "outside my data" not in text
    assert model.forced == [False, True]
    assert [c["name"] for c in ctx.tool_calls] == ["get_track_notes"]


# Turn #224, verbatim: a PTT answer (spoken only after `done`, per the sidecar log --
# so voice='ptt', VOICE_PTT in the system prompt) that ran to four paragraphs.
TURN_224_PRE = "Let me pull that up."
TURN_224 = (
    "As of race lap 11: you're not making it to the flag on this stint — energy's the "
    "tighter constraint, 18.9 laps of range on 64% left against 31 laps to go, so you're "
    "about 12 laps short on energy (fuel's short too, by about 9 laps, but energy runs "
    "out first).\n\nYour pit window is laps 13 to 29 — that's earliest-to-latest, set by "
    "energy on the late end. Tires aren't a factor at all, front-left's only at 93% "
    "tread and good for well over 100 laps.\n\nGiven you're this far short, one stop "
    "won't be enough to run dry-to-dry on saving alone — you'll want to fill for the "
    "rest of the race when you stop.")


def _turn_224(tmp_path, voice, pre=TURN_224_PRE, forced=False):
    # Streamed in 7-char deltas, so a sentence end lands mid-chunk as it does live.
    deltas = [("text", TURN_224[i:i + 7]) for i in range(0, len(TURN_224), 7)]
    script = [("text", pre), ("tool", "get_strategy", {}), *deltas]
    model = (FakeModel(stream_script=[("text", "You're P7 in class.")],   # ungrounded
                       forced_stream_script=script) if forced
             else FakeModel(stream_script=script))
    events = list(Coach(model).ask_agentic_stream(
        _ctx(tmp_path), "You should have enough laps for fuel strategy.", car="C",
        voice=voice))
    return "".join(e["text"] for e in events if e["type"] == "text"), events


# #168: PTT says the WHOLE answer after the last tool -- no sentence cap -- and still
# drops the narration before the tool.
@pytest.mark.parametrize("pre", ["Let me pull that up.",
                                 "Copy that. Let me check.",
                                 "Copy that. I hear you. Let me check."])   # red-team repro
def test_ptt_says_the_whole_answer_after_the_tool(tmp_path, pre):
    text, events = _turn_224(tmp_path, "ptt", pre=pre)
    assert text == TURN_224
    assert [e["name"] for e in events if e["type"] == "tool"] == ["get_strategy"]


def test_ptt_whole_answer_on_the_forced_159_round(tmp_path):
    text, _ = _turn_224(tmp_path, "ptt", pre="Copy that. I hear you. Let me check.",
                             forced=True)
    assert text == TURN_224


def test_ptt_tool_free_answer_is_not_cut(tmp_path):
    long = " ".join(f"Point {w}." for w in ("one", "two", "three", "four", "five", "six"))
    model = FakeModel(reply=long)
    events = list(Coach(model).ask_agentic_stream(_ctx(tmp_path), "T1 tips in detail?",
                                                  car="C", voice="ptt"))
    assert "".join(e["text"] for e in events if e["type"] == "text") == long


def test_ptt_prompt_makes_length_a_default_not_a_cap():
    from simcoach.coach import VOICE_PTT
    assert "2-3" in VOICE_PTT and "in detail" in VOICE_PTT and "cut you off" in VOICE_PTT
    assert "is cut unheard" not in VOICE_PTT


def test_conversation_and_typed_answers_are_not_cut(tmp_path):
    for voice in ("conversation", None):
        text = _turn_224(tmp_path, voice)[0]
        assert text.startswith(TURN_224_PRE) and text.endswith("when you stop."), voice


@pytest.mark.parametrize("said", ["Let me check.", "You're P8. Pit on lap 20."])
def test_ptt_stream_ending_on_a_tool_still_says_the_last_text(tmp_path, said):
    """Red-team round 2: a stream that ends on a tool left no text after it -- silence,
    and an empty stored row. The last non-empty segment is said instead."""
    model = FakeModel(stream_script=[("text", said), ("tool", "get_race_state", {})])
    events = list(Coach(model).ask_agentic_stream(_ctx(tmp_path), "q", car="C",
                                                  voice="ptt"))
    assert "".join(e["text"] for e in events if e["type"] == "text") == said
