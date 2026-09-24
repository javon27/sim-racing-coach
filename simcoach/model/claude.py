from collections.abc import Callable, Iterator

import anthropic

from .base import RunResult, TokenUsage

# Safety stop for the tool-use loop: a coach question should need only a few
# tool calls; this bounds a runaway model without capping normal use.
MAX_TOOL_ITERATIONS = 8


class ClaudeModel:
    """Claude model implementation via anthropic SDK."""

    def __init__(self, model: str = "claude-sonnet-5", api_key: str | None = None,
                 max_tokens: int = 4096, thinking: bool = False):
        self.model = model
        self.max_tokens = max_tokens
        self.thinking = thinking
        self.client = anthropic.Anthropic(api_key=api_key)

    def _thinking_param(self):
        # sonnet-5 accepts {"type": "disabled"}; budget_tokens is rejected (400).
        return {"type": "adaptive"} if self.thinking else {"type": "disabled"}

    def complete(self, system: str, messages: list[dict]) -> str:
        """Generate a completion via Claude.

        Args:
            system: System prompt.
            messages: List of messages with 'role' and 'content'.

        Returns:
            The model's text response.
        """
        # Same cache breakpoint as the tool loop, so a large system prompt on this
        # path caches too. INERT on today's three callers (server/app.py track-notes
        # draft, memory distillation, key ping): their system prompts are <=86 tokens,
        # under the 512-1024 minimum cacheable prefix, so the API silently declines to
        # cache and charges no write premium. Their bulk (pasted guides, digests) is
        # in the USER message, which is after this breakpoint — moving that content
        # into `system`, or marking the user block, is what would make it bite.
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=_cached_system(system),
            messages=messages,
            thinking=self._thinking_param(),
        )
        return _first_text(resp)

    def _stream_turn(self, system: str, convo: list[dict], tools: list[dict] | None,
                     force_tool: bool = False):
        """One model turn. Streams token deltas via `messages.stream` (yielding a
        `text` event per chunk) when the client supports it; falls back to a
        single whole-block `messages.create` call otherwise (a client/test-double
        without `.stream` — the real Anthropic client always has it). Either way,
        returns the final message so the caller can inspect `stop_reason`/`content`
        — its text is never re-yielded (that would double the answer once `run`
        accumulates deltas). `tools=None` omits the `tools` kwarg entirely (the
        iteration-cap call).

        Also yields this turn's billed `usage` event last. EVERY billed API call in
        this class goes through here — the tool turns and the tool-free iteration-cap
        call alike — so summing the usage events is structurally complete rather than
        something a later edit can forget on one code path. That is also why the
        cache breakpoint goes here: one place, every billed call."""
        kw = {"model": self.model, "max_tokens": self.max_tokens,
              "system": _cached_system(system),
              "messages": convo, "thinking": self._thinking_param()}
        if tools is not None:
            kw["tools"] = tools
        if force_tool:
            # #159: must call a tool this turn. The API rejects a forced tool_choice
            # with thinking on, so this one turn runs without it.
            kw["tool_choice"] = {"type": "any"}
            kw["thinking"] = {"type": "disabled"}
        if not hasattr(self.client.messages, "stream"):
            resp = self.client.messages.create(**kw)
            text = _first_text(resp)
            if text:
                yield {"type": "text", "text": text}
            yield self._usage_event(resp)
            return resp
        with self.client.messages.stream(**kw) as stream:
            for delta in stream.text_stream:
                yield {"type": "text", "text": delta}
            final = stream.get_final_message()
        yield self._usage_event(final)
        return final

    def _usage_event(self, resp) -> dict:
        return {"type": "usage", "usage": _usage_of(resp).as_dict(), "model": self.model}

    def run_stream(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
        dispatch: Callable[[str, dict], str],
        force_tool: bool = False,
    ) -> Iterator[dict]:
        """Agentic tool-use loop: let the model call `tools` to fetch data on
        demand, executing each via `dispatch`, until it stops calling tools.
        Yields a `tool` event before dispatching each tool_use block, and
        streams the answer as one or more `text` delta events."""
        # Copy so we don't mutate the caller's history (the coach appends only the
        # final Q/A itself). tool_use/tool_result turns stay internal to this call.
        convo = list(messages)
        for i in range(MAX_TOOL_ITERATIONS):
            final = yield from self._stream_turn(system, convo, tools,
                                                 force_tool=force_tool and i == 0)
            if final.stop_reason != "tool_use":
                return
            convo.append({"role": "assistant", "content": final.content})
            results = []
            for block in final.content:
                if getattr(block, "type", None) != "tool_use":
                    continue
                yield {"type": "tool", "name": block.name, "input": block.input}
                try:
                    out = dispatch(block.name, block.input)
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": out})
                except Exception as e:
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": f"error: {type(e).__name__}: {e}",
                                    "is_error": True})
            convo.append({"role": "user", "content": results})
        # Hit the iteration cap — make one final tool-free call so the model answers
        # from what it has gathered rather than returning nothing.
        yield from self._stream_turn(system, convo, tools=None)

    def run(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
        dispatch: Callable[[str, dict], str],
    ) -> RunResult:
        """Agentic tool-use loop; drains `run_stream`, accumulating text deltas
        into the final answer and the per-turn usage events into the turn's total
        cost (every loop iteration, including the tool-free iteration-cap call)."""
        text = ""
        usage = TokenUsage()
        for ev in self.run_stream(system, messages, tools, dispatch):
            if ev["type"] == "text":
                text += ev["text"]
            elif ev["type"] == "usage":
                usage = usage + TokenUsage(**ev["usage"])
        return RunResult(text=text, usage=usage)


def _cached_system(system: str):
    """Send the system prompt as one cacheable block (issue #42).

    The agentic loop re-sends the same tools array + system prompt on every
    iteration of a single question — up to MAX_TOOL_ITERATIONS + 1 billed calls,
    a ~3.5k-token prefix each time. Caching is a PREFIX match and the render
    order is `tools` → `system` → `messages`, so one breakpoint on the last (only)
    system block caches tools and system together; nothing volatile precedes it.
    Iteration 1 pays 1.25x to write, iterations 2..N pay 0.1x to read, so it is
    ahead after a single read — which the loop guarantees inside the 5-minute TTL.

    Anything that makes the prefix vary between iterations (a timestamp in the
    prompt, a set-ordered tool list) silently drops the hit rate to zero with no
    error — tests/test_prompt_caching.py asserts the prefix stays byte-stable.

    Falsy system stays a bare string: an empty `text` block is a 400."""
    if not system:
        return system
    return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]


def _usage_of(resp) -> TokenUsage:
    """Read anthropic's `response.usage` into our provider-neutral shape. Defensive
    by design: a response double (or a future SDK) without the field contributes
    zero rather than killing the answer the driver is waiting on. This is the ONLY
    place in the codebase that touches the SDK's usage field names."""
    u = getattr(resp, "usage", None)
    if u is None:
        return TokenUsage()
    return TokenUsage(
        input=getattr(u, "input_tokens", 0) or 0,
        output=getattr(u, "output_tokens", 0) or 0,
        cache_read=getattr(u, "cache_read_input_tokens", 0) or 0,
        cache_write=getattr(u, "cache_creation_input_tokens", 0) or 0,
    )


def _first_text(resp) -> str:
    # Return the first TEXT block. Models with extended thinking put a ThinkingBlock
    # (no .text) first, so content[0].text would AttributeError (observed live
    # 2026-08-28 with claude-sonnet-5).
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            return block.text
    return "".join(getattr(b, "text", "") for b in resp.content)
