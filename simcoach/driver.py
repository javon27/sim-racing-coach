from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class DriverProfile:
    """Driver's stated preferences + risk tolerance, plus freeform notes learned
    over time, used to personalize coaching advice.

    Field schema (braking_style/balance_preference/risk_tolerance) is an
    educated guess pending user refinement — not derived from a spec.
    """
    braking_style: str | None = None       # e.g. "late", "early", "progressive"
    balance_preference: str | None = None  # e.g. "understeer", "oversteer", "neutral"
    risk_tolerance: str | None = None      # e.g. "conservative", "moderate", "aggressive"
    learned_notes: list[str] = field(default_factory=list)

    def to_prompt_text(self) -> str | None:
        """Render as a system-prompt fragment, or None if the profile is empty."""
        facts = []
        if self.braking_style:
            facts.append(f"braking style {self.braking_style}")
        if self.balance_preference:
            facts.append(f"balance preference {self.balance_preference}")
        if self.risk_tolerance:
            facts.append(f"risk tolerance {self.risk_tolerance}")
        if self.learned_notes:
            facts.append("learned: " + "; ".join(self.learned_notes))
        if not facts:
            return None
        return "Driver profile — " + "; ".join(facts) + ". Personalize advice to this driver."
