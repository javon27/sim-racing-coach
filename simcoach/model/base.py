from typing import Protocol


class Model(Protocol):
    """Protocol for LLM interaction."""

    def complete(self, system: str, messages: list[dict]) -> str:
        """Generate a completion given a system prompt and messages.

        Args:
            system: System prompt.
            messages: List of messages, each with 'role' ("user"|"assistant") and 'content' (str).

        Returns:
            The model's text response.
        """
        ...
