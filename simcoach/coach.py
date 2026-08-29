import re
from pathlib import Path

from simcoach.analysis import LapAnalysis
from simcoach.driver import DriverProfile
from simcoach.model.base import Model

# Setup params worth putting in front of the coach — a full LMU setup is ~170 keys,
# many cryptic/indexed (CHASSIS_ADJ_NN); these substrings keep the human-meaningful,
# commonly-coached adjustments and drop the noise.
_SETUP_TERMS = (
    "wing", "aero", "brake", "antilock", "abs", "diff", "ramp", "preload", "coast",
    "ride_height", "rideheight", "ride", "spring", "damp", "bump", "rebound", "packer",
    "camber", "toe", "caster", "antiroll", "arb", "roll_bar", "pressure", "traction",
    "tc_", "migration", "balance", "steer", "duct", "radiator", "fuel", "engine_brak",
)


def _setup_lines(setup: dict) -> list[str]:
    """Human-meaningful setup params, formatted for the prompt (filters ~170 -> the
    commonly-coached subset; flags tier-locked ones)."""
    out = []
    for key, v in setup.items():
        if not isinstance(v, dict) or "stringValue" not in v:
            continue
        k = (key[3:] if key.startswith("VM_") else key).lower()
        if not any(t in k for t in _SETUP_TERMS):
            continue
        sv = str(v["stringValue"]).strip()
        if sv in ("N/A", "Non-adjustable", "None", ""):  # no-info params for this car
            continue
        label = key[3:].replace("_", " ").title() if key.startswith("VM_") else key
        locked = "" if v.get("isFreeSetting", True) else " [locked this tier]"
        out.append(f"- {label}: {v['stringValue']}{locked}")
    return out


SYSTEM_PROMPT = (
    "You are an expert sim-racing race engineer and driver coach for the driver of "
    "a {car}. You have deep knowledge of vehicle dynamics, car setup, and driving "
    "technique — apply that expertise freely. The ONE hard rule: each corner's "
    "identity (its number, name, and direction) is authoritative ground truth from "
    "telemetry, given in the user message. Never infer, guess, or change a corner's "
    "number, name, or direction; if the question asserts a different one, it is "
    "mistaken — correct it from the facts.\n\n"
    "How to coach:\n"
    "- Lead with the biggest lap-time loss. Rank issues by time impact; don't list "
    "everything.\n"
    "- For each: name the symptom in the data, the likely root cause, and a specific, "
    "actionable fix — a setup change with direction and rough amount, or a driving "
    "change with a concrete cue. State cause→effect briefly.\n"
    "- Keep setup changes and driving changes separate.\n"
    "- Weigh lap context: tire temperature / warm-up, fuel load, and tire wear all "
    "change how a lap reads. If the data suggests cold tires, heavy fuel, or high "
    "wear made this lap unrepresentative, say so before over-reading it.\n"
    "- TONE: constructive and encouraging, like a race engineer who wants the driver to "
    "improve. Note what they're doing well before the fixes; never berate. Honor stated "
    "preferences; push back only when one clearly costs time or stability, and say why.\n"
    "- FORMAT for a terminal: open with a one-line headline naming the single biggest gain. "
    "Then at most 3 short points, each a line or two, with a blank line between them. No "
    "dense paragraphs or walls of text. This is a quick between-laps radio call, not a "
    "written report — favor brevity over completeness; the driver will ask follow-ups."
)


class Coach:
    """Assembles prompts from analysis + corner facts + knowledge notes and calls the Model."""

    def __init__(self, model: Model, knowledge_dir: str):
        self.model = model
        self.knowledge_dir = knowledge_dir

    def build_prompt(
        self,
        analysis: LapAnalysis | None,
        car: str,
        question: str,
        allowed_adjustments: list[str] | None = None,
        driver_profile: DriverProfile | None = None,
        setup: dict | None = None,
        track_note: str | None = None,
        input_device: str | None = None,
    ) -> tuple[str, str]:
        sections: list[str] = []
        if analysis is not None:
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
            sections.append("Corner facts (authoritative, from telemetry):")
            if analysis.has_reference:
                sections.append("Deltas below are vs the driver's best lap on this track/car.")
            sections.extend(corner_lines)
        else:
            sections.append(
                "No lap captured yet — answer from the track/car knowledge and current "
                "setup below. Give general track/setup guidance, not lap-specific claims."
            )

        # Current setup values, so setup advice can be specific ("wing 6->8") rather
        # than generic. Only real setup params (dict entries with a stringValue) are
        # included; garage-data metadata (driverName, carVelocity, ...) is skipped.
        if setup:
            lines = _setup_lines(setup)
            if lines:
                sections.append("\nCurrent car setup (adjust THESE for setup fixes):")
                sections.extend(lines)

        # Track note (already a resolved slug, e.g. the map filename stem); slugify
        # again defensively so it can't escape the knowledge dir.
        if track_note:
            tslug = re.sub(r"[^a-z0-9]+", "_", track_note.lower()).strip("_")
            tfile = Path(self.knowledge_dir) / f"{tslug}.md"
            if tslug and tfile.exists():
                sections.append(f"\nTrack notes:\n{tfile.read_text()}")

        # Load the car note by a filesystem-safe slug of the car name (slugifying also
        # neutralizes any path separators, so a crafted car name can't escape the dir).
        slug = re.sub(r"[^a-z0-9]+", "_", car.lower()).strip("_")
        if slug:
            notes_file = Path(self.knowledge_dir) / f"{slug}.md"
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

        if driver_profile is not None:
            profile_text = driver_profile.to_prompt_text()
            if profile_text:
                system += f" {profile_text}"

        if input_device:
            system += f" The driver is using a {input_device}."
            if any(w in input_device.lower() for w in ("controller", "gamepad", "pad")):
                system += (
                    " Do NOT read input-device artifacts as technique errors: on a controller, "
                    "near-zero trail-braking, stepped/digital steering, and abrupt throttle or "
                    "brake are the DEVICE, not the driver. Coach line, braking point, commitment, "
                    "and setup — do not push trail-brake modulation or input smoothness as fixes."
                )

        user_content = "\n".join(sections)
        return system, user_content

    def ask(
        self,
        analysis: LapAnalysis | None,
        car: str,
        question: str,
        history: list[dict] | None = None,
        allowed_adjustments: list[str] | None = None,
        driver_profile: DriverProfile | None = None,
        setup: dict | None = None,
        track_note: str | None = None,
        input_device: str | None = None,
    ) -> str:
        system, user_content = self.build_prompt(
            analysis, car, question, allowed_adjustments, driver_profile, setup,
            track_note, input_device,
        )
        messages = list(history) if history else []
        messages.append({"role": "user", "content": user_content})
        return self.model.complete(system, messages)
