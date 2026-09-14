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
    # Params the terms above silently dropped: LMU packs the anti-roll bars as ANTISWAY
    # (not the "antiroll"/"arb"/"roll_bar" spellings listed) and the engine map as
    # ELECTRIC_MOTOR_MAP, and nothing matched mixture or regen at all. All five are
    # settings the driver actually has, so the coach could not see or discuss them.
    # Together these render 5 more lines: VM_FRONT_ANTISWAY, VM_REAR_ANTISWAY,
    # VM_ELECTRIC_MOTOR_MAP, VM_ENGINE_MIXTURE, VM_REGEN_LEVEL -- every one a cockpit
    # electronic, so every one carries the garage caveat below.
    "antisway", "motor_map", "mixture", "regen",
)

# Cockpit-adjustable ELECTRONICS -- the dials a driver turns mid-stint. LMU's REST
# reports the GARAGE setup and ANY electronic change made on track is lost when the
# car returns to the garage, so these values can differ from what the driver is
# actually running (issue #33). Source: the app's driver, stated 2026-09-09 -- this
# supersedes the earlier note here that recorded brake balance as "unmeasured" and
# left it uncaveated; the discard covers all cockpit electronics, BB included.
#
# The criterion is CAN THE DRIVER CHANGE IT FROM THE COCKPIT -- not whether LMU
# reports it live. For most of the set the two coincide: TelemInfoV01 pairs mABS /
# mTC / mTCSlip / mTCCut / mMotorMap / mMigration / mFront+mRearAntiSway each with a
# *Max index, and carries mRearBrakeBias, precisely because they are cockpit dials.
# That pairing is strong evidence of cockpit-adjustability, not the definition of it:
# engine mixture, regen level and engine brake map are cockpit controls in this sim
# family too, yet no live source exists for them at all (mRegen is instantaneous
# regen, a c_float with no *Max -- the actual energy flow, not the driver's dialed
# level; there is no mEngineMixture field whatsoever). Having no live source makes
# the garage value LESS verifiable, not more, so they are caveated: asserting a
# possibly-stale value is exactly what issue #33 was filed for.
#
# Anti-sway is here because it sits in that cockpit-dial block, so LMU itself treats
# it as driver-adjustable, even though the bar is mechanical. On a car whose bar has
# no rotary this over-hedges one line; to revert, drop "antisway" from the tuple.
#
# Matched on the packed names so two things stay UNcaveated: the availability flags
# (ANTILOCK_BRAKES / TRACTION_CONTROL, "Available") -- not dials -- and every
# mechanical param (springs, ride height, camber, wings, dampers), which a driver
# cannot change on track; hedging those would make the coach hedge a fact.
_ELECTRONICS_TERMS = (
    "antilockbrakesystem", "tractioncontrol", "brake_balance", "brake_migration",
    "antisway", "motor_map", "engine_mixture", "engine_brak", "regen",
)
_GARAGE_CAVEAT = " [garage value — confirm what's dialed on-track]"


