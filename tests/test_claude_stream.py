from simcoach.model.base import TokenUsage
from simcoach.model.claude import ClaudeModel


class _Usage:
    """Stands in for anthropic's `response.usage` (same field names)."""
    def __init__(self, i, o, cr=0, cw=0):
        self.input_tokens = i; self.output_tokens = o
        self.cache_read_input_tokens = cr; self.cache_creation_input_tokens = cw


class _Resp:
    def __init__(self, content, stop_reason, usage=None):
        self.content = content; self.stop_reason = stop_reason
        if usage is not None:
            self.usage = usage          # absent entirely when None (legacy-fixture guard)


class _Text:
    type = "text"
    def __init__(self, t): self.text = t


class _ToolUse:
    type = "tool_use"
    def __init__(self, id, name, inp): self.id = id; self.name = name; self.input = inp


class _StreamCM:
    """Fake `messages.stream(...)` context manager: `text_stream` yields
    `deltas`, `get_final_message()` returns `final`."""
    def __init__(self, deltas, final):
        self.text_stream = deltas
        self._final = final

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._final


def _resp_text(resp) -> str:
    return "".join(getattr(b, "text", "") for b in resp.content)


class _FakeClient:
    """Scripts two turns: a tool_use, then a final text. `.create()` supplies
    both turns (unchanged); `.stream()` wraps the SAME turn as a streamed
    response — pass `stream_deltas`/`final_text` to also cover token-delta
    streaming with this one fixture."""
    def __init__(self, stream_deltas=None, final_text="Carry more speed through Turn 1.",
                 usages=(None, None)):
        self._turns = [
            _Resp([_ToolUse("t1", "get_corner", {"corner": "1"})], "tool_use", usages[0]),
            _Resp([_Text(final_text)], "end_turn", usages[1]),
        ]
        self.messages = self
        self._stream_deltas = stream_deltas

    def create(self, **kw):
        return self._turns.pop(0)

    def stream(self, **kw):
        resp = self.create(**kw)
        text = _resp_text(resp)
        if not text:
            deltas = []
        elif self._stream_deltas is not None:
            deltas = self._stream_deltas
        else:
            deltas = [text]
        return _StreamCM(deltas, resp)


def _model(stream_deltas=None, final_text="Carry more speed through Turn 1.",
           usages=(None, None), model="x"):
    m = ClaudeModel.__new__(ClaudeModel)          # bypass __init__ (no real anthropic client)
    m.model = model; m.max_tokens = 100; m.thinking = False
    m.client = _FakeClient(stream_deltas=stream_deltas, final_text=final_text, usages=usages)
    return m


def test_run_stream_yields_tool_then_text():
    m = _model()
    calls = []
    events = list(m.run_stream("sys", [{"role": "user", "content": "q"}],
                               tools=[{"name": "get_corner"}],
                               dispatch=lambda n, i: calls.append((n, i)) or "corner data"))
    assert calls == [("get_corner", {"corner": "1"})]
    # Ignore the per-turn usage events (a text-less tool turn emits its usage first).
    flow = [e for e in events if e["type"] in ("tool", "text")]
    assert flow[0] == {"type": "tool", "name": "get_corner", "input": {"corner": "1"}}
    assert flow[-1] == {"type": "text", "text": "Carry more speed through Turn 1."}


def test_run_drains_run_stream():
    m = _model()
    res = m.run("sys", [{"role": "user", "content": "q"}],
                tools=[{"name": "get_corner"}], dispatch=lambda n, i: "corner data")
    assert res.text == "Carry more speed through Turn 1."


def test_run_sums_usage_across_tool_loop_iterations():
    """Two turns (tool_use then text) must bill as the SUM, not the last call."""
    m = _model(usages=(_Usage(100, 10), _Usage(250, 40)))
    res = m.run("sys", [{"role": "user", "content": "q"}],
                tools=[{"name": "get_corner"}], dispatch=lambda n, i: "corner data")
    assert res.text == "Carry more speed through Turn 1."
    assert res.usage == TokenUsage(input=350, output=50)


def test_run_sums_cache_buckets_too():
    """cache_read/cache_write are real once the loop caches its prefix (issue #42);
    they must sum across the loop like input/output, not report just the last call."""
    m = _model(usages=(_Usage(1, 1, cr=7, cw=3), _Usage(1, 1, cr=5, cw=2)))
    res = m.run("sys", [{"role": "user", "content": "q"}],
                tools=[{"name": "get_corner"}], dispatch=lambda n, i: "corner data")
    assert (res.usage.cache_read, res.usage.cache_write) == (12, 5)


