import numpy as np

from simcoach.driver import DriverProfile
from simcoach.format import unit_fns
from simcoach.model.base import Model
from simcoach.race import WEAR_FLOOR_PCT

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

# Packed garage key -> live channel base name (simcoach/lmu/smm.py). Capture stores
# the LIVE cockpit dial state per sample; rig-verified 2026-09-09/2026-09-15 that the
# live index tracks on-track changes the garage REST value discards (issue #33).
_LIVE_ELECTRONICS = {
    "VM_ANTILOCKBRAKESYSTEMMAP": "abs_map",
    "VM_TRACTIONCONTROLMAP": "tc_map",
    "VM_TRACTIONCONTROLSLIPANGLEMAP": "tc_slip_map",
    "VM_TRACTIONCONTROLPOWERCUTMAP": "tc_cut_map",
    "VM_ELECTRIC_MOTOR_MAP": "motor_map",
    "VM_BRAKE_MIGRATION": "migration",
    "VM_FRONT_ANTISWAY": "front_antisway",
    "VM_REAR_ANTISWAY": "rear_antisway",
    "VM_BRAKE_BALANCE": "rear_brake_bias",
}


def live_electronics(channels: dict) -> dict[str, str]:
    """Last finite live dial read per packed garage key, for the ones that resolved.

    An index channel also needs its `_max` last finite value > 0 -- Max == 0 means
    the block never populated this lap, not "no ABS" (issue comment 2026-09-10).
    `rear_brake_bias` is the REAR fraction 0-1; rendered front:rear like the garage
    string (0.495 -> "50.5:49.5")."""
    out: dict[str, str] = {}
    for key, name in _LIVE_ELECTRONICS.items():
        ch = channels.get(name)
        if ch is None:
            continue
        finite = np.asarray(ch)[np.isfinite(ch)]
        if finite.size == 0:
            continue
        v = finite[-1]
        if name == "rear_brake_bias":
            out[key] = f"{(1 - v) * 100:.1f}:{v * 100:.1f}"
            continue
        max_ch = channels.get(f"{name}_max")
        if max_ch is None:
            continue
        max_finite = np.asarray(max_ch)[np.isfinite(max_ch)]
        if max_finite.size == 0 or max_finite[-1] <= 0:
            continue
        out[key] = f"{int(v)} of 0-{int(max_finite[-1])}"
    return out