def _setup_lines(setup: dict) -> list[str]:
    """Human-meaningful setup params, formatted for the prompt (filters ~170 -> the
    commonly-coached subset; flags tier-locked ones and garage-sourced electronics).

    Appends "(step N of lo-hi)" -- the param's ORDINAL position among the garage's
    available options (issue #32). minValue/maxValue on LMU's garage payload are
    option INDICES, not physical bounds: VM_REAR_WING comes back as
    stringValue "6.5 deg", value 6, minValue 0, maxValue 9 -- wing position 6 of
    0-9. Rendering those bounds next to the physical stringValue ("1200lbf/in
    (range 0-4)") reads as a physical limit and is flatly wrong, so the suffix
    MUST name the position and MUST be omitted entirely when `value` is absent --
    bounds without their ordinal are the same unit mismatch. stringValue stays the
    displayed physical value; it already carries real units/labels.

    The payload exposes no step/increment SIZE anywhere, so the step count says
    only how far a param can still move, never by how much -- the system prompts
    tell the coach to give direction rather than an exact amount.
    """
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
        lo, hi, cur = v.get("minValue"), v.get("maxValue"), v.get("value")
        step = "" if None in (lo, hi, cur) else f" (step {cur} of {lo}-{hi})"
        garage = _GARAGE_CAVEAT if any(t in k for t in _ELECTRONICS_TERMS) else ""
        out.append(f"- {label}: {v['stringValue']}{step}{locked}{garage}")
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
    "SCOPE: you help with driving technique, car setup, race strategy/energy, and this "
    "car/track's telemetry and corners — nothing else. If asked for something outside that "
    "(write code, general trivia, unrelated tasks), decline in one friendly sentence and "
    "steer back to the driving; don't refuse legitimate racing questions like explaining a "
    "setup concept, a flag/rule, tyre behaviour, or what a telemetry channel means.\n\n"
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
    "- SETUP STEPS: each adjustable setup line ends with (step N of lo-hi) — N is that "
    "param's CURRENT position among the garage's available positions. It is an index, "
    "NOT a physical value: never restate it as a spring rate, pressure or angle. Use it "
    "only to tell whether a param is already at its limit and which way it can still "
    "move. One step's physical size is not exposed, so NEVER state an exact increment "
    "(e.g. '-50 lbf/in') — give direction ('soften the front spring a step or two').\n"
    "- COCKPIT ELECTRONICS: a setup line ending [garage value — ...] is the GARAGE "
    "setting. LMU discards ANY electronics change the driver made on track once the car "
    "returns to the garage — ABS, the TC family, the engine and motor maps, mixture, "
    "regen, brake migration, brake balance, anti-roll — so it never reports what they "
    "actually dialed in. Never "
    "assert one as what they're currently running — name it as the garage value and ask "
    "them to confirm. Mechanical params (springs, ride height, camber, wings, dampers) "
    "carry no such marker and ARE the truth; state those plainly.\n"
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
    "'how was that lap' OR 'where am I losing time / why am I slower / what should I work "
    "on' → get_lap_summary FIRST (it now leads with the ranked time-loss corners vs the "
    "reference), then get_corner on the worst one to explain why; reach for get_time_loss "
    "instead only for an explicit corner-by-corner ranking or a comparison against a "
    "SPECIFIC reference lap. A "
    "question about the "
    "SESSION or RACE AS A WHOLE — 'how's my race going', 'how am I doing overall', "
    "'am I improving', 'how's my pace / consistency' — is NOT about one lap: use "
    "list_recent_laps (and get_session_status) to reason across laps, and do NOT zoom into "
    "the selected/last lap unless the driver asked about a specific lap. Unsure what's "
    "available → get_session_status first. Setup → get_setup; tire temps/warm-up → "
    "get_tire_report; brakes/pressures/wear → get_wheel_report; fuel or energy saving → "
    "get_energy_report. Don't dump a full lap breakdown for a narrow question. If a tool "
    "says no lap/data is captured yet, coach from general knowledge and say you'll have "
    "specifics once there's a lap.\n\n"
    "When saying how far into a race or qualy the driver is, use the CURRENT-PHASE lap "
    "count (get_session_status reports 'this <phase>: N'), never the session total, which "
    "includes other phases.\n\n"
    "RACECRAFT: for a question about the race or how it went, call get_race_result. When it "
    "returns a result, LEAD with the race story — positions gained or lost, the incidents "
    "and penalties, the recovery arc — and treat lap-time and corner analysis as supporting "
    "detail; be encouraging through a comeback. When there's no race result (practice/qualy), "
    "coach pace and technique as normal.\n\n"
    "Corner identity (number, name, direction) in tool results is authoritative ground "
    "truth from the saved corner map — never guess or change it; refer to each corner by "
    "BOTH number and name, e.g. 'Turn 1 (Copse)', so the driver can place it. NEVER recall "
    "corner names, numbers, or directions from your own memory of the track — even for a "
    "pre-lap track walkthrough with zero laps, call list_track_corners and use ONLY the "
    "corners it returns; if there's no map, say so rather than inventing corners. When a corner is flagged "
    "as a double-apex (it reports per-apex minimum speeds), coach it as one: sacrifice entry "
    "for the first apex, rotate the car, and get the line right for the second apex and the "
    "exit that follows — reference both minima.\n\n"
    "For a track's character, cues, or hazards — or how any corner drives — call "
    "get_track_notes and quote the authored notes; NEVER invent track character from "
    "memory. If no notes exist, say so and coach from telemetry. "
    "When reference lap times are given, you MAY compare the driver's time to the "
    "benchmark for their class as an absolute yardstick — but these are community times, "
    "not the driver's data; frame them as a reference, not a verdict.\n\n"
    "For the car's handling character, a setup direction, or a driver-aid (ABS/TC/"
    "brake-bias) question, call get_car_notes and ground your advice in the authored "
    "car notes and the shared LMU setup concepts it returns — prefer these over general "
    "motorsport memory, and NEVER invent the car's character. Respect the provenance "
    "tags: [LMU] is game-specific, [general-sim] is cross-sim baseline, and "
    "[needs-refinement] items are tentative — hedge on those.\n\n"
    "HISTORY: for anything spanning past sessions — 'what's my all-time/personal best here', "
    "'am I improving over time', 'how did my last few sessions go', 'what setup did we run "
    "here last time' — call get_personal_best, get_progress, list_past_sessions, or "
    "get_last_setup rather than reasoning from this session's laps alone; they default to "
    "the current track/car if not given.\n\n"
    "DRIVER MEMORY: when you notice a durable, cross-lap tendency worth remembering (not "
    "one-off, per-lap noise), call record_driver_learning and tell the driver it's saved for "
    "their review — it is NOT applied automatically. Call get_driver_memories when the "
    "driver's history beyond the summary above would sharpen your advice.\n\n"
    "UNITS: speeds are in "
    "{speed_unit}, temperatures in {temp_unit}, distances in meters — use exactly these and "
    "don't convert. Brake/throttle points come RELATIVE to the apex; describe them that way "
    "or vs the driver's best lap — NEVER quote an absolute lap-distance position.\n\n"
    "SCOPE: you help with driving technique, car setup, race strategy/energy, and this "
    "car/track's telemetry and corners — nothing else. If asked for something outside that "
    "(write code, general trivia, unrelated tasks), decline in one friendly sentence and "
    "steer back to the driving; don't refuse legitimate racing questions like explaining a "
    "setup concept, a flag/rule, tyre behaviour, or what a telemetry channel means.\n\n"
    "How to coach — talk like a race engineer on the radio between laps, but WRITE it so it "
    "reads easily on screen, never a daunting wall of text:\n"
    "- Break the answer into SHORT paragraphs, one idea each, with a blank line between them. "
    "Never one dense block.\n"
    "- When you have several discrete findings (multiple corners, a few setup changes, a list "
    "of laps), use a short '- ' bulleted list — one per line — instead of cramming them into "
    "a run-on sentence. Put the key number or corner name in **bold** so it's scannable.\n"
    "- Stay conversational, not a form: no 'Root cause:'/'Fix:' labels, and no headings unless "
    "the answer is genuinely long. Weave the fix into the point — what the data shows, why, and "
    "the specific change (a setup move with direction and rough amount, or a driving cue). Keep "
    "setup vs driving advice distinct.\n"
    "- Lead with the one thing that matters most. Weigh context before over-reading a lap (tire "
    "temp/warm-up, fuel, wear). Encouraging and constructive; note what's working; never berate.\n"
    "- SETUP STEPS: get_setup ends each adjustable line with (step N of lo-hi) — N is that "
    "param's CURRENT position among the garage's available positions. It is an index, "
    "NOT a physical value: never restate it as a spring rate, pressure or angle. Use it "
    "only to tell whether a param is already at its limit and which way it can still "
    "move. One step's physical size is not exposed, so NEVER state an exact increment "
    "(e.g. '-50 lbf/in') — give direction ('soften the front spring a step or two').\n"
    "- COCKPIT ELECTRONICS: a setup line ending [garage value — ...] is the GARAGE "
    "setting. LMU discards ANY electronics change the driver made on track once the car "
    "returns to the garage — ABS, the TC family, the engine and motor maps, mixture, "
    "regen, brake migration, brake balance, anti-roll — so it never reports what they "
    "actually dialed in. Never "
    "assert one as what they're currently running — name it as the garage value and ask "
    "them to confirm. Mechanical params (springs, ride height, camber, wings, dampers) "
    "carry no such marker and ARE the truth; state those plainly.\n"
    "- Match length to the question: a quick question gets a sentence or two; a broad one ('how's "
    "my race going') can be a few short paragraphs or a bullet list — thorough but never padded. "
    "The driver will ask follow-ups."
)


