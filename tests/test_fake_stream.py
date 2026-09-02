from simcoach.model.fake import FakeModel


def test_run_stream_emits_tool_then_text():
    seen = []
    fm = FakeModel(reply="Turn 1 looks good.",
                   tool_script=[("get_corner", {"corner": "1"})])
    events = list(fm.run_stream("sys", [{"role": "user", "content": "q"}],
                                tools=[], dispatch=lambda n, i: seen.append((n, i)) or "ok"))
    assert seen == [("get_corner", {"corner": "1"})]          # tool was dispatched
    assert events[0] == {"type": "tool", "name": "get_corner", "input": {"corner": "1"}}
    assert events[-1] == {"type": "text", "text": "Turn 1 looks good."}
    assert all(e["type"] in ("tool", "text") for e in events)  # no done/error here
