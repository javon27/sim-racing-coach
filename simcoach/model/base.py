from typing import Callable, Protocol


class Model(Protocol):
    """Protocol for LLM interaction."""

    def complete(self, system: str, messages: list[dict]) -> str:
        """Generate a plain completion given a system prompt and messages.

        Args:
            system: System prompt.
            messages: List of messages, each with 'role' ("user"|"assistant") and 'content' (str).

        Returns:
            The model's text response.
        """
        ...

    def run(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
        dispatch: Callable[[str, dict], str],
    ) -> str:
        """Run an agentic tool-use loop and return the final text.

        The model may call the given tools to fetch data on demand; each call is
        executed by `dispatch(name, input) -> str` and fed back until the model
        stops calling tools. Provider-agnostic: implementations own the loop and
        their own wire format; the caller only supplies tool specs + a dispatcher.

        Args:
            system: System prompt.
            messages: Conversation so far (role/content dicts).
            tools: Tool specs, each {"name", "description", "input_schema"}.
            dispatch: Executes one tool call, returning its text result.

        Returns:
            The model's final text response after any tool calls.
        """
        ...
