from typing import Callable, Iterator

import anthropic

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
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=messages,
            thinking=self._thinking_param(),
        )
        return _first_text(resp)

    def run_stream(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
        dispatch: Callable[[str, dict], str],
    ) -> Iterator[dict]:
        """Agentic tool-use loop: let the model call `tools` to fetch data on
        demand, executing each via `dispatch`, until it stops calling tools.
        Yields a `tool` event before dispatching each tool_use block, then a
        `text` event with the final answer."""
        # Copy so we don't mutate the caller's history (the coach appends only the
        # final Q/A itself). tool_use/tool_result turns stay internal to this call.
        convo = list(messages)
        for _ in range(MAX_TOOL_ITERATIONS):
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                messages=convo,
                tools=tools,
                thinking=self._thinking_param(),
            )
            if resp.stop_reason != "tool_use":
                yield {"type": "text", "text": _first_text(resp)}
                return
            convo.append({"role": "assistant", "content": resp.content})
            results = []
            for block in resp.content:
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
        resp = self.client.messages.create(
            model=self.model, max_tokens=self.max_tokens, system=system,
            messages=convo, thinking=self._thinking_param(),
        )
        yield {"type": "text", "text": _first_text(resp)}

    def run(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
        dispatch: Callable[[str, dict], str],
    ) -> str:
        """Agentic tool-use loop; drains `run_stream` and returns the final text."""
        text = ""
        for ev in self.run_stream(system, messages, tools, dispatch):
            if ev["type"] == "text":
                text = ev["text"]
        return text


def _first_text(resp) -> str:
    # Return the first TEXT block. Models with extended thinking put a ThinkingBlock
    # (no .text) first, so content[0].text would AttributeError (observed live
    # 2026-08-28 with claude-sonnet-5).
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            return block.text
    return "".join(getattr(b, "text", "") for b in resp.content)
