from dataclasses import dataclass
from typing import Callable, Iterator, Protocol


@dataclass(frozen=True)
class TokenUsage:
    """Billed tokens for one or more model calls, in provider-neutral buckets.

    Summable on purpose: the agentic tool loop makes up to MAX_TOOL_ITERATIONS + 1
    API calls per question, and the cost is the SUM of them, not the last one.
    `cache_read`/`cache_write` are populated once the loop's stable prefix is
    cached (issue #42): iteration 1 of a question writes it, iterations 2..N read
    it. They price differently from `input` — see simcoach/pricing.py."""
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        if not isinstance(other, TokenUsage):
            return NotImplemented
        return TokenUsage(
            input=self.input + other.input,
            output=self.output + other.output,
            cache_read=self.cache_read + other.cache_read,
            cache_write=self.cache_write + other.cache_write,
        )

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_write

    def as_dict(self) -> dict:
        """The wire/storage shape; `TokenUsage(**d)` reverses it."""
        return {"input": self.input, "output": self.output,
                "cache_read": self.cache_read, "cache_write": self.cache_write}


@dataclass(frozen=True)
class RunResult:
    """What `Model.run` returns: the answer plus what it cost to produce."""
    text: str
    usage: TokenUsage = TokenUsage()


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
    ) -> RunResult:
        """Run an agentic tool-use loop and return the final text plus its token cost.

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
            A RunResult: the final text response after any tool calls, and the
            token usage SUMMED over every API call the loop made.
        """
        ...

    def run_stream(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
        dispatch: Callable[[str, dict], str],
    ) -> Iterator[dict]:
        """Like `run`, but yield events as they occur instead of returning a string:
        {"type":"tool","name","input"} as each tool is called, then one or more
        {"type":"text","text"} events for the answer. Terminal done/error markers are
        added by the caller (coach/server), not here.

        Each billed model turn also yields exactly ONE usage event, after that
        turn's text/tool events:
            {"type":"usage", "usage": {"input","output","cache_read","cache_write"},
             "model": "<model id>"}
        A multi-turn tool loop therefore yields several; the cost of the question is
        their sum. `model` rides along per event because the configured model can
        change between calls, so a chat total is not priceable from one id."""
        ...
