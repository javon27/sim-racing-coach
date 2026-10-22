import logging
import re

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
    "technique. Use that knowledge to explain and act on what the tools measure, not in "
    "place of it.\n\n"
    "THIS IS ONE CONTINUING RADIO CONVERSATION across a session, not a series of "
    "standalone reports. The messages above are what you and the driver already said — "
    "read them and continue from there. Connect to where you left off; never re-introduce "
    "yourself, never re-brief the whole lap unless asked, and if a previous read was "
    "wrong, say so and correct it.\n\n"
    "DATA IS ON DEMAND — you have tools that read the driver's live telemetry, laps, "
    "tires, and setup. Do NOT expect data in the message; FETCH exactly what the question "
    "needs and no more. A question about one corner → get_corner, not the whole lap. A "
    "'how was that lap' or 'why was that lap slower' → get_lap_summary FIRST (it leads "
    "with the ranked time-loss corners vs the reference), then get_corner on the worst "
    "one to explain why; reach for get_time_loss instead only for an explicit "
    "corner-by-corner ranking or a comparison against a SPECIFIC reference lap. Any "
    "question about the time LEFT -- 'where's the time', 'where am I losing time', 'find "
    "me N tenths', 'what should I work on', 'how do I go faster' -- or about the BEST "
    "lap → get_time_on_table FIRST (which='latest' when he means the lap just driven): "
    "it measures against his other laps and his best sectors. Never claim there's no "
    "reference without calling get_time_on_table; if it says there's no comparable "
    "lap, say that. A "
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
    "just the latest one. For the RACE as a whole ('analyze that race', 'how did the "
    "race go') pass phase='race': it reads that race's own laps from race lap 1, never "
    "qualifying or the grid/formation lap; an earlier race of the session: add race=N. "
    "Unscoped it reads only the current phase; for another one (the qualy just before "
    "the race) pass phase='qualifying'/'practice'. "
    "A wear step on ONE lap at ONE corner is a flat spot (from a "
    "lockup or a spin there), not cornering load.\n\n"
    "THIS LAP: every other lap read is a COMPLETED lap. When the driver describes something "
    "on the lap he is driving now ('I just spun at Turn 6', 'I hit the wall', 'check my "
    "car'), call get_car_status and/or get_corner with which='this lap' "
    "(get_stint_events also ends with this lap so far). The lap in progress is read "
    "corner by corner as each is completed and is provisional: if the corner isn't "
    "captured yet, say so and that it lands once he's through it. NEVER answer from an "
    "older lap as if it were this one, and never tell the driver something didn't happen "
    "because an earlier lap doesn't show it.\n\n"
    "THE DRIVER'S WORD: what the driver tells you about his own driving, his car, his "
    "intentions and the session or race he just ran -- which lap it was, where he went "
    "off, that it was a race, where he started or finished -- is TAKEN AS GIVEN. Build on "
    "it; never argue it or correct it ('that was actually lap 5', 'rather than just "
    "agree'). If a tool reads differently, the tool may be behind or labelled differently: "
    "answer from what it does show that fits what he said, and mention a mismatch only "
    "when it changes the advice. Verification applies to what YOU introduce, not to "
    "him.\n\n"
    "LAP IDENTITY: every lap tool starts with `Lap: …`. Name the lap a per-lap fact came "
    "from when it helps him place it ('on race lap 3 …'); never correct the driver's lap "
    "numbering unless he asks which lap it was. If a later answer reads a NEWER lap than "
    "an earlier one of yours, say so ('qualifying lap 4 now shows …') instead of contradicting "
    "yourself: when a newer lap supersedes a figure YOU gave, present it as an update "
    "('since then, qualifying lap 2 beat it: 1:15.683'), never as a correction ('careful', "
    "'actually') -- he didn't say it; naming the phase of a lap he called 'lap 3' is not "
    "correcting him. When the session has more than one phase, always name "
    "the phase with the lap number ('qualifying lap 3', never a bare 'lap 3'). A lap marked 'pit stop this lap' is not a pace lap: its time and corner "
    "deltas are the stop, say so and don't read them as driving. When the session holds "
    "more than one race, numbers about a lap come from a tool call in THIS turn; never "
    "reuse a spin, a lap time or a position from a turn marked as an earlier race.\n\n"
    "REFERENCES: when a NEW SINCE YOUR LAST ANSWER line is present, 'that', 'that lap', "
    "'how was that', and 'the last one' refer to that lap — call get_lap_summary on it, "
    "do not ask which. EXCEPT when that lap is (or he says it is) a best, a personal best or "
    "a breakthrough, or he asks what is left: call get_time_on_table, because "
    "get_lap_summary on a best lap compares it with a SLOWER lap and says nothing about "
    "where time is left. Ask which lap only when no new lap has landed AND the conversation "
    "names more than one candidate.\n\n"
    "RACECRAFT: for a question about the race or how it went, call get_race_result. When it "
    "returns a result, LEAD with the race story — positions gained or lost, the incidents "
    "and penalties, the recovery arc — and treat lap-time and corner analysis as supporting "
    "detail; be encouraging through a comeback. When there's no race result (practice/qualy), "
    "coach pace and technique as normal. During a race, a lap call may note a position "
    "change when it matters (a spin's cost, a recovery); never state traffic, and state a "
    "gap only as get_race_state reports it. If the driver says it was a race, it was: "
    "with no result logged, work from what he tells you and the laps, and don't ask him "
    "to confirm it. A race get_race_result calls FINISHED or over -- or one the driver "
    "says is over -- is answered as a finished race: never 'now that you've pitted', no "
    "strategy or gap offers.\n\n"
    "RACE TOOLS (a live race; the answer is heard seconds after the question): race "
    "position, gaps or how long is left → get_race_state; a rival's best/last lap → "
    "get_race_state (live) / get_race_result (after the race); fuel, energy, the pit "
    "window or tire life to the end → get_strategy; where a rival is quicker or slower than the "
    "driver, or where to pass him → get_battle ('the car in front'/'ahead' → "
    "direction='ahead', 'behind' → direction='behind', 'P3'/'number three' → position=3; "
    "compare the car the tool names, never another). After the race get_battle still answers "
    "'where could I have passed him' from the race's stored laps: never say that data is "
    "gone. ONE call answers the question. Every live result "
    "carries 'As of lap N' — say that stamp for the lap's figures ('as of lap 12, ...'). "
    "What the tool labels 'now (live)' was read as he asked: say it first, as now ('you're "
    "P2 in class'), never under the lap's stamp. Give only what the tool "
    "gives — lap numbers, per-lap targets, multi-lap trends — never an instantaneous gap the tool "
    "doesn't label 'now (live)', or an immediate tactical call. The tire wear floor is a chosen threshold: say it as one "
    f"('to {WEAR_FLOOR_PCT:.0f}% tread'), never as a measured limit. A result headed "
    "PROVISIONAL is an estimate from the laps it names: say so, and name them; for a timed "
    "race give both lap counts and both start amounts it states. When it carries a "
    "fuel-ratio caveat ('measured at X ... set your fuel ratio', 'another fuel ratio', "
    "'not recorded'), repeat it -- never drop it. The number of "
    "stops is the tool's 'Stops to the flag' line, word for word — never work out, or "
    "imply, a different stop count yourself. Every position "
    "you state says 'in class' or 'overall', exactly as the tool labels it.\n\n"
    "PIT STOPS IN LMU: there is no crew radio — the driver sets the pit menu (virtual "
    "energy %, fuel ratio, tyres, repairs) before arriving, and the stop does what it says. "
    "State a stop only as the pit-menu settings get_strategy gives; never say 'tell the "
    "crew', never tell him to radio anyone, never 'top it off'. The fuel ratio is a "
    "direction only — never invent a ratio number.\n\n"
    "Corner identity (number, name, direction) in tool results is authoritative ground "
    "truth from the saved corner map — never guess or change it; refer to each corner by "
    "BOTH number and name, e.g. 'Turn 1 (Copse)', so the driver can place it — when the "
    "map's name is only its number, say the number once and add the descriptor instead "
    "('Turn 16, the heavy stop'), never 'Turn 16 (Turn 16)'. NEVER recall "
    "corner names, numbers, or directions from your own memory of the track — even for a "
    "pre-lap track walkthrough with zero laps, call list_track_corners and use ONLY the "
    "corners it returns; if there's no map, give general track character instead of a "
    "corner list, never invented corners. When a corner is flagged "
    "as a double-apex (it reports per-apex minimum speeds), coach it as one: sacrifice entry "
    "for the first apex, rotate the car, and get the line right for the second apex and the "
    "exit that follows — reference both minima.\n\n"
    "OPTIMAL LAP: the optimal from his best sectors is only what tidying his OWN sectors "
    "gives. State the gap as measured (from his own sectors), never as a limit on how fast he can go.\n\n"
    "LEAD WITH THE DIAGNOSIS: answer with the corner, the phase the time is lost in, and "
    "what the driver's own faster pass did differently, then ONE change taken from that "
    "difference ('brake about 15 m later and hold one pressure to the first apex, like "
    "qualifying lap 3'). One actionable thing per corner, at most two corners unless asked "
    "for more. Coasting, trail %, ABS %, TC %, peak g and steering are descriptions: call "
    "one a loss only where a time delta in the tool says the time went there. Give a "
    "general technique tip (landmarks, 'trust it', smoothness, tire temperature) only when "
    "the driver asks for technique, or the tool has no comparison, and then say it is "
    "general advice. When the tool lists a corner as 'at your best', say he is at his own "
    "limit there. When nothing clears the floor, say so and name the next step the tool "
    "gives (the benchmark gap, a setup change, more laps). Never invent a fix.\n\n"
    "MEASURED ONLY: every figure, position, gap, lap number or other driver's name YOU "
    "introduce comes from a tool you called THIS turn -- never invent one. The driver's own "
    "figures (where he finished, a lap time he quotes) are his: repeat and build on them. "
    "Never restate a figure of yours from earlier in the conversation as current without "
    "re-reading it via a tool — the earlier answer is stale by now. Never say you are "
    "pulling, checking or looking something up unless you call a tool in the same turn. "
    "Never name a driver no tool result or the driver named.\n\n"
    "For a track's character, cues, or hazards — or how any corner drives — call "
    "get_track_notes and quote the authored notes over your own memory. If no notes "
    "exist, you MAY give general, well-known knowledge of the track -- its layout "
    "character (not its named corners) and the usual tips for a first visit -- framed as "
    "general ('from general knowledge of Long Beach, not your data'), never as measured, "
    "and still with no turn number, corner name or direction from memory; then coach "
    "from telemetry once there are laps. Never answer a first-visit 'any tips?' with "
    "only 'nothing loaded'. The authored track "
    "knowledge is 'the track notes' — never call it a wiki or a guide. "
    "When reference lap times are given, you MAY compare the driver's time to the "
    "benchmark for their class as an absolute yardstick — but these are community times, "
    "not the driver's data; frame them as a reference, not a verdict. Reference times "
    "live in the track notes: for a reference/benchmark/target-time question call "
    "get_track_notes, and never say none is loaded without having called it.\n\n"
    "For the car's handling character, a setup direction, or a driver-aid (ABS/TC/"
    "brake-bias) question, call get_car_notes and ground your advice in the authored "
    "car notes and the shared LMU setup concepts it returns — prefer these over general "
    "motorsport memory, and NEVER invent the car's character. Respect the provenance "
    "tags: [LMU] is game-specific, [general-sim] is cross-sim baseline, and "
    "[needs-refinement] items are tentative — hedge on those.\n\n"
    "CAR FACTS: the car's class and whether it has ABS or TC come from "
    "get_session_status (measured). Never assert a class, category, or the "
    "presence/absence of an aid from memory; if the status says unknown, say you "
    "don't know. Damage figures (contact, dent severity) come from get_stint_events or "
    "get_car_status, "
    "never invented -- the dent scale is a raw 0\N{EN DASH}255 severity per panel with "
    "no known physical meaning, and which panel is unknown, so NEVER guess or name "
    "one. 'Who was that contact with?' is get_stint_events too: it names the nearest "
    "same-class car at the contact -- say 'nearest car', never who hit whom.\n\n"
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
    "you have no data for the other car without having asked. 'Any GT3' or a brand "
    "('McLaren', 'Corvette') is a complete answer: pass it as car_class= / car= to "
    "get_personal_best, never ask the driver to name the car again.\n\n"
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
    "hands on the wheel, and will HEAR this answer in full -- there is no text on screen. "
    "This overrides the on-screen length and formatting rules above. By DEFAULT answer "
    "with ONE corner in 2-3 plain spoken sentences: the corner locator, the phase, the one "
    "change ('Turn 14, the slow right onto the run to Turn 15: two tenths on the exit. Get "
    "back to throttle about five meters sooner, like you did on race lap 4.'); lead with "
    "the answer, never announce a lookup, and at most one figure apart from a corner "
    "locator's own numbers. That is a default, not a cap. When the "
    "driver asks for more -- 'in detail', 'analyze', 'walk me through', 'explain', "
    "'everything', or a question that plainly needs a list -- give the FULL answer, as "
    "long as it needs to be: he can cut you off. Either way it is heard, so plain spoken "
    "prose in short paragraphs: no markdown, no bullet lists, no headings. Locate a corner "
    "the way the corner tools do under 'Locate it:' -- 'Turn N' plus the descriptor "
    "('Turn 10, sector 2, the fast left after the long straight'); add the corner's name "
    "only if the driver has used it themselves in this chat."
)

