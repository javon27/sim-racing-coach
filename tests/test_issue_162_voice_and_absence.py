"""#162: Road Atlanta, 2026-09-23 (chat-20260923T205800-210b5a). Turn #215 said no
reference time was loaded without calling get_track_notes; turns #223-#225 were PTT
answers of several paragraphs."""
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
                 "Give me a direction and I'll pull the data."):
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
