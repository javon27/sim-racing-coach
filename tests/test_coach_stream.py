from simcoach.coach import Coach
from simcoach.model.fake import FakeModel
from simcoach.coach_tools import CoachContext


def test_ask_agentic_stream_passes_events_and_dispatches(tmp_path):
    from simcoach.store import LapStore
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    ctx = CoachContext(store, track="T", car="C")
    coach = Coach(FakeModel(reply="Brake later into 1.",
                            tool_script=[("get_corner", {"corner": "1"})]),
                  "simcoach/knowledge")
    events = list(coach.ask_agentic_stream(ctx, "where do I lose time?", car="C"))
    assert {"type": "tool", "name": "get_corner", "input": {"corner": "1"}} in events
    assert events[-1] == {"type": "text", "text": "Brake later into 1."}
    # dispatch records dicts (verified against CoachContext.dispatch), not tuples:
    assert {"name": "get_corner", "input": {"corner": "1"}} in ctx.tool_calls


def test_ask_agentic_and_stream_build_same_system_prompt(tmp_path):
    """ask_agentic and ask_agentic_stream must assemble byte-identical system
    prompts for the same inputs — the stream path is not allowed to drift."""
    from simcoach.store import LapStore
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    ctx = CoachContext(store, track="T", car="C")

    coach = Coach(FakeModel(reply="ok"), "simcoach/knowledge")
    coach.ask_agentic(ctx, "where do I lose time?", car="C")
    system_from_ask = coach.model.last_system

    stream_model = FakeModel(reply="ok")
    coach_stream = Coach(stream_model, "simcoach/knowledge")
    list(coach_stream.ask_agentic_stream(ctx, "where do I lose time?", car="C"))
    system_from_stream = stream_model.last_system

    assert system_from_ask == system_from_stream