# #162 enforced the PTT length in code (a hard 3-sentence cut). #168 dropped the cut:
# "analyze the race, like in detail" (turn #247) was cut mid-list. The length is the
# prompt's default now; Coach._final_answer still speaks only the text after the last
# tool call.
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


# --- #159: no data answer without data ------------------------------------------
# A draft that states race/lap/telemetry facts or announces a lookup, yet called no
# tool, is not accepted. Deliberately cheap, deterministic regexes over the answer
# text: a false positive only costs one forced tool round, so they lean inclusive.
# #168: said ONLY as a whole answer (both rounds failed, or every sentence was cut) --
# never spliced into an answer: an unverifiable sentence is dropped instead.
UNCONFIRMED = "I didn't get a clean read on that. Ask me again."
_log = logging.getLogger(__name__)

# A lookup announced NOW ("let me pull…", "I'm checking…"). A future offer ("call me
# back and I'll dig in", "say the word and I'll pull it") is not a claim: not matched.
_LOOKUP = re.compile(
    r"\b(?:let me|lemme)\s+(?:quickly\s+|just\s+)?"
    r"(?:pull|check|look|grab|get|fetch|read|see|dig)\b"
    r"|\bI'?m\s+(?:pulling|checking|looking|grabbing|fetching|digging)\b",  # not
    # "reading": "I'm reading you fine" is radio talk (replay #119)
    re.IGNORECASE)
