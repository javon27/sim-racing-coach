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


class _FakeClientDispatchError:
    """A tool_use turn whose dispatch raises, then a final text turn. Records
    every create() call's kwargs so the test can inspect the tool_result sent
    back on the next turn."""
    def __init__(self):
        self._turns = [
            _Resp([_ToolUse("t1", "get_corner", {"corner": "1"})], "tool_use"),
            _Resp([_Text("Still got you covered.")], "end_turn"),
        ]
        self.messages = self
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        return self._turns.pop(0)


def test_run_continues_after_dispatch_error_and_returns_final_text():
    m = ClaudeModel.__new__(ClaudeModel)
    m.model = "x"; m.max_tokens = 100; m.thinking = False
    client = _FakeClientDispatchError()
    m.client = client

    def dispatch(name, inp):
        raise RuntimeError("boom")

    out = m.run("sys", [{"role": "user", "content": "q"}],
                tools=[{"name": "get_corner"}], dispatch=dispatch)

    assert out == "Still got you covered."
    # the tool_result on the NEXT create() call's messages carries is_error
    second_call_messages = client.calls[1]["messages"]
    tool_result_msg = second_call_messages[-1]
    assert tool_result_msg["role"] == "user"
    result_block = tool_result_msg["content"][0]
    assert result_block["is_error"] is True
    assert result_block["tool_use_id"] == "t1"


class _FakeClientIterationCap:
    """Every tool-loop call (has `tools=`) returns tool_use; the iteration-cap
    final call (no `tools=`) returns text."""
    def __init__(self):
        self.messages = self
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        if kw.get("tools"):
            return _Resp([_ToolUse(f"t{len(self.calls)}", "get_corner", {"corner": "1"})],
                         "tool_use")
        return _Resp([_Text("Iteration cap reached.")], "end_turn")


def test_run_hits_iteration_cap_and_makes_final_tool_free_call():
    from simcoach.model.claude import MAX_TOOL_ITERATIONS

    m = ClaudeModel.__new__(ClaudeModel)
    m.model = "x"; m.max_tokens = 100; m.thinking = False
    client = _FakeClientIterationCap()
    m.client = client

    out = m.run("sys", [{"role": "user", "content": "q"}],
                tools=[{"name": "get_corner"}], dispatch=lambda n, i: "corner data")

    assert out == "Iteration cap reached."
    assert len(client.calls) == MAX_TOOL_ITERATIONS + 1
    assert "tools" not in client.calls[-1]