def test_run_stream_emits_one_usage_event_per_turn():
    m = _model(usages=(_Usage(100, 10), _Usage(250, 40)), model="claude-sonnet-5")
    events = list(m.run_stream("sys", [{"role": "user", "content": "q"}],
                               tools=[{"name": "get_corner"}],
                               dispatch=lambda n, i: "corner data"))
    usage_events = [e for e in events if e["type"] == "usage"]
    assert len(usage_events) == 2
    assert usage_events[0]["usage"]["input"] == 100
    assert usage_events[1]["usage"]["output"] == 40
    assert usage_events[0]["model"] == "claude-sonnet-5"


def test_missing_usage_contributes_zero_not_an_error():
    """A response double without .usage must not raise (guards old fixtures)."""
    m = _model()
    res = m.run("sys", [{"role": "user", "content": "q"}],
                tools=[{"name": "get_corner"}], dispatch=lambda n, i: "corner data")
    assert res.text == "Carry more speed through Turn 1."
    assert res.usage.total == 0


def test_usage_field_names_match_the_installed_sdk():
    """`_usage_of` (simcoach/model/claude.py) reads these four names with a
    defensive `getattr(..., 0)` -- an absent field silently contributes 0,
    indistinguishable from a measured zero. That's correct only as long as the
    installed SDK still names its fields this way; this pins that assumption so a
    future SDK bump that renames one turns into a red test here instead of a
    silent under-report on the dollar meter."""
    from anthropic.types import Usage
    assert {"input_tokens", "output_tokens", "cache_read_input_tokens",
             "cache_creation_input_tokens"} <= set(Usage.model_fields)


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

    res = m.run("sys", [{"role": "user", "content": "q"}],
                tools=[{"name": "get_corner"}], dispatch=dispatch)

    assert res.text == "Still got you covered."
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
                         "tool_use", _Usage(10, 1))
        return _Resp([_Text("Iteration cap reached.")], "end_turn", _Usage(10, 1))


def test_run_hits_iteration_cap_and_makes_final_tool_free_call():
    from simcoach.model.claude import MAX_TOOL_ITERATIONS

    m = ClaudeModel.__new__(ClaudeModel)
    m.model = "x"; m.max_tokens = 100; m.thinking = False
    client = _FakeClientIterationCap()
    m.client = client

    res = m.run("sys", [{"role": "user", "content": "q"}],
                tools=[{"name": "get_corner"}], dispatch=lambda n, i: "corner data")

    assert res.text == "Iteration cap reached."
    assert len(client.calls) == MAX_TOOL_ITERATIONS + 1
    assert "tools" not in client.calls[-1]


def test_iteration_cap_final_call_is_also_billed():
    """The tool-free call made after hitting MAX_TOOL_ITERATIONS must be counted —
    emitting usage from _stream_turn is what makes that structural."""
    from simcoach.model.claude import MAX_TOOL_ITERATIONS

    m = ClaudeModel.__new__(ClaudeModel)
    m.model = "x"; m.max_tokens = 100; m.thinking = False
    m.client = _FakeClientIterationCap()

    res = m.run("sys", [{"role": "user", "content": "q"}],
                tools=[{"name": "get_corner"}], dispatch=lambda n, i: "corner data")

    assert res.usage.input == 10 * (MAX_TOOL_ITERATIONS + 1)
    assert res.usage.output == 1 * (MAX_TOOL_ITERATIONS + 1)


def test_run_stream_token_deltas():
    """The final turn streams via `messages.stream(...)` — text arrives as
    per-chunk deltas, not one whole-block event. Double-count guard: the
    deltas' total length equals the final answer's length (the streamed
    message's own text is never separately re-yielded)."""
    final_answer = "Brake later into 1."
    deltas = ["Brake ", "later ", "into 1."]

    m = _model(stream_deltas=deltas, final_text=final_answer)
    events = list(m.run_stream("sys", [{"role": "user", "content": "q"}],
                               tools=[{"name": "get_corner"}],
                               dispatch=lambda n, i: "corner data"))
    text_events = [e["text"] for e in events if e["type"] == "text"]
    assert text_events == deltas
    assert sum(len(d) for d in text_events) == len(final_answer)

    m2 = _model(stream_deltas=deltas, final_text=final_answer)
    res = m2.run("sys", [{"role": "user", "content": "q"}],
                 tools=[{"name": "get_corner"}], dispatch=lambda n, i: "corner data")
    assert res.text == final_answer
