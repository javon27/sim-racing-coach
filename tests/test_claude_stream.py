from simcoach.model.claude import ClaudeModel


class _Resp:
    def __init__(self, content, stop_reason): self.content = content; self.stop_reason = stop_reason


class _Text:
    type = "text"
    def __init__(self, t): self.text = t


class _ToolUse:
    type = "tool_use"
    def __init__(self, id, name, inp): self.id = id; self.name = name; self.input = inp


class _FakeClient:
    """Scripts two turns: a tool_use, then a final text."""
    def __init__(self):
        self._turns = [
            _Resp([_ToolUse("t1", "get_corner", {"corner": "1"})], "tool_use"),
            _Resp([_Text("Carry more speed through Turn 1.")], "end_turn"),
        ]
        self.messages = self
    def create(self, **kw):
        return self._turns.pop(0)


def _model():
    m = ClaudeModel.__new__(ClaudeModel)          # bypass __init__ (no real anthropic client)
    m.model = "x"; m.max_tokens = 100; m.thinking = False
    m.client = _FakeClient()
    return m


def test_run_stream_yields_tool_then_text():
    m = _model()
    calls = []
    events = list(m.run_stream("sys", [{"role": "user", "content": "q"}],
                               tools=[{"name": "get_corner"}],
                               dispatch=lambda n, i: calls.append((n, i)) or "corner data"))
    assert calls == [("get_corner", {"corner": "1"})]
    assert events[0] == {"type": "tool", "name": "get_corner", "input": {"corner": "1"}}
    assert events[-1] == {"type": "text", "text": "Carry more speed through Turn 1."}


def test_run_drains_run_stream():
    m = _model()
    out = m.run("sys", [{"role": "user", "content": "q"}],
                tools=[{"name": "get_corner"}], dispatch=lambda n, i: "corner data")
    assert out == "Carry more speed through Turn 1."
