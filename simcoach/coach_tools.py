"""On-demand data tools for the agentic coach. The model calls these to fetch
exactly what a question needs instead of receiving the whole lap every turn.

Each tool returns a deterministic text summary built from the analysis/store
layer — the two-stage invariant holds: raw ~50 Hz telemetry never reaches the
model, only computed per-corner/lap facts. Corner identity comes from the map,
never guessed. `CoachContext` is rebuilt per question so tools always read the
current session (track/car/lap), which the live-session re-resolution keeps fresh.
"""
from __future__ import annotations

import re
import time
from collections import Counter
from types import SimpleNamespace

from simcoach.analysis import (
    _ENVELOPE_MIN_LAPS,
    CornerMetrics,
    analyze_lap,
    corner_tag,
    damage_events,
    damage_state,
    geometry_clause,
    grip_envelope,
    lap_events,
    stint_consistency,
    stint_tire_trend,
    stint_wear_baseline,
    time_loss_buckets,
)
from simcoach.battle import format_battle
from simcoach.coach import _setup_lines, live_electronics
from simcoach.corners import CornerMap
from simcoach.format import (
    corner_descriptor,
    find_corner,
    format_consistency,
    format_corner_line,
    format_damage_line,
    format_events,
    format_lap_facts,
    format_laptime,
    format_time_loss,
    format_tire_report,
    format_tire_trend,
    format_wheel_report,
    ordinal,
)
from simcoach.fuel import laps_remaining
from simcoach.livelap import impact_text, incident_lines
from simcoach.race import format_race_state, format_strategy, lap_facts, picked_up_mid_lap
from simcoach.track_lookup import load_track_knowledge, note_key_binds

# ponytail: hard cap on an incident scan. Each lap past the cache costs a telemetry
# load plus a full analysis, so a model asking for "the whole stint" must not walk a
# 60-lap race. Raise it if a rig endurance run genuinely needs a longer look-back.
_MAX_EVENT_LAPS = 30

# How an unfinished lap (LapRecord.partial, #77) is named to the model: on the `Lap:`
# identity line, and in the `list_recent_laps` listing where a lap time would go.
_PARTIAL_WORD = {"out": "out-lap", "in": "in-lap", "partial": "partial lap"}
_PARTIAL_LIST_WORD = {"out": "out lap", "in": "in lap", "partial": "partial"}

# Tool specs advertised to the model (Anthropic tool-use format).
TOOLS = [
    {
        "name": "get_session_status",
        "description": "Current session orientation: track, car, whether a lap is "
                       "captured yet, how many laps this track/car, and whether a "
                       "reference (best) lap exists. Call this first if unsure what's available.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_recent_laps",
        "description": "List recent laps for the current track/car (newest first) with "
                       "the lap's in-session number (per phase, e.g. 'lap 3' — the "
                       "same numbering the driver sees), time, and validity. Refer to laps "
                       "by that number; never invent or cite a database id.",
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer",
                                     "description": "Max laps to list (default 10)."}},
        },
    },
    {
        "name": "get_lap_summary",
        "description": "The full per-corner breakdown (speeds, apex-relative brake/throttle, "
                       "trail, peak g, deltas vs best) plus the tire report for one lap. "
                       "Use for a whole-lap analysis; prefer get_corner for a single corner.",
        "input_schema": {
            "type": "object",
            "properties": {"which": {"type": "string",
                                     "description": "'latest' (default), 'best', or an in-session "
                                                    "lap number like 'lap 3'."}},
        },
    },
    {
        "name": "get_time_loss",
        "description": "WHERE on track a lap loses time vs a reference lap, as a ranked "
                       "list of corner-by-corner and straight-by-straight deltas that sum "
                       "to the net lap delta. Call this FIRST for 'where am I losing time', "
                       "'why am I slower', or 'what should I work on' — then get_corner on "
                       "the worst bucket to explain why. Needs a second valid lap on this "
                       "track/car to compare against.",
        "input_schema": {
            "type": "object",
            "properties": {
                "which": {"type": "string",
                          "description": "Which lap to analyse: 'latest' (default) or an "
                                         "in-session lap number like 'race lap 3'."},
                "reference": {"type": "string",
                              "description": "What to compare against: 'best' (default — "
                                             "this session's best, else all-time best) or "
                                             "an in-session lap number."},
            },
        },
    },
    {
        "name": "get_stint_events",
        "description": "Incidents across the last N laps of this session — spins, "
                       "off-tracks, lockups, wheelspin, and per-wheel wear steps (flat "
                       "spots), each with lap and corner. Call this when the driver "
                       "mentions a spin, an off, a moment, a flat spot, or asks what "
                       "happened a few laps ago; never assume the latest lap is the one "
                       "they mean. It ends with THIS LAP SO FAR: the incidents on the lap "
                       "being driven, corner by corner as each is completed (provisional).",
        "input_schema": {
            "type": "object",
            "properties": {"laps": {"type": "integer",
                                    "description": "How many recent laps to scan "
                                                   "(default 10, max 30)."}},
        },
    },
    {
        "name": "get_consistency",
        "description": "HOW CONSISTENT the driver is across this stint — lap-time spread "
                       "and the corners whose min speed / brake point / coasting vary most "
                       "lap to lap. Use for 'am I consistent', 'which corner do I keep "
                       "getting wrong', 'is my pace stable'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "phase": {"type": "string",
                          "description": "Optional: which phase's laps to measure "
                                         "('practice'/'qualifying'/'race'). Defaults to the "
                                         "current lap's phase."},
            },
        },
    },
    {
        "name": "get_tire_trend",
        "description": "HOW THE TIRES ARE TRENDING across this stint — per-wheel temp / "
                       "pressure / wear slope per lap and pace drift, and whether the pace "
                       "loss is tire-limited. Use for 'are my tires going off', 'is the pace "
                       "dropping because of deg', 'which tire is wearing', 'how many laps "
                       "do I have'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "phase": {"type": "string",
                          "description": "Optional: which phase's laps to measure "
                                         "('practice'/'qualifying'/'race'). Defaults to the "
                                         "current lap's phase."},
            },
        },
    },
    {
        "name": "get_handling",
        "description": "The car's BALANCE per corner on a lap — understeer / oversteer read "
                       "from steering vs lateral g, plus lockup and wheelspin flags. Use for "
                       "'is the car understeering', 'why does the rear step out', 'am I "
                       "locking up', and before any setup advice about balance.",
        "input_schema": {
            "type": "object",
            "properties": {"which": {"type": "string",
                                     "description": "Which lap: 'latest' (default), 'best', or "
                                                    "an in-session lap number like 'lap 2'."}},
        },
    },
    {
        "name": "list_track_corners",
        "description": "The AUTHORITATIVE corner list for the current track from its saved "
                       "corner map: each corner's number/label, name, and direction (and any "
                       "double-apex). Works even with NO laps captured yet. Use this for track "
                       "orientation or ANY corner reference before you have telemetry — never "
                       "recall or guess corner names, numbers, or directions from memory.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_corner",
        "description": "The telemetry facts for ONE corner, by number (e.g. '10') or name "
                       "(e.g. 'Village'), plus its baked elevation change / banking and "
                       "authored note when the map has them. Use this "
                       "for elevation/banking questions, not just lap facts — "
                       "never invent geometry from general knowledge, and use it instead of "
                       "pulling the whole lap. Pass `which` to target a specific lap — do "
                       "this whenever the conversation is about a particular lap (e.g. "
                       "'lap 2'); it defaults to the latest lap otherwise.",
        "input_schema": {
            "type": "object",
            "properties": {
                "corner": {"type": "string", "description": "Corner number or name."},
                "which": {"type": "string",
                          "description": "Which lap: 'latest' (default, the last COMPLETED "
                                         "lap), 'best', an in-session lap number like "
                                         "'lap 2', or 'this lap' (also 'current', 'now') "
                                         "-- the lap being driven now, readable corner by "
                                         "corner as each is completed (provisional)."},
            },
            "required": ["corner"],
        },
    },
    {
        "name": "get_tire_report",
        "description": "Per-wheel mean tire temps (front/rear, left/right imbalance) and the "
                       "early→late warm-up trend for the latest lap.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_wheel_report",
        "description": "Brake temperatures, hot tire pressures, and tire wear per wheel for the "
                       "latest lap — for brake fade, cold/overheating brakes, pressure balance, "
                       "or tire-wear/degradation questions.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_setup",
        "description": "The car's current adjustable setup values, so setup advice can name "
                       "specific changes. Includes which adjustments are legal this tier, and "
                       "each param's current position as (step N of lo-hi) -- an option INDEX, "
                       "not a physical value: use it to see whether a param is already at its "
                       "limit and which way it can move, never restate it as a rate/pressure/"
                       "angle. Step size is NOT exposed; give direction, not an exact amount.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_track_notes",
        "description": "Authored track knowledge (character, cues, hazards) for the current "
                       "track (refer to it as 'the track notes'): a short overview plus "
                       "per-corner notes, as OWNED data. Call this before describing how a "
                       "track or corner drives. Pass `corner` (number or label) to get just "
                       "that corner's note. Works with NO laps captured. Never invent track "
                       "character from memory — quote these notes.",
        "input_schema": {
            "type": "object",
            "properties": {"corner": {"type": "string",
                                      "description": "Optional corner number/label to scope to."}},
        },
    },
    {
        "name": "get_car_notes",
        "description": "Authored knowledge for the CURRENT car (handling character, setup "
                       "concepts, driver aids like ABS/TC) plus the shared LMU setup principles "
                       "that apply to any car, as OWNED data. Call this for car-handling, setup-"
                       "direction, or driver-aid questions. Works with NO laps captured. Quote "
                       "these notes and respect their provenance tags ([LMU]/[general-sim]/"
                       "[needs-refinement] are tentative — hedge); never invent car character.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_energy_report",
        "description": "Fuel and virtual-energy usage for the latest lap: per-lap burn, amount "
                       "remaining, and roughly how many laps that lasts — for fuel/energy saving "
                       "and stint-length questions.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_race_result",
        "description": "The race outcome + racecraft context for this session — finishing "
                       "position, incidents, penalties. Call this for questions about the "
                       "race/result/how it went overall.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_car_status",
        "description": "LIVE SESSION ONLY. The car right now, read live from the game: "
                       "impacts since the start of THIS lap (where, which side, how hard), "
                       "bodywork dent severity, detached parts or wheels, flat tires, engine "
                       "overheating, and whether the car is on track, in the pit lane or in "
                       "the garage. Use for 'check my car', 'am I damaged', 'did I just hit "
                       "something', 'is a wheel off'.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_race_state",
        "description": "LIVE RACE ONLY. Where the driver stands as of the last completed lap: "
                       "overall and class position, laps or time remaining (and laps left at "
                       "the recent clean pace), and the in-class car ahead and behind with the "
                       "gap TREND (s/lap) and the lap he'd be caught at that rate. Use for "
                       "position, gap, 'am I catching him', 'how long is left'. One call "
                       "answers it.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_strategy",
        "description": "LIVE RACE ONLY. Fuel and virtual energy to the flag (makes it or not, "
                       "margin in laps, per-lap save target), the pit window (earliest/latest "
                       "lap and what sets it), and tire life to a named wear threshold, as of "
                       "the last completed lap. Use for 'will I make it', 'when do I pit', "
                       "'how long will the tires last'. One call answers it.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_battle",
        "description": "LIVE RACE ONLY. Where a same-class rival was faster or slower than "
                       "the driver on the LAST COMPLETED lap: corners and sections ranked by "
                       "time gained or lost, plus apex-speed differences (the passing-chance "
                       "read). By default the rival is the car the driver was nearest for most "
                       "of that lap (it may name two). Use for 'where is he quicker', 'where "
                       "can I pass him'. One call answers it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "rival": {"type": "string",
                          "description": "Optional: a rival the driver named: 'P4' (CLASS "
                                         "position), a driver name, or a car/team name "
                                         "('the Ferrari')."},
            },
        },
    },
    {
        "name": "record_driver_learning",
        "description": "Propose a durable observation about the driver (a cross-lap tendency "
                       "worth remembering, not per-lap noise) for the driver's long-term profile. "
                       "It is saved for the driver's review, not applied immediately — tell them "
                       "so after calling this.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The observation, in plain language."},
                "track": {"type": "string",
                          "description": "Optional: scope this to the current track only."},
                "car": {"type": "string",
                        "description": "Optional: scope this to the current car only."},
                "replaces": {"type": "integer",
                             "description": "Id of a note saved earlier in THIS conversation "
                                             "that this corrects; the old text is replaced, "
                                             "no new row."},
            },
            "required": ["text"],
        },
    },
    {
        "name": "get_driver_memories",
        "description": "The driver's reviewed, active long-term memories — tendencies and notes "
                       "beyond what's already in the profile summary. Call this when tailoring "
                       "advice would benefit from the driver's history.",
        "input_schema": {
            "type": "object",
            "properties": {
                "track": {"type": "string", "description": "Optional: scope to this track."},
                "car": {"type": "string", "description": "Optional: scope to this car."},
                "topic": {"type": "string",
                          "description": "Optional: filter to memories mentioning this topic."},
            },
        },
    },
    {
        "name": "get_personal_best",
        "description": "The all-time fastest recorded lap for a track/car (defaults to the "
                       "current one) and when it was set, contrasted with this session's best "
                       "if different. Use for 'what's my personal best / all-time fastest here'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "track": {"type": "string", "description": "Optional: defaults to the current track."},
                "car": {"type": "string", "description": "Optional: defaults to the current car."},
            },
        },
    },
    {
        "name": "get_progress",
        "description": "Best-lap-per-session progression over time for a track/car (defaults "
                       "to the current one) — use for 'am I improving / how has my pace "
                       "trended' questions that span multiple sessions, not just this one.",
        "input_schema": {
            "type": "object",
            "properties": {
                "track": {"type": "string", "description": "Optional: defaults to the current track."},
                "car": {"type": "string", "description": "Optional: defaults to the current car."},
            },
        },
    },
    {
        "name": "list_past_sessions",
        "description": "Recent PAST sessions (excluding the current one) for a track/car "
                       "(defaults to the current one): date, online/offline, lap count, best "
                       "lap, and finishing result if raced. Use for 'how did my last sessions "
                       "here go'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "track": {"type": "string", "description": "Optional: defaults to the current track."},
                "car": {"type": "string", "description": "Optional: defaults to the current car."},
                "limit": {"type": "integer", "description": "Max sessions to list (default 5)."},
            },
        },
    },
    {
        "name": "get_last_setup",
        "description": "The most recently saved setup for a track/car (defaults to the current "
                       "one), from a PAST session — not the live current setup (use get_setup "
                       "for that). Use for 'what setup did we run here last time'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "track": {"type": "string", "description": "Optional: defaults to the current track."},
                "car": {"type": "string", "description": "Optional: defaults to the current car."},
            },
        },
    },
]