class Coach:
    """Assembles prompts from analysis + corner facts + knowledge notes and calls the Model."""

    def __init__(self, model: Model):
        self.model = model

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
    ):
        """Answer via the on-demand tool loop: the model fetches exactly the data the
        question needs through `context`'s tools instead of receiving the whole lap.

        Returns the model's `RunResult` (`.text` + `.usage`) so callers can account
        for what the turn cost; the usage is summed over every tool-loop call."""
        from simcoach.coach_tools import TOOLS
        _, _, spd_u, temp_u = unit_fns(units)
        system = self._augment_system(
            AGENTIC_SYSTEM_PROMPT.format(car=car, speed_unit=spd_u, temp_unit=temp_u),
            context.allowed_adjustments, driver_profile, input_device,
        )
        messages = list(history) if history else []
        messages.append({"role": "user", "content": question})
        return self.model.run(system, messages, TOOLS, context.dispatch)

    def ask_agentic_stream(
        self,
        context,
        question: str,
        car: str,
        history: list[dict] | None = None,
        driver_profile: DriverProfile | None = None,
        input_device: str | None = None,
        units: str = "metric",
    ):
        """Like `ask_agentic`, but yields tool/text events as they occur instead of
        returning a final string. Does not persist or emit done/error — the caller does."""
        from simcoach.coach_tools import TOOLS
        _, _, spd_u, temp_u = unit_fns(units)
        system = self._augment_system(
            AGENTIC_SYSTEM_PROMPT.format(car=car, speed_unit=spd_u, temp_unit=temp_u),
            context.allowed_adjustments, driver_profile, input_device,
        )
        messages = list(history) if history else []
        messages.append({"role": "user", "content": question})
        yield from self.model.run_stream(system, messages, TOOLS, context.dispatch)

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