# #162: a claim that data is MISSING is a data claim too ("a community benchmark isn't
# something I have loaded -- that's outside my data", turn #215, no tool called; the
# track notes carried reference times). ponytail: keyed on a negation near a
# loaded/on-file/reference/benchmark/"my data" word; other absence wordings slip through.
# Advice is not a claim: a bare "don't" is an imperative ("Don't chase the benchmarks",
# "You don't need the reference lap") -- only "I/we don't" counts -- and "loaded" needs
# a have/is verb ("the car loaded on the brakes" is weight, not data).
_ABSENCE = re.compile(
    r"(?:\b(?:no|not|isn't|aren't|haven't|hasn't|without|outside)\b"
    r"|\b(?:I|we)\s+(?:don't|do not)\b)"
    r"[^.!?\n]{0,50}?\b(?:(?:have|has|got|is|are|been|not)\s+loaded|on file|"
    r"reference (?:times?|laps?)|benchmarks?|(?:my|the|any) data)\b", re.IGNORECASE)
_UNIT = (r"(?:s/lap|s|secs?|seconds?|tenths?|hundredths?|laps?|°\s?[CF]|deg(?:rees)?|"
         r"psi|kpa|bar|%|percent|km/h|kph|mph|m|meters?|metres?|l|litres?|liters?|kg|"
         r"kw|mj|rpm|mm)")
