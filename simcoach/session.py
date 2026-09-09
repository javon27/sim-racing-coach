"""CoachSession: holds session state (car, track, input device, driver profile)
and maintains the conversation history so the coach remembers what was discussed
within a session. Threads prior Q&A into each ask() so follow-up questions have
context ("did the change you suggested help?"), and persists each exchange to the
LapStore chat log. This is memory layer L1 (short-term); cross-session memory (L3)
reads back the persisted log.

Setup and the legal-adjustment list are passed per ask() because they change
during a session (fresh setup fetched each question, tier constraints derived
from it); the rest is session-constant and held on the instance."""
from __future__ import annotations

from simcoach.coach import Coach
from simcoach.driver import DriverProfile
from simcoach.model.base import TokenUsage


class CoachSession:
    def __init__(
        self,
        coach: Coach,
        car: str,
        *,
        track_note: str | None = None,
        input_device: str | None = None,
        driver_profile: DriverProfile | None = None,
        store=None,
        session_id: str | None = None,
        units: str = "metric",
        chat_id: str | None = None,
    ):
        self.coach = coach
        self.car = car
        self.track_note = track_note
        self.input_device = input_device
        self.driver_profile = driver_profile
        self.store = store            # optional LapStore; when set, exchanges persist
        self.session_id = session_id
        self.units = units
        # Chat is decoupled from the race session (a chat can span sessions). Minted
        # lazily on first persisted exchange if not supplied, so no message is ever
        # stored with a NULL chat_id.
        self.chat_id = chat_id
        # Lightweight Q&A history (questions + answers only — NOT the per-lap corner
        # fact dumps, which would bloat context and mix lap contexts). The coach
        # appends the current lap's facts + question after this history.
        self.history: list[dict] = []
        # Usage accumulated over this session's asks, for the REPL. NOTE: the
        # authoritative per-chat accumulator is the store (coach_chat columns +
        # LapStore.chat_usage) — the desktop sidecar never builds a CoachSession.
        self.last_usage = TokenUsage()

    def ask(self, context, question: str) -> str:
        """Answer one question via the agentic tool loop. `context` (a CoachContext)
        holds the live lap/setup/units the tools read; history threads the chat."""
        res = self.coach.ask_agentic(
            context,
            question,
            self.car,
            history=self.history,
            driver_profile=self.driver_profile,
            input_device=self.input_device,
            units=self.units,
        )
        answer = res.text
        self.last_usage = self.last_usage + res.usage
        self.history.append({"role": "user", "content": question})
        self.history.append({"role": "assistant", "content": answer})
        if self.store is not None:
            # A persistence hiccup must never swallow an answer the driver is waiting on.
            try:
                if self.chat_id is None:
                    self.chat_id = self.store.create_chat()
                self.store.save_chat(self.chat_id, self.session_id, context.track, self.car,
                                     context.lap_id, question, answer,
                                     tool_calls=context.tool_calls)
            except Exception:
                pass
        return answer

    def reset(self) -> None:
        """Clear the conversation (e.g. new session/stint)."""
        self.history = []