# Claims a stored answer makes that the code now knows were never true, as
# (pattern, cutoff, retraction): an answer written before `cutoff` (epoch seconds) that
# matches `pattern` re-enters the model's context with `retraction` appended. Each
# pattern was measured against every answer in the driver's store and against the
# authored knowledge/*.md and cars/*.json prose, and matches exactly the poisoned rows.
_STORED_ANSWER_RETRACTIONS = (
    # #72: mGripFract is never populated by LMU, so every grip reading was fabricated --
    # no cutoff. Shapes in the store: "min grip reading zero", "**Min grip 0.00**",
    # "grip drops to **0.00**".
    (re.compile(r"min[ _]grip|grip[^.\n]{0,40}\b0\.00\b", re.IGNORECASE),
     float("inf"),
     ("\n\n[Correction, added on read and not part of what was said: the answer above "
      "cites a grip reading (a \"min grip\" figure, or grip falling to zero). That "
      "channel (mGripFract) is never populated by the game -- it reads zero on every "
      "captured lap of every car -- so the figure was fabricated and any conclusion "
      "drawn from it is void. Disregard it and do not repeat it.]")),
    # #67: until the fix merged (56aa319, 2026-09-10 10:22:07 -05:00) get_race_result
    # could not reach a live session's result log and never had the grid slot. Shapes:
    # "started **P18**", "P1 starting spot", "P14 to P9", "P1→P34", "from P4 down",
    # "No incidents". After the fix the coach legitimately says "Started P9 on the grid",
    # which this pattern also matches -- the cutoff, not the pattern, spares it.
    # ponytail: a sidecar still running pre-fix code past the cutoff writes rows this
    # treats as post-fix; none exist in the store, key on the code version if one does.
    (re.compile(r"\bstart\w*\W{1,4}P\d+|\bP\d+\W{1,4}start|\bP\d+\W{0,4}(?:→|->|to)"
                r"\W{0,4}P\d+|\bfrom\W{1,4}P\d+\W{1,4}(?:up|down)\b|\bno incidents\b",
                re.IGNORECASE),
     1789053727.0,
     ("\n\n[Correction, added on read and not part of what was said: the answer above "
      "states a starting position, or that no incidents were logged. It was written "
      "before the coach could read the race's result log: its \"start\" was only the "
      "first lap capture happened to record, already underway and never the grid slot, "
      "and \"no incidents\" meant none were visible to it, not that none happened. Treat "
      "both as unverified and do not repeat the earlier figures. get_race_result "
      "names the track of the session it reports: use its figures for the race this "
      "answer was about only if {same_race}.]")),
)


def scrub_stored_answer(answer: str, ts: float, track: str | None = None) -> str:
    """Annotate a stored coach answer, written at `ts`, before it re-enters the model's
    context.

    Filter on READ, never on disk: the coach did say those things, and rewriting the
    driver's own conversation history is a worse wrong than leaving a bad number in it
    (issue #72). The stored row and the UI display stay untouched; only the copy that
    becomes the model's prior reasoning carries the retraction.

    Deliberately narrow. A blanket filter on "grip" would strip the legitimate authored
    driving prose that `simcoach/knowledge/*.md` and `simcoach/cars/*.json` are full of,
    and that prose reaches the model through other tools.

    New answers CAN still match: each retraction names the claim it retracts, so a model
    that echoes a correction writes a row that matches again. For grip that row is just
    retracted again; for the race start it falls after the cutoff and is left alone.
    """
    if not answer:
        return answer
    # `track` is the track the stored answer was about. The race retraction can only be
    # checked against get_race_result if it names one: the resumed history itself usually
    # never does, so without it "the same race" is a condition the model cannot test.
    same_race = f"the track it names is {track}" if track else "it is that same race"
    return answer + "".join(retraction.replace("{same_race}", same_race)
                            for pattern, cutoff, retraction in _STORED_ANSWER_RETRACTIONS
                            if ts < cutoff and pattern.search(answer))


def _race_of_turn(ts: float, windows: list[dict]) -> dict | None:
    """The race a chat turn written at `ts` was about: the last one already under way
    when it was written. None for a turn from before the sitting's first race lap."""
    found = None
    for win in windows:
        if win["start_ts"] <= ts:
            found = win
    return found


def race_turn_note(ts: float, windows: list[dict]) -> str:
    """Prefix marking a stored coach answer as being about an EARLIER race of this
    session, or '' (issue #136).

    Same shape as scrub_stored_answer: computed from stored timestamps on READ, never
    written to disk, no LLM. A session id spans a whole evening, so resuming the chat
    during race 3 re-fed the model its race-1 answers as its own prior reasoning with
    nothing to say they were about another race -- and it answered about race 3 with
    race 1's spin, lap times and 'cleanest laps'.

    Silent with one race: there is nothing to mistake it for."""
    if len(windows) < 2 or ts >= windows[-1]["start_ts"]:
        return ""
    win = _race_of_turn(ts, windows)
    if win is None:
        return ""
    started = time.strftime("%H:%M", time.localtime(win["start_ts"]))
    return (f"[About an earlier race in this session (race {win['index']}, started "
            f"{started}) — not the current one.] ")


def current_race_fact(windows: list[dict]) -> str:
    """The CURRENT RACE line for the user turn, or '' with at most one race (#136).

    Rides in the user turn (the #131 seam), not the system prompt: it changes every
    race, and the system block is prompt-cached."""
    if len(windows) < 2:
        return ""
    win = windows[-1]
    started = time.strftime("%H:%M", time.localtime(win["start_ts"]))
    return (f"CURRENT RACE: race {win['index']} of this session (started {started}, "
            f"{win['laps']} laps). Earlier turns marked as earlier races are history, "
            f"not this race.")


