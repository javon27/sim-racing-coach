"""Prompt caching on the agentic coach loop (issue #42).

The loop re-sends a byte-stable prefix — the tools array then the system prompt —
on every iteration of ONE question (up to MAX_TOOL_ITERATIONS + 1 billed calls).
Caching that prefix is the largest cost lever in the codebase, and it only works
while two properties hold, both asserted here offline:

  1. a `cache_control` breakpoint is actually on the prefix, and
  2. the prefix is byte-identical across iterations 1..N.

(2) is the property a future edit breaks silently — a timestamp interpolated into
the system prompt, a `set`-ordered tool list — with no error, just a higher bill.
Whether the live API returns non-zero `cache_read_input_tokens` on iterations 2..N
is [LIVE-LLM] and needs a real key; these tests make no API call.
"""
import json

from simcoach.model.claude import MAX_TOOL_ITERATIONS, ClaudeModel


class _Text:
    type = "text"

    def __init__(self, t):
        self.text = t


class _ToolUse:
    type = "tool_use"

    def __init__(self, id, name, inp):
        self.id = id
        self.name = name
        self.input = inp


class _Resp:
    def __init__(self, content, stop_reason):
        self.content = content
        self.stop_reason = stop_reason


class _ScriptedClient:
    """A multi-tool conversation: `tool_turns` turns that call a tool, then a
    final text turn. Records every request's kwargs so the test can inspect the
    exact prefix the model layer built. No `.stream` attribute, so `_stream_turn`
    takes its `messages.create` path — same kwargs either way."""

    def __init__(self, tool_turns=3):
        self.messages = self
        self.calls = []
        self._left = tool_turns

    def create(self, **kw):
        self.calls.append(kw)
        if self._left > 0:
            self._left -= 1
            return _Resp(
                [_ToolUse(f"t{len(self.calls)}", "get_corner", {"corner": "1"})],
                "tool_use",
            )
        return _Resp([_Text("Brake later into 1.")], "end_turn")


TOOLS = [
    {"name": "get_corner", "description": "corner detail", "input_schema": {}},
    {"name": "get_lap", "description": "lap summary", "input_schema": {}},
]
SYSTEM = "You are a race engineer. " * 40


def _run(client):
    m = ClaudeModel.__new__(ClaudeModel)  # bypass __init__: no real anthropic client
    m.model = "claude-sonnet-5"
    m.max_tokens = 100
    m.thinking = False
    m.client = client
    return m.run(SYSTEM, [{"role": "user", "content": "q"}], TOOLS,
                 dispatch=lambda n, i: "corner data")


def _blocks(kw):
    """Every content block of the request prefix, in render order (tools, system)."""
    system = kw["system"]
    return list(kw.get("tools") or []) + (system if isinstance(system, list) else [])


def _breakpoints(kw):
    return [b for b in _blocks(kw) if isinstance(b, dict) and "cache_control" in b]


def _prefix_bytes(kw):
    """The cached prefix as sent: tools then system, serialized deterministically."""
    return json.dumps({"tools": kw.get("tools"), "system": kw["system"]},
                      sort_keys=True, default=str)


def test_every_billed_call_carries_a_cache_breakpoint_on_the_stable_prefix():
    client = _ScriptedClient(tool_turns=3)
    res = _run(client)

    assert res.text == "Brake later into 1."
    assert len(client.calls) == 4
    for i, kw in enumerate(client.calls):
        marks = _breakpoints(kw)
        # Exactly one, and on the LAST prefix block: tools render before system, so
        # one marker on the final system block caches tools + system together.
        assert len(marks) == 1, f"call {i}: {len(marks)} breakpoints, want 1"
        assert marks[0] is _blocks(kw)[-1], f"call {i}: breakpoint is not the last block"
        assert marks[0]["cache_control"] == {"type": "ephemeral"}


def test_cached_prefix_is_byte_identical_across_tool_loop_iterations():
    """THE property that makes caching pay: iterations 2..N read what iteration 1
    wrote only if the prefix bytes never move. Runs the loop to the iteration cap
    so this covers iteration 1 vs iteration N, not just two turns."""
    client = _ScriptedClient(tool_turns=MAX_TOOL_ITERATIONS + 5)
    _run(client)

    tool_calls = [kw for kw in client.calls if kw.get("tools") is not None]
    assert len(tool_calls) == MAX_TOOL_ITERATIONS  # iterations 1..N of one question

    first, last = _prefix_bytes(tool_calls[0]), _prefix_bytes(tool_calls[-1])
    assert first == last, "prefix drifted between iteration 1 and iteration N"
    assert len({_prefix_bytes(kw) for kw in tool_calls}) == 1
    # A byte-stable prefix with no breakpoint is not a cached prefix.
    assert "cache_control" in first


def test_system_prompt_text_survives_the_cache_wrapper():
    """The wrapper must not drop or mangle the prompt the coach built."""
    client = _ScriptedClient(tool_turns=0)
    _run(client)

    system = client.calls[0]["system"]
    assert [b["text"] for b in system] == [SYSTEM]


def test_complete_carries_the_same_cache_breakpoint():
    """`complete()` is the non-agentic path (server/app.py track-notes draft, memory
    distillation, key ping — all billed to the driver's own BYO key and invisible to
    the #39 meter). It gets the same breakpoint so a large system prompt on this path
    caches. Note this is INERT for those three callers today: their system prompts are
    <=86 tokens, under the 512-1024 minimum cacheable prefix, so the API silently
    declines to cache and charges no write premium — see the comment on `complete`."""
    client = _ScriptedClient(tool_turns=0)
    m = ClaudeModel.__new__(ClaudeModel)
    m.model = "claude-sonnet-5"
    m.max_tokens = 100
    m.thinking = False
    m.client = client
    assert m.complete(SYSTEM, [{"role": "user", "content": "q"}]) == "Brake later into 1."

    kw = client.calls[0]
    assert "tools" not in kw  # no tools on this path: system IS the whole prefix
    assert kw["system"] == [{"type": "text", "text": SYSTEM,
                             "cache_control": {"type": "ephemeral"}}]


def test_empty_system_is_not_sent_as_an_empty_text_block():
    """An empty `text` block is a 400. Falsy system stays a bare string."""
    client = _ScriptedClient(tool_turns=0)
    m = ClaudeModel.__new__(ClaudeModel)
    m.model = "claude-sonnet-5"
    m.max_tokens = 100
    m.thinking = False
    m.client = client
    m.run("", [{"role": "user", "content": "q"}], TOOLS, dispatch=lambda n, i: "x")

    assert client.calls[0]["system"] == ""
