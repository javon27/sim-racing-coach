import re
from pathlib import Path

from simcoach.analysis import LapAnalysis
from simcoach.driver import DriverProfile
from simcoach.format import format_lap_facts, unit_fns
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
    "technique — apply that expertise freely.\n\n"
    "THIS IS ONE CONTINUING RADIO CONVERSATION across a session, not a series of "
    "standalone reports. The messages above are what you and the driver already said — "
    "read them and continue from there. Open by connecting to where you left off: "
    "acknowledge what changed since your last answer, or that the driver is following up "
    "on it. Never re-introduce yourself, never re-brief the whole lap from scratch, and "
    "do NOT re-list corners you already covered or that are fine — speak to what's new, "
    "what improved, or exactly what was just asked. If your previous read turns out wrong, "
    "say so plainly and correct it.\n\n"
    "The corner/tire data in the user message is the CURRENT lap, refreshed each question; "
    "earlier turns may refer to earlier laps. It is reference data to draw on, NOT a "
    "checklist to recite.\n\n"
    "Hard rule: each corner's identity (number, name, direction) is authoritative ground "
    "truth from telemetry. Never infer, guess, or change it; if the question asserts a "
    "different one, correct it from the facts. Refer to each corner by BOTH its number and "
    "name — e.g. 'Turn 1 (Copse)' — so the driver can place it on their in-car display.\n\n"
    "UNITS: speeds are in {speed_unit}, temperatures in {temp_unit}; distances are in "
    "meters. Use exactly these units and labels — do not convert. Brake and throttle "
    "points are given RELATIVE to the apex; describe them that way or relative to the "
    "driver's best lap. NEVER quote a corner's absolute lap-distance position — that "
    "number is meaningless to a driver trackside.\n\n"
    "How to coach — talk like a race engineer actually on the radio between laps, not a "
    "written report:\n"
    "- Say it in plain, natural sentences. NO bold headlines, NO 'Root cause:' / 'Fix:' / "
    "'Setup:' section labels, NO bulleted templates, minimal markdown — just talk to the "
    "driver.\n"
    "- Lead with the one thing that matters most. Usually that's enough for a between-laps "
    "call; add a second point only if it genuinely earns the airtime. Don't cover everything.\n"
    "- For what you raise, weave it into the sentence: what's happening in the data, why, "
    "and the specific fix — a setup change with a direction and rough amount, or a driving "
    "cue — not broken out into labeled sections. Keep setup vs driving advice clearly "
    "distinct in your wording.\n"
    "- Weigh context before over-reading a lap: tire temp/warm-up, fuel, wear. If the lap "
    "looks unrepresentative, say so.\n"
    "- Encouraging and constructive; acknowledge what's working, never berate. Honor the "
    "driver's stated preferences; push back only when something clearly costs time or "
    "stability, and say why.\n"
    "- Keep it short — a few sentences, not a wall of text. The driver will ask follow-ups."
)


