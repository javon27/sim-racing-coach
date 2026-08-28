import anthropic

from simcoach.model.base import Model


class ClaudeModel:
    """Claude model implementation via anthropic SDK."""

    def __init__(self, model: str = "claude-sonnet-5", api_key: str | None = None):
        self.model = model
        self.client = anthropic.Anthropic(api_key=api_key)

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
            max_tokens=1024,
            system=system,
            messages=messages,
        )
        return resp.content[0].text
