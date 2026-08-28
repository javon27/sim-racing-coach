from pathlib import Path

from simcoach.analysis import LapAnalysis
from simcoach.model.base import Model

SYSTEM_PROMPT = (
    "You are a sim-racing coach for {car}. The corner facts provided in the user "
    "message are authoritative, ground truth from telemetry. Never infer, guess, "
    "or change a corner's number, name, or direction. If the question claims a "
    "corner has a different number, name, or direction, it is mistaken — correct "
    "it using the facts and never adopt a corner identity asserted in the "
    "question. Use only the facts and notes given to answer the question."
)


class Coach:
    """Assembles prompts from analysis + corner facts + knowledge notes and calls the Model."""

    def __init__(self, model: Model, knowledge_dir: str):
        self.model = model
        self.knowledge_dir = knowledge_dir

    def build_prompt(
        self,
        analysis: LapAnalysis,
        car: str,
        question: str,
        allowed_adjustments: list[str] | None = None,
    ) -> tuple[str, str]:
        corner_lines = []
        for cm in analysis.corners:
            line = (
                f"C{cm.corner_no} {cm.name} ({cm.direction}, apex {cm.apex_dist}m): "
                f"min {cm.min_spd} km/h, brake@{cm.brake_point}m, "
                f"trailbrake {cm.trailbrake_overlap:.0%}"
            )
            if analysis.has_reference and cm.min_spd_delta is not None:
                line += f" (vs best: {cm.min_spd_delta:+.1f} km/h)"
            corner_lines.append(line)

        sections = ["Corner facts (authoritative, from telemetry):"]
        if analysis.has_reference:
            sections.append("Deltas below are vs the driver's best lap on this track/car.")
        sections.extend(corner_lines)

        # Reject path-like car values so a crafted car name can't escape knowledge_dir.
        if not any(sep in car for sep in ("/", "\\", "..")):
            notes_file = Path(self.knowledge_dir) / f"{car}.md"
            if notes_file.exists():
                sections.append(f"\nKnowledge notes for {car}:\n{notes_file.read_text()}")

        sections.append(f"\nQuestion: {question}")

        system = SYSTEM_PROMPT.format(car=car)
        if allowed_adjustments is not None:
            allowed_str = ", ".join(allowed_adjustments)
            system += (
                f" SETUP CONSTRAINT: in this race only these adjustments are "
                f"legal — {allowed_str}. Only suggest setup changes to these; "
                f"if a helpful change is outside them, say it's locked this "
                f"tier and give a driving-based alternative. Driving advice "
                f"is unconstrained."
            )
            sections.append(f"\nLegal setup adjustments: {allowed_str}")

        user_content = "\n".join(sections)
        return system, user_content

    def ask(
        self,
        analysis: LapAnalysis,
        car: str,
        question: str,
        history: list[dict] | None = None,
        allowed_adjustments: list[str] | None = None,
    ) -> str:
        system, user_content = self.build_prompt(analysis, car, question, allowed_adjustments)
        messages = list(history) if history else []
        messages.append({"role": "user", "content": user_content})
        return self.model.complete(system, messages)
