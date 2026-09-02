from typing import Callable


class FakeModel:
    """Fake model for testing. Records calls and returns a fixed reply. `run` and
    `run_stream` can be scripted with tool calls to exercise the agentic loop offline."""

    def __init__(self, reply: str = "ok", tool_script: list | None = None):
        self.reply = reply
        # tool_script: list of (tool_name, input_dict) the fake "model" will call
        # via dispatch before returning `reply`. Empty = answer with no tool calls.
        self.tool_script = tool_script or []
        self.last_system: str | None = None
        self.last_messages: list[dict] | None = None
        self.last_tools: list[dict] | None = None
        self.dispatched: list[tuple] = []   # (name, input, result) actually dispatched

    def complete(self, system: str, messages: list[dict]) -> str:
        """Record the call and return the fixed reply."""
        self.last_system = system
        self.last_messages = messages
        return self.reply

    def run(self, system: str, messages: list[dict], tools: list[dict],
            dispatch: Callable[[str, dict], str]) -> str:
        """Dispatch the scripted tool calls, then return the fixed reply."""
        self.last_system = system
        self.last_messages = messages
        self.last_tools = tools
        for name, inp in self.tool_script:
            result = dispatch(name, inp)
            self.dispatched.append((name, inp, result))
        return self.reply

    def run_stream(self, system: str, messages: list[dict], tools: list[dict],
                   dispatch: Callable[[str, dict], str]):
        """Dispatch the scripted tool calls, yielding a tool event per call, then
        yield the fixed reply as one text event. Mirrors ClaudeModel: shows the
        tool event before dispatching, and swallows a raising dispatch so the
        loop continues."""
        self.last_system = system
        self.last_messages = messages
        self.last_tools = tools
        for name, inp in self.tool_script:
            yield {"type": "tool", "name": name, "input": inp}
            try:
                result = dispatch(name, inp)
                self.dispatched.append((name, inp, result))
            except Exception:
                pass
        yield {"type": "text", "text": self.reply}