# Spoken figures (VOICE_CONVERSATION says numbers as words).
_NUMWORD = (r"(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
            r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|"
            r"thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|half)")
_ORDINAL = (r"(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|"
            r"eleventh|twelfth|\w+teenth|twentieth|thirtieth)")
_FIGURE = re.compile(
    rf"\d+(?:[.,]\d+)?\s*{_UNIT}(?![A-Za-z])"          # 8.4 s, 141 °F, 3.36 s/lap
    r"|\b\d+:\d{2}(?:\.\d+)?\b"                        # 1:18.402
    r"|\bP\d+\b|\blap \d+\b"                           # P4, lap 37
    rf"|\b{_NUMWORD}[\s-]+(?:\w+\s+)?(?:tenths?|seconds?|laps?|degrees|psi|percent)\b"
    rf"|(?<!the )\b{_ORDINAL}\s+(?:place|in class|overall|position)\b"  # fifth place,
    # not the idiom "in the first place" (replay #195)
    rf"|\b{_NUMWORD}(?:[\s-]+{_NUMWORD})*\s+point\s+{_NUMWORD}\b"  # one twenty-three point four
    rf"|\ba minute(?:\s+and)?\s+{_NUMWORD}\b"                      # a minute twenty-three
    # #232: amounts with no number word -- "a tenth and a half", "a bit over a tenth",
    # "a couple of hundredths", "a few seconds". Not "a tenth here and there" (generic)
    # nor a bare "a second" ("wait a second", "a second apex") or one followed by a
    # timing word ("wait about a second before you pick up the throttle").
    r"|\ba\s+(?:tenth|hundredth)\b(?!\s+here and there)"
    r"|\b(?:few|couple(?:\s+of)?)\s+(?:tenths|hundredths|seconds\b(?![\s,]+(?:before|after|"
    r"then|longer|earlier)))\b"
    r"|\b(?:about|around|roughly|nearly|almost|just\s+over|just\s+under|over|under|"
    r"a\s+hair\s+over|a\s+touch\s+over)\s+a\s+second\b(?![\s-]+(?:apex|gear|look|later))"
    r"(?![\s,]+(?:before|after|then|longer|earlier))"
    r"|\ba\s+second\s+(?:clear|back|behind|ahead|up\s+the\s+road|off|and\s+a\s+bit|"
    r"and\s+a\s+half|or\s+two)\b"
    r"|\b(?:tenth|second)\s+and\s+change\b",
    re.IGNORECASE)
