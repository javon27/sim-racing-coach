from collections.abc import Callable

from .base import RunResult, TokenUsage

# Stand-in model id: never priced as a paid model (see simcoach.pricing.FREE).
FAKE_MODEL_ID = "fake"


class FakeModel:
    """Fake model for testing. Records calls and returns a fixed reply. `run` and
    `run_stream` can be scripted with tool calls to exercise the agentic loop offline."""

    can_force_tool = True      # see Model.run_stream(force_tool=)

    def __init__(self, reply: str = "ok", tool_script: list | None = None,
                 stream_script: list[tuple] | None = None,
                 forced_stream_script: list[tuple] | None = None):
        self.reply = reply
        # tool_script: list of (tool_name, input_dict) the fake "model" will call
        # via dispatch before returning `reply`. Empty = answer with no tool calls.
        self.tool_script = tool_script or []
        # stream_script (run_stream only): list of ("text", str) / ("tool", name,
        # input) in emission order, for tests that need text blocks interleaved
        # with tool calls (#133) rather than tool_script's tools-then-one-reply
        # shape. None -> fall back to tool_script + reply.
        self.stream_script = stream_script
        # forced_stream_script (#159): what run_stream replays when called with
        # force_tool=True -- a forced re-ask. None -> the normal script again.
        self.forced_stream_script = forced_stream_script
        self.forced: list[bool] = []    # force_tool of each run_stream call, in order
        self.last_system: str | None = None
        self.last_messages: list[dict] | None = None
        self.last_tools: list[dict] | None = None
        self.dispatched: list[tuple] = []   # (name, input, result) actually dispatched

    def complete(self, system: str, messages: list[dict]) -> str:
        """Record the call and return the fixed reply."""
        self.last_system = system
        self.last_messages = messages
        return self.reply

    def _usage(self, messages: list[dict]) -> TokenUsage:
        """Deterministic, network-free stand-in for billed usage (~4 chars/token)."""
        return TokenUsage(input=sum(len(str(m)) for m in messages) // 4,
                          output=len(self.reply) // 4)

    def run(self, system: str, messages: list[dict], tools: list[dict],
            dispatch: Callable[[str, dict], str]) -> RunResult:
        """Dispatch the scripted tool calls, then return the fixed reply + usage."""
        self.last_system = system
        self.last_messages = messages
        self.last_tools = tools
        for name, inp in self.tool_script:
            result = dispatch(name, inp)
            self.dispatched.append((name, inp, result))
        return RunResult(text=self.reply, usage=self._usage(messages))

    def run_stream(self, system: str, messages: list[dict], tools: list[dict],
                   dispatch: Callable[[str, dict], str], force_tool: bool = False):
        """With stream_script set, replay it verbatim (dispatching each "tool" step)
        so callers can script text/tool/text interleavings (#133). Otherwise: dispatch
        the scripted tool calls, yielding a tool event per call, then yield the fixed
        reply as one text event and one usage event. Mirrors ClaudeModel: shows the
        tool event before dispatching, swallows a raising dispatch so the loop
        continues, and puts usage last (one "turn")."""
        self.last_system = system
        self.last_messages = messages
        self.last_tools = tools
        self.forced.append(force_tool)
        script = self.stream_script
        if force_tool and self.forced_stream_script is not None:
            script = self.forced_stream_script
        if script is not None:
            for step in script:
                if step[0] == "text":
                    yield {"type": "text", "text": step[1]}
                else:
                    _, name, inp = step
                    yield {"type": "tool", "name": name, "input": inp}
                    try:
                        result = dispatch(name, inp)
                        self.dispatched.append((name, inp, result))
                    except Exception:
                        pass
            yield {"type": "usage", "usage": self._usage(messages).as_dict(),
                   "model": FAKE_MODEL_ID}
            return
        for name, inp in self.tool_script:
            yield {"type": "tool", "name": name, "input": inp}
            try:
                result = dispatch(name, inp)
                self.dispatched.append((name, inp, result))
            except Exception:
                pass
        yield {"type": "text", "text": self.reply}
        yield {"type": "usage", "usage": self._usage(messages).as_dict(),
               "model": FAKE_MODEL_ID}
