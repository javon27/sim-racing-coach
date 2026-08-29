import anthropic

from simcoach.model.base import Model


class ClaudeModel:
    """Claude model implementation via anthropic SDK."""

    def __init__(self, model: str = "claude-sonnet-5", api_key: str | None = None,
                 max_tokens: int = 4096, thinking: bool = False):
        self.model = model
        self.max_tokens = max_tokens
        self.thinking = thinking
        self.client = anthropic.Anthropic(api_key=api_key)

    def complete(self, system: str, messages: list[dict]) -> str:
        """Generate a completion via Claude.

        Args:
            system: System prompt.
            messages: List of messages with 'role' and 'content'.

        Returns:
            The model's text response.
        """
        # sonnet-5 accepts thinking {"type": "disabled"}; budget_tokens is rejected
        # (400) on this model, so never send it. Default off for a snappy coach.
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=messages,
            thinking={"type": "adaptive"} if self.thinking else {"type": "disabled"},
        )
        # Return the first TEXT block. Models with extended thinking put a
        # ThinkingBlock (no .text) first, so content[0].text would AttributeError
        # (observed live 2026-08-28 with claude-sonnet-5).
        for block in resp.content:
            if getattr(block, "type", None) == "text":
                return block.text
        # Fallback: concatenate any text-bearing blocks.
        return "".join(getattr(b, "text", "") for b in resp.content)
