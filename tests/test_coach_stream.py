from simcoach.coach import Coach
from simcoach.coach_tools import CoachContext
from simcoach.model.fake import FakeModel


def test_ask_agentic_stream_passes_events_and_dispatches(tmp_path):
    from simcoach.store import LapStore
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    ctx = CoachContext(store, track="T", car="C")
    coach = Coach(FakeModel(reply="Brake later into 1.",
                            tool_script=[("get_corner", {"corner": "1"})]))
    events = list(coach.ask_agentic_stream(ctx, "where do I lose time?", car="C"))
    assert {"type": "tool", "name": "get_corner", "input": {"corner": "1"}} in events
    texts = [e for e in events if e["type"] == "text"]
    assert texts[-1] == {"type": "text", "text": "Brake later into 1."}
    # usage rides through the coach untouched (the server sums it)
    assert [e for e in events if e["type"] == "usage"]
    # dispatch records dicts (verified against CoachContext.dispatch), not tuples:
    # (#159: each entry also carries the tool's result.)
    assert [(c["name"], c["input"]) for c in ctx.tool_calls] == [("get_corner", {"corner": "1"})]
    assert "result" in ctx.tool_calls[0]


def test_ask_agentic_and_stream_build_same_system_prompt(tmp_path):
    """ask_agentic and ask_agentic_stream must assemble byte-identical system
    prompts for the same inputs — the stream path is not allowed to drift."""
    from simcoach.store import LapStore
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    ctx = CoachContext(store, track="T", car="C")

    coach = Coach(FakeModel(reply="ok"))
    coach.ask_agentic(ctx, "where do I lose time?", car="C")
    system_from_ask = coach.model.last_system

    stream_model = FakeModel(reply="ok")
    coach_stream = Coach(stream_model)
    list(coach_stream.ask_agentic_stream(ctx, "where do I lose time?", car="C"))
    system_from_stream = stream_model.last_system

    assert system_from_ask == system_from_stream


# --- #133: text blocks around a tool round get a paragraph separator ------

def test_stream_inserts_separator_between_text_blocks_around_a_tool_round(tmp_path):
    from simcoach.store import LapStore
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    ctx = CoachContext(store, track="T", car="C")
    model = FakeModel(stream_script=[
        ("text", "A."), ("tool", "get_corner", {"corner": "1"}), ("text", "B"),
    ])
    events = list(Coach(model).ask_agentic_stream(ctx, "q", car="C"))
    texts = [e["text"] for e in events if e["type"] == "text"]
    assert texts == ["A.", "\n\n", "B"]
    assert "".join(texts) == "A.\n\nB"


def test_stream_no_extra_separator_when_text_already_ends_in_whitespace(tmp_path):
    from simcoach.store import LapStore
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    ctx = CoachContext(store, track="T", car="C")
    model = FakeModel(stream_script=[
        ("text", "A.\n\n"), ("tool", "get_corner", {"corner": "1"}), ("text", "B"),
    ])
    events = list(Coach(model).ask_agentic_stream(ctx, "q", car="C"))
    texts = [e["text"] for e in events if e["type"] == "text"]
    assert texts == ["A.\n\n", "B"]


def test_stream_unchanged_with_no_tool_round(tmp_path):
    from simcoach.store import LapStore
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    ctx = CoachContext(store, track="T", car="C")
    model = FakeModel(stream_script=[("text", "A."), ("text", "B")])
    events = list(Coach(model).ask_agentic_stream(ctx, "q", car="C"))
    texts = [e["text"] for e in events if e["type"] == "text"]
    assert texts == ["A.", "B"]


def test_stream_no_leading_separator_when_tool_round_comes_first(tmp_path):
    from simcoach.store import LapStore
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    ctx = CoachContext(store, track="T", car="C")
    model = FakeModel(stream_script=[("tool", "get_corner", {"corner": "1"}), ("text", "B")])
    events = list(Coach(model).ask_agentic_stream(ctx, "q", car="C"))
    texts = [e["text"] for e in events if e["type"] == "text"]
    assert texts == ["B"]


def test_voice_mode_selects_the_matching_length_rule(tmp_path):
    """#119: 'ptt' gets the PTT length rule, 'conversation' gets the
    conversation rule, and no voice gets neither -- never both."""
    from simcoach.coach import VOICE_CONVERSATION, VOICE_PTT
    from simcoach.store import LapStore
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    for voice, want_ptt, want_conv in ((None, False, False), ("conversation", False, True), ("ptt", True, False)):
        m = FakeModel(reply="ok")
        ctx = CoachContext(store, track="T", car="C")
        list(Coach(m).ask_agentic_stream(ctx, "q", car="c", voice=voice))
        assert (VOICE_PTT in m.last_system) is want_ptt, voice
        assert (VOICE_CONVERSATION in m.last_system) is want_conv, voice
