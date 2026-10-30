import json
import logging
from collections.abc import Callable, Iterator

import anthropic

from .base import TURN_TAG, CoachUserError, RunResult, TokenUsage

_log = logging.getLogger(__name__)

# Safety stop for the tool-use loop: a coach question should need only a few
# tool calls; this bounds a runaway model without capping normal use.
MAX_TOOL_ITERATIONS = 8


class ClaudeModel:
    """Claude model implementation via anthropic SDK."""

    def __init__(self, model: str = "claude-sonnet-5", api_key: str | None = None,
                 max_tokens: int = 4096):
        self.model = model
        self.max_tokens = max_tokens
        self.client = anthropic.Anthropic(api_key=api_key)

    @property
    def can_force_tool(self) -> bool:
        """#159: whether `run_stream(force_tool=True)` can be honoured. Forcing sends
        tool_choice any with thinking disabled; Claude Opus 5.5 and Fable 5.1 / Mythos
        5.1 400 on forced tool_choice, and every Fable/Mythos (plus Opus 5.5) 400s on
        thinking disabled -- so those never get a forced round (the coach answers
        UNCONFIRMED instead). Opus 5 accepts disabled thinking at the default effort."""
        return not self.model.startswith(("claude-opus-5-5", "claude-fable-", "claude-mythos-"))

    def _tier_kw(self, tier: str) -> dict:
        """#175: the request kwargs a thinking tier maps to on this model. Confirmed
        against the installed SDK (anthropic 1.8.0): `thinking` takes
        {"type": "adaptive", "display": ...} / {"type": "disabled"}
        (types/thinking_config_*_param.py); effort is `output_config={"effort": ...}`,
        low|medium|high|xhigh|max (types/output_config_param.py; `max` is the top level
        every model's EffortCapability reports). budget_tokens is rejected on sonnet-5,
        so it is never sent.
          quick    -> thinking disabled
          standard -> adaptive (the model decides how much)
          deep     -> adaptive + effort max
        Families that 400 on disabled thinking (Opus 5.5, Fable, Mythos -- the same set
        can_force_tool names) run quick as adaptive at the lowest effort instead. Haiku
        has no adaptive thinking and no effort: every tier runs it plain.
        display "summarized": the text is logged for tuning (INFO), never shown.
        Thinking shares max_tokens with the answer, so every adaptive tier gets real
        headroom (16k; deep 32k) -- at 4096 thinking ate the whole answer (#175 red
        team). Streaming only, so a large max_tokens has no HTTP-timeout cost; a turn
        that still hits it yields a `stop` event (see _cut)."""
        if self.model.startswith("claude-haiku-"):
            # ponytail: Haiku is the only no-adaptive family the factory's aliases
            # reach; add a prefix here if an older model id is ever configured.
            return {"thinking": {"type": "disabled"}}
        adaptive = {"type": "adaptive", "display": "summarized"}
        if tier == "deep":
            return {"thinking": adaptive, "output_config": {"effort": "max"},
                    "max_tokens": max(self.max_tokens, 32000)}
        if tier == "quick":
            if not self.can_force_tool:       # this family rejects disabled thinking
                return {"thinking": adaptive, "output_config": {"effort": "low"},
                        "max_tokens": max(self.max_tokens, 16000)}
            return {"thinking": {"type": "disabled"}}
        return {"thinking": adaptive, "max_tokens": max(self.max_tokens, 16000)}

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
            **self._tier_kw("quick"),
        )
        return _first_text(resp)

    def classify(self, prompt: str, schema: dict, *,
                 timeout: float) -> tuple[dict | None, TokenUsage]:
        """#175 router call. JSON is asked for in words (no structured-output mode,
        so any model id works); SDK timeout `timeout` s (the router's budget), no
        retries -- the router enforces its own budget as well. No `thinking` kwarg:
        the classifier model's default (none on Haiku). Never raises."""
        try:
            resp = self.client.with_options(timeout=timeout, max_retries=0).messages.create(
                model=self.model, max_tokens=256,
                system=("Reply with ONLY one JSON object matching this JSON schema, "
                        "no prose: " + json.dumps(schema)),
                messages=[{"role": "user", "content": prompt}])
        except Exception as e:
            _log.info("classifier call failed: %s: %s", type(e).__name__, e)
            return None, TokenUsage()
        return _json_object(_first_text(resp)), _usage_of(resp)

    def _stream_turn(self, system: str, convo: list[dict], tools: list[dict] | None,
                     force_tool: bool = False, thinking: str = "standard"):
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
              "messages": convo, **self._tier_kw(thinking)}
        if tools is not None:
            kw["tools"] = tools
        if force_tool:
            # #159: must call a tool this turn. The API rejects a forced tool_choice
            # with thinking on, so this one turn runs without it -- and without an
            # effort level (Opus 5 400s on disabled thinking at effort max).
            kw["tool_choice"] = {"type": "any"}
            kw["thinking"] = {"type": "disabled"}
            kw.pop("output_config", None)
        if not hasattr(self.client.messages, "stream"):
            resp = self.client.messages.create(**kw)
            text = _first_text(resp)
            if text:
                yield {"type": "text", "text": text}
            yield self._usage_event(resp)
            return resp
        with self.client.messages.stream(**kw) as stream:
            for ev in stream:
                kind = getattr(ev, "type", None)
                if kind == "text":
                    yield {"type": "text", "text": ev.text}
                elif (kind == "content_block_start"
                      and getattr(ev.content_block, "type", None) == "thinking"):
                    yield {"type": "thinking"}      # #175: the UI's "thinking..." label
            final = stream.get_final_message()
        # #175: thinking text is for tuning only -- logged, never yielded, so never
        # shown, spoken or grounded as data. It bills as output: already in usage.
        thought = "".join(getattr(b, "thinking", "") or "" for b in final.content
                          if getattr(b, "type", None) == "thinking")
        if thought:
            _log.info("coach thinking %s (%s, %s tier): %s", TURN_TAG.get(), self.model,
                      thinking, thought)

        yield self._usage_event(final)
        return final

    def _cut(self, final) -> list[dict]:
        """#175: a turn that ran out of max_tokens mid-answer -> one
        {"type":"stop","reason":"max_tokens"} event (logged WARNING), so the coach
        never releases the cut-off sentence. [] for any other stop."""
        if getattr(final, "stop_reason", None) != "max_tokens":
            return []
        _log.warning("coach answer hit max_tokens (%s): the cut-off tail is dropped",
                     self.model)
        return [{"type": "stop", "reason": "max_tokens"}]

    def _usage_event(self, resp) -> dict:
        return {"type": "usage", "usage": _usage_of(resp).as_dict(), "model": self.model}

    def run_stream(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
        dispatch: Callable[[str, dict], str],
        force_tool: bool = False,
        thinking: str = "standard",
    ) -> Iterator[dict]:
        """Agentic tool-use loop: let the model call `tools` to fetch data on
        demand, executing each via `dispatch`, until it stops calling tools.
        Yields a `tool` event before dispatching each tool_use block, and
        streams the answer as one or more `text` delta events. `thinking` is the
        #175 tier for every round (a forced #159 round runs without it). An API
        failure the driver can act on (bad key, no credit, rate limit, offline)
        raises CoachUserError with a plain message instead of the masked
        "coach error"."""
        try:
            yield from self._run_stream(system, messages, tools, dispatch,
                                        force_tool=force_tool, thinking=thinking)
        except anthropic.APIError as e:
            msg = _api_error_message(e)
            if msg is None:
                raise
            raise CoachUserError(msg) from e

    def _run_stream(self, system, messages, tools, dispatch, force_tool, thinking):
        # Copy so we don't mutate the caller's history (the coach appends only the
        # final Q/A itself). tool_use/tool_result turns stay internal to this call.
        convo = list(messages)
        for i in range(MAX_TOOL_ITERATIONS):
            final = yield from self._stream_turn(system, convo, tools,
                                                 force_tool=force_tool and i == 0,
                                                 thinking=thinking)
            if final.stop_reason != "tool_use":
                yield from self._cut(final)
                return
            # The WHOLE content goes back, thinking blocks (and their signatures)
            # included: the API requires them unchanged across tool-use turns.
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
        final = yield from self._stream_turn(system, convo, tools=None, thinking=thinking)
        yield from self._cut(final)


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