def race_result_text(store, session_id) -> str:
    """The session's race outcome as prompt text: grid slot, finish, incidents, and
    the live per-lap position arc.

    The single owner of that wording. Every surface that describes a race to the model
    calls this -- the coach tool AND the profile digest. The digest used to phrase its
    own copy ("Race arc: started P{start} ... finished P{last}") off an unscoped arc,
    which is exactly the #67 defect: `start` is the first lap capture happened to
    record, mid-race, and an unscoped arc reads it from whatever phase came first.
    Two copies meant fixing one left the other asserting the opposite as fact.
    """
    rr = store.race_session(session_id) if session_id else None
    # Race stint only — a session_id spans practice/qualy/race, so an all-phase arc
    # would read the start/worst from an earlier phase (wrong start position).
    # Bound the arc to the race the result row actually describes: phase='race'
    # alone spans every race in the sitting, so a second race's grid would be
    # reported next to the first race's lap positions. With no result row (a race
    # still being driven, whose log does not exist yet) there is no race_ts, so fall
    # back to the floor the store derives from the last logged race.
    arc = None
    if session_id:
        since = rr.get("race_ts") if rr else store.unlogged_race_since(session_id)
        if not rr and since is None:
            # #136: no result row and no logged race to floor on (never imported, or an
            # online race whose log we never see). The sitting's own race windows still
            # say where THIS race began, so the arc describes one race rather than every
            # race of the evening -- and agrees with the race get_race_result names.
            windows = store.race_windows(session_id)
            since = windows[-1]["start_ts"] if windows else None
        arc = store.session_position_arc(
            session_id, phase="race", since=since,
            until=rr.get("next_race_ts") if rr else None)
    if not rr and not arc:
        return ("No race result recorded for this session (practice/qualy, or not a "
                "completed race).")
    lines = []
    if rr:
        if rr["grid_position"] is not None:
            grid = f"Started P{rr['grid_position']} on the grid"
            if (rr["class_grid_position"] is not None
                    and rr["class_grid_position"] != rr["grid_position"]):
                grid += f" (P{rr['class_grid_position']} in class)"
            lines.append(grid + ".")
        parts = []
        if rr["position"] is not None:
            parts.append(f"Finished P{rr['position']} overall")
        if rr["class_position"] is not None:
            parts.append(f"P{rr['class_position']} in class")
        if rr["finish_status"]:
            parts.append(f"status: {rr['finish_status']}")
        lines.append("; ".join(parts) + "." if parts else "Race result recorded.")
        incidents = rr["incidents"]
        if incidents:
            head = "; ".join(incidents[:3])
            more = f" (+{len(incidents) - 3} more)" if len(incidents) > 3 else ""
            lines.append(f"Incidents ({len(incidents)}): {head}{more}")
        penalties = rr["penalties"]
        if penalties:
            texts = [str(p.get("text") or p) for p in penalties[:3]]
            lines.append(f"Penalties ({len(penalties)}): {'; '.join(texts)}")
    if arc:
        # NEVER call arc['start'] a starting position: it is the first lap capture
        # happened to record, which is already underway (issue #67 reported a P9
        # start as P18 this way). The grid slot only ever comes from the result log.
        line = (f"Live per-lap positions over {arc['laps']} captured lap(s): "
                f"P{arc['start']} on the first captured lap, as low as "
                f"P{arc['worst']}, P{arc['last']} on the most recent.")
        if not (rr and rr["grid_position"] is not None):
            line += (" The grid position was not captured, so the first captured lap "
                     "is NOT the starting position -- do not report a start.")
        lines.append(line)
    return "\n".join(lines)


def _lap_age(ts: float) -> str:
    """'13 s ago' / '5 min ago' for a lap set moments ago this session (#130); once
    it's no longer fresh (>= 2h), falls back to the date, same as the all-time-best
    wording."""
    elapsed = time.time() - ts
    if elapsed < 90:
        return f"{max(0, round(elapsed))} s ago"
    if elapsed < 7200:
        return f"{round(elapsed / 60)} min ago"
    return time.strftime("%Y-%m-%d", time.localtime(ts))


# #160: how the driver (and so the model) names the lap being driven.
_THIS_LAP = frozenset(("this lap", "current lap", "this lap so far", "the current lap",
                       "lap in progress", "in progress", "this", "now", "current"))

# #154: appended to a mapping-mode lap's identity line and lap-list row.
_MAPPING_NOTE = "mapping capture lap (boundary weave, not a timed attempt)"


