from simcoach.model.base import Model


class FakeModel:
    """Fake model for testing. Records calls and returns a fixed reply."""

    def __init__(self, reply: str = "ok"):
        self.reply = reply
        self.last_system: str | None = None
        self.last_messages: list[dict] | None = None

    def complete(self, system: str, messages: list[dict]) -> str:
        """Record the call and return the fixed reply."""
        self.last_system = system
        self.last_messages = messages
        return self.reply