def _api_error_message(e: Exception) -> str | None:
    """A driver-facing message for an API failure they can act on, or None (stays
    masked). Keyed on the body's error `type` first: an error that arrives
    mid-stream comes back as a bare APIStatusError carrying the stream's HTTP 200,
    so the exception class alone can't tell an overload from a bad key."""
    if isinstance(e, anthropic.APIConnectionError):     # includes APITimeoutError
        return "Can't reach Anthropic — check your internet connection."
    body = getattr(e, "body", None)
    err = body.get("error") if isinstance(body, dict) else None
    kind = err.get("type") if isinstance(err, dict) else None
    status = getattr(e, "status_code", None)
    if kind == "authentication_error" or status == 401:
        return "Anthropic rejected the API key (expired or revoked) — add a valid one in Settings."
    if kind == "billing_error" or status == 402 or "credit balance" in str(e).lower():
        return "Your Anthropic account is out of credit — top it up in the Anthropic Console."
    if kind == "permission_error" or status == 403:
        return "This API key isn't allowed to use the coach's model — check it in the Anthropic Console."
    if kind == "rate_limit_error" or status == 429:
        return "Anthropic's rate limit was hit — try again in a moment."
    if kind in ("overloaded_error", "api_error") or (status or 0) >= 500:
        return "Anthropic is having trouble right now — try again in a moment."
    return None


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


def _json_object(text: str) -> dict | None:
    """The one JSON object in `text` (a model may fence it), or None."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        obj = json.loads(text[start:end + 1])
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _first_text(resp) -> str:
    # Return the first TEXT block. Models with extended thinking put a ThinkingBlock
    # (no .text) first, so content[0].text would AttributeError (observed live
    # 2026-08-28 with claude-sonnet-5).
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            return block.text
    return "".join(getattr(b, "text", "") for b in resp.content)