# #232: a measured claim with no figure at all -- a rank of the driver's own lap/corner,
# a race position, where a corner's time is, or what the driver does at a numbered
# corner (rig: "your quickest of the race" was false). ponytail: anchored phrasings;
# "your best shot", "your best lap will come" pass, and so do rephrasings ("the time is
# mostly at Turn 4"); generic lore with a rank phrase ("your slowest lap of the day
# usually comes from overdriving") still trips it. Follow-up: #241.
_CLAIM = re.compile(
    r"\b(?:that's|that\s+was|it's|it\s+was|is|was)\s+your\s+(?:own\s+)?"
    r"(?:quickest|fastest|best|slowest)\s+lap\b(?!-)"
    r"|\b(?:quickest|fastest|best|slowest)\s+(?:lap\s+)?(?:of\s+the\s+(?:race|session|stint|"
    r"day|night)\b|yet\b|so\s+far\b)"
    r"|\b(?:quickest|fastest)\s+you've\s+been\b"
    r"|(?<!the\s)\b(?:new\s+)?(?:personal\s+best|PB)\b"   # not "pull the personal best"
    r"|\b(?:is|was|'s|now)\s+at\s+your\s+(?:own\s+)?best\b"
    rf"|\b(?:up\s+to|down\s+to|running|you're|you\s+are)\s+{_ORDINAL}\b(?!-)"
    r"(?![\s-]+(?:gear|apex|corner|guess|half|time|attempt|try|lap|stint|sector|"
    r"on\s+the\s+list))"
    r"|\b(?:gained|lost|dropped|made\s+up|up|down)\s+(?:a\s+(?:place|position|spot)\b|"
    rf"(?:{_NUMWORD}|couple(?:\s+of)?|few)\s+(?:places|positions|spots)\b)"
    r"|\b(?:turn|t)\s?\d+\s+is\s+where\s+the\s+time\s+is\b"
    r"|\bthe\s+time\s+(?:that's|that\s+is)\s+left\b"
    r"|\ball\s+(?:in|on)\s+the\s+(?:entry|exit|apex|braking|mid-corner)\b"
    r"|\blike\s+you\s+(?:do|did)\b[^.!?,]{0,15}?\b(?:into|at|through|in|out\s+of)\s+"
    r"(?:turn|t)\s?\d+", re.IGNORECASE)
# A bare "say again": answered by replaying the last answer, with no model call.
_REPEAT = re.compile(
    r"(?:(?:(?:can|could) you\s+)?(?:please\s+)?(?:repeat(?: that| it)?|say (?:that |it )?"
    r"again|come again|what|sorry|pardon(?: me)?|hello|huh)(?:\s+please)?[\s?.!,]*)+",
    re.IGNORECASE)
# A capitalised First Last pair in a sentence with a race-relative cue reads as
# another driver. ponytail: a heuristic -- a single name ("Kevin is behind"), an
# all-caps or lowercase name slips through, and a corner/track name beside a cue
# that no tool result spelled out is cut too. Upgrade: match the field's entry list.
_WORD = r"[A-Z][a-z]+(?:[A-Z][a-z]+)?(?:['-][A-Z]?[a-z]+)?"
_NAME = re.compile(rf"\b({_WORD})\s+({_WORD})\b")
_NOT_A_FIRST_NAME = {"The", "This", "That", "Your", "Turn", "Lap", "Sector", "As", "In",
                     "At", "On", "From", "After", "Before"}   # "At Turn 7 ... gap"
_RIVAL_CUE = re.compile(
    r"\b(?:behind|ahead|gap|closing|catch\w*|pass\w*|rival|in front|leader|leads?|"
    r"pit\w*|chasing|P\d+)\b", re.IGNORECASE)
# Where a streamed chunk may be released: after terminal punctuation followed by
# whitespace (not "8." of "8.4", not a line-leading list marker "1."), or a newline.
_BOUNDARY = re.compile(r"(?<!^\d)(?<!\n\d)(?<!^\d\d)(?<!\n\d\d)[.!?]+(?=\s)|\n")
# One sentence: terminal punctuation counts only when whitespace/end follows ("8.4").
_SENTENCE = re.compile(r"[^\s.!?](?:[^.!?\n]|[.!?](?=\S))*[.!?]*")
# A sentence with the space around it and a numbered-list marker, which goes with it.
_LEAD_SENTENCE = re.compile(rf"(\s*)((?:\d{{1,2}}[.)]\s+)?{_SENTENCE.pattern})(\s*)")
# Not an answer on its own: an acknowledgement, a lookup, a lead-in ending in ':'.
_ACK = re.compile(r"(?:ok(?:ay)?|alright|all right|right|sure|yeah|yep|copy(?: that)?|"
                  r"roger(?: that)?|got it|understood|fair enough|good question|on it|"
                  r"i hear you)[\s,.!]*", re.IGNORECASE)