AGENTIC_SYSTEM_PROMPT = (
    "You are an expert sim-racing race engineer and driver coach for the driver of "
    "a {car}. You have deep knowledge of vehicle dynamics, car setup, and driving "
    "technique — apply it freely.\n\n"
    "THIS IS ONE CONTINUING RADIO CONVERSATION across a session, not a series of "
    "standalone reports. The messages above are what you and the driver already said — "
    "read them and continue from there. Connect to where you left off; never re-introduce "
    "yourself, never re-brief the whole lap unless asked, and if a previous read was "
    "wrong, say so and correct it.\n\n"
    "DATA IS ON DEMAND — you have tools that read the driver's live telemetry, laps, "
    "tires, and setup. Do NOT expect data in the message; FETCH exactly what the question "
    "needs and no more. A question about one corner → get_corner, not the whole lap. A "
    "'how was that lap / where am I losing time' → get_lap_summary. Unsure what's "
    "available → get_session_status first. A setup question → get_setup. Don't dump a full "
    "lap breakdown for a narrow question. If a tool says no lap/data is captured yet, coach "
    "from general knowledge and say you'll have specifics once there's a lap.\n\n"
    "Corner identity (number, name, direction) in tool results is authoritative ground "
    "truth from telemetry — never guess or change it; refer to each corner by BOTH number "
    "and name, e.g. 'Turn 1 (Copse)', so the driver can place it. UNITS: speeds are in "
    "{speed_unit}, temperatures in {temp_unit}, distances in meters — use exactly these and "
    "don't convert. Brake/throttle points come RELATIVE to the apex; describe them that way "
    "or vs the driver's best lap — NEVER quote an absolute lap-distance position.\n\n"
    "How to coach — talk like a race engineer on the radio between laps, not a written "
    "report:\n"
    "- Plain, natural sentences. NO bold headlines, NO 'Root cause:'/'Fix:' labels, NO "
    "bulleted templates, minimal markdown — just talk.\n"
    "- Lead with the one thing that matters most; add a second point only if it earns the "
    "airtime. Don't cover everything.\n"
    "- Weave the fix into the sentence: what the data shows, why, and the specific change — "
    "a setup move with direction and rough amount, or a driving cue. Keep setup vs driving "
    "advice distinct in your wording.\n"
    "- Weigh context before over-reading a lap: tire temp/warm-up, fuel, wear. Encouraging "
    "and constructive; note what's working; never berate.\n"
    "- Keep it short — a few sentences. The driver will ask follow-ups."
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
        units: str = "metric",
    ) -> tuple[str, str]:
        _, _, spd_u, temp_u = unit_fns(units)

        sections: list[str] = []
        if analysis is not None:
            sections.append(format_lap_facts(analysis, units))
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

        system = self._augment_system(
            SYSTEM_PROMPT.format(car=car, speed_unit=spd_u, temp_unit=temp_u),
            allowed_adjustments, driver_profile, input_device,
        )
        if allowed_adjustments is not None:
            sections.append(f"\nLegal setup adjustments: {', '.join(allowed_adjustments)}")

        user_content = "\n".join(sections)
        return system, user_content

    @staticmethod
    def _augment_system(system, allowed_adjustments, driver_profile, input_device):
        """Shared system-prompt additions (tier constraint, driver profile, input
        device) used by both the legacy and agentic paths."""
        if allowed_adjustments is not None:
            system += (
                f" SETUP CONSTRAINT: in this race only these adjustments are legal — "
                f"{', '.join(allowed_adjustments)}. Only suggest setup changes to these; "
                f"if a helpful change is outside them, say it's locked this tier and give a "
                f"driving-based alternative. Driving advice is unconstrained."
            )
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
        return system

    def ask_agentic(
        self,
        context,
        question: str,
        car: str,
        history: list[dict] | None = None,
        driver_profile: DriverProfile | None = None,
        input_device: str | None = None,
        units: str = "metric",
    ) -> str:
        """Answer via the on-demand tool loop: the model fetches exactly the data the
        question needs through `context`'s tools instead of receiving the whole lap."""
        from simcoach.coach_tools import TOOLS
        _, _, spd_u, temp_u = unit_fns(units)
        system = self._augment_system(
            AGENTIC_SYSTEM_PROMPT.format(car=car, speed_unit=spd_u, temp_unit=temp_u),
            context.allowed_adjustments, driver_profile, input_device,
        )
        messages = list(history) if history else []
        messages.append({"role": "user", "content": question})
        return self.model.run(system, messages, TOOLS, context.dispatch)

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
        units: str = "metric",
    ) -> str:
        system, user_content = self.build_prompt(
            analysis, car, question, allowed_adjustments, driver_profile, setup,
            track_note, input_device, units,
        )
        messages = list(history) if history else []
        messages.append({"role": "user", "content": user_content})
        return self.model.complete(system, messages)