class CoachContext:
    """Live data bound for one question; tools read from it. Rebuilt each turn."""

    def __init__(self, store, units="metric", record=None, lap_id=None, cmap=None,
                 setup=None, track=None, car=None, on_track=None,
                 allowed_adjustments=None, session_id=None,
                 car_notes=None, setup_concepts=None, car_class=None, race=None,
                 race_note=None, live_lap=None, car_status=None):
        self.store = store
        self.units = units
        self.record = record
        self.lap_id = lap_id
        self.cmap = cmap
        self.setup = setup
        self.track = track or (record.track if record else None)
        self.car = car or (record.car if record else None)
        # #108 item 5: the car's CLASS, from data (REST carClass or the scraped
        # result-log table) -- never asserted from memory. None -> "unknown".
        self.car_class = car_class
        # #152: the RaceSnapshot as of the last committed race lap, or None. The race
        # tools only read it -- nothing is computed while the driver waits.
        self.race = race
        # What to say instead when a live race has no snapshot for THIS race yet.
        self.race_note = race_note
        # #160: the lap being driven now (LmuSource meta: session_id, phase, lap_no,
        # start_ts) and the car's live status text, read when the question came in.
        # Both None off track.
        self.live_lap = live_lap
        self.car_status = car_status
        self.on_track = on_track
        self.allowed_adjustments = allowed_adjustments
        # Authored car knowledge (merged shipped+overlay `notes` dict) and the shared
        # LMU setup-concepts doc; loaded per turn like cmap. Empty {} when none.
        self.car_notes = car_notes or {}
        self.setup_concepts = setup_concepts or {}
        # The current on-track stint (practice/qualy/race), for scoping laps and the
        # reference to this session rather than all-time. From the live lap's stamp.
        self.session_id = session_id or (record.session_id if record else None)
        self.tool_calls: list = []   # {name, input, result} invoked this turn, for the chat log
        self._analysis_cache: dict = {}
        self._lap_numbering_cache: tuple[dict, dict] | None = None
        self._current_lap_cache: tuple | None = None
        self._newest_lap_cache: tuple | None = None
        # #136: this session's races (store.race_windows), loaded once per turn.
        self._race_windows_cache: list[dict] | None = None
        # #38 P1.4: grip envelope, computed once per context (see `_grip_envelope`).
        self._envelope: float | None = None
        self._envelope_computed = False
        # #108 item 2: how many records fed that envelope -- format.py won't
        # report a percentage off one resting on too few laps.
        self._envelope_laps: int | None = None
        # The recent stored laps those reads are built from, loaded ONCE and shared
        # with the wear baseline (#108) so events cost no extra lap loads.
        self._recent: list | None = None
        self._wear_baseline_cache: dict | None = None

    # --- helpers ---------------------------------------------------------
    def _current_lap(self) -> tuple:
        """(record, lap_id) for the driver's CURRENT lap — an injected record if the
        caller pinned one, else the most recent stored lap for this track/car/session,
        loaded lazily. The coach is not handed a lap up front (that fixated it on a UI
        selection); 'latest'/'current' and the corner tools resolve it on demand here."""
        if self.record is not None:
            return self.record, self.lap_id
        if self._current_lap_cache is not None:
            return self._current_lap_cache
        found = (None, None)
        if self.track and self.car:
            # complete_only (#77): out/in/partial laps are stored now, but "the
            # driver's current lap" is the latest lap they FINISHED -- a return to
            # the garage must not make the in-lap the default subject of every
            # answer. Partials stay reachable by explicit reference (_resolve_lap).
            rows = self.store.recent_laps(self.track, self.car, 1,
                                          session_id=self.session_id, complete_only=True)
            if rows:
                lid = rows[0]["lap_id"]
                try:
                    found = (self.store.load(lid), lid)
                except Exception:
                    found = (None, None)
        self._current_lap_cache = found
        return found

    def _newest_lap_any_kind(self) -> tuple:
        """(record, lap_id) for the newest stored lap of ANY kind -- out/in/partial
        included -- for this track/car/session. Issue #128: the cockpit electronics
        (TC/ABS/brake bias) a driver changes mid-stint are live in the shared-memory
        channels, but #77's `_current_lap()` only ever sees the latest COMPLETE lap,
        so a change made on an in-lap or a spin stayed invisible until the next timed
        lap. `get_setup`'s live dial read uses this instead; every analytical read
        (corners, grip envelope, wear) keeps `_current_lap()`."""
        if self._newest_lap_cache is not None:
            return self._newest_lap_cache
        found = (None, None)
        if self.track and self.car:
            rows = self.store.recent_laps(self.track, self.car, 1,
                                          session_id=self.session_id, complete_only=False)
            if rows:
                lid = rows[0]["lap_id"]
                try:
                    found = (self.store.load(lid), lid)
                except Exception:
                    found = (None, None)
        self._newest_lap_cache = found
        return found

    def _lap_kind_label(self, record, lap_id) -> str:
        """'practice lap 6 (in-lap)' -- the in-session label (#77 numbering) plus
        the out/in/partial tag, shared by `_lap_line` and the live-electronics note."""
        label_by_id, _ = self._lap_numbering()
        label = label_by_id.get(lap_id) if lap_id is not None else None
        label = label or ("this lap" if lap_id is None else f"lap id {lap_id}")
        kind = _PARTIAL_WORD.get(record.partial, "")
        return f"{label} ({kind})" if kind else label

    def _live_lap_note(self, record, lap_id) -> str:
        """'Live values from the last sample of practice lap 6 (in-lap), 20:51:16.'
        -- issue #128: names which stored lap the cockpit dials above came from,
        so a live read is never mistaken for the latest COMPLETE lap's telemetry."""
        when = (time.strftime("%H:%M:%S", time.localtime(record.timestamp))
                if record.timestamp else "")
        tail = f", {when}" if when else ""
        return f"Live values from the last sample of {self._lap_kind_label(record, lap_id)}{tail}."

    def _race_windows(self) -> list[dict]:
        """This session's races, oldest first (store.race_windows), cached per turn.
        Empty when there is no session or no race lap."""
        if self._race_windows_cache is None:
            self._race_windows_cache = (self.store.race_windows(self.session_id)
                                        if self.session_id else [])
        return self._race_windows_cache

    def _current_race(self) -> dict | None:
        """The race this session is in, or its most recent one -- the last window.
        None when the session has no race lap. What 'this race' means everywhere:
        an unqualified lap reference, the stint-event scan, the CURRENT RACE fact."""
        windows = self._race_windows()
        return windows[-1] if windows else None

    def _race_line(self, race_ts: float | None = None) -> str:
        """'Race 3 of this session (started 00:26, 8 laps).' for the race a tool is
        about, or '' when the session holds at most one race (nothing to disambiguate,
        and a lone 'Race 1 of this session' is noise). `race_ts` names a specific race
        by the moment its result log says it began; without one the CURRENT race."""
        windows = self._race_windows()
        if len(windows) < 2:
            return ""
        win = self._current_race()
        if race_ts is not None:
            # The window this result describes: the first one still running at race_ts.
            # race_ts is the race's own start stamp, which is exactly how the position
            # arc is floored (`since=race_ts`), so the two can never disagree.
            win = next((w for w in windows if w["end_ts"] >= race_ts), win)
        started = time.strftime("%H:%M", time.localtime(win["start_ts"]))
        return (f"Race {win['index']} of this session (started {started}, "
                f"{win['laps']} laps).")

    def _lap_numbering(self) -> tuple[dict, dict]:
        """In-session lap numbering matching the UI (LapList.svelte): laps are
        grouped into CONSECUTIVE same-phase runs in ts order. Within a run, the
        game's own lap number (lap_no -- scoring mTotalLaps at the wrap, issue #77)
        is used when EVERY lap in the run has one (so a garage return or an
        out-lap in between shows the game's real gaps, e.g. 2,3,4,8,9); otherwise
        the run falls back to counting 1..N, same as before lap_no existed (a
        legacy session, or any lap in the run still missing it). A run restarts
        if the phase value changes and later comes back (e.g. practice -> race ->
        practice), same as the UI's groups. Returns (label_by_id, id_by_ref):
        label_by_id[lap_id] -> e.g. 'race lap 3'; id_by_ref maps textual refs the
        model might use ('race 3', 'race lap 3', and -- for the current phase's
        run -- the UI's unprefixed 'lap 3' or a bare '3') back to the DB lap_id.
        Cached per turn.

        #136: when the session holds more than one RACE (store.race_windows -- three
        races in an evening share one session id), a race lap's label carries the race
        too: 'race 3 lap 2'. "race lap 2" is ambiguous across three races, and that
        ambiguity is what let the coach answer about race 3 with race 1's lap times.
        The unqualified refs ('race lap 2', 'lap 2', a bare '2') then resolve in the
        CURRENT race only -- an earlier race's laps must be asked for by name. A
        single-race session is unchanged."""
        if self._lap_numbering_cache is not None:
            return self._lap_numbering_cache
        label_by_id: dict[int, str] = {}
        id_by_ref: dict[str, int] = {}
        if self.session_id:
            cur_rec, _ = self._current_lap()
            cur_phase = cur_rec.phase if cur_rec is not None else None
            windows = self._race_windows()
            multi = len(windows) > 1
            cur_win = windows[-1]["index"] if windows else None
            win_of_lap = {lid: w["index"] for w in windows for lid in w["lap_ids"]}
            # A race window boundary starts a new run as well as a phase change: the
            # second race's laps count from 1 (or from the game's own lap_no), never
            # on from the first race's total.
            win_starts = {w["lap_ids"][0] for w in windows}
            runs: list[list[dict]] = []
            prev_phase: object = object()  # sentinel -- never equals a real phase value
            for lap in self.store.session_laps(self.session_id):
                ph = lap.get("phase")
                if ph == prev_phase and runs and lap["id"] not in win_starts:
                    runs[-1].append(lap)
                else:
                    runs.append([lap])
                prev_phase = ph
            for run in runs:
                ph = run[0].get("phase")
                win = win_of_lap.get(run[0]["id"]) if ph == "race" else None
                # The phase word the label and the refs are built from: 'race 3' once
                # the session holds more than one race, plain 'race' otherwise.
                name = f"race {win}" if (multi and win) else ph
                # Unqualified refs belong to the current race only (see docstring).
                unqualified = not multi or win is None or win == cur_win
                use_lap_no = all(lap.get("lap_no") is not None for lap in run)
                for i, lap in enumerate(run):
                    n = lap["lap_no"] if use_lap_no else i + 1
                    label_by_id[lap["id"]] = f"{name} lap {n}" if ph else f"lap {n}"
                    if ph:
                        if multi and win:
                            id_by_ref[f"race {win} lap {n}".lower()] = lap["id"]
                            id_by_ref[f"lap {n} of race {win}".lower()] = lap["id"]
                        if unqualified:
                            id_by_ref[f"{ph} {n}".lower()] = lap["id"]
                            id_by_ref[f"{ph} lap {n}".lower()] = lap["id"]
                        if ph == cur_phase and unqualified:
                            # Bare number AND the UI's own unprefixed wording ("lap 35"),
                            # which is what the driver reads off the lap list and the model
                            # then echoes -- it must resolve, not look like a third scheme.
                            id_by_ref[str(n)] = lap["id"]
                            id_by_ref[f"lap {n}"] = lap["id"]
                    else:
                        id_by_ref[str(n)] = lap["id"]
                        id_by_ref[f"lap {n}"] = lap["id"]
        self._lap_numbering_cache = (label_by_id, id_by_ref)
        return self._lap_numbering_cache

    def _lap_line(self, record, lap_id) -> str:
        """'Lap: race lap 3 (game lap 7, 21:14:05)' -- the in-session label the driver sees
        in the lap list (#77 numbering), the game's own lap number when stored, and the
        local wall-clock time the lap was captured, so two answers a minute apart can be
        told apart (#108 item 1)."""
        label_by_id, _ = self._lap_numbering()
        label = label_by_id.get(lap_id) if lap_id is not None else None
        parts = []
        if record.lap_no is not None:
            parts.append(f"game lap {record.lap_no}")
        if record.timestamp:
            parts.append(time.strftime("%H:%M:%S", time.localtime(record.timestamp)))
        head = label or ("this lap" if lap_id is None else f"lap id {lap_id}")
        # An out/in/partial lap says so right here (#77), so the model can never read
        # its missing lap time or NaN corners as a timed lap that went wrong.
        kind = _PARTIAL_WORD.get(record.partial, "")
        # #134: a race lap's stored position, plus the previous race lap's position
        # (so a spin's cost or a recovery is visible right in the identity line) --
        # looked up from session_laps rather than `record` (store.load() doesn't
        # thread position onto LapRecord).
        tail_bits = [kind] if kind else []
        # #138: a pit-stop lap's time and corner deltas are the stop, not the
        # driving -- flag it right in the identity line so the model doesn't
        # read a stint-ending outlier as damage or a spin.
        if record.pit:
            tail_bits.append("pit stop this lap")
        # #154: a mapping pass keeps its real phase (a race lap reads as one) but is a
        # boundary weave -- say so, or its time and corners read as a bad lap.
        if record.mapping:
            tail_bits.append(_MAPPING_NOTE)
        if record.phase == "race" and lap_id is not None and self.session_id:
            sess_laps = self.store.session_laps(self.session_id)
            idx = next((i for i, r in enumerate(sess_laps) if r["id"] == lap_id), None)
            cur_pos = sess_laps[idx]["position"] if idx is not None else None
            if cur_pos is not None:
                prev_pos = next((r["position"] for r in reversed(sess_laps[:idx])
                                 if r.get("phase") == "race" and r.get("position") is not None),
                                None)
                pos_txt = f"position P{cur_pos}"
                if prev_pos is not None:
                    pos_txt += f" (from P{prev_pos})"
                tail_bits.append(pos_txt)
        return (f"Lap: {head}" + (f" ({', '.join(parts)})" if parts else "")
                + (f" — {'; '.join(tail_bits)}" if tail_bits else ""))

    def _lap_id_for(self, record) -> int | None:
        """Best-effort DB lap_id for a record with no id attached (e.g. `best_lap`,
        which only returns the record) -- matched by timestamp within this session.
        None when it can't be found (outside this session, or no session_id), which
        just leaves `_lap_line` to fall back to the record's own game-lap/time facts
        without an in-session label."""
        if not (self.session_id and record is not None):
            return None
        for row in self.store.session_laps(self.session_id):
            if row["ts"] == record.timestamp:
                return row["id"]
        return None

    def _recent_records(self) -> list:
        """This track/car's ~10 most recent stored laps, loaded ONCE per context.
        Shared by the grip envelope and the wear baseline so the second reader is
        free."""
        if self._recent is not None:
            return self._recent
        records = []
        if self.track and self.car:
            # Complete laps only: a half lap must not shape the grip envelope or
            # the wear baseline (#77).
            for row in self.store.recent_laps(self.track, self.car, limit=10,
                                              complete_only=True):
                try:
                    rec = self.store.load(row["lap_id"])
                except Exception:
                    rec = None   # a result-log row with no telemetry -- nothing to read
                if rec is not None:
                    records.append(rec)
        self._recent = records
        return records

    def _wear_baseline(self) -> dict:
        """{wheel: median per-lap wear} for spotting a flat spot (#108), over the
        same recent laps the grip envelope already loaded.
        ponytail: those laps are scoped to track/car, not to this stint, so a
        long-run median can drag a short stint's bar around. `get_stint_events`
        passes its own stint-scoped baseline; this is the cheap one for the
        single-lap tools. Scope it per session if a rig stint reads wrong."""
        if self._wear_baseline_cache is None:
            self._wear_baseline_cache = stint_wear_baseline(self._recent_records())
        return self._wear_baseline_cache

    def _events_for(self, record, lap_id, baseline=None) -> list[str]:
        """Formatted incident lines for one lap, or [] with no corner map."""
        if self.cmap is None or record is None:
            return []
        analysis = self._analysis_for(record, lap_id)
        events = lap_events(record, self.cmap, analysis,
                            self._wear_baseline() if baseline is None else baseline)
        return format_events(events, self.cmap, self.units)

    def _grip_envelope(self, reference):
        """The grip envelope (g), computed ONCE per context (cached on
        `self._envelope`) from this track/car's recent stored laps plus
        `reference` -- later calls reuse the same cached value regardless of
        which record/reference triggered the first one, so every tool call
        this turn reads the same envelope."""
        if self._envelope_computed:
            return self._envelope
        self._envelope_computed = True
        recent = self._recent_records()
        # #108 item 2: how many of the driver's own recent laps actually had both
        # g channels -- a envelope built from fewer than `_ENVELOPE_MIN_LAPS` of
        # those isn't a real ceiling. The reference lap (below) isn't counted here:
        # it's usually already inside `recent`, and the "N of 5 laps" message is
        # about the driver's lap history, not a duplicate sample.
        self._envelope_laps = sum(
            1 for r in recent
            if r is not None and r.channels.get("lat_g") is not None
            and r.channels.get("long_g") is not None
        )
        records = list(recent)
        if reference is not None:
            records.append(reference)
        self._envelope = grip_envelope(records) if records else None
        return self._envelope

    def _analysis_for(self, record, lap_id):
        if self.cmap is None:
            return None
        key = lap_id if lap_id is not None else id(record)
        if key not in self._analysis_cache:
            # Prefer this session's best (fair same-phase comparison — a race lap vs
            # the race best, not a qualy hot lap); fall back to all-time.
            ref = None
            if self.session_id:
                ref = self.store.best_lap(record.track, record.car, exclude_lap_id=lap_id,
                                          session_id=self.session_id)
            if ref is None:
                ref = self.store.best_lap(record.track, record.car, exclude_lap_id=lap_id)
            envelope = self._grip_envelope(ref)
            self._analysis_cache[key] = analyze_lap(record, self.cmap, reference=ref,
                                                     envelope=envelope,
                                                     envelope_laps=self._envelope_laps)
        return self._analysis_cache[key]

    def _resolve_lap(self, which):
        """which -> (record, lap_id). 'latest'/None -> current; 'best' -> best lap;
        an in-session lap reference ('race lap 3', 'race 3', 'lap 3' or a bare number
        in the current phase) -> that lap. A raw DB id is accepted ONLY where the
        listing itself had to fall back to raw ids (no in-session numbering) and only
        for this track/car -- never as a guess at a number the in-session numbering
        simply doesn't cover, which could pull an unrelated lap (issue #34)."""
        which = (which or "latest").strip().lower()
        if which in ("latest", "current", ""):
            return self._current_lap()
        if which == "best":
            if not (self.track and self.car):
                return None, None
            best = self.store.best_lap(self.track, self.car)
            # best_lap returns the record; find its id via recent list match on time
            return best, None
        # In-session reference (matches how laps are named to the driver) first.
        _, id_by_ref = self._lap_numbering()
        lid = id_by_ref.get(which)
        # With no in-session numbering (e.g. the REPL asking before this run's first
        # lap lands), `list_recent_laps` labels laps by their raw DB id -- so that is
        # the only reference the model can give back, and refusing it makes the tool
        # surface contradict itself. Accept it there, but only for a lap that really
        # belongs to this track/car, so it can never pull an unrelated lap (issue #34).
        if lid is None and not id_by_ref:
            raw = which.removeprefix("lap ")
            if raw.isdigit() and int(raw) in set(self.store.list_lap_ids(self.track, self.car)):
                lid = int(raw)
        if lid is None:
            return None, None
        try:
            return self.store.load(lid), lid
        except Exception:
            return None, None

    def _no_lap_msg(self, which) -> str:
        """Failure text for an unresolvable lap reference. Names this session's phase
        and lap range so the model can correct itself instead of handing the driver a
        dead end -- the schema's old 'race lap N' example primed exactly that wrong
        guess in a practice session (issue #34)."""
        cur_rec, _ = self._current_lap()
        phase = cur_rec.phase if cur_rec is not None else None
        # The bare-number keys are precisely the current phase's run of laps.
        _, id_by_ref = self._lap_numbering()
        n = max((int(k) for k in id_by_ref if k.isdigit()), default=0)
        if phase and n:
            return f"No lap matching {which!r}. This session is {phase}, laps 1-{n}."
        return "No lap available for that request (none captured yet, or unknown lap)."

    def _aid_status(self, key: str) -> str:
        """#108 item 5: 'available' / 'not fitted' / 'unknown' for a driver-aid
        availability flag (e.g. VM_ANTILOCK_BRAKES), read from the garage setup's
        stringValue -- never asserted from memory. 'unknown' with no setup, a
        missing key, or any stringValue that isn't a recognized availability read."""
        if not self.setup:
            return "unknown"
        entry = self.setup.get(key)
        if not isinstance(entry, dict):
            return "unknown"
        sv = str(entry.get("stringValue", "")).strip()
        if sv == "Available":
            return "available"
        if sv in ("N/A", "None"):
            return "not fitted"
        return "unknown"

    # --- tools -----------------------------------------------------------
    def get_session_status(self):
        # Complete laps only, in all three counts (#77): these say how much DATA the
        # coach has, and counting the newly-stored out/in/partial rows would inflate
        # every historical number the day they started being stored.
        n_all = (len(self.store.list_lap_ids(self.track, self.car, complete_only=True))
                 if (self.track and self.car) else 0)
        n_sess = (len(self.store.session_lap_ids(self.session_id, complete_only=True))
                  if self.session_id else None)
        has_ref = bool(self.track and self.car and self.store.best_lap(self.track, self.car))
        on = "on track" if self.on_track else ("unknown" if self.on_track is None else "not on track")
        cur_rec, _ = self._current_lap()
        lap = "yes" if cur_rec is not None else "no lap captured yet"
        phase = cur_rec.phase if cur_rec else None
        # Current-phase count: "8 laps into the race" must be race laps, not the
        # whole session (qualy + race). Grouped like _lap_numbering's counts[ph].
        # #136: for a race that count is THIS race's, not every race the sitting
        # holds -- "8 laps into the race" was reporting all three races' laps.
        cur_race = self._current_race() if phase == "race" else None
        n_phase = (cur_race["laps"] if cur_race is not None else
                   (sum(1 for r in self.store.session_laps(self.session_id)
                        if r.get("phase") == phase and not r.get("partial"))
                    if self.session_id and phase else None))
        if n_sess is not None:
            head = f"this {phase}: {n_phase}, " if n_phase is not None else ""
            laps_txt = f"{head}session: {n_sess}, all-time this track/car: {n_all}"
        else:
            laps_txt = f"this track/car: {n_all} (no session split)"
        # Latest STORED lap (any kind -- out/in/partial included): the count above is
        # complete laps only, so a driver asking about "lap 5" while only 1 lap is
        # "complete" must not read that 1 as a lap number (#126). ANY kind so a spin on
        # a partial lap is still the latest thing the coach knows about.
        sess_laps = self.store.session_laps(self.session_id) if self.session_id else []
        n_other = sum(1 for r in sess_laps if r.get("partial"))
        if sess_laps:
            latest = sess_laps[-1]
            label_by_id, _ = self._lap_numbering()
            label = label_by_id.get(latest["id"]) or f"lap {latest.get('lap_no') or '?'}"
            kind = _PARTIAL_LIST_WORD.get(latest.get("partial"))
            latest_txt = f"{label} ({kind})" if kind else label
        else:
            latest_txt = "none"
        n_corners = len(self.cmap.corners) if (self.cmap and self.cmap.corners) else 0
        cmap_txt = (f"yes ({n_corners} corners — use list_track_corners)" if n_corners
                    else "no")
        other_txt = (f" | Other laps this session (out/in/partial): {n_other}"
                     if n_other else "")
        # #134: live race-position arc (started/low/current) when this phase is a race.
        pos_txt = ""
        if phase == "race" and self.session_id:
            # #136: floored on THIS race. Unbounded, the arc spans every race of the
            # sitting, so race 1's worst place was reported as this race's.
            arc = self.store.session_position_arc(
                self.session_id, phase="race",
                since=cur_race["start_ts"] if cur_race else None)
            if arc:
                pos_txt = (f" | Position: P{arc['last']} (started P{arc['start']}, "
                           f"low P{arc['worst']})")
        # #136: how many races this one session id covers, and which one is current.
        races_txt = ""
        if len(self._race_windows()) > 1:
            cur = self._current_race()
            races_txt = (f" | Races this session: {len(self._race_windows())} "
                         f"(current: race {cur['index']}, started "
                         f"{time.strftime('%H:%M', time.localtime(cur['start_ts']))})")
        return (f"Track: {self.track or 'unknown'} | Car: {self.car or 'unknown'} | "
                f"Session type: {phase or 'unknown'} | State: {on} | Current lap: {lap} | "
                f"Latest stored lap: {latest_txt} | "
                f"Complete laps ({laps_txt}){other_txt} | Corner map: {cmap_txt} | "
                f"Reference (best) lap available: {'yes' if has_ref else 'no'} | "
                f"Class: {self.car_class or 'unknown'} | "
                f"ABS: {self._aid_status('VM_ANTILOCK_BRAKES')} | "
                f"TC: {self._aid_status('VM_TRACTION_CONTROL')}{pos_txt}{races_txt}")

    def list_recent_laps(self, limit=10):
        if not (self.track and self.car):
            return "No track/car resolved yet — no laps to list."
        # Scope to the current stint when we have one, so the driver sees this
        # session's laps rather than every lap ever run here.
        rows = self.store.recent_laps(self.track, self.car, int(limit or 10),
                                      session_id=self.session_id)
        if not rows:
            return "No laps stored for this session yet."
        # Name laps by their in-session, per-phase number (what the driver sees in
        # the UI), not the raw database id.
        label_by_id, _ = self._lap_numbering()
        out = []
        for r in rows:
            valid = "valid" if r["valid"] else "INVALID"
            label = label_by_id.get(r["lap_id"]) or (
                f"lap {r['lap_id']}" + (f" [{r['phase']}]" if r["phase"] else "")
            )
            # #134: race-phase position, appended when the column is set.
            pos = (f" P{r['position']}" if r.get("phase") == "race"
                   and r.get("position") is not None else "")
            pit = " (pit)" if r.get("pit") else ""
            if r.get("mapping"):
                pit += f" — {_MAPPING_NOTE}"
            # A partial lap keeps its NUMBER (that is why it is stored) but has no
            # time to print and is not "INVALID" -- it is unfinished (#77).
            if r.get("partial"):
                out.append(f"{label}: {_PARTIAL_LIST_WORD[r['partial']]} (no time){pos}{pit}")
            else:
                out.append(f"{label}: {format_laptime(r['lap_time'])} {valid}{pos}{pit}")
        return "\n".join(out)

    def get_lap_summary(self, which="latest"):
        record, lap_id = self._resolve_lap(which)
        if record is None:
            return self._no_lap_msg(which)
        if self.cmap is None:
            return "No corner map for this track yet — can't break the lap into corners."
        # #108: what went WRONG on this lap leads, right under the identity -- a spin
        # or a flat spot reframes every corner number below it.
        events = self._events_for(record, lap_id)
        lap_line = (self._lap_line(record, lap_id) + "\nEvents this lap: "
                    + (", ".join(events) if events else "none"))
        analysis = self._analysis_for(record, lap_id)
        # #102: lead with the time-loss ranking (and surface its worst corners
        # first below) whenever a reference resolves -- a flat table buries the
        # finding the driver actually needs 15-30 lines down.
        ref, ref_label = self._reference_lap(record, lap_id, "best")
        buckets = time_loss_buckets(record, ref, self.cmap) if ref is not None else None
        if not buckets:
            return lap_line + "\n" + format_lap_facts(analysis, self.units)
        by_label = {corner_tag(cm): cm.corner_no for cm in analysis.corners}
        losses = sorted((b for b in buckets if b["kind"] == "corner" and b["delta"] > 0),
                        key=lambda b: -b["delta"])[:3]
        top = [by_label[b["label"]] for b in losses if b["label"] in by_label]
        order = top + [cm.corner_no for cm in analysis.corners if cm.corner_no not in top]
        body = format_lap_facts(analysis, self.units, corner_order=order, top_loss=set(top))
        return lap_line + "\n" + format_time_loss(buckets, ref_label) + "\n\n" + body

    def _reference_lap(self, record, lap_id, reference="best"):
        """(record, label) for a time-loss comparison, or (None, label).

        'best' resolves the SAME way `_analysis_for` does — this session's best
        first (same-phase fairness), all-time best as the fallback — so
        get_time_loss and the per-corner 'min vs best' deltas never disagree about
        what "best" means. Never returns the subject lap itself.
        """
        reference = (reference or "best").strip().lower()
        if reference not in ("best", ""):
            ref, _ = self._resolve_lap(reference)
            return ref, f"your {reference}"
        if self.session_id:
            ref = self.store.best_lap(record.track, record.car, exclude_lap_id=lap_id,
                                      session_id=self.session_id)
            if ref is not None:
                return ref, "your session best"
        ref = self.store.best_lap(record.track, record.car, exclude_lap_id=lap_id)
        return ref, "your all-time best"

    def get_time_loss(self, which="latest", reference="best"):
        record, lap_id = self._resolve_lap(which)
        if record is None:
            return "No lap available for that request (none captured yet, or unknown lap)."
        if self.cmap is None:
            return "No corner map for this track yet — can't break the lap into corners."
        if record.partial is not None:
            # An out/in/spin lap covers a different piece of track than the reference
            # and carries no lap time; the overlap gate below would let a long in-lap
            # through and report a delta that silently includes a pit-lane detour.
            return ("That lap can't be compared to the reference (partial lap, or a "
                    "different track length).")
        ref, ref_label = self._reference_lap(record, lap_id, reference)
        if ref is None:
            return ("No reference lap to compare against yet — you need a second valid "
                    "lap on this track/car.")
        # `which='best'` resolves no lap_id, so exclude_lap_id can't keep the best lap
        # out of its own comparison. Say so rather than report a flat zero delta.
        if ref.timestamp == record.timestamp and ref.lap_time == record.lap_time:
            return ("That lap IS the reference — a lap can't be compared to itself. "
                    "Ask about a specific lap (e.g. 'race lap 3') instead.")
        buckets = time_loss_buckets(record, ref, self.cmap)
        if buckets is None:
            return ("That lap can't be compared to the reference (partial lap, or a "
                    "different track length).")
        ref_word = (reference or "best").strip().lower()
        ref_line = self._lap_line(ref, self._lap_id_for(ref)).removeprefix("Lap: ")
        identity = f"{self._lap_line(record, lap_id)} vs {ref_word}: {ref_line}"
        return identity + "\n" + format_time_loss(buckets, ref_label)

    def _stint_analyses(self, phase=None, need_corners=True):
        """(analyses, lap_times) for one stint -- session + phase, valid laps only,
        telemetry-backed laps only, in lap order. Shared by `get_consistency` and
        `get_tire_trend`, which only differ in which pure function they feed it to."""
        if phase is None:
            cur_rec, _ = self._current_lap()
            phase = cur_rec.phase if cur_rec is not None else None
        analyses, lap_times = [], []
        for row in self.store.session_laps(self.session_id):
            # Mapping laps (#154) are boundary weaves, not driving: out of the stint, as
            # they were when capture stamped them phase='mapping'.
            if (not row["valid"] or row.get("mapping")
                    or (phase is not None and row.get("phase") != phase)):
                continue
            try:
                record = self.store.load(row["id"])
            except Exception:
                record = None   # a result-log row with no telemetry file -- nothing to analyse
            if record is None:
                continue
            if self.cmap is None and not need_corners:
                # Lap-level reads (tires) don't need a corner map; a track with no map
                # still gets its stint trend. Not cached -- _analysis_for's cache is
                # keyed on the corner map this context has.
                analysis = analyze_lap(record, CornerMap([]))
            else:
                analysis = self._analysis_for(record, row["id"])
            if analysis is None:
                continue
            analyses.append(analysis)
            lap_times.append(record.lap_time)
        return analyses, lap_times

    def get_stint_events(self, laps=10):
        """Incidents over the last N laps of this session, grouped by lap.

        Deliberately NOT filtered to valid laps -- a lap with a spin or an off is
        usually the INVALID one, which is exactly the lap the driver is asking
        about."""
        if not self.session_id and not self.on_track:
            return "No active session — no stint to scan for incidents."
        if self.cmap is None:
            return "No corner map for this track yet — can't place incidents on corners."
        this_lap = self._this_lap_events()
        if not self.session_id:
            return this_lap or "No active session — no stint to scan for incidents."
        try:
            n = int(laps or 10)
        except (TypeError, ValueError):
            n = 10
        n = max(1, min(n, _MAX_EVENT_LAPS))
        rows = self.store.session_laps(self.session_id)   # ts order
        # #136: counting back N laps from the newest one walks straight out of this
        # race and into the previous one when a sitting holds several -- which is how
        # "that spin a few laps ago" came back as race 1's spin during race 3. Scan
        # this race only; an earlier race's laps stay reachable by name ('race 1 lap 2').
        cur_race = self._current_race()
        if cur_race is not None and len(self._race_windows()) > 1:
            rows = [r for r in rows if r["ts"] >= cur_race["start_ts"]]
        rows = rows[-n:]
        loaded = []
        for row in rows:
            try:
                record = self.store.load(row["id"])
            except Exception:
                record = None   # a result-log row with no telemetry file
            if record is not None:
                loaded.append((record, row["id"]))
        if not loaded:
            return this_lap or "No telemetry-backed laps in this session yet — nothing to scan."
        baseline = stint_wear_baseline([rec for rec, _ in loaded])
        out = []
        for record, lap_id in loaded:
            lines = self._events_for(record, lap_id, baseline)
            out.append(self._lap_line(record, lap_id))
            out.extend(f"  {line}" for line in (lines or ["none"]))
            # #143: contact is its own line, not folded into "none" -- a clean lap
            # (or one captured before #143, with no damage channels at all) says
            # nothing here rather than a "no damage" line on every lap.
            damage_line = format_damage_line(damage_events(record.channels),
                                             damage_state(record.channels))
            if damage_line:
                out.append(f"  {damage_line}")
        if this_lap:
            out.append(this_lap)
        return "\n".join(out)

    def get_consistency(self, phase=None):
        if not self.session_id:
            return "No active session — no stint to measure consistency across."
        if self.cmap is None:
            return "No corner map for this track yet — can't break laps into corners."
        analyses, lap_times = self._stint_analyses(phase)
        return format_consistency(stint_consistency(analyses, lap_times), self.units)

    def get_tire_trend(self, phase=None):
        if not self.session_id:
            return "No active session — no stint to measure tire trend across."
        analyses, lap_times = self._stint_analyses(phase, need_corners=False)
        return format_tire_trend(stint_tire_trend(analyses, lap_times), self.units)

    def get_handling(self, which="latest"):
        record, lap_id = self._resolve_lap(which)
        if record is None:
            return self._no_lap_msg(which)
        if self.cmap is None:
            return "No corner map for this track yet — can't break the lap into corners."
        analysis = self._analysis_for(record, lap_id)
        lap_line = self._lap_line(record, lap_id)
        lines = []
        for cm in analysis.corners:
            flags = []
            if cm.handling and cm.handling != "neutral":
                flags.append(f"{cm.handling} — {cm.handling_why}")
            if cm.lockup:
                flags.append("lockup under braking")
            if cm.wheelspin:
                flags.append("wheelspin under throttle")
            if flags:
                lines.append(f"{corner_tag(cm)}: " + ". ".join(flags) + ".")
        if not lines:
            return (lap_line + "\n" + f"No handling flags on this lap — balance read neutral "
                    f"in all {len(analysis.corners)} corners.")
        return lap_line + "\n" + "\n".join(lines)

    def _latest_analysis(self):
        """This session's current/latest lap's analysis, or None with no lap or
        no corner map -- used to seed `list_track_corners`' spoken locators with
        real measured speeds rather than geometry alone."""
        record, lap_id = self._current_lap()
        if record is None:
            return None
        return self._analysis_for(record, lap_id)

    def list_track_corners(self):
        if self.cmap is None or not self.cmap.corners:
            return "No corner map for this track yet — no corner list available."
        analysis = self._latest_analysis()
        parts = []
        for c in self.cmap.corners:
            multi = " (double-apex)" if c.apexes and len(c.apexes) > 1 else ""
            descriptor = corner_descriptor(self.cmap, c.number, analysis=analysis)
            parts.append(f"{corner_tag(c)} — {c.direction}{multi} — {descriptor}")
        return (f"Corners for {self.track or 'this track'} ({len(parts)}), from the saved map:\n"
                + "\n".join(parts))

    def _provisional(self) -> tuple[dict | None, list[dict], str]:
        """#160: (lap meta, provisional corner rows, identity line) for 'this lap'. On
        track that is ONLY the lap being driven -- never an older lap's rows; off track,
        the newest lap of this session that never completed (a crash, a retirement)."""
        if self.on_track:
            m = self.live_lap
            if m is None:
                return None, [], "No lap in progress: nothing on this lap is captured yet."
            stored, rows = self.store.live_lap_corners(m["session_id"], m["lap_no"],
                                                       m["start_ts"])
            if m.get("incidents") is None and stored is not None:
                # No question-time scan (LiveLap.now not run / failed): the worker's
                # stored lap-wide scan of this same lap is the next best, never nothing.
                m = dict(m, incidents=stored["incidents"])
            return m, rows, (f"Lap: game lap {m['lap_no']} so far "
                             f"(this lap so far, provisional -- lap in progress)")
        m, rows = (self.store.live_lap_corners(self.session_id) if self.session_id
                   else (None, []))
        if m is None:
            return None, [], ("No unfinished lap in this session: 'this lap' is read live, "
                              "only while you're on track.")
        when = time.strftime("%H:%M:%S", time.localtime(m["start_ts"]))
        return m, rows, (f"Lap: game lap {m['lap_no']} ({m['phase'] or 'session'}), started "
                         f"{when} -- never completed (provisional, the corners driven "
                         f"before it stopped)")

    def _row_incidents(self, row) -> list[str]:
        facts = row["facts"]
        return (format_events(facts.get("events", []), self.cmap, self.units)
                + [impact_text(e, self.cmap) for e in facts.get("impacts", [])])

    def _row_corner_reads(self, row) -> list[str]:
        """A corner row's own reads (lockup / wheelspin / off). Spins and hits come
        from the lap-wide scan, which also sees the ones between corner windows."""
        return format_events([e for e in row["facts"].get("events", [])
                              if e["kind"] in ("lockup", "wheelspin", "off")],
                             self.cmap, self.units)

    def _this_lap_corner(self, corner) -> str:
        m, rows, head = self._provisional()
        if m is None:
            return head
        if self.cmap is None:
            return "No corner map for this track yet — can't break the lap into corners."
        cms = [CornerMetrics(**r["facts"]["metrics"]) for r in rows]
        cm = find_corner(SimpleNamespace(corners=cms), corner)
        if cm is None:
            done = ", ".join(corner_tag(c) for c in cms) or "none yet"
            return (f"{head}\n{corner!r} is not captured on this lap yet — corners "
                    f"completed so far: {done}.")
        row = rows[cms.index(cm)]
        line = format_corner_line(cm, self.units, envelope_formed=False)
        incidents = self._row_incidents(row)
        if incidents:
            line += "\nIncidents here: " + "; ".join(incidents)
        return f"{head}\n{line}"

    def _this_lap_events(self) -> str | None:
        """#160: get_stint_events' closing block -- the lap being driven, or an
        unfinished one, corner by corner; None when there is neither."""
        m, rows, head = self._provisional()
        if m is None:
            return None
        out = [f"THIS LAP SO FAR — {head.removeprefix('Lap: ')}"]
        tags = ", ".join(corner_tag(CornerMetrics(**r["facts"]["metrics"])) for r in rows)
        out.append(f"  corners completed: {tags or 'none yet'}")
        incidents = [line for r in rows for line in self._row_corner_reads(r)]
        scan = m.get("incidents")
        if scan is None:
            # Never "no incidents" off a partial look: only corner windows were read.
            out.extend(f"  {line}" for line in incidents)
            out.append("  (spins and hits outside corner windows: not scanned)")
            return "\n".join(out)
        incidents += incident_lines(scan, self.cmap, self.units)
        if incidents:
            out.extend(f"  {line}" for line in incidents)
        elif scan.get("scanned_to") is not None:
            out.append(f"  no incidents so far (whole lap scanned to "
                       f"{scan['scanned_to']:,.0f} m)")
        return "\n".join(out)

    def get_car_status(self):
        """#160: the live car, gated like the race tools to the live session."""
        if not self.on_track:
            return ("No live session: car status is read live from the game, only while "
                    "you're in a session.")
        if self.car_status is None:
            return ("Car status isn't readable yet: no lap under way in this session since "
                    "it (re)started.")
        return self.car_status

    def get_corner(self, corner, which="latest"):
        if (which or "").strip().lower() in _THIS_LAP:
            return self._this_lap_corner(corner)
        record, lap_id = self._resolve_lap(which)
        if record is None:
            return self._no_lap_msg(which)
        if self.cmap is None:
            return "No corner map for this track yet — can't break the lap into corners."
        analysis = self._analysis_for(record, lap_id)
        cm = find_corner(analysis, corner)
        if cm is None:
            names = ", ".join(corner_tag(c) for c in analysis.corners)
            return f"No corner matching {corner!r}. Corners this track: {names}"
        envelope_formed = (analysis.gg_envelope_laps or 0) >= _ENVELOPE_MIN_LAPS
        line = format_corner_line(cm, self.units, envelope_formed=envelope_formed)
        usually = self._gear_usually_line(cm)
        if usually:
            line += f"\n{usually}"
        label = cm.label or cm.corner_no
        note = self._corner_note(label)
        if note:
            line += f"\nAuthored note: {note}"
        geo = self._corner_geometry_clause(label, cm.direction)
        if geo:
            line += f"\nGeometry (baked from the track map): {geo}"
        # #108 item 12: locate it by direction/speed/position, not a bare number --
        # the reference/best lap's speeds when one resolves (a spun lap's min speed
        # lies), else this lap's own.
        ref, _ = self._reference_lap(record, lap_id, "best")
        ref_analysis = self._analysis_for(ref, None) if ref is not None else analysis
        descriptor = corner_descriptor(self.cmap, cm.corner_no, analysis=ref_analysis,
                                       sector_bounds=record.sector_bounds)
        line += f"\nLocate it: {descriptor}"
        return self._lap_line(record, lap_id) + "\n" + line

    def _gear_usually_line(self, cm) -> str | None:
        """#38 P1.7: "You took T{label} in {gear} (usually {mode})." when this
        lap's gear_min at this corner disagrees with the stint's mode -- silent
        with no gear_min, <3 comparable laps (`_stint_analyses`), or agreement."""
        if cm.gear_min is None:
            return None
        analyses, _ = self._stint_analyses()
        if len(analyses) < 3:
            return None
        counts = Counter(other.gear_min for a in analyses for other in a.corners
                         if other.corner_no == cm.corner_no and other.gear_min is not None)
        if not counts:
            return None
        mode = counts.most_common(1)[0][0]
        if mode == cm.gear_min:
            return None
        tag = f"T{cm.label or cm.corner_no}"
        return f"You took {tag} in {ordinal(cm.gear_min)} (usually {ordinal(mode)})."

    def _corner_note(self, label) -> str | None:
        """Authored note for one corner label from the merged map, or None.

        Notes may be keyed by a hyphen range ("10-11") covering several corners,
        while corner maps label corners individually ("10", "11"). `note_key_binds`
        owns that grammar, shared with the map-save guard that decides which notes
        survive a rebuild -- the two must agree on what a key names, or the guard
        deletes notes this path could still serve.

        Not a pure delegation: two rules live here rather than in the grammar.
        An exact label is looked up FIRST, so a specific '12' note wins over a
        '11-13' note covering it whatever order the dict happens to carry them in.
        And the range scan takes the first covering key it meets, so two overlapping
        ranges resolve by insertion order -- no shipped map has any.
        """
        # 'corner' reaches here straight from the model's tool call, so it is arbitrary
        # text; `note_key_binds` is total and the lookups are by string, so no shape of
        # it can raise out of an LLM-callable tool.
        notes = getattr(self.cmap, "notes", None) or {}
        by_corner = notes.get("corners") or {}
        label = str(label)
        if label in by_corner:   # an exact label wins over a range that covers it
            return by_corner[label]
        for key, text in by_corner.items():
            if note_key_binds(key, {label}):
                return text
        return None

    def _corner_geometry_clause(self, label, direction) -> str:
        """Baked elevation/banking for one corner label, as
        an owned-data clause, or '' if the map carries none for this corner.

        `direction` is the corner's computed turn direction; banking reads as
        banked or off-camber only against it (issue #35).
        """
        geometry = getattr(self.cmap, "geometry", None) or {}
        metrics = geometry.get(str(label))
        if not metrics:
            return ""
        return geometry_clause(metrics, direction)

    def get_track_notes(self, corner=None):
        notes = getattr(self.cmap, "notes", None) or {}
        overview = (notes.get("overview") or "").strip()
        by_corner = notes.get("corners") or {}
        if not overview and not by_corner:
            # No notes on the map file (or no map file at all) — fall back to the
            # authored `knowledge/<slug>.md` prose note. Verbatim: the coach states
            # these as owned fact, so reaching them must not reformat them.
            return (load_track_knowledge(self.track)
                    or f"No authored notes for {self.track or 'this track'} yet.")
        lines = []
        if overview:
            lines.append(overview)
        if corner is not None:
            note = self._corner_note(corner)
            lines.append(f"T{corner}: {note}" if note else f"No authored note for corner {corner}.")
        else:
            for label, text in by_corner.items():
                lines.append(f"T{label}: {text}")
            refs = notes.get("reference_times") or {}
            if refs:
                pairs = "; ".join(f"{cls} {t}" for cls, t in refs.items())
                lines.append(f"Reference lap times (community benchmark): {pairs}")
        head = f"Authored notes for {self.track or 'this track'}:\n" if overview or by_corner else ""
        return head + "\n".join(lines)

    def _format_car_notes(self, notes: dict) -> list[str]:
        """The authored per-car note fields, in order, as labeled lines. Skips empties."""
        lines = []
        for key, label in (("overview", "Overview"), ("handling", "Handling"),
                            ("setup_concepts", "Setup concepts"),
                            ("driver_aids", "Driver aids")):
            val = (notes.get(key) or "").strip()
            if val:
                lines.append(f"{label}: {val}")
        # #108 item 6: which way a map means MORE intervention -- only stated
        # when the car notes carry it; never guessed.
        electronics = notes.get("electronics") or {}
        if electronics:
            pairs = "; ".join(f"{k} \N{EM DASH} {v}" for k, v in electronics.items())
            lines.append(f"Electronics: {pairs}")
        sources = notes.get("sources") or []
        if sources:
            lines.append("Sources: " + "; ".join(sources))
        return lines

    def _format_concepts(self) -> str:
        """The shared LMU setup-concepts doc as text, or '' if none is shipped."""
        c = self.setup_concepts.get("notes") if "notes" in self.setup_concepts else self.setup_concepts
        c = c or {}
        lines = []
        overview = (c.get("overview") or "").strip()
        if overview:
            lines.append(overview)
        for step in c.get("method") or []:
            lines.append(f"- {step}")
        for lever, text in (c.get("levers") or {}).items():
            lines.append(f"{lever}: {text}")
        return "\n".join(lines)

    def get_car_notes(self):
        concepts = self._format_concepts()
        car = self.car or "this car"
        car_lines = self._format_car_notes(self.car_notes)
        if not car_lines:
            head = f"No car-specific notes for {car} yet — general LMU setup principles:"
            return f"{head}\n{concepts}" if concepts else f"No authored notes for {car} yet."
        out = f"Authored notes for {car}:\n" + "\n".join(car_lines)
        if concepts:
            out += "\n\nShared LMU setup concepts (apply to any car):\n" + concepts
        return out

    def get_tire_report(self):
        record, lap_id = self._current_lap()
        analysis = self._analysis_for(record, lap_id) if record is not None else None
        if analysis is None:
            return "No lap captured yet (or no corner map for this track)."
        report = format_tire_report(analysis, self.units)
        return (self._lap_line(record, lap_id) + "\n"
                + (report or "No tire-temperature data was captured for this lap."))

    def get_wheel_report(self):
        record, lap_id = self._current_lap()
        analysis = self._analysis_for(record, lap_id) if record is not None else None
        if analysis is None:
            return "No lap captured yet (or no corner map for this track)."
        report = format_wheel_report(analysis, self.units)
        # #108: a wear STEP on one lap at one corner is a flat spot, not deg. The
        # wear line above reads the same number as a stint slope and gets blamed on
        # cornering load, so name the step next to it.
        steps = [line for line in self._events_for(record, lap_id) if "wear step" in line]
        return (self._lap_line(record, lap_id) + "\n"
                + (report or "No brake/wear data was captured for this lap (older capture).")
                + ("\n" + "\n".join(steps) if steps else ""))

    def get_energy_report(self):
        # #153: the session's latest COMPLETE lap (#77), the same "latest" every other
        # lap tool reads -- the app never pins `self.record`, so reading it directly
        # always said "no lap captured".
        record, lap_id = self._current_lap()
        if record is None:
            return "No lap captured yet — no fuel/energy usage to report."
        f = lap_facts(record)
        # A lap picked up mid-lap burned over only part of it: the amounts at the line
        # still stand, a per-lap rate does not.
        no_rate = ("burn not measured on a full lap -- capture joined mid-lap"
                   if picked_up_mid_lap(record) else "no clear per-lap burn this lap")
        lines = []
        if f.fuel_end is not None:
            if f.fuel_used is not None:
                lines.append(f"Fuel: {f.fuel_used:.2f} L used this lap; {f.fuel_end:.1f} L in "
                             f"tank at the end of that lap "
                             f"(~{laps_remaining(f.fuel_end, f.fuel_used):.1f} laps left).")
            else:
                lines.append(f"Fuel: {f.fuel_end:.1f} L in tank at the end of that lap ({no_rate}).")
        ve = record.channels.get("virtual_energy")
        dead_ve = ve is not None and len(ve) > 0 and not bool((ve != 0).any())
        # A car with no virtual energy captures a flat 0 -- a dead channel, not "0% left".
        if f.energy_end is not None and not dead_ve:
            if f.energy_used is not None:
                lines.append(f"Virtual energy: {f.energy_used * 100:.1f}% used this lap; "
                             f"{f.energy_end * 100:.0f}% remaining "
                             f"(~{f.energy_end / f.energy_used:.1f} laps left).")
            else:
                lines.append(f"Virtual energy: {f.energy_end * 100:.0f}% remaining ({no_rate}).")
        ch = record.channels
        if "soc" in ch:
            lines.append(f"Battery state of charge: {float(ch['soc'][-1]) * 100:.0f}%.")
        return (self._lap_line(record, lap_id) + "\n"
                + ("\n".join(lines) if lines else "No fuel/energy data captured for this lap."))

    def get_race_result(self):
        # Name the track of the session these figures DESCRIBE, taken from that session's
        # own laps -- never self.track, which follows the live game or the last capture
        # and can be another track entirely (reviewing Silverstone while the game sat at
        # Fuji labelled Silverstone's result "Fuji"). A resumed chat's race retraction lets
        # the model use these figures only for the same race, so a wrong label is worse
        # than none.
        text = race_result_text(self.store, self.session_id)
        laps = self.store.session_laps(self.session_id) if self.session_id else []
        track = laps[0].get("track") if laps else None
        head = [f"Session track: {track}." if track else ""]
        # #136: WHICH race of the sitting these figures describe. race_result_text
        # already bounds the arc to one race (race_ts..next_race_ts); this names it,
        # so an answer about race 3 can't be read as being about race 1.
        rr = self.store.race_session(self.session_id) if self.session_id else None
        head.append(self._race_line(rr.get("race_ts") if rr else None))
        return "\n".join([h for h in head if h] + [text])

    def _no_live_race(self) -> str | None:
        """#152 guard for the race tools: a snapshot exists only in a live race, from its
        first completed lap. Never a guess."""
        if self.on_track and self.race is None and self.race_note:
            return self.race_note
        if not self.on_track or self.race is None:
            return ("No live race: race state, strategy and battle reads exist only during "
                    "a live race session, from its first completed lap.")
        return None

    def get_race_state(self):
        return self._no_live_race() or format_race_state(self.race)

    def get_strategy(self):
        return self._no_live_race() or format_strategy(self.race)

    def get_battle(self, rival=None):
        return self._no_live_race() or format_battle(self.race.battle, self.race.as_of_lap,
                                                     self.units, rival,
                                                     note=self.race.battle_note)

    def get_setup(self):
        if not self.setup:
            return "No setup data available (REST unavailable or open setup)."
        # #128: the live cockpit dials read from the newest stored lap of ANY kind
        # (out/in/partial included), not the latest COMPLETE lap (#77) -- a TC/ABS/
        # brake-bias change made mid-stint must be visible before the next timed lap.
        record, lap_id = self._newest_lap_any_kind()
        live = live_electronics(record.channels) if record is not None else {}
        live_note = self._live_lap_note(record, lap_id) if live else ""
        if record is not None and not live:
            # This lap's electronics block never populated (Max == 0) -- fall back
            # to the newest COMPLETE lap rather than silently going quiet or
            # claiming a live read off a lap that never captured one.
            fb_record, fb_lap_id = self._current_lap()
            if fb_record is not None and fb_lap_id != lap_id:
                fb_live = live_electronics(fb_record.channels)
                if fb_live:
                    live = fb_live
                    live_note = (self._live_lap_note(fb_record, fb_lap_id)
                                 + f" ({self._lap_kind_label(record, lap_id)} had no "
                                   "electronics captured.)")
        lines = _setup_lines(self.setup, live)
        if not lines:
            return "No adjustable setup parameters exposed for this car."
        out = "Current setup:\n" + "\n".join(lines)
        if live:
            out += "\n" + live_note
        if self.allowed_adjustments is not None:
            out += "\nLegal to change this tier: " + ", ".join(self.allowed_adjustments)
        # #108 item 6: electronics map direction -- from the car notes when
        # authored, else say so rather than guessing which way is more intervention.
        electronics = (self.car_notes or {}).get("electronics") or {}
        if electronics:
            pairs = "; ".join(f"{k} \N{EM DASH} {v}" for k, v in electronics.items())
            out += f"\nMap direction (from car notes): {pairs}"
        else:
            out += ("\nMap direction: unknown for this car \N{EM DASH} do not guess "
                     "which way is more intervention.")
        return out

    def record_driver_learning(self, text, track=None, car=None, replaces=None):
        if replaces is not None:
            row = self.store.get_learning(replaces)
            if row and (row["session_id"] == self.session_id or row["status"] == "pending"):
                self.store.update_learning(replaces, text)
                return f"Updated note {replaces} for your review."
        note_id = self.store.add_learning(text, status="pending", track=track or self.track,
                                car=car or self.car, source="coach_tool",
                                session_id=self.session_id)
        if replaces is not None:
            return (f"Note {replaces} wasn't from this conversation — saved as a new "
                    f"note for your review (note {note_id}).")
        return f"Saved for your review (note {note_id})."

    def get_driver_memories(self, track=None, car=None, topic=None):
        rows = self.store.active_learnings(track=track or self.track, car=car or self.car)
        if topic:
            topic = topic.lower()
            rows = [r for r in rows if topic in r["text"].lower()]
        if not rows:
            return "No saved memories yet."
        return "\n".join(f"- {r['text']}" for r in rows)

    def get_personal_best(self, track=None, car=None):
        track, car = track or self.track, car or self.car
        if not (track and car):
            return "No track/car resolved yet — no personal best available."
        best = self.store.best_lap(track, car)
        if best is None:
            return f"No recorded laps yet for {track} / {car}."
        prefix = f"All-time personal best at {track} in {car}: {format_laptime(best.lap_time)}"
        if self.session_id and best.session_id == self.session_id:
            # #130: the PB *is* the lap just driven -- "set <date>" reads as history
            # when it's actually this session, so name the lap and say how long ago.
            label_by_id, _ = self._lap_numbering()
            lap_id = next((lap["id"] for lap in self.store.session_laps(self.session_id)
                           if lap["ts"] == best.timestamp), None)
            label = label_by_id.get(lap_id) if lap_id is not None else None
            this_lap = f"this session's {label}" if label else "this session"
            return f"{prefix} — that's {this_lap}, set {_lap_age(best.timestamp)}."
        when = time.strftime("%Y-%m-%d", time.localtime(best.timestamp))
        out = f"{prefix}, set {when}."
        if self.session_id:
            session_best = self.store.best_lap(track, car, session_id=self.session_id)
            if session_best is not None and session_best.lap_time != best.lap_time:
                out += f" This session's best so far: {format_laptime(session_best.lap_time)}."
        return out

    def get_progress(self, track=None, car=None):
        track, car = track or self.track, car or self.car
        if not (track and car):
            return "No track/car resolved yet — no progress to show."
        summary = self.store.session_summary(track=track, car=car)
        points = summary["series"][0]["points"] if summary["series"] else []
        if not points:
            return f"No session history yet for {track} / {car} to show progress."
        parts = [f"{time.strftime('%Y-%m-%d', time.localtime(p['x']))} {format_laptime(p['value'])}"
                 for p in points]
        out = (f"Best-lap progression at {track} in {car} ({len(points)} sessions): "
               + " -> ".join(parts) + ".")
        if summary["improvement"] is not None:
            direction = "faster" if summary["improvement"] > 0 else "slower"
            out += (f" Net {format_laptime(abs(summary['improvement']))} {direction} from "
                    "first to most recent session.")
        return out

    def list_past_sessions(self, track=None, car=None, limit=5):
        track, car = track or self.track, car or self.car
        if not (track and car):
            return "No track/car resolved yet — no past sessions to list."
        rows = self.store.race_sessions_page(track=track, car=car, limit=int(limit or 5))["rows"]
        rows = [r for r in rows if r["session_id"] != self.session_id]
        if not rows:
            return f"No past sessions recorded for {track} / {car}."
        out = []
        for r in rows:
            date = time.strftime("%Y-%m-%d", time.localtime(r["started"]))
            sid = r["session_id"] or ""
            kind = "online" if "online" in sid else ("offline" if "offline" in sid else "session")
            best = format_laptime(r["best"]) if r["best"] is not None else "no valid lap"
            rr = self.store.race_session(r["session_id"])
            result = f", finished P{rr['position']}" if rr and rr.get("position") is not None else ""
            out.append(f"{date} ({kind}, {r['laps']} laps, best {best}{result})")
        return f"Recent sessions at {track} / {car}:\n" + "\n".join(out)

    def get_last_setup(self, track=None, car=None):
        track, car = track or self.track, car or self.car
        if not (track and car):
            return "No track/car resolved yet — no setup on record."
        # Exclude the current session so this returns the PRIOR session's setup
        # ("what did we run here last time?"), not the one this session autosaved.
        row = self.store.last_setup(track, car, exclude_session_id=self.session_id)
        if row is None:
            return f"No stored setup for {track} / {car} yet."
        when = time.strftime("%Y-%m-%d", time.localtime(row["updated_ts"]))
        lines = _setup_lines(row["setup"])
        if not lines:
            return f"Setup stored for {track} / {car} ({when}) but no adjustable parameters were captured."
        return f"Last setup run at {track} / {car} ({when}):\n" + "\n".join(lines)

    def dispatch(self, name: str, args: dict) -> str:
        """Execute one tool call by name. Unknown tool -> error string. The call AND
        its result land in `tool_calls` (#159), so a persisted answer can be audited
        against exactly what the coach read."""
        args = args or {}
        entry = {"name": name, "input": args}
        self.tool_calls.append(entry)
        try:
            entry["result"] = self._run_tool(name, args)
        except Exception as e:
            entry["result"] = f"error: {type(e).__name__}: {e}"
            raise
        return entry["result"]

    def _run_tool(self, name: str, args: dict) -> str:
        handler = {
            "get_session_status": lambda: self.get_session_status(),
            "list_recent_laps": lambda: self.list_recent_laps(args.get("limit", 10)),
            "get_lap_summary": lambda: self.get_lap_summary(args.get("which", "latest")),
            "get_time_loss": lambda: self.get_time_loss(args.get("which", "latest"),
                                                        args.get("reference", "best")),
            "get_stint_events": lambda: self.get_stint_events(args.get("laps", 10)),
            "get_consistency": lambda: self.get_consistency(args.get("phase")),
            "get_tire_trend": lambda: self.get_tire_trend(args.get("phase")),
            "get_handling": lambda: self.get_handling(args.get("which", "latest")),
            "list_track_corners": lambda: self.list_track_corners(),
            "get_corner": lambda: self.get_corner(args.get("corner", ""), args.get("which", "latest")),
            "get_tire_report": lambda: self.get_tire_report(),
            "get_wheel_report": lambda: self.get_wheel_report(),
            "get_track_notes": lambda: self.get_track_notes(args.get("corner")),
            "get_car_notes": lambda: self.get_car_notes(),
            "get_energy_report": lambda: self.get_energy_report(),
            "get_race_result": lambda: self.get_race_result(),
            "get_race_state": lambda: self.get_race_state(),
            "get_strategy": lambda: self.get_strategy(),
            "get_battle": lambda: self.get_battle(args.get("rival")),
            "get_car_status": lambda: self.get_car_status(),
            "get_setup": lambda: self.get_setup(),
            "record_driver_learning": lambda: self.record_driver_learning(
                args.get("text", ""), args.get("track"), args.get("car"), args.get("replaces")),
            "get_driver_memories": lambda: self.get_driver_memories(
                args.get("track"), args.get("car"), args.get("topic")),
            "get_personal_best": lambda: self.get_personal_best(args.get("track"), args.get("car")),
            "get_progress": lambda: self.get_progress(args.get("track"), args.get("car")),
            "list_past_sessions": lambda: self.list_past_sessions(
                args.get("track"), args.get("car"), args.get("limit", 5)),
            "get_last_setup": lambda: self.get_last_setup(args.get("track"), args.get("car")),
        }.get(name)
        if handler is None:
            return f"error: unknown tool {name!r}"
        return handler()