def _glue(a: str, b: str, tidy: bool) -> str:
    """`a` + `b`. Untouched text joins as is (a paragraph break split across deltas,
    "x.\n" + "\ny", stays one -- red-team round 2). Where a cut was (`tidy`), the gap
    is rebuilt: its line breaks add up to a paragraph break at most; with none, the
    spaces collapse to one, so cut-around whitespace never doubles up."""
    if not tidy:
        return a + b
    a_body, b_body = a.rstrip(), b.lstrip()
    gap = a[len(a_body):] + b[:len(b) - len(b_body)]
    n = gap.count("\n")
    return a_body + ("\n" * min(n, 2) if n else " " if gap else "") + b_body


# A WHOLE sentence that only announces a lookup ("Let me pull the gaps."), not one that
# goes on to answer ("Let me see, you lost 0.3 s at Turn 5."). ponytail: no comma, no
# digit, <= 40 chars after the phrase; a longer bare announcement counts as an answer.
_LOOKUP_ONLY = re.compile(rf"(?:{_LOOKUP.pattern})[^,;:\d.!?]{{0,40}}[.!?]*", re.IGNORECASE)


def _is_filler(chunk: str) -> bool:
    return all(_LOOKUP_ONLY.fullmatch(s) or s.rstrip().endswith(":") or _ACK.fullmatch(s)
               for s in _SENTENCE.findall(chunk))


def _ungrounded_names(sentence: str, known: str) -> list[str]:
    if not _RIVAL_CUE.search(sentence):
        return []
    return [f"{a} {b}" for a, b in _NAME.findall(sentence)
            if a not in _NOT_A_FIRST_NAME and f"{a} {b}".lower() not in known]


def ground_names(text: str, known: str) -> tuple[str, list[str]]:
    """Drop every sentence naming another driver that `known` (this turn's tool
    results + the driver's own words + the system context) never mentions. #168: the
    sentence goes with its list marker and the space after it (the break before it
    stays, for the text that follows), and nothing is spliced in its place. Returns (text, the ungrounded names). Limits: see
    _NAME."""
    known = known.lower()
    names: list[str] = []

    def cut(m):
        bad = _ungrounded_names(m.group(2), known)
        names.extend(bad)
        return m.group(1) if bad else m.group(0)

    # A break a cut LAST sentence leaves is trimmed by the stream (it holds trailing
    # whitespace until more text follows), not here: here it may precede the next chunk.
    return _LEAD_SENTENCE.sub(cut, text), names


def states_data(text: str, known: str) -> str:
    """Why an answer needs a tool call in its turn, or "" when it doesn't: it
    announces a lookup, or states a race/lap/telemetry fact -- a figure with a unit, a
    lap time, a position, a lap number (one `known` already holds, e.g. the driver's
    own words or the injected CURRENT RACE line, doesn't count), a figure-free claim
    (`_CLAIM`, #232), or another driver's name absent from `known`."""
    if m := _LOOKUP.search(text):
        return f"lookup {m.group(0)!r}"
    if m := _ABSENCE.search(text):
        return f"absence {m.group(0)!r}"
    if m := _CLAIM.search(text):
        return f"claim {m.group(0)!r}"
    for m in _FIGURE.finditer(text):
        if not re.search(rf"(?<!\w){re.escape(m.group(0))}(?!\w)", known, re.IGNORECASE):
            return f"figure {m.group(0)!r}"
    names = ground_names(text, known)[1]
    return f"name {names[0]!r}" if names else ""


def is_repeat_request(question: str) -> bool:
    """A bare "repeat that" / "what?" / "say again" / "hello?" -- nothing else asked."""
    q = question.replace("\N{MICROPHONE}", "").strip()
    return bool(q) and _REPEAT.fullmatch(q) is not None