def _setup_lines(setup: dict, live: dict | None = None) -> list[str]:
    """Human-meaningful setup params, formatted for the prompt (filters ~170 -> the
    commonly-coached subset; flags tier-locked ones and garage-sourced electronics).

    `live` (from `live_electronics`) overrides a key's line with the on-track read
    as fact -- `- {label}: {live value} [live on-track; garage {stringValue}]`, no
    step suffix (the live index already carries its own range) and no garage caveat.

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
    live = live or {}
    out = []
    for key, v in setup.items():
        if not isinstance(v, dict) or "stringValue" not in v:
            continue
        k = (key.removeprefix("VM_")).lower()
        if not any(t in k for t in _SETUP_TERMS):
            continue
        sv = str(v["stringValue"]).strip()
        if sv in ("N/A", "Non-adjustable", "None", ""):  # no-info params for this car
            continue
        label = key[3:].replace("_", " ").title() if key.startswith("VM_") else key
        locked = "" if v.get("isFreeSetting", True) else " [locked this tier]"
        if key in live:
            out.append(f"- {label}: {live[key]}{locked} [live on-track; garage {sv}]")
            continue
        lo, hi, cur = v.get("minValue"), v.get("maxValue"), v.get("value")
        step = "" if None in (lo, hi, cur) else f" (step {cur} of {lo}-{hi})"
        garage = _GARAGE_CAVEAT if any(t in k for t in _ELECTRONICS_TERMS) else ""
        out.append(f"- {label}: {v['stringValue']}{step}{locked}{garage}")
    return out


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
    "'am I improving', 'how's my pace' — is NOT about one lap: use "
    "list_recent_laps (and get_session_status) to reason across laps, and do NOT zoom into "
    "the selected/last lap unless the driver asked about a specific lap. A question about "
    "CONSISTENCY specifically — 'am I consistent', 'which corner do I keep getting wrong', "
    "'is my pace stable' — call get_consistency instead: it reads the stint's lap-time and "
    "per-corner spread directly rather than a raw lap list. A question about TIRE "
    "DEGRADATION — 'are my tires going off', 'is the pace dropping because of deg', 'which "
    "tire is wearing' — call get_tire_trend: it reads per-wheel temp/pressure/wear slope "
    "and pace drift across the stint, and whether the pace loss is tire-limited. A question "
    "about BALANCE or "
    "HANDLING — 'is the car understeering', 'why does the rear step out', 'am I locking up' "
    "— or any setup advice touching balance, call get_handling first: it reads the "
    "understeer/oversteer signature plus lockup/wheelspin per corner. Unsure what's "
    "available → get_session_status first. Setup → get_setup; tire temps/warm-up → "
    "get_tire_report; brakes/pressures/wear → get_wheel_report; fuel or energy saving → "
    "get_energy_report. Don't dump a full lap breakdown for a narrow question. If a tool "
    "says no lap/data is captured yet, coach from general knowledge and say you'll have "
    "specifics once there's a lap. Never ask permission to call a tool: if a tool would "
    "answer the question, or the follow-up you're about to offer ('want me to check the "
    "incident log?'), call it now and answer.\n\n"
    "When saying how far into a race or qualy the driver is, use the CURRENT-PHASE lap "
    "count (get_session_status reports 'this <phase>: N'), never the session total, which "
    "includes other phases.\n\n"
    "INCIDENTS: when the driver mentions a spin, an off, a lockup, a flat spot or 'a few "
    "laps ago', call get_stint_events BEFORE answering — it scans the last N laps, not "
    "just the latest one. A wear step on ONE lap at ONE corner is a flat spot (from a "
    "lockup or a spin there), not cornering load.\n\n"
    "LAP IDENTITY: every lap tool starts with `Lap: …`. When you report a per-lap fact, say "
    "which lap it came from ('on race lap 3 …'). If a later answer reads a NEWER lap than an "
    "earlier one, say so ('that was lap 3; lap 4 now shows …') instead of contradicting "
    "yourself. A lap marked 'pit stop this lap' is not a pace lap: its time and corner "
    "deltas are the stop, say so and don't read them as driving. When the session holds "
    "more than one race, numbers about a lap come from a tool call in THIS turn; never "
    "reuse a spin, a lap time or a position from a turn marked as an earlier race.\n\n"
    "REFERENCES: when a NEW SINCE YOUR LAST ANSWER line is present, 'that', 'that lap', "
    "'how was that', and 'the last one' refer to that lap — call get_lap_summary on it, "
    "do not ask which. Ask which lap only when no new lap has landed AND the conversation "
    "names more than one candidate.\n\n"
    "RACECRAFT: for a question about the race or how it went, call get_race_result. When it "
    "returns a result, LEAD with the race story — positions gained or lost, the incidents "
    "and penalties, the recovery arc — and treat lap-time and corner analysis as supporting "
    "detail; be encouraging through a comeback. When there's no race result (practice/qualy), "
    "coach pace and technique as normal. During a race, a lap call may note a position "
    "change when it matters (a spin's cost, a recovery); never state traffic, and state a "
    "gap only as get_race_state reports it.\n\n"
    "RACE TOOLS (a live race; the answer is heard seconds after the question): race "
    "position, gaps or how long is left → get_race_state; fuel, energy, the pit window or "
    "tire life to the end → get_strategy. ONE call answers the question. Every result "
    "starts 'As of lap N' — say that stamp ('as of lap 12, ...'). Give only what the tool "
    "gives — lap numbers, per-lap targets, multi-lap trends — never an instantaneous gap or "
    "an immediate tactical call. The tire wear floor is a chosen threshold: say it as one "
    f"('to {WEAR_FLOOR_PCT:.0f}% tread'), never as a measured limit.\n\n"
    "Corner identity (number, name, direction) in tool results is authoritative ground "
    "truth from the saved corner map — never guess or change it; refer to each corner by "
    "BOTH number and name, e.g. 'Turn 1 (Copse)', so the driver can place it — when the "
    "map's name is only its number, say the number once and add the descriptor instead "
    "('Turn 16, the heavy stop'), never 'Turn 16 (Turn 16)'. NEVER recall "
    "corner names, numbers, or directions from your own memory of the track — even for a "
    "pre-lap track walkthrough with zero laps, call list_track_corners and use ONLY the "
    "corners it returns; if there's no map, say so rather than inventing corners. When a corner is flagged "
    "as a double-apex (it reports per-apex minimum speeds), coach it as one: sacrifice entry "
    "for the first apex, rotate the car, and get the line right for the second apex and the "
    "exit that follows — reference both minima.\n\n"
    "For a track's character, cues, or hazards — or how any corner drives — call "
    "get_track_notes and quote the authored notes; NEVER invent track character from "
    "memory. If no notes exist, say so and coach from telemetry. The authored track "
    "knowledge is 'the track notes' — never call it a wiki or a guide. "
    "When reference lap times are given, you MAY compare the driver's time to the "
    "benchmark for their class as an absolute yardstick — but these are community times, "
    "not the driver's data; frame them as a reference, not a verdict.\n\n"
    "For the car's handling character, a setup direction, or a driver-aid (ABS/TC/"
    "brake-bias) question, call get_car_notes and ground your advice in the authored "
    "car notes and the shared LMU setup concepts it returns — prefer these over general "
    "motorsport memory, and NEVER invent the car's character. Respect the provenance "
    "tags: [LMU] is game-specific, [general-sim] is cross-sim baseline, and "
    "[needs-refinement] items are tentative — hedge on those.\n\n"
    "CAR FACTS: the car's class and whether it has ABS or TC come from "
    "get_session_status (measured). Never assert a class, category, or the "
    "presence/absence of an aid from memory; if the status says unknown, say you "
    "don't know. Damage figures (contact, dent severity) come from get_stint_events, "
    "never invented -- the dent scale is a raw 0\N{EN DASH}255 severity per panel with "
    "no known physical meaning, and which panel is unknown, so NEVER guess or name "
    "one.\n\n"
    "ELECTRONICS DIRECTION: which way a TC/ABS/motor map means MORE intervention is "
    "known only when get_setup says so from the car notes; otherwise say it's "
    "unknown and suggest one step in either direction as an experiment.\n\n"
    "HISTORY: for anything spanning past sessions — 'what's my all-time/personal best here', "
    "'am I improving over time', 'how did my last few sessions go', 'what setup did we run "
    "here last time' — call get_personal_best, get_progress, list_past_sessions, or "
    "get_last_setup rather than reasoning from this session's laps alone; they default to "
    "the current track/car if not given. Comparing this car or track with another the driver "
    "has run → the same tools with car= / track= set to the other one (get_personal_best, "
    "get_progress, get_lap_summary); say what you compared and when it was set. Never claim "
    "you have no data for the other car without having asked.\n\n"
    "DRIVER MEMORY: when you notice a durable, cross-lap tendency worth remembering (not "
    "one-off, per-lap noise), call record_driver_learning and tell the driver it's saved for "
    "their review — it is NOT applied automatically. Call get_driver_memories when the "
    "driver's history beyond the summary above would sharpen your advice. For an incident, "
    "record the note only once the driver has confirmed the lap and corner; never save a "
    "fact neither a tool nor the driver stated. To correct a note you saved in this "
    "conversation, pass replaces=<its id> instead of saving a correction.\n\n"
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
    "- COCKPIT ELECTRONICS: a setup line tagged [live on-track; garage X] is what the "
    "driver is ACTUALLY running — read from the last sample of the newest stored lap, "
    "partial laps included. State "
    "it plainly, never hedge it; if the driver says they changed it since that lap, "
    "believe them. A line ending [garage value — ...] instead is the GARAGE setting "
    "with no live read (mixture, regen, engine brake map, or any electronic before the "
    "first captured lap) — LMU discards an on-track change to it once the car returns "
    "to the garage, so it never reports what they actually dialed in. Never assert one "
    "of those as what they're currently running — name it as the garage value and ask "
    "them to confirm. Mechanical params (springs, ride height, camber, wings, dampers) "
    "carry no such marker and ARE the truth; state those plainly.\n"
    "- Match length to the question: a quick question gets a sentence or two; a broad one ('how's "
    "my race going') can be a few short paragraphs or a bullet list — thorough but never padded. "
    "The driver will ask follow-ups."
)


# #119: one answer per turn, printed and spoken identically -- no separate
# [spoken] cue. The model controls LENGTH from the voice mode instead.
VOICE_PTT = (
    "VOICE (push-to-talk): the driver asked by push-to-talk from the headset, mid-session, "
    "hands on the wheel, and will HEAR this answer in full -- there is no longer text on "
    "screen. Answer in 2-3 plain spoken sentences: one cue, one action. No markdown, no "
    "lists, no headings, no numbers they can't hold in their head. Apart from a corner "
    "locator's own numbers, at most one figure in the whole answer. Locate a corner the way "
    "the corner tools do under 'Locate it:' -- 'Turn N' plus the descriptor ('Turn 10, "
    "sector 2, the fast left after the long straight'); add the corner's name only if the "
    "driver has used it themselves in this chat."
)

VOICE_CONVERSATION = (
    "VOICE (conversation): the driver asked out loud at the desk and will HEAR the whole "
    "answer -- it is spoken, not read. Answer in plain spoken prose, a few short paragraphs "
    "at most; no markdown, no bullet lists, no tables, no headings (they are read aloud as "
    "punctuation soup). Say numbers as words in sentences ('about three tenths'), not as a "
    "table. Length follows the question: a quick one gets a couple of sentences, a broad one "
    "('how's my race going') gets a few short paragraphs. Locate a corner the way the corner "
    "tools do under 'Locate it:' -- 'Turn N' plus the descriptor ('Turn 10, sector 2, the "
    "fast left after the long straight'); add the corner's name only if the driver has used "
    "it themselves in this chat."
)


class Coach:
    """Assembles prompts from analysis + corner facts + knowledge notes and calls the Model."""

    def __init__(self, model: Model):
        self.model = model

    @staticmethod
    def _augment_system(system, allowed_adjustments, driver_profile, input_device):
        """Shared system-prompt additions (tier constraint, driver profile, input
        device) used by both agentic paths (`ask_agentic`, `ask_agentic_stream`).
        Per-turn facts (#131's NEW SINCE line) go in the USER turn instead: the
        system block is prompt-cached, and a value that changes every turn there
        would bust the cache on every question."""
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
        voice: str | None = None,
    ):
        """Like `ask_agentic`, but yields tool/text events as they occur instead of
        returning a final string. Does not persist or emit done/error — the caller does."""
        from simcoach.coach_tools import TOOLS
        _, _, spd_u, temp_u = unit_fns(units)
        system = self._augment_system(
            AGENTIC_SYSTEM_PROMPT.format(car=car, speed_unit=spd_u, temp_unit=temp_u),
            context.allowed_adjustments, driver_profile, input_device,
        )
        if voice == "ptt":
            system += " " + VOICE_PTT
        elif voice == "conversation":
            system += " " + VOICE_CONVERSATION
        messages = list(history) if history else []
        messages.append({"role": "user", "content": question})
        # #133: a text block that starts right after a tool round (rather than
        # continuing the same block) needs a paragraph break, or two openings glue
        # together ("...pair.Good news first..."). Model-agnostic: every adapter's
        # run_stream yields the same {"type": ...} event shape, so this one seam
        # covers the fake model in tests and the real one alike.
        text_so_far = ""
        saw_tool_since_text = False
        for ev in self.model.run_stream(system, messages, TOOLS, context.dispatch):
            if ev["type"] == "text":
                if saw_tool_since_text and text_so_far and not text_so_far[-1].isspace():
                    yield {"type": "text", "text": "\n\n"}
                    text_so_far += "\n\n"
                saw_tool_since_text = False
                text_so_far += ev["text"]
            elif ev["type"] != "usage":
                saw_tool_since_text = True
            yield ev
