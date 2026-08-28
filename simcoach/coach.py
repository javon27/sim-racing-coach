from pathlib import Path

from simcoach.analysis import LapAnalysis
from simcoach.corners import CornerMap
from simcoach.model.base import Model

SYSTEM_PROMPT = (
    "You are a sim-racing coach for {car}. The corner list below is authoritative "
    "fact derived from telemetry. Never infer, guess, or change a corner's number, "
    "name, or direction. Use only the facts and notes given to answer the question."
)


class Coach:
    """Assembles prompts from analysis + corner facts + knowledge notes and calls the Model."""

    def __init__(self, model: Model, knowledge_dir: str):
        self.model = model
        self.knowledge_dir = knowledge_dir

    def build_prompt(
        self, analysis: LapAnalysis, cmap: CornerMap, car: str, question: str
    ) -> tuple[str, str]:
        apex_by_no = {corner.number: corner.apex_dist for corner in cmap.corners}

        corner_lines = [
            f"C{cm.corner_no} {cm.name} ({cm.direction}, apex {apex_by_no.get(cm.corner_no)}m): "
            f"min {cm.min_spd} km/h, brake@{cm.brake_point}m, "
            f"trailbrake {cm.trailbrake_overlap:.0%}"
            for cm in analysis.corners
        ]

        sections = ["Corner facts (authoritative, from telemetry):", *corner_lines]

        notes_file = Path(self.knowledge_dir) / f"{car}.md"
        if notes_file.exists():
            sections.append(f"\nKnowledge notes for {car}:\n{notes_file.read_text()}")

        sections.append(f"\nQuestion: {question}")

        system = SYSTEM_PROMPT.format(car=car)
        user_content = "\n".join(sections)
        return system, user_content

    def ask(
        self,
        analysis: LapAnalysis,
        cmap: CornerMap,
        car: str,
        question: str,
        history: list[dict] | None = None,
    ) -> str:
        system, user_content = self.build_prompt(analysis, cmap, car, question)
        messages = list(history) if history else []
        messages.append({"role": "user", "content": user_content})
        return self.model.complete(system, messages)