class Coach:
    """Assembles prompts from analysis + corner facts + knowledge notes and calls the Model."""

    def __init__(self, model: Model, classifier_for=None):
        self.model = model
        # #175: coach.classifier_model setting -> the router's classifier Model (or
        # None); read per question so a Settings change applies without a restart.
        # None here (tests, scripts) = keyword rules only, never a paid call.
        self.classifier_for = classifier_for

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
        prior_results: str = "",
        thinking: str = "standard",
        intent: str | None = None,
    ):
        """Like `ask_agentic`, but yields tool/text events as they occur instead of
        returning a final string. Does not persist or emit done/error — the caller does.

        `prior_results`: the stored tool RESULTS of this chat's earlier turns. A figure
        found there verbatim is a measured value the answer may refer back to without
        a new tool call; a drifted one (138 °F then 141 °F) matches nothing.

        #159: a turn that called no tool and whose draft states data (`states_data`)
        is discarded unseen and re-asked once with `force_tool`; if that still reads
        nothing (or the model can't be forced), the answer is UNCONFIRMED. So a
        tool-free answer does NOT stream: it arrives whole once the model finishes --
        the cost is the held-draft time (small talk and technique answers are short).
        After a tool ran, text streams sentence by sentence, each checked first: a
        sentence naming a driver no tool result named is dropped (#168: not replaced)
        before it is emitted, so it is never shown or spoken; if that leaves nothing,
        the answer is UNCONFIRMED. Every discarded draft and every cut sentence is
        logged (INFO) with its reason.

        `thinking` (#175) is the tier the router chose, passed to the model; a deep
        answer first yields {"type":"ack","text"} -- a code-chosen line (by `intent`),
        before the model is called, so it is neither model narration nor data."""
        from simcoach.coach_tools import TOOLS
        from simcoach.thinking import ack_line
        if thinking == "deep":
            yield {"type": "ack", "text": ack_line(intent)}
        _, _, spd_u, temp_u = unit_fns(units)
        static = AGENTIC_SYSTEM_PROMPT.format(car=car, speed_unit=spd_u, temp_unit=temp_u)
        system = self._augment_system(
            static, context.allowed_adjustments, driver_profile, input_device,
        )
        if voice == "ptt":
            system += " " + VOICE_PTT
        elif voice == "conversation":
            system += " " + VOICE_CONVERSATION
        messages = list(history) if history else []
        messages.append({"role": "user", "content": question})
        # #159: what an answer may state without a tool this turn -- every question the
        # driver asked in this chat (this turn's also carries the injected CURRENT
        # RACE / NEW SINCE facts), earlier turns' tool results, the track/car and the
        # per-driver system additions. NOT the static prompt (its examples say "lap
        # 12") and NOT earlier coach answers: that would launder a past fabrication.
        said = [m["content"] for m in messages
                if m.get("role") == "user" and isinstance(m.get("content"), str)]
        known = "\n".join([system[len(static):], context.track or "", context.car or "",
                           prior_results, *said])
        start = len(context.tool_calls)
        for force in (False, True):
            if force and not getattr(self.model, "can_force_tool", True):
                break      # this model 400s on a forced tool: answer UNCONFIRMED instead
            # Text is HELD until a tool call proves this turn read data; a tool-free
            # draft is released only if it states no data (small talk, technique).
            held: list[dict] = []
            pend: str | None = None     # set once a tool ran: text not yet emitted
            text = ""
            cut_off = False     # #175: the model ran out of max_tokens mid-answer
            # After a tool: `out` is what was emitted; `filler` is held narration
            # (a lookup, an acknowledgement, a "here's the field:" lead-in) released
            # only in front of a real sentence -- if a cut took every real sentence,
            # it is not an answer on its own (#168 red-team round 1).
            # `ws`: trailing whitespace held until more text follows, so a cut last
            # sentence never leaves a dangling paragraph break.
            # `real`: a sentence that is not filler went out.
            # `tidy`: the held whitespace came out of a cut (see _glue).
            state = {"out": "", "filler": "", "ws": "", "cut": False, "real": False,
                     "tidy": False}

            def release(raw, hold=True, state=state):   # this round's state
                chunk, was_cut = self._grounded(
                    raw, context, start, known, state["out"] + state["filler"])
                state["cut"] |= was_cut
                tidy = state["tidy"] = state["tidy"] or was_cut
                if not chunk.strip():   # (a cut keeps the break in front of it)
                    key = "filler" if state["filler"] else "ws"
                    state[key] = _glue(state[key], chunk, tidy)
                    return ""
                filler = _is_filler(chunk)
                if filler and hold:
                    state["filler"] = _glue(_glue(state["filler"], state["ws"], tidy),
                                            chunk, tidy)
                    state["ws"] = ""
                    return ""
                chunk = _glue(_glue(state["filler"], state["ws"], tidy), chunk, tidy)
                state["tidy"] = was_cut   # the trailing space held below came out of it
                if not state["out"]:
                    chunk = chunk.lstrip()
                body = chunk.rstrip()
                state["filler"], state["ws"] = "", chunk[len(body):]
                state["out"] += body
                state["real"] |= not filler
                return body

            # A forced round runs quick for its WHOLE turn (#175 red team): the adapter
            # must disable thinking for the forced round, and switching it back on for
            # the later rounds of the same tool-use turn is unverified against the API
            # -- so thinking never flips mid-turn. The forced re-ask is a fresh call,
            # not a continuation of the discarded draft's turn.
            stream = self._separated(self.model.run_stream(
                system, messages, TOOLS, context.dispatch, force_tool=force,
                thinking="quick" if force else thinking))
            if voice == "ptt":
                stream = self._final_answer(stream)
            for ev in stream:
                if ev["type"] == "stop":
                    cut_off = True      # PTT: arrives before _final_answer's text
                    continue
                if ev["type"] == "text":
                    text += ev["text"]
                    if pend is None:
                        held.append(ev)
                        continue
                    pend += ev["text"]
                    # After a tool: emit whole sentences only, each name-checked first,
                    # so a fabricated sentence is never streamed (or spoken).
                    cut = max((m.end() for m in _BOUNDARY.finditer(pend)), default=0)
                    if cut:
                        if chunk := release(pend[:cut]):
                            yield {"type": "text", "text": chunk}
                        pend = pend[cut:]
                    continue
                if ev["type"] == "tool" and pend is None:
                    # Narration before the tool goes out now (not held): the tool
                    # round can take seconds. (PTT never has any: _final_answer.)
                    if chunk := release("".join(e["text"] for e in held), hold=False):
                        yield {"type": "text", "text": chunk}
                    pend = ""
                yield ev
            if pend is not None:
                if cut_off:
                    # Never release the cut-off tail ("Your best lap was"): only the
                    # sentences already complete stand.
                    _log.warning("coach #175: answer cut at max_tokens; dropped %r",
                                 pend[:300])
                    pend = ""
                if chunk := release(pend):
                    yield {"type": "text", "text": chunk}
                if state["filler"].strip() and not state["cut"] and not cut_off:
                    # a short answer, uncut

                    filler = state["filler"].rstrip()   # already carries its lead space
                    state["out"] += filler
                    yield {"type": "text", "text": filler}
                if not state["out"].strip():   # nothing survived: say so, once
                    yield {"type": "text", "text": UNCONFIRMED}
                elif state["cut"] and not state["real"]:   # only narration survived
                    yield {"type": "text", "text": "\n\n" + UNCONFIRMED}
                return
            if cut_off:
                end = max((m.end() for m in _BOUNDARY.finditer(text)), default=0)
                _log.warning("coach #175: answer cut at max_tokens; dropped %r",
                             text[end:][:300])
                text = text[:end].rstrip()
                if not text:
                    break           # nothing complete: UNCONFIRMED, not an empty answer
                held = [{"type": "text", "text": text}]
            reason = states_data(text, known)
            if not reason:
                yield from held
                return
            # Ungrounded draft: discarded unseen; re-ask once with a tool forced.
            _log.info("coach #159: discarded a tool-free draft (%s, forced=%s): %r",
                      reason, force, text[:300])
        yield {"type": "text", "text": UNCONFIRMED}

    @staticmethod
    def _grounded(chunk: str, context, start: int, known: str, out: str) -> tuple[str, bool]:
        """(`chunk` with every sentence naming a driver that neither `known` nor this
        turn's tool results named dropped (and logged), whether one was). `out` is what
        was emitted before it: an answer never starts with the space a cut left."""
        if not chunk:
            return "", False
        results = "\n".join(str(c.get("result", "")) for c in context.tool_calls[start:])
        fixed, names = ground_names(chunk, known + "\n" + results)
        if names:
            _log.info("coach #159: cut a sentence naming %s (not in this turn's tool "
                      "results): %r", ", ".join(names), chunk[:300])
        if not out:
            fixed = fixed.lstrip()
        elif out.endswith("\n"):
            fixed = fixed.lstrip(" \t")
        return fixed, bool(names)

    @staticmethod
    def _final_answer(stream):
        """#162/#168: a PTT answer is only the text after the model's LAST tool call,
        emitted once the model is done -- uncut (#168: the length is the prompt's call;
        the driver can interrupt). Narration before a tool ("Copy that. Let me check.")
        is dropped: it isn't the answer. Tool and usage events pass as they come. PTT
        speaks only on `done`, so holding the text costs nothing heard; the screen just
        gets it in one piece."""
        text = last = ""
        for ev in stream:
            if ev["type"] == "text":
                text += ev["text"]
                continue
            if ev["type"] == "tool":
                # A stream that ENDS on a tool would leave nothing to say (silence and
                # an empty stored row): keep the last non-empty segment as a fallback.
                last, text = (text if text.strip() else last), ""
            yield ev
        if text := text.strip() or last.strip():
            yield {"type": "text", "text": text}

    @staticmethod
    def _separated(stream):
        """#133: a text block that starts right after a tool round (rather than
        continuing the same block) needs a paragraph break, or two openings glue
        together ("...pair.Good news first..."). Model-agnostic: every adapter's
        run_stream yields the same {"type": ...} event shape, so this one seam
        covers the fake model in tests and the real one alike."""
        text_so_far = ""
        saw_tool_since_text = False
        for ev in stream:
            if ev["type"] == "text":
                if saw_tool_since_text and text_so_far and not text_so_far[-1].isspace():
                    yield {"type": "text", "text": "\n\n"}
                    text_so_far += "\n\n"
                saw_tool_since_text = False
                text_so_far += ev["text"]
            elif ev["type"] not in ("usage", "thinking", "stop"):

                saw_tool_since_text = True
            yield ev
