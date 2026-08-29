"""CoachSession: holds session state (car, tier constraints, driver profile) and
maintains the conversation history so the coach remembers what was discussed
within a session. Threads prior Q&A into each ask() so follow-up questions have
context ("did the change you suggested help?"). This is memory layer L1 (short-
term); cross-session memory (L3) is a separate, durable store."""
from __future__ import annotations

from simcoach.analysis import LapAnalysis
from simcoach.coach import Coach
from simcoach.driver import DriverProfile


class CoachSession:
    def __init__(
        self,
        coach: Coach,
        car: str,
        allowed_adjustments: list[str] | None = None,
        driver_profile: DriverProfile | None = None,
    ):
        self.coach = coach
        self.car = car
        self.allowed_adjustments = allowed_adjustments
        self.driver_profile = driver_profile
        # Lightweight Q&A history (questions + answers only — NOT the per-lap corner
        # fact dumps, which would bloat context and mix lap contexts). The coach
        # appends the current lap's facts + question after this history.
        self.history: list[dict] = []

    def ask(self, analysis: LapAnalysis, question: str) -> str:
        answer = self.coach.ask(
            analysis,
            self.car,
            question,
            history=self.history,
            allowed_adjustments=self.allowed_adjustments,
            driver_profile=self.driver_profile,
        )
        self.history.append({"role": "user", "content": question})
        self.history.append({"role": "assistant", "content": answer})
        return answer

    def reset(self) -> None:
        """Clear the conversation (e.g. new session/stint)."""
        self.history = []
