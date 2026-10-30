"""On-demand data tools for the agentic coach. The model calls these to fetch
exactly what a question needs instead of receiving the whole lap every turn.

Each tool returns a deterministic text summary built from the analysis/store
layer — the two-stage invariant holds: raw ~50 Hz telemetry never reaches the
model, only computed per-corner/lap facts. Corner identity comes from the map,
never guessed. `CoachContext` is rebuilt per question so tools always read the
current session (track/car/lap), which the live-session re-resolution keeps fresh.
"""
from __future__ import annotations

import math
import os
import re
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import replace
from itertools import pairwise
from types import SimpleNamespace

from simcoach.analysis import (
    _ENVELOPE_MIN_LAPS,
    CornerMetrics,
    analyze_lap,
    corner_form,
    corner_tag,
    damage_events,
    damage_state,
    form_segments,
    geometry_clause,
    grip_envelope,
    is_incident_lap,
    lap_events,
    optimal_headline,
    optimal_sectors,
    phase_split,
    stint_consistency,
    stint_tire_trend,
    stint_wear_baseline,
    time_loss_buckets,
    window_inputs,
)
from simcoach.battle import (
    build_battle,
    contact_nearest,
    format_battle,
    format_race_battle,
    rival_matches,
    rival_position,
    same_car,
    target_position,
)
from simcoach.car_lookup import resolve_car_class
from simcoach.coach import _setup_lines, live_electronics
from simcoach.corners import CornerMap
from simcoach.format import (
    SLOWER_REF_NOTE,
    SUSPENSION_UNSEEN,
    corner_descriptor,
    find_corner,
    format_consistency,
    format_corner_line,
    format_damage_line,
    format_events,
    format_lap_facts,
    format_laptime,
    format_time_loss,
    format_time_on_table,
    format_tire_report,
    format_tire_trend,
    format_wheel_report,
    ordinal,
)
from simcoach.fuel import laps_remaining
from simcoach.livelap import impact_text, incident_lines

# #207: the off-track read needs the per-wheel surface capture (`off_surface`); a lap
# captured before it, or with the channel dead, has no measured offs either way.
OFFS_UNSCANNED = "off-track not captured"
from simcoach.race import (
    FILL_ROWS,
    FILL_TOL,
    HISTORY_LAPS,
    HISTORY_LOADS,
    MIN_STINT_LAPS,
    NO_RACE_LAP_YET,
    PACE_LAPS,
    RACE_OVER,
    RATE_LAPS,
    YOU_LEAD,
    HistLap,
    _clock,
    departure,
    fill_ratio,
    flag_counts,
    format_provisional,
    format_race_state,
    format_strategy,
    formation_burn,
    history_pick,
    lap_facts,
    laps_left,
    live_battle_target,
    pick_formation,
    picked_up_mid_lap,
    plans_longer,
    provisional_strategy,
    race_sample,
    rival_laps_line,
)
from simcoach.store import _SESSION_GAP_S, _session_base
from simcoach.track_lookup import load_track_knowledge, note_key_binds

# ponytail: hard cap on an incident scan. Each lap past the cache costs a telemetry
# load plus a full analysis, so a model asking for "the whole stint" must not walk a
# 60-lap race. Raise it if a rig endurance run genuinely needs a longer look-back.
_MAX_EVENT_LAPS = 30
_PHASES = ("practice", "qualifying", "warmup", "race")    # the store's phase labels
# The words for "the (latest) race as a whole" -- get_battle's `which`, and #204's race
# grammar (_race_pick) reads them as no particular race.
_WHOLE_RACE = ("race", "the race", "this race", "last race", "the whole race")

# #240: a clean race lap is "slow" past max(_SLOW_LAP_MIN_S, _SLOW_LAP_FRAC of the stint
# median). Real race stints (19, 2026-10): clean-lap noise runs to ~1.5 s on a 75-105 s
# lap, so a flat 2 s catches only real losses there while 2.5% scales it to a 4 min lap.
# ponytail: a guessed bar; a laps-per-stint rate across more stints settles it.
_SLOW_LAP_MIN_S = 2.0
_SLOW_LAP_FRAC = 0.025
_SLOW_LAP_MIN_LAPS = 5          # clean scanned laps needed for a median worth comparing to
_SLOW_LAP_BUCKET_S = 0.5        # a bucket this far behind the best lap is named...
_SLOW_LAP_TOP_S = 1.0           # ...and the worst one must be this far (loss is localised)
_SLOW_LAP_COAST_M = 20.0        # coasting / lower peak throttle said only when it differs
_SLOW_LAP_THROTTLE = 0.30       # from the best lap's by this much (m / fraction)

# How an unfinished lap (LapRecord.partial, #77) is named to the model: on the `Lap:`
# identity line, and in the `list_recent_laps` listing where a lap time would go.
_PARTIAL_WORD = {"out": "out-lap", "in": "in-lap", "partial": "partial lap"}
_PARTIAL_LIST_WORD = {"out": "out lap", "in": "in lap", "partial": "partial"}


_PRE_GREEN_NOTE = "grid/formation lap before the green flag, not racing"
# #181: a COMPLETE lap with no time (partial NULL, lap_time NULL).
_UNTIMED_NOTE = "no time \N{EM DASH} LMU didn't time it (invalidated)"
# #181: said wherever a finished race is described -- the cool-down is the lap BEGUN
# after the flag; an untimed lap alone never makes one.
_COOL_DOWN_RULE = ("the lap begun after the chequered flag is the cool-down (listed as an "
                   "in-lap or a partial lap), not a pit stop -- a lap marked invalidated "
                   "is never the cool-down")
# #209: a race review asked right after the flag ran before LMU wrote the result log
# (rig 2026-09-27, Long Beach: tool call 33 s after the last race lap, log written at
# 52 s). get_race_result waits for it -- at most _RESULT_LOG_WAIT_S, and only while the
# race's last lap is under _RESULT_LOG_RECENT_S old -- nudging the importer every
# _RESULT_LOG_POLL_S. The turn runs on coach_ws's worker thread, so blocking is fine.
# 25 s, not 20: that log landed 18.8 s after the call, and a 20 s wait caught it on its
# very last poll.
_RESULT_LOG_WAIT_S = 25.0
# #172: the player's live mFinishStatus (lmu.live) -> what he has done. 0 (still
# racing) and any other value say nothing; a DNF or DQ is never called "finished".
_FINISH_WORDS = {1: "finished (taken the chequered flag)", 2: "retired (DNF)",
                 3: "been disqualified (DQ)"}
_RESULT_LOG_RECENT_S = 90.0
_RESULT_LOG_POLL_S = 1.0


# #195: what get_strategy reads off a stored lap's .npz, kept in-process -- the history
# path loads dozens of them (1-3.5 s) while the driver waits, and a lap's .npz is written
# once at save and never replaced. Keyed on (path, mtime, size): a lap id is unique only
# within one store's db (a test's, another data dir's), and an out-of-band rewrite changes
# the mtime or the size.
# ponytail: FIFO-bounded at _LAP_READS_MAX laps (a few hundred bytes each); a real LRU
# only if one question ever needs more laps than that.
_LAP_READS: dict = {}
_LAP_READS_MAX = 4096


def _lap_reads(store, lap_id):
    """(lap_facts, departure, formation_burn, (ts, phase, session_id)) of a stored lap,
    memoised. Raises as store.load does (no row, no telemetry)."""
    path = store.telemetry_file(lap_id)
    st = os.stat(path)
    key = (path, st.st_mtime_ns, st.st_size)
    got = _LAP_READS.get(key)
    if got is None:
        rec = store.load(lap_id)
        got = _LAP_READS[key] = (lap_facts(rec), departure(rec), formation_burn(rec),
                                 (rec.timestamp, rec.phase, rec.session_id))
        while len(_LAP_READS) > _LAP_READS_MAX:
            _LAP_READS.pop(next(iter(_LAP_READS)), None)
    return got


def _pre_green(phase, lap_no) -> bool:
    """#180: race lap 0 -- the grid/garage segments and the formation lap, before the
    green flag (never timed: every real one is an out/in/partial lap). Its position is
    a grid shuffle, not a race position, and it is not part of a race review."""
    return phase == "race" and lap_no == 0

# #169: a lap slower than this multiple of the subject's time is never its reference
# or a form sample (Long Beach #237 compared a 1:23 lap with a 1:36 exploration lap).
_PACE_GATE = 1.07
# The reference label when the subject IS the best lap: a slower lap, never "best".
_NEXT_BEST = "your next-best lap"

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
                       "to the net lap delta. Use it for an explicit corner-by-corner "
                       "ranking, or against a SPECIFIC reference lap ('vs race lap 3') — "
                       "then get_corner on the worst bucket to explain why. For 'where's "
                       "the time' / 'what should I work on', get_time_on_table comes first. "
                       "Needs a second comparable lap on this track/car.",
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
        "name": "get_time_on_table",
        "description": "WHERE THE TIME IS on a lap (the BEST lap by default), measured "
                       "against the driver's OWN other laps this session: the optimal lap "
                       "from his best game sectors (the headline number), the corners where "
                       "two of his laps were quicker than this one (ranked), and for the "
                       "top corners the PHASE the time went in (approach/entry/exit/run-out) "
                       "and what his faster pass did differently. Also the all-time best "
                       "and the class benchmark. Call this FIRST for 'where's the time', "
                       "'where am I losing time', 'find me N tenths', 'what should I work "
                       "on', 'how do I go faster', or any question about the best lap. If "
                       "it says there is no comparable lap, say that.",
        "input_schema": {
            "type": "object",
            "properties": {
                "which": {"type": "string",
                          "description": "'best' (default), 'latest', or an in-session lap "
                                         "number like 'race lap 3'."},
                "detail": {"type": "boolean",
                           "description": "true when the driver asks for more detail: "
                                          "diagnoses the top 3 corners instead of 2."},
            },
        },
    },
    {
        "name": "get_stint_events",
        "description": "Incidents across the last N laps of the current phase — spins, "
                       "off-tracks, lockups, wheelspin, and per-wheel wear steps (flat "
                       "spots), each with lap and corner. Call this when the driver "
                       "mentions a spin, an off, a moment, a flat spot, or asks what "
                       "happened a few laps ago; never assume the latest lap is the one "
                       "they mean. It ends with THIS LAP SO FAR: the incidents on the lap "
                       "being driven, corner by corner as each is completed (provisional). "
                       "For a question about the RACE (how it went, analyze the race, "
                       "the start) pass phase='race': only the race's own laps from "
                       "race lap 1, never qualifying or the grid/formation lap.",
        "input_schema": {
            "type": "object",
            "properties": {
                "laps": {"type": "integer",
                         "description": "How many recent laps to scan (default 10, "
                                        "max 30). Ignored with phase='race', which "
                                        "always scans the whole race."},
                "phase": {"type": "string",
                          "enum": ["practice", "qualifying", "warmup", "race"],
                          "description": "Which phase's laps to scan. Default: the "
                                         "current phase only. 'race': the current or "
                                         "most recent race's racing laps; any other: "
                                         "that phase's most recent laps."},
                "race": {"type": "integer",
                         "description": "Which race of a session holding several "
                                        "(1 = the first; laps are labelled 'race N lap "
                                        "M'). Implies phase='race'. Default: the current "
                                        "or most recent race."},
                "which": {"type": "string",
                          "description": "Same as race=N, as text: 'race 2'."},
            },
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
                       "position, incidents, penalties, every class rival's best lap. Call "
                       "this for questions about the "
                       "race/result/how it went overall; for the race lap by lap, "
                       "get_stint_events with phase='race'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "race": {"type": "integer",
                         "description": "Which race of a session holding several (1 = "
                                        "the first). Default: the most recent race."},
                "which": {"type": "string",
                          "description": "Same as race=N, as text: 'race 2'."},
            },
        },
    },
    {
        "name": "get_car_status",
        "description": "LIVE SESSION ONLY. The car right now, read live from the game: "
                       "impacts since the start of THIS lap (where, which side, how hard), "
                       "bodywork dent severity, detached parts or wheels, flat tires, engine "
                       "overheating (NOT suspension or mechanical damage: the telemetry this "
                       "app reads doesn't carry it), and whether the car is on track, in the pit lane or in "
                       "the garage. Use for 'check my car', 'am I damaged', 'did I just hit "
                       "something', 'is a wheel off'.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_race_state",
        "description": "LIVE RACE ONLY. Where the driver stands as of the last completed lap: "
                       "overall and class position, laps or time remaining (and laps left at "
                       "the recent clean pace), and the in-class car ahead and behind with the "
                       "gap TREND (s/lap) and the lap he'd be caught at that rate, and their "
                       "best and last lap times; a "
                       "'Penalties:' line appears only when the game reports one outstanding "
                       "(no line says nothing either way). Use for position, gap, "
                       "'am I catching him', 'how long is left'. One call "
                       "answers it.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_strategy",
        "description": "IN A LIVE SESSION. Fuel and virtual energy to the flag (makes it or "
                       "not, margin in laps, per-lap save target), the pit window (earliest/"
                       "latest lap and what sets it), tire life to a named wear threshold, "
                       "and the stop as PIT-MENU settings (virtual energy %, fuel ratio "
                       "direction, tyres, repairs), as of the last completed lap. Before 2 "
                       "clean race laps (grid, practice, qualifying, race lap 1) it returns a "
                       "PROVISIONAL plan from this session's practice/qualifying burn rates, "
                       "or -- with fewer than 2 of those -- the median burn of this car's "
                       "stored laps at this track, with the energy/fuel the race needs. "
                       "Use for 'will I make it', 'when do I pit', 'how much do I refuel', "
                       "'how much energy for this race', 'how long will the tires last'. One "
                       "call answers it.",
        "input_schema": {"type": "object", "properties": {
            "race_laps": {"type": "integer",
                          "description": "Race length in laps, ONLY as the driver stated it "
                                         "(outside a live race the game doesn't report it)."},
            "race_minutes": {"type": "number",
                             "description": "Race length in minutes, ONLY as the driver "
                                            "stated it."}}},
    },
    {
        "name": "get_battle",
        "description": "Where a same-class rival was faster or slower than the driver: "
                       "corners and sections ranked by time gained or lost, plus apex-speed "
                       "differences (the passing-chance read). In a live race, by default the "
                       "LAST COMPLETED lap. With no race live (after the race), by default the "
                       "most recent race of this session from its stored laps: per corner the "
                       "median over the race laps compared, naming those laps. By default the "
                       "rival is the car the driver was nearest (it may name two live). Use for "
                       "'where is he quicker', 'where can I pass him', 'where could I have "
                       "passed him'. 'The car/guy in front', 'ahead' -> direction='ahead'; "
                       "'behind' -> direction='behind'; 'P3', 'number three' -> position=3. "
                       "One call answers it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "rival": {"type": "string",
                          "description": "Optional: a rival the driver named: 'P4' (CLASS "
                                         "position), a driver name, or a car/team name "
                                         "('the Ferrari')."},
                "direction": {"type": "string", "enum": ["ahead", "behind"],
                              "description": "Optional: the car one CLASS position ahead "
                                             "of ('in front', 'ahead') or behind the driver."},
                "position": {"type": "integer",
                             "description": "Optional: a CLASS position the driver named "
                                            "('P3', 'number three' -> 3)."},
                "which": {"type": "string",
                          "description": "Optional: 'race' for the whole most recent race, "
                                         "or one stored race lap by its in-session number "
                                         "like 'race lap 7'; 'race 2' for the whole second "
                                         "race of a multi-race session, 'race 1 lap 5' for "
                                         "one lap of it. Omit for the default (live: "
                                         "the last completed lap; after the race: 'race')."},
                "race": {"type": "integer",
                         "description": "Optional: a whole race of a session holding "
                                        "several (1 = the first), like which='race N'."},
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
                       "if different. Use for 'what's my personal best / all-time fastest here'. "
                       "Counts the game's result-log laps too. A brand/model word in car= "
                       "('McLaren', 'Z06') or car_class= ('GT3', for 'any GT3') returns one "
                       "best per matching car.",
        "input_schema": {
            "type": "object",
            "properties": {
                "track": {"type": "string", "description": "Optional: defaults to the current track."},
                "car": {"type": "string", "description": "Optional: a model or brand/model "
                        "word; defaults to the current car."},
                "car_class": {"type": "string", "description": "Optional: every car of this "
                              "class (e.g. 'GT3', 'Hyper', 'LMP2')."},
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
        "description": "The setup on the fastest lap of the previous session at a track/car "
                       "(defaults to the current one; older sessions fall back to their last "
                       "autosave, labelled so) — not the live current setup (use get_setup "
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


def session_turn_note(row: dict, laps: list[dict]) -> str:
    """Prefix marking a stored coach answer as being about a DIFFERENT session than
    the one being asked about now (#167): its track, car, online/offline and the
    phase it was written in. `laps` is that session's laps (store.session_laps).

    #97 used to start a new chat instead, so an answer about another race never
    re-entered the context; a chat now spans sessions and weekends, so each such turn
    says what it was about. Same shape as race_turn_note: from stored data on READ,
    never written to disk, no LLM, no tool call."""
    mode = _session_base(row["session_id"]).rsplit("--", 1)[-1]
    phase = None
    for lap in laps:
        if lap["ts"] <= row["ts"]:
            phase = lap["phase"]
    bits = [b for b in (row.get("track"), row.get("car"),
                        mode if mode in ("online", "offline") else None, phase) if b]
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(row["ts"]))
    # "different", not "earlier": viewing a past session, the other turns may be later.
    return f"[About a different session ({', '.join(bits + [when])}) — not the current one.] "


def _complete_laps(win: dict) -> str:
    """'8 complete laps captured' -- a window's complete-lap count, worded so it is
    never read as the race's length (out/in/partial laps aren't in it, #204)."""
    n = win["laps"]
    return f"{n} complete lap{'' if n == 1 else 's'} captured"


def current_race_fact(windows: list[dict]) -> str:
    """The CURRENT RACE line for the user turn, or '' with at most one race (#136).

    Rides in the user turn (the #131 seam), not the system prompt: it changes every
    race, and the system block is prompt-cached."""
    if len(windows) < 2:
        return ""
    win = windows[-1]
    started = time.strftime("%H:%M", time.localtime(win["start_ts"]))
    return (f"CURRENT RACE: race {win['index']} of this session (started {started}, "
            f"{_complete_laps(win)}). Earlier turns marked as earlier races are history, "
            f"not this race.")


_FIRST_CAPTURED = (" (the first captured lap of this race: where it ENDED, NOT the starting "
                   "position)")


def _multiclass(pairs, rr=None) -> bool:
    """#208: a multiclass race -- a lap's (overall, class) place pair that differs, or
    the result row's finish or grid pair. In a one-class race the two are the same
    number everywhere; the field's class list is not stored, so a race where the
    driver's class led the whole field on every lap reads as one class -- and there
    either number is true."""
    if rr:
        pairs = [*pairs, (rr["position"], rr["class_position"]),
                 (rr["grid_position"], rr["class_grid_position"])]
    return any(p is not None and c is not None and p != c for p, c in pairs)


def _place(pos, cls, multi, inner=False) -> str:
    """#208: 'P9 in class (P17 overall)' in a multiclass race -- the driver races for
    class, so class leads -- 'P17 overall' otherwise, or when the lap's class place
    wasn't sampled -- then it says so (the post-flag in-lap is committed in the garage
    stall, the live field empty), so an overall number is never read as a class one.
    `inner`: 'P9 in class, P17 overall', for inside parentheses."""
    if not multi:
        return f"P{pos} overall"
    if cls is None:
        return (f"P{pos} overall, class place not sampled" if inner
                else f"P{pos} overall (class place not sampled)")
    if cls == pos:
        return f"P{pos} in class and overall"
    return f"P{cls} in class, P{pos} overall" if inner else f"P{cls} in class (P{pos} overall)"


def _arc_places(arc, multi, inner=False) -> tuple[str, str, str]:
    """#208: an arc's (first captured, lowest, most recent) places as text. Multiclass:
    the lowest CLASS place, with that same lap's overall place -- overall only when no
    lap sampled a class place (laps captured before class places were)."""
    pos = arc["positions"]
    classed = [p for p in pos if p[1] is not None]
    multi = multi and bool(classed)
    low = max(classed, key=lambda p: p[1]) if multi else (arc["worst"], None)
    return tuple(_place(p, c, multi, inner) for p, c in (pos[0], low, pos[-1]))


def race_result_text(store, session_id, race: int | None = None, bests: bool = True) -> str:
    """The session's race outcome as prompt text: grid slot, finish, incidents, and
    the live per-lap position arc. #204: race=N (store.race_windows' index) is that
    race of the sitting, its arc kept inside its own window; default the latest.

    The single owner of that wording. Every surface that describes a race to the model
    calls this -- the coach tool AND the profile digest. The digest used to phrase its
    own copy ("Race arc: started P{start} ... finished P{last}") off an unscoped arc,
    which is exactly the #67 defect: `start` is the first lap capture happened to
    record, mid-race, and an unscoped arc reads it from whatever phase came first.
    Two copies meant fixing one left the other asserting the opposite as fact.
    `bests=False` leaves out the class best-laps line (#213): the profile digest must
    not turn a list of rival names into memories.
    """
    rr = store.race_session(session_id, race) if session_id else None
    # Race stint only — a session_id spans practice/qualy/race, so an all-phase arc
    # would read the start/worst from an earlier phase (wrong start position).
    # Bound the arc to the race the result row actually describes: phase='race'
    # alone spans every race in the sitting, so a second race's grid would be
    # reported next to the first race's lap positions. With no result row (a race
    # still being driven, whose log does not exist yet) there is no race_ts, so fall
    # back to the floor the store derives from the last logged race.
    arc = None
    if session_id and race is not None:
        # One named race: its row's bounds, never past its own window -- the next
        # race's log may not exist, and next_race_ts is then None.
        wins = store.race_windows(session_id)
        win = wins[race - 1]
        since = rr["race_ts"] if rr else win["start_ts"]
        ends = [t for t in (rr["next_race_ts"] if rr else None,
                            wins[race]["start_ts"] if race < len(wins) else None)
                if t is not None]
        arc = store.session_position_arc(session_id, phase="race", since=since,
                                         until=min(ends) if ends else None)
    elif session_id:
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
    multi = _multiclass(arc["positions"] if arc else [], rr)
    if rr:
        if rr["grid_position"] is not None:
            # #162: every position says whether it is overall or in class. #208: class
            # leads in a multiclass race.
            grid = f"Started {_place(rr['grid_position'], rr['class_grid_position'], multi)}"
            lines.append(grid + " on the grid.")
        parts = []
        if multi and rr["position"] is not None and rr["class_position"] is not None:
            parts.append(f"Finished {_place(rr['position'], rr['class_position'], multi)}")
        else:
            if rr["position"] is not None:
                parts.append(f"Finished P{rr['position']} overall")
            if rr["class_position"] is not None:
                parts.append(f"P{rr['class_position']} in class")
        if rr["finish_status"]:
            parts.append(f"status: {rr['finish_status']}")
        lines.append("; ".join(parts) + "." if parts else "Race result recorded.")
        # #213: every class rival's best lap from the log, ranked here, not by the model.
        # The bracket is his class position in the log -- a DNF's too -- with the finish
        # status whenever it isn't a normal finish (a 3-lap DNF is no "P16 finish").
        ranked = sorted(rr.get("class_bests") or [], key=lambda d: d["best"]) if bests else []
        if ranked:
            def tag(d):
                st = d.get("st") if d.get("st") != "Finished Normally" else None
                t = ", ".join(x for x in (f"P{d['p']}" if d["p"] else None, st) if x)
                return f" ({t})" if t else ""
            lines.append("Best laps in your class (result log), fastest first, class position "
                         "in brackets: " + "; ".join(
                             f"{format_laptime(d['best'])} {'you' if d['me'] else d['name']}"
                             + tag(d) for d in ranked) + ".")
        if ranked:
            lines.append("A best lap is one lap, not race pace: a quicker best lap does not "
                         "mean quicker over the race -- the finishing order says that.")
        incidents = rr["incidents"]
        if incidents:
            lines.append(f"Incident reports in the result log ({len(incidents)}), grouped. "
                         "Every car in a contact logs its own report, so one touch can be "
                         "two or more reports, and a report does not say who was at fault:")
            lines.extend(_incident_groups(incidents))
        penalties = rr["penalties"]
        if penalties:
            texts = [str(p.get("text") or p) for p in penalties[:3]]
            # #162: the log has a line per penalty issued AND per penalty served, so
            # its length is entries, not a count of penalties.
            n = len(penalties)
            lines.append(f"Penalty log ({n} entr{'y' if n == 1 else 'ies'}): "
                         f"{'; '.join(texts)}")
    if arc:
        # NEVER call arc['start'] a starting position: it is the first lap capture
        # happened to record, which is already underway (issue #67 reported a P9
        # start as P18 this way). The grid slot only ever comes from the result log.
        first, low, last = _arc_places(arc, multi)
        line = (f"Live per-lap positions over {arc['laps']} captured lap(s): "
                f"{first} on the first captured lap, as low as {low}, {last} on the most "
                "recent.")
        if not (rr and rr["grid_position"] is not None):
            line += (" The grid position was not captured, so the first captured lap "
                     "is NOT the starting position -- do not report a start.")
        lines.append(line)
    if rr and (places := _net_places(rr, arc, multi)):
        lines.append(places)
    return "\n".join(lines)


def _incident_groups(incidents) -> list[str]:
    """#239: the result log's incident reports grouped by the other party, each group with
    its count and race laps -- every report is counted, none hides behind "+N more". A
    pre-#239 row holds bare strings (no ET, so no lap): those are listed in full."""
    groups: dict = {}
    for i in incidents:
        i = i if isinstance(i, dict) else {"kind": "other", "text": str(i)}
        key = ((i["kind"], i.get("other"), i.get("car"))
               if i.get("kind") == "contact" and i.get("other")
               else ("track_limits",) if i.get("kind") == "track_limits"
               else ("other", i.get("text") or "incident (no text logged)"))
        groups.setdefault(key, []).append(i)
    out = []
    for key, items in groups.items():
        n = len(items)
        reports = f"{n} report{'' if n == 1 else 's'}"
        if key[0] == "contact" and key[2]:
            mine = sum(1 for i in items if i.get("by_me"))
            head = (f"contact with {key[1]} (another car): {reports} ({mine} logged by you, "
                    f"{n - mine} by {key[1]})")
        elif key[0] == "contact":
            head = f"contact with {key[1]} (not a car): {reports}"
        elif key[0] == "track_limits":
            kinds = ", ".join(dict.fromkeys(i.get("text") or "no resolution" for i in items))
            head = f"track limits: {reports} ({kinds})"
        else:
            head = key[1] if n == 1 else f"{key[1]}: {reports}"
        laps = sorted({i["lap"] for i in items if i.get("lap") is not None})
        unknown = sum(1 for i in items if i.get("lap") is None)
        where = (f"lap{'s' if len(laps) > 1 else ''} {', '.join(map(str, laps))}"
                 if laps else "")
        if unknown:
            where = (f"{where}; {unknown} with lap not known" if laps else "lap not known")
        out.append(f"- {head}, {where}")
    return out


def _net_places(rr, arc, multi) -> str | None:
    """#239: places gained/lost, counted here so the model never does the arithmetic
    ("clawing three back" from a low of P14 to a P12 finish). Grid and finish are the
    result log's; the lowest place is the worst race lap the arc captured, never better
    than the finish, pit laps left out (a car stopped in the pits reads far down the
    order). Class places in a multiclass race, overall otherwise."""
    grid, fin = ((rr["class_grid_position"], rr["class_position"]) if multi
                 else (rr["grid_position"], rr["position"]))
    if fin is None:
        return None
    pos = arc["positions"] if arc else []
    pits = (arc.get("pit") if arc else None) or [None] * len(pos)
    lows = [c if multi else p for (p, c), pit in zip(pos, pits) if not pit]
    lows = [x for x in lows if x is not None]
    # A lap with no pit flag recorded may have been a stop: say so, don't hide it.
    what = ("worst captured lap place, pit stops included" if None in pits
            else "lowest race-lap place (pit laps left out)")

    def moved(a, b):
        d = a - b
        return "no change" if d == 0 else f"{abs(d)} {'gained' if d > 0 else 'lost'}"
    parts = [f"grid P{grid} to finish P{fin} = {moved(grid, fin)}"] if grid else []
    if lows:
        low = max(*lows, fin)
        parts.append(f"{what} P{low} to finish P{fin} = {low - fin} regained")
    scope = "in class" if multi else "overall"
    return f"Places (computed, {scope}): {'; '.join(parts)}." if parts else None


_CLASS_ALIASES = {"HYPERCAR": "HYPER", "H": "HYPER", "DH": "HYPER"}  # LMH / LMDh


def _norm_class(name: str | None) -> str:
    """#182: a class word as the driver/REST/result log may spell it -> one key
    ('LMGT3' = 'GT3', 'Hypercar' = 'LMDh' = 'Hyper'). 'LMP2_ELMS' stays distinct
    from 'LMP2': a different spec of the same car."""
    key = (name or "").strip().upper().removeprefix("LM")
    return _CLASS_ALIASES.get(key, key)


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
                 race_note=None, live_lap=None, car_status=None, live_session=None,
                 live_field=None, match_class=None, scan_results=None):
        self.store = store
        # #209: a zero-argument nudge that makes the result-log importer scan now (the
        # sidecar's poller wake). None = no importer to wait on (tests, dev builds).
        self.scan_results = scan_results
        self._result_log_waited = False   # #209: waited once this turn (see _await_result_log)
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
        # #190: the class stored laps are MATCHED by -- the scraped result-log class
        # first (car_groups' own spelling: REST may call the ELMS Oreca 'LMP2'), REST
        # only when that doesn't resolve. `car_class` stays what is displayed.
        self.match_class = match_class or car_class
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
        # #161: the live session (lmu.live.SessionState: phase, max_laps, clock) when
        # the question came in -- the race length for a provisional pre-race plan.
        self.live_session = live_session
        # #186: a zero-argument reader of one live scoring frame (lmu.live.
        # read_field_snapshot in the sidecar; injected in tests), called as a race tool
        # answers -- the snapshot's position is up to a lap old. None = no live read.
        self.live_field = live_field
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
        self._records: dict = {}   # lap id -> LapRecord, see `_load`
        self._session_rows: list | None = None   # see `_session_laps`
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

    def _race_line(self, win: dict | None) -> str:
        """'Race 3 of this session (started 00:26, 8 complete laps captured).' for the
        race window a tool
        is about, or '' when the session holds at most one race (nothing to
        disambiguate, and a lone 'Race 1 of this session' is noise)."""
        if len(self._race_windows()) < 2:
            return ""
        started = time.strftime("%H:%M", time.localtime(win["start_ts"]))
        return (f"Race {win['index']} of this session (started {started}, "
                f"{_complete_laps(win)}).")

    def _race_window_at(self, race_ts: float | None) -> dict | None:
        """The race window a result row describes: the first one still running at its
        race_ts -- the race's own start stamp, the same floor the position arc uses
        (`since=race_ts`). The current race without one; None when the session has no
        race. A window is a run of race laps, not a race: two races driven back to back
        with no other lap between merge into one, and then this window also holds the
        other race -- anything pairing a lap with the result must check the lap's own
        time against race_ts/next_race_ts too."""
        win = self._current_race()
        if race_ts is not None:
            win = next((w for w in self._race_windows() if w["end_ts"] >= race_ts), win)
        return win

    def _race_pick(self, race=None, which=None) -> tuple[dict | None, str | None]:
        """#204: the one race grammar of the race tools -- race=N or which='race N'
        (1-based store.race_windows index, #174's 'race N'). Returns (window, None) for
        a race named, (None, None) for none named ('race', 'latest' and the like mean
        the tool's default: the current or most recent race), or (None, message) --
        never a silent fallback to the latest race for a race it can't find."""
        windows = self._race_windows()
        held = (f"{len(windows)} races, race 1 to race {len(windows)}" if len(windows) > 1
                else "1 race" if windows else "no race")
        want = None
        words = " ".join(str(which or "").lower().split())
        if words not in ("", "latest") + _WHOLE_RACE:
            m = re.fullmatch(r"(?:the )?race (\d+)", words)
            if m is None:
                one = (f" For one lap use get_lap_summary({words!r})."
                       if re.fullmatch(r"race (\d+ )?lap \d+", words) else "")
                return None, (f"which must name a race like 'race 2' (this session holds "
                              f"{held}), not {which!r}.{one}")
            want = int(m[1])
        if race is not None:
            try:   # a whole number only: 1.5 is not race 1
                n = (int(race) if not isinstance(race, bool)
                     and float(race) == int(race) else None)
            except (TypeError, ValueError, OverflowError):
                n = None
            if n is None:
                return None, f"No race {race} in this session: it holds {held}."
            if want is not None and n != want:
                return None, f"race={race} and which={which!r} name different races."
            want = n
        if want is None:
            return None, None
        win = next((w for w in windows if w["index"] == want), None)
        if win is None:
            return None, f"No race {want if race is None else race} in this session: it holds {held}."
        return win, None

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
        if (label is None and self.session_id and record.session_id
                and record.session_id != self.session_id):
            # #169: never "this lap" for a lap from another session.
            head = "a lap from another session"
            if record.timestamp:
                parts[-1] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.timestamp))
        else:
            head = label or ("this lap" if lap_id is None else f"lap id {lap_id}")
        # An out/in/partial lap says so right here (#77), so the model can never read
        # its missing lap time or NaN corners as a timed lap that went wrong.
        kind = _PARTIAL_WORD.get(record.partial, "")
        # #134: a race lap's stored position, plus the previous race lap's position
        # (so a spin's cost or a recovery is visible right in the identity line) --
        # looked up from session_laps rather than `record` (store.load() doesn't
        # thread position onto LapRecord).
        tail_bits = [kind] if kind else []
        # #181: a complete lap with no time -- LMU didn't time it (an invalidated lap).
        if record.partial is None and not record.lap_time:
            tail_bits.append(_UNTIMED_NOTE)
        # #180: race lap 0 is not racing -- said here so every lap read (lap list,
        # unscoped stint scan, lap summary) carries it even when a race question
        # forgot phase='race'.
        if _pre_green(record.phase, record.lap_no):
            tail_bits.append(_PRE_GREEN_NOTE)
        # #138: a pit-stop lap's time and corner deltas are the stop, not the
        # driving -- flag it right in the identity line so the model doesn't
        # read a stint-ending outlier as damage or a spin.
        if record.pit:
            tail_bits.append("pit stop this lap")
        # #154: a mapping pass keeps its real phase (a race lap reads as one) but is a
        # boundary weave -- say so, or its time and corners read as a bad lap.
        if record.mapping:
            tail_bits.append(_MAPPING_NOTE)
        if (record.phase == "race" and lap_id is not None and self.session_id
                and not _pre_green(record.phase, record.lap_no)):
            place = self._race_place(lap_id)
            if place is not None:
                tail_bits.append("position " + "".join(place))
        return (f"Lap: {head}" + (f" ({', '.join(parts)})" if parts else "")
                + (f" — {'; '.join(tail_bits)}" if tail_bits else ""))

    def _race_place(self, lap_id) -> tuple[str, str] | None:
        """A race lap's stored place and what it is measured from, as ('P12 in class
        (P24 overall)', ' (from ...)') -- the context is the previous lap of this race,
        the grid slot, or the first-captured-lap note. None with no stored place.
        Shared by the lap line and the lap list (#208)."""
        sess_laps = self._session_laps()
        idx = next((i for i, r in enumerate(sess_laps) if r["id"] == lap_id), None)
        cur_pos = sess_laps[idx]["position"] if idx is not None else None
        if cur_pos is None:
            return None
        # #187: the previous lap of THIS race -- race 2 lap 1 is not measured
        # against race 1's finish.
        win = next((w for w in self._race_windows() if lap_id in w["lap_ids"]), None)
        ids = set(win["lap_ids"]) if win else set()
        prev = next((r for r in reversed(sess_laps[:idx])
                     if r["id"] in ids and r.get("position") is not None
                     and not _pre_green(r["phase"], r.get("lap_no"))), None)
        # #208: class leads in a multiclass race (this race's laps say so).
        multi = _multiclass((r["position"], r.get("class_position"))
                            for r in sess_laps if r["id"] in ids)
        cls = sess_laps[idx].get("class_position")
        place = _place(cur_pos, cls, multi)   # laps.position is mPlace
        if prev is not None:
            was = _place(prev["position"], prev.get("class_position"), multi, True)
            return place, f" (from {was})"
        if sess_laps[idx]["lap_no"] == 1 and win is not None:
            # Race lap 1 starts from the grid slot -- only ever the result log's,
            # and only when that log is THIS race's (never guessed): its window,
            # and this lap inside race_ts..next_race_ts (a window can hold two
            # back-to-back races). #204: this race's own row, so an earlier
            # race's lap 1 reaches its grid slot too.
            rr = self.store.race_session(self.session_id, win["index"])
            grid = rr.get("grid_position") if rr else None
            start = rr.get("race_ts") if rr else None
            ts = sess_laps[idx]["ts"]
            at = self._race_window_at(start) if rr else None
            # A pit-lane start: the start segment just before lap 1 (the last
            # lap-0 row -- the earlier ones are garage/grid segments, pit set on
            # grid starts too) or lap 1 itself is in the pits. The grid slot is
            # not where that lap started.
            before = [r for r in sess_laps[:idx] if r["id"] in ids
                      and start is not None and r["ts"] >= start]
            pit_start = (sess_laps[idx].get("pit")
                         or bool(before and before[-1].get("pit")))
            if (grid is not None and at is not None and at["index"] == win["index"]
                    and start is not None and start <= ts
                    and (rr.get("next_race_ts") is None or ts < rr["next_race_ts"])
                    and not pit_start):
                grid_txt = _place(grid, rr.get("class_grid_position"), multi, True)
                return place, f" (from {grid_txt} on the grid)"
        # #208: this race's first captured lap, with no grid slot to measure it from --
        # its place is where the lap ENDED. 'up from P24 at the start' (rig 2026-09-27,
        # started P31) read it as a start.
        return place, _FIRST_CAPTURED

    def _lap_id_for(self, record) -> int | None:
        """Best-effort DB lap_id for a record with no id attached (e.g. `best_lap`,
        which only returns the record) -- matched by timestamp within this session.
        None when it can't be found (outside this session, or no session_id), which
        just leaves `_lap_line` to fall back to the record's own game-lap/time facts
        without an in-session label."""
        if record is None:
            return None
        if self.session_id:
            for row in self._session_laps():
                if row["ts"] == record.timestamp:
                    return row["id"]
        # #169: a lap from another session (the all-time fallback) still has an id.
        return next((r["id"] for r in self.store.timed_laps(record.track, record.car)
                     if r["ts"] == record.timestamp), None)

    def _recent_records(self) -> list:
        """This track/car's ~10 most recent stored laps, loaded ONCE per context.
        Shared by the grip envelope and the wear baseline so the second reader is
        free."""
        if self._recent is not None:
            return self._recent
        records = []
        # #216: this car's class only (#190); none when that can't be told.
        if self.track and self.car and (cls := self._class_scope()) is not False:
            # Complete laps only: a half lap must not shape the grip envelope or
            # the wear baseline (#77).
            for row in self.store.recent_laps(self.track, self.car, limit=10,
                                              complete_only=True, car_class=cls):
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

    def _offs_unscanned(self, record, lap_id) -> bool:
        """#207: no corner on this lap has a surface read (captured before it, or a
        dead channel), so an absent "off track" there is not a measured "no off"."""
        return all(cm.off_surface is None for cm in self._analysis_for(record, lap_id).corners)

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
        # #157: no corner map still gets the lap-level reads (tires, wheels, lap-time
        # spread) -- just no corners. Per-corner tools guard `self.cmap` themselves.
        key = lap_id if lap_id is not None else id(record)
        if key not in self._analysis_cache:
            # The same reference get_lap_summary names (#169): never the lap itself,
            # never a lap outside the comparable-pace gate.
            ref, _ = self._reference_lap(record, lap_id)
            envelope = self._grip_envelope(ref)
            self._analysis_cache[key] = analyze_lap(record, self.cmap or CornerMap([]),
                                                     reference=ref,
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
            # #169: "the best lap" is THIS session's fastest valid lap; all-time only
            # when the session has none (the all-time best is its own line).
            best = (self.store.best_lap(self.track, self.car, session_id=self.session_id)
                    if self.session_id else None)
            if best is None and (cls := self._class_scope()) is not False:
                # #190: this car's class only; none when that can't be told
                best = self.store.best_lap(self.track, self.car, car_class=cls)
            # #173: resolve its id, or every comparison of the best lap excludes
            # nothing and picks the best lap itself as its own reference.
            return best, self._lap_id_for(best)
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
            # This car's class only (#190, #214 review): the model expands to its entries.
            cls = self._class_scope()
            if (raw.isdigit() and cls is not False and int(raw) in set(
                    self.store.list_lap_ids(self.track, self.car, car_class=cls))):
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
        # #214: this car's class only (#190); a count that can't be told is unknown.
        cls = self._class_scope() if self.track and self.car else False
        n_all = (len(self.store.list_lap_ids(self.track, self.car, complete_only=True,
                                             car_class=cls)) if cls is not False
                 else "unknown" if self.track and self.car else 0)
        n_sess = (len(self.store.session_lap_ids(self.session_id, complete_only=True))
                  if self.session_id else None)
        has_ref = bool(cls is not False
                       and self.store.best_lap(self.track, self.car, car_class=cls))
        # #190: the class stored laps are matched by (PB labels, history) is stated
        # too when it isn't the shown one -- REST may call the ELMS Oreca 'LMP2'.
        shown_class = self.car_class or "unknown"
        if self.match_class and _norm_class(self.match_class) != _norm_class(self.car_class):
            shown_class = (f"{self.car_class} (result log: {self.match_class})"
                           if self.car_class else f"{self.match_class} (result log)")
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
                # #67: arc['start'] is the first CAPTURED lap, never a start position.
                # #208: class leads in a multiclass race.
                rr = (self.store.race_session(self.session_id, cur_race["index"])
                      if cur_race else None)
                multi = _multiclass(arc["positions"], rr)
                first, low, _ = _arc_places(arc, multi, inner=True)
                last = _arc_places(arc, multi)[2]
                pos_txt = (f" | Position: {last} ({first} on the first captured lap"
                           f"{'; ' if multi else ', '}low {low})")
                if (multi and rr and rr["position"] is not None
                        and rr["class_position"] is not None):
                    # M1: once this race's log exists, its finish leads -- not the last
                    # lap's place: the post-flag in-lap samples no class place (committed
                    # in the garage stall), lap 22's P10 was a frame at the line whose
                    # order didn't hold, and a retirement's last lap isn't where the
                    # log classifies him (Road Atlanta: live P19, logged P22 DNF).
                    fin = _place(rr["position"], rr["class_position"], True)
                    status = rr["finish_status"]
                    # A retirement is never "finished": the log's status says what it was.
                    fin = (f"{fin}, {status}" if status and status != "Finished Normally"
                           else f"finished {fin}")
                    pos_txt = (f" | Position: {fin} per the result log; live laps: {first} "
                               f"on the first captured lap; low {low}")
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
                f"Class: {shown_class} | "
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
            race_lap = (r.get("phase") == "race" and r.get("position") is not None
                        and not _pre_green(r["phase"], r.get("lap_no")))
            pos = f" P{r['position']} overall" if race_lap else ""
            # #208: class leads in a multiclass race, and a race's first captured lap
            # with no grid slot says it isn't the start ("how's my race going" lands
            # here: the rig heard "race lap 1 ... P24 overall" from a P31 starter).
            place = self._race_place(r["lap_id"]) if race_lap and self.session_id else None
            if place is not None:
                pos = f" {place[0]}" + (place[1] if place[1] == _FIRST_CAPTURED else "")
            pit = " (pit)" if r.get("pit") else ""
            if r.get("mapping"):
                pit += f" — {_MAPPING_NOTE}"
            if _pre_green(r.get("phase"), r.get("lap_no")):
                pit += f" — {_PRE_GREEN_NOTE}"
            # A partial lap keeps its NUMBER (that is why it is stored) but has no
            # time to print and is not "INVALID" -- it is unfinished (#77).
            if r.get("partial"):
                out.append(f"{label}: {_PARTIAL_LIST_WORD[r['partial']]} (no time){pos}{pit}")
            elif r["lap_time"] is None:
                # #181: a complete lap LMU gave no time (live: invalidated; a result-log
                # row: any untimed lap, out-laps included) -- so no claim past that.
                out.append(f"{label}: {_UNTIMED_NOTE}{pos}{pit}")
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
                    + (", ".join(events) if events else "none")
                    + (f" ({OFFS_UNSCANNED} on this lap)"
                       if self._offs_unscanned(record, lap_id) else ""))
        analysis = self._analysis_for(record, lap_id)
        # #102: lead with the time-loss ranking (and surface its worst corners
        # first below) whenever a reference resolves -- a flat table buries the
        # finding the driver actually needs 15-30 lines down.
        ref, ref_label = self._reference_lap(record, lap_id, "best")
        if ref is None:
            # #169: with no comparable lap say so, rather than print deltas vs a lap
            # 16% slower (Long Beach #237).
            return (lap_line + (f"\n{ref_label}." if ref_label else "") + "\n"
                    + format_lap_facts(analysis, self.units))
        if ref_label.startswith(_NEXT_BEST):
            # #169: the best lap has no time-loss line -- vs a slower lap it is empty.
            faster = self.store.best_lap(record.track, record.car)
            which_best = ("your best lap this session" if faster is not None
                          and faster.lap_time < record.lap_time else "your best lap")
            return (lap_line + f"\nThis is {which_best}: get_time_on_table finds the time "
                    "left in it.\n" + format_lap_facts(analysis, self.units, ref_label=ref_label))
        buckets = time_loss_buckets(record, ref, self.cmap)
        if not buckets:
            return lap_line + "\n" + format_lap_facts(analysis, self.units, ref_label=ref_label)
        by_label = {corner_tag(cm): cm.corner_no for cm in analysis.corners}
        losses = sorted((b for b in buckets if b["kind"] == "corner" and b["delta"] > 0),
                        key=lambda b: -b["delta"])[:3]
        top = [by_label[b["label"]] for b in losses if b["label"] in by_label]
        order = top + [cm.corner_no for cm in analysis.corners if cm.corner_no not in top]
        body = format_lap_facts(analysis, self.units, corner_order=order, top_loss=set(top),
                                ref_label=ref_label)
        return lap_line + "\n" + format_time_loss(buckets, ref_label) + "\n\n" + body

    def _reference_lap(self, record, lap_id, reference="best"):
        """(record, label) for a time-loss comparison, or (None, label).

        'best' is the one reference `_analysis_for` also uses -- this session's
        quickest comparable lap first, all-time best as the fallback -- so
        get_time_loss and the per-corner 'min vs best' deltas never disagree about
        what "best" means. Never returns the subject lap itself. (None, None) with no
        other lap; (None, 'No comparable lap yet (...)') when the only ones fail the
        pace gate.
        """
        reference = (reference or "best").strip().lower()
        if reference not in ("best", ""):
            ref, _ = self._resolve_lap(reference)
            return ref, f"your {reference}"
        # #169: only a comparable lap is a reference (see `_comparable_rows`), and when
        # the subject is quicker than all of them it IS the best: the reference is
        # then its next-best lap, labelled so -- never "best".
        rows, slower = self._comparable_rows(record, lap_id)
        label_by_id, _ = self._lap_numbering()
        # Only a valid lap can BE the best: an invalid faster lap keeps "session best".
        can_be_best = bool(record.valid and record.lap_time)
        for row in rows:
            ref = self._load(row["id"])
            if ref is None:
                continue   # a result-log row with no telemetry
            if can_be_best and ref.lap_time > record.lap_time:
                return ref, (f"{_NEXT_BEST} ({label_by_id.get(row['id'], 'this session')}, "
                             f"+{ref.lap_time - record.lap_time:.2f} s)")
            return ref, "your session best"
        ref = self.store.best_lap(record.track, record.car, exclude_lap_id=lap_id)
        # This session's laps were all judged above; the lap itself is never its own.
        if ref is not None and (not ref.lap_time or ref.timestamp == record.timestamp or (
                self.session_id and ref.session_id == self.session_id)):
            ref = None
        if ref is not None and record.lap_time and ref.lap_time > _PACE_GATE * record.lap_time:
            slower = slower or {"lap_time": ref.lap_time, "id": None}
            ref = None
        if ref is None:
            if slower is not None:
                pct = (slower["lap_time"] / record.lap_time - 1.0) * 100.0
                name = label_by_id.get(slower["id"])
                return None, (f"No comparable lap yet ({name} is {pct:.0f}% slower)" if name
                              else f"No comparable lap yet (your other laps here are "
                                   f"{pct:.0f}%+ slower)")
            if self.session_id and any(
                    r["phase"] == "race" and r["lap_no"] == 1 and r["valid"] and r["lap_time"]
                    and r["id"] != lap_id for r in self._session_laps()):
                return None, ("No comparable lap yet (the only other lap is the race start, "
                              "race lap 1, which isn't a fair comparison)")
            return None, None
        if can_be_best and ref.lap_time > record.lap_time:
            return ref, (f"{_NEXT_BEST} (another session, "
                         f"+{ref.lap_time - record.lap_time:.2f} s)")
        return ref, "your all-time best"

    def _session_laps(self) -> list[dict]:
        """This session's lap rows, read once per context (each read resolves every
        row's car name -- ~60 ms a call on a real store, and the reference/comparison
        paths ask once per lap)."""
        if self._session_rows is None:
            self._session_rows = self.store.session_laps(self.session_id)
        return self._session_rows

    def _load(self, lap_id):
        """A stored lap's record, loaded once per context; None with no telemetry."""
        if lap_id not in self._records:
            try:
                self._records[lap_id] = self.store.load(lap_id)
            except Exception:
                self._records[lap_id] = None   # a result-log row with no telemetry
        return self._records[lap_id]

    def _comparable_rows(self, record, lap_id):
        """#169: (rows, slower). `rows` are this session's laps comparable to `record`,
        fastest first: valid, timed, complete, not a pit or mapping lap, not race lap 1
        (the start), and within 107% of its time (no pace gate for an untimed lap).
        `slower` is the fastest lap refused by the pace gate alone, for the 'No
        comparable lap yet' line; None when nothing was refused on pace."""
        if not self.session_id:
            return [], None
        rows, slower = [], None
        for r in sorted((r for r in self._session_laps()
                         if r["id"] != lap_id and r["ts"] != record.timestamp
                         and r["valid"] and r["lap_time"] and not r["pit"]
                         and not r["partial"] and not r["mapping"]
                         # The game's lap 1 of a race is the standing/rolling start.
                         and not (r["phase"] == "race" and r["lap_no"] == 1)),
                        key=lambda r: r["lap_time"]):
            if record.lap_time and r["lap_time"] > _PACE_GATE * record.lap_time:
                slower = slower or r
                continue
            rows.append(r)
        return rows, slower

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
        if not record.lap_time:
            # #181: a complete lap LMU didn't time (invalidated) -- no time to lose.
            return (self._lap_line(record, lap_id) + "\nThat lap has no lap time (LMU "
                    "didn't time it -- invalidated), so there's no time loss to measure "
                    "against the reference.")
        ref, ref_label = self._reference_lap(record, lap_id, reference)
        if ref is None:
            if ref_label and ref_label.startswith("No comparable"):
                return ref_label + "."
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
        ref_word = ("next-best" if ref_label.startswith(_NEXT_BEST)
                    else (reference or "best").strip().lower())
        ref_line = self._lap_line(ref, self._lap_id_for(ref)).removeprefix("Lap: ")
        identity = f"{self._lap_line(record, lap_id)} vs {ref_word}: {ref_line}"
        return identity + "\n" + format_time_loss(buckets, ref_label)

    def _incidents(self, record, lap_id) -> list[float]:
        """Lap distances of this lap's spins, stops, offs and track-limits strikes
        (#169: a sector or a corner segment a lap ran off in is never a best; #215: nor
        one LMU counted a time-gaining off in)."""
        return [e["lap_distance"] for e in
                lap_events(record, self.cmap, self._analysis_for(record, lap_id))
                if e["kind"] in ("spin", "stop", "off", "track_limits")]

    def _class_benchmark(self):
        """(class, 'm:ss.mmm', seconds) from the track notes' reference_times for this
        car's class, or None with no class, no time, or no single matching key."""
        refs = (getattr(self.cmap, "notes", None) or {}).get("reference_times") or {}

        def norm(x):
            return re.sub(r"[\s_-]+", "", str(x)).lower()
        cls = norm(self.match_class or "")
        if not cls:
            return None
        # Exact after normalising; else the game's short class name for a notes key:
        # 'GT3' -> 'LMGT3', 'Hyper' -> 'Hypercar'. Never a substring ('LMP2_ELMS' is
        # not 'LMP2').
        keys = ([k for k in refs if norm(k) == cls]
                or [k for k in refs if norm(k) in (f"lm{cls}", f"{cls}car")])
        if len(keys) != 1:
            return None
        txt = str(refs[keys[0]]).strip()
        try:
            m, _, s = txt.rpartition(":")
            return keys[0], txt, (int(m) * 60 if m else 0) + float(s)
        except ValueError:
            return None

    def _all_time(self, record, lap_id):
        """{"best", "this_lap", "when", "other"} over every timed lap here in this
        car (one query, `store.timed_laps`), or None with none. `other`: the best
        valid time per sector in OTHER sessions (None where there is none) -- the
        formatter builds the all-time optimal from it exactly the way it builds this
        session's. #190: in this lap's own class -- the Oreca 07 races as LMP2 AND
        LMP2_ELMS, 12 s apart at Sarthe."""
        rows = self.store.timed_laps(record.track, self.car or record.car,
                                     resolve_car_class(record.car))
        if not rows:
            return None
        best = min(rows, key=lambda r: r["lap_time"])
        label_by_id, _ = self._lap_numbering()
        other = [min((r[k] for r in rows if r[k] and r["session_id"] != self.session_id),
                     default=None) for k in ("s1", "s2", "s3")]
        return {"best": best["lap_time"],
                "this_lap": best["id"] == lap_id or best["ts"] == record.timestamp,
                "when": label_by_id.get(best["id"])
                or time.strftime("%Y-%m-%d", time.localtime(best["ts"])),
                "other": other}

    def _optimal_for_lap(self, record, lap_id):
        """(sectors, bounds, optimal, laps) for `record`: its own sectors, its sector
        bounds, `analysis.optimal_sectors` over it and its comparable laps (each with
        incidents), and the raw `(row_id, record, incidents)` comparable list
        `corner_form`/diagnoses also need. Shared by `get_time_on_table` and #176's
        `get_optimal_seconds`, so the comparable-set and incident rules live in
        exactly one place. `laps` is `[]` with no comparable lap -- the only case the
        callers need to branch on."""
        rows, _ = self._comparable_rows(record, lap_id)
        laps = []
        for row in rows:
            rec = self._load(row["id"])
            if rec is not None:
                laps.append((row["id"], rec, self._incidents(rec, row["id"])))
        if not laps:
            return None, None, None, []
        sess = {r["id"]: r for r in rows}
        # The subject's own sectors come with its record: a best lap from another
        # session (the all-time fallback) has them too.
        sectors = record.sectors if record.sectors and all(record.sectors) else None
        bounds = self.store.sector_bounds(record.track, record.car)
        optimal = optimal_sectors(
            [{"key": "this", "sectors": sectors, "incidents": self._incidents(record, lap_id)}]
            + [{"key": k, "sectors": [sess[k][s] for s in ("s1", "s2", "s3")], "incidents": inc}
               for k, _, inc in laps], bounds)
        return sectors, bounds, optimal, laps

    def get_optimal_seconds(self, which="best") -> float | None:
        """#176: the lap rail's Optimal row -- the same headline figure
        `get_time_on_table` states (`analysis.optimal_headline`), computed the same
        way, without the prose or the `corner_form`/diagnoses the rail doesn't show
        (cheaper than the full tool). None wherever `get_time_on_table` would have
        nothing to state for `which`: no such lap, no corner map (the full tool
        refuses without one too -- #176 can't honestly do better), no lap time, or no
        comparable lap this session."""
        record, lap_id = self._resolve_lap(which)
        if record is None or self.cmap is None or not record.lap_time:
            return None
        sectors, _, optimal, laps = self._optimal_for_lap(record, lap_id)
        if not laps:
            return None
        return optimal_headline(record.lap_time, sectors, optimal)

    def get_time_on_table(self, which="best", detail=False):
        """#169: where the time is on a lap -- above all the best lap -- measured
        against the driver's own comparable laps this session (see
        `format_time_on_table`)."""
        record, lap_id = self._resolve_lap(which)
        if record is None:
            return self._no_lap_msg(which)
        if self.cmap is None:
            return "No corner map for this track yet — can't break the lap into corners."
        lap_line = self._lap_line(record, lap_id)
        if not record.lap_time:
            return lap_line + "\nThat lap has no lap time (see the Lap line for why)."
        sectors, bounds, optimal, laps = self._optimal_for_lap(record, lap_id)
        if not laps:
            _, msg = self._reference_lap(record, lap_id)
            return lap_line + "\n" + (msg + "." if msg and msg.startswith("No comparable")
                                      else "No other lap this session to compare against yet.")
        label_by_id, _ = self._lap_numbering()
        form = corner_form(record, laps, self.cmap)
        diagnoses = []
        if form["ok"]:
            segs = form_segments(self.cmap)
            after = {id(c): (segs[i + 1][0] if i + 1 < len(segs) else None)
                     for i, (c, _, _) in enumerate(segs)}
            recs = {k: rec for k, rec, _ in laps}
            mine_a = self._analysis_for(record, lap_id)
            for r in form["listed"][:3 if detail else 2]:
                donor = recs[r["donor"]]
                phases = phase_split(record, donor, r["corner"], r["seg"])
                if phases is None:
                    continue
                i = self.cmap.corners.index(r["corner"])
                diagnoses.append({"row": r, "phases": phases, "next": after[id(r["corner"])],
                                  "you": mine_a.corners[i],
                                  "them": self._analysis_for(donor, r["donor"]).corners[i]})
        return lap_line + "\n" + format_time_on_table(
            lap_time=record.lap_time, sectors=sectors, optimal=optimal,
            all_time=self._all_time(record, lap_id), benchmark=self._class_benchmark(),
            form=form, diagnoses=diagnoses,
            label_of=lambda k: "this lap" if k == "this" else label_by_id.get(k, f"lap id {k}"),
            cmap=self.cmap, bounds=bounds,
            units=self.units)

    def _stint_analyses(self, phase=None):
        """(analyses, lap_times, incident) for one stint -- session + phase, valid laps
        only, telemetry-backed laps only, in lap order. Shared by `get_consistency` and
        `get_tire_trend`, which only differ in which pure function they feed it to.
        `incident[i]` flags a spin/stop/off/contact lap (#161: kept out of the pace trend).

        The stint starts after the LAST stop -- a pit, out- or in-lap, the same rule
        `RaceTracker.commit` uses (#161 red-team: Road Atlanta's puncture stop read as
        "deg-limited" across the tyre change)."""
        if phase is None:
            cur_rec, _ = self._current_lap()
            phase = cur_rec.phase if cur_rec is not None else None
        analyses, lap_times, incident = [], [], []
        stopped = False
        for row in self.store.session_laps(self.session_id):
            if phase is not None and row.get("phase") != phase:
                continue
            if row.get("pit") or row.get("partial") in ("out", "in"):
                # The next lap driven starts a new stint; until then (an in-lap back to
                # the garage) the stint just finished is still the one to read.
                stopped = True
                continue
            # Mapping laps (#154) are boundary weaves, not driving: out of the stint, as
            # they were when capture stamped them phase='mapping'.
            if not row["valid"] or row.get("mapping"):
                continue
            try:
                record = self.store.load(row["id"])
            except Exception:
                record = None   # a result-log row with no telemetry file -- nothing to analyse
            if record is None:
                continue
            analysis = self._analysis_for(record, row["id"])
            if stopped:
                analyses, lap_times, incident, stopped = [], [], [], False
            analyses.append(analysis)
            lap_times.append(record.lap_time)
            incident.append(is_incident_lap(record, self.cmap or CornerMap([]), analysis))
        return analyses, lap_times, incident

    def get_stint_events(self, laps=None, phase=None, race=None, which=None):
        """Incidents over the last N laps of this session, grouped by lap.

        Deliberately NOT filtered to valid laps -- a lap with a spin or an off is
        usually the INVALID one, which is exactly the lap the driver is asking
        about.

        #180: phase='race' is a race review -- the current (or most recent) race's
        racing laps only, never qualifying or race lap 0 (grid/formation), and by
        default the whole race rather than the last 10 laps of the session. Opt-in
        (the model passes it for a race question), not automatic: 'that spin a few
        laps ago' right after qualy must still reach the qualy lap.

        #187: race=N (1-based, store.race_windows' index -- #174's 'race N') reviews
        that race of a multi-race session instead of the latest; it implies
        phase='race'. #204: which='race N' says the same (_race_pick).

        #212: any other scan reads ONE phase's laps -- `phase` ('practice',
        'qualifying', 'warmup'), else the current lap's phase (the latest complete
        lap, as get_consistency) -- so a qualy question gets neither practice laps nor
        the race's lap 0 that follows it. Right after qualy (no complete race lap yet)
        the current lap is still qualy's: 'that spin a few laps ago' reaches it. A
        non-race phase reads its most recent consecutive run only (one session id
        spans the evening: an earlier qualy is not reachable by phase=); the race
        default, the current race's laps, lap 0 included. A defaulted scan also keeps
        every lap stored after that run (a new phase's out-lap, a spin partial) and
        says so in a header. The scope limits the LISTING, not the wear baseline: a
        pool under 3 laps can't flag a step (a 2-lap median holds the step lap), so it
        falls back to this session's recent complete laps."""
        if not self.session_id and not self.on_track:
            return "No active session — no stint to scan for incidents."
        if self.cmap is None:
            return "No corner map for this track yet — can't place incidents on corners."
        this_lap = self._this_lap_events()
        if not self.session_id:
            return this_lap or "No active session — no stint to scan for incidents."
        # #204: `which` picks a race only when it names one ('race 2', 'the race'); a
        # lap-ish which ('latest', 'last lap') is no race selector here and is ignored,
        # as before #204 -- the scan already covers the recent laps.
        race_which = which if "race" in str(which or "").lower().split() else None
        if phase is not None:
            phase = str(phase).strip().lower()
            if phase not in _PHASES:
                return f"phase must be one of {', '.join(_PHASES)} -- not {phase!r}."
        race_only = phase == "race" or race is not None or race_which is not None
        try:
            # A race review is the WHOLE race: `laps` is ignored, or laps=10 (what the
            # model sent in #279) drops the start of any race longer than 10 laps.
            n = _MAX_EVENT_LAPS if race_only else int(laps or 10)
        except (TypeError, ValueError):
            n = 10
        n = max(1, min(n, _MAX_EVENT_LAPS))
        rows = self.store.session_laps(self.session_id)   # ts order
        # #136: counting back N laps from the newest one walks straight out of this
        # race and into the previous one when a sitting holds several -- which is how
        # "that spin a few laps ago" came back as race 1's spin during race 3. Scan
        # this race only; an earlier race's laps stay reachable by name ('race 1 lap 2').
        cur_race = self._current_race()
        cut = []
        if race_only:
            if cur_race is None:
                return "No race in this session — no race laps to scan for incidents."
            picked, err = self._race_pick(race, race_which)
            if err:
                return err
            cur_race = picked or cur_race
            ids = set(cur_race["lap_ids"])
            rows = [r for r in rows if r["id"] in ids and not _pre_green("race", r["lap_no"])]
            if not rows:
                msg = ("The race hasn't started yet: only the grid/formation lap so far, "
                       "no racing laps to scan.")
                return msg + ("\n" + this_lap if this_lap else "")
            if len(rows) > n:
                cut.append(f"(last {n} of {len(rows)} race laps)")
        else:
            defaulted = phase is None
            if defaulted:
                # The latest COMPLETE lap's phase (as get_consistency); laps stored
                # after it are kept below, so a new phase's out-lap is not lost.
                cur_rec, _ = self._current_lap()
                phase = cur_rec.phase if cur_rec is not None else None
                if phase is not None:
                    cut.append(f"Scanned: the {phase} laps (the latest completed lap's "
                               "phase) + anything stored since.")
            if phase == "race" and cur_race is not None:
                ids = set(cur_race["lap_ids"])
                rows = [r for r in rows if r["id"] in ids
                        or (defaulted and r["ts"] > cur_race["end_ts"])]
            elif phase is not None:
                end = max((i for i, r in enumerate(rows) if r.get("phase") == phase),
                          default=None)
                if end is None:
                    msg = f"No {phase} laps in this session — nothing to scan."
                    return msg + ("\n" + this_lap if this_lap else "")
                start = end
                while start and rows[start - 1].get("phase") == phase:
                    start -= 1
                rows = rows[start:] if defaulted else rows[start:end + 1]
            elif cur_race is not None and len(self._race_windows()) > 1:
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
        pool = [rec for rec, _ in loaded]
        if len(pool) < 3:
            # This SESSION's recent complete laps -- by session_id alone: ctx.car is
            # the display name, laps are stored under the raw entry name (#212 r2).
            pool = [rec for row in self.store.recent_laps(
                        self.track, self.car, 10, session_id=self.session_id,
                        complete_only=True)
                    if (rec := self._load(row["lap_id"])) is not None]
        baseline = stint_wear_baseline(pool)
        slow = self._slow_lap_context(loaded)
        out = cut
        contact = False
        unscanned = 0
        for record, lap_id in loaded:
            lines = self._events_for(record, lap_id, baseline)
            unscanned += self._offs_unscanned(record, lap_id)
            out.append(self._lap_line(record, lap_id))
            # #143: contact is its own line, not folded into "none" -- a clean lap
            # (or one captured before #143, with no damage channels at all) says
            # nothing here rather than a "no damage" line on every lap.
            hits = damage_events(record.channels)
            damage_line = format_damage_line(hits, damage_state(record.channels), self.cmap)
            if slow:
                lines += [ln for ln in [self._slow_lap_line(record, lap_id, *slow)] if ln]
            out.extend(f"  {line}" for line in (lines or ["none"]))
            if damage_line:
                out.append(f"  {damage_line}")
                contact = True
                # #238: the car(s) nearest at the heaviest impact, from the stored field
                # trace; nothing when the lap has none (offline, practice, unplaced hit).
                heavy = max(hits, key=lambda e: e["magnitude"])["lap_distance"]
                laps = self.store.battle_laps(lap_id) if math.isfinite(heavy) else []
                near = contact_nearest(laps, self.cmap.corners, heavy) if laps else None
                if near:
                    out.append(f"  {near}")
        if unscanned:                # #207: once -- "none" above doesn't cover offs there
            which = "these laps" if unscanned == len(loaded) else (
                f"{unscanned} of these {len(loaded)} laps")
            out.append(f"({OFFS_UNSCANNED} on {which}: 'none' there does not rule out an off)")
        if this_lap:
            out.append(this_lap)
        if contact:                  # #206: once, not on every contact lap
            out.append(f"Note: {SUSPENSION_UNSEEN}.")
        return "\n".join(out)

    def _slow_lap_context(self, loaded):
        """#240: (median, best lap) over the scanned clean timed race laps (not the
        standing-start lap, out/in/pit/mapping laps, or any incident lap), or None
        under _SLOW_LAP_MIN_LAPS laps.
        ponytail: full-course yellows leave no flag channel in the lap data, so a
        lap-wide FCY slows the median pool too; add a yellow channel if capture gains one."""
        clean = [r for r, i in loaded
                 if r.phase == "race" and r.lap_time and not r.partial and not r.pit
                 and not r.mapping and (r.lap_no or 0) != 1 and not _pre_green(r.phase, r.lap_no)
                 and not is_incident_lap(r, self.cmap, self._analysis_for(r, i))]
        if len(clean) < _SLOW_LAP_MIN_LAPS:
            return None
        return (statistics.median(r.lap_time for r in clean),
                min(clean, key=lambda r: r.lap_time))

    def _slow_lap_line(self, record, lap_id, median, best) -> str | None:
        """#240: 'slow lap: +5.3 s vs the median of the scanned laps ...' for a race lap
        far off pace with no incident (spin, stop, off, track limits, contact), else
        None. Measured only: the concentrated losses vs the best scanned lap, each with
        its coasting / peak throttle where they differ materially from the best lap's.
        Never a cause."""
        # phase: the scan can append laps stored after the race (a practice cool-down),
        # which the race-only median says nothing about
        if (not record.lap_time or record.partial or record.pit or record.phase != "race"
                or record.mapping or (record.lap_no or 0) <= 1
                or is_incident_lap(record, self.cmap, self._analysis_for(record, lap_id))):
            return None
        off = record.lap_time - median
        if off <= max(_SLOW_LAP_MIN_S, _SLOW_LAP_FRAC * median):
            return None
        buckets = time_loss_buckets(record, best, self.cmap) or []
        top = sorted((b for b in buckets if b["delta"] >= _SLOW_LAP_BUCKET_S),
                     key=lambda b: -b["delta"])[:2]
        if not top or top[0]["delta"] < _SLOW_LAP_TOP_S:
            return None          # the loss is spread thin, not somewhere to point at
        parts = []
        for b in top:
            mine = window_inputs(record, b["start_dist"], b["end_dist"])
            ref = window_inputs(best, b["start_dist"], b["end_dist"])
            sig = []
            if mine and ref:
                if mine[0] - ref[0] >= _SLOW_LAP_COAST_M:
                    sig.append(f"{mine[0]:.0f} m with neither pedal (best lap: {ref[0]:.0f} m)")
                if ref[1] - mine[1] >= _SLOW_LAP_THROTTLE:
                    sig.append(f"max throttle {mine[1] * 100:.0f}% "
                               f"(best: {ref[1] * 100:.0f}%)")
            parts.append(f"{b['label']} ({b['delta']:.1f} s" + (", " if sig else "")
                         + ", ".join(sig) + ")")
        return (f"slow lap: +{off:.1f} s vs the median of the scanned laps "
                f"(+{record.lap_time - best.lap_time:.1f} s vs the best); most of the loss "
                f"vs the best lap at " + ", ".join(parts))

    def get_consistency(self, phase=None):
        if not self.session_id:
            return "No active session — no stint to measure consistency across."
        analyses, lap_times, _ = self._stint_analyses(phase)
        out = format_consistency(stint_consistency(analyses, lap_times), self.units)
        if self.cmap is None:
            out += " Per-corner consistency needs a corner map for this track (none yet)."
        return out

    def get_tire_trend(self, phase=None):
        if not self.session_id:
            return "No active session — no stint to measure tire trend across."
        analyses, lap_times, incident = self._stint_analyses(phase)
        return format_tire_trend(stint_tire_trend(analyses, lap_times, incident), self.units)

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
        """This session's current/latest lap's analysis, or None with no lap --
        used to seed `list_track_corners`' spoken locators with
        real measured speeds rather than geometry alone."""
        record, lap_id = self._current_lap()
        if record is None:
            return None
        return self._analysis_for(record, lap_id)

    def list_track_corners(self):
        if self.cmap is None or not self.cmap.corners:
            return ("No corner map for this track yet — no corner list available. General "
                    "knowledge of the track's character is fine (say it's general, not "
                    "the driver's data); no turn numbers, names or directions from memory.")
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
        # #207: offs are read from the wheel surface; none on any corner row = unmeasured.
        offs_note = (f"  ({OFFS_UNSCANNED} on this lap so far)"
                     if all(r["facts"]["metrics"].get("off_surface") is None for r in rows)
                     else None)
        if scan is None:
            # Never "no incidents" off a partial look: only corner windows were read.
            out.extend(f"  {line}" for line in incidents)
            out.append("  (spins and hits outside corner windows: not scanned)")
            return "\n".join(out + ([offs_note] if offs_note else []))
        incidents += incident_lines(scan, self.cmap, self.units)
        if incidents:
            out.extend(f"  {line}" for line in incidents)
        # #210: a buffer still behind the line (an out-lap before it) reads a negative
        # lap distance -- "scanned to -241 m" is no measured reach; the line is dropped.
        elif (scan.get("scanned_to") or -1) >= 0:
            out.append(f"  no incidents so far (whole lap scanned to "
                       f"{scan['scanned_to']:,.0f} m)")
        if offs_note:
            out.append(offs_note)
        return "\n".join(out)

    def get_car_status(self):
        """#160: the live car, gated like the race tools to the live session."""
        if not self.on_track:
            return ("No live session: car status is read live from the game, only while "
                    "you're in a session.")
        if self.car_status is None:
            return ("Car status isn't readable yet: no lap under way in this session since "
                    "it (re)started.")
        # #253: "no impacts" beside three spins read as "nothing happened" -- the lap's
        # measured spins and stops travel with it (stated only when there are some), and
        # lead it: rig 2026-10-29, listed after the damage, "I lost it" got the dent read
        # and no word of the 9 s spin.
        events = [e for e in ((self.live_lap or {}).get("incidents") or {}).get("events", [])
                  if e["kind"] in ("spin", "stop")]
        if not events:
            return self.car_status
        lines = format_events(events, self.cmap, self.units)
        head, _, rest = self.car_status.partition("\n")
        return "\n".join([head, (f"Spins and stops since the start of this lap ({len(lines)}) "
                                 "-- lead with these when he says he lost it or went off:")]
                         + [f"  {line}" for line in lines] + ([rest] if rest else []))

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
        ref, ref_label = self._reference_lap(record, lap_id, "best")
        if ref_label:
            # #169: "vs best" on the line above means this reference -- the next-best
            # lap when this one is the best.
            line += (f"\nDeltas ('vs best') are vs {ref_label}."
                     + (SLOWER_REF_NOTE if ref_label.startswith(_NEXT_BEST) else "")
                     if ref is not None
                     else f"\n{ref_label}.")
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
        analyses, _, _ = self._stint_analyses()
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
            # #168: an empty result read as "say nothing" ("I've got nothing loaded for
            # Long Beach yet", turn #236) -- the hint licenses framed general knowledge.
            return (load_track_knowledge(self.track)
                    or f"No notes on file for {self.track or 'this track'} — general "
                       "knowledge of the track is fine: say it's general, not the "
                       "driver's data, with no turn numbers, names or directions from "
                       "memory.")
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
        if record is None:
            return "No lap captured yet."
        analysis = self._analysis_for(record, lap_id)
        report = format_tire_report(analysis, self.units)
        return (self._lap_line(record, lap_id) + "\n"
                + (report or "No tire-temperature data was captured for this lap."))

    def get_wheel_report(self):
        record, lap_id = self._current_lap()
        if record is None:
            return "No lap captured yet."
        analysis = self._analysis_for(record, lap_id)
        report = format_wheel_report(analysis, self.units)
        # #108: a wear STEP on one lap at one corner is a flat spot, not deg. The
        # wear line above reads the same number as a stint slope and gets blamed on
        # cornering load, so name the step next to it.
        steps = [line for line in self._events_for(record, lap_id) if "wear step" in line]
        if self.cmap is None:
            steps = [("Per-corner wear steps (flat spots) need a corner map for this track "
                      "(none yet) — not checked.")]
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

    def get_race_result(self, race=None, which=None):
        # #204: race=N / which='race N' reads that race of the sitting (_race_pick);
        # none named is the latest, as before.
        picked, err = self._race_pick(race, which)
        if err:
            return err
        n = picked["index"] if picked else None
        # Name the track of the session these figures DESCRIBE, taken from that session's
        # own laps -- never self.track, which follows the live game or the last capture
        # and can be another track entirely (reviewing Silverstone while the game sat at
        # Fuji labelled Silverstone's result "Fuji"). A resumed chat's race retraction lets
        # the model use these figures only for the same race, so a wrong label is worse
        # than none.
        text = race_result_text(self.store, self.session_id, n)
        # #209: no positions and no row is no race to review -- nothing to wait for.
        waited = None if text.startswith("No race result") else self._await_result_log(picked, n)
        if waited is False:   # the log landed during the wait
            text = race_result_text(self.store, self.session_id, n)
        laps = self.store.session_laps(self.session_id) if self.session_id else []
        track = laps[0].get("track") if laps else None
        head = [f"Session track: {track}." if track else ""]
        # #136: WHICH race of the sitting these figures describe. race_result_text
        # already bounds the arc to one race (race_ts..next_race_ts); this names it,
        # so an answer about race 3 can't be read as being about race 1.
        rr = self.store.race_session(self.session_id, n) if self.session_id else None
        win = picked or self._race_window_at(rr.get("race_ts") if rr else None)
        head.append(self._race_line(win))
        # #168: "How was that race?" after the flag got "now that you've pitted ... want
        # me to check the strategy?" (turn #245): nothing said the race was over. A
        # result row exists only once a race is finished (race_result_text drops it while
        # a later race runs); with none, no live race means the arc's race is over too.
        # A live race-phase session with no fresh snapshot (before the first commit, a
        # sidecar restart mid-race) carries race_note instead: still live, not over.
        if rr:
            text += ("\nThis race is FINISHED (the result log is written after the flag): "
                     f"talk about it as a finished race -- {_COOL_DOWN_RULE}; no pit-stop "
                     "or strategy offers.")
        elif text.startswith("No race result"):
            pass
        elif picked is not None and picked is not self._current_race():
            text += ("\nA later race of this session followed it, so this race is over "
                     "(its result log isn't read): talk about it as a finished race -- "
                     f"{_COOL_DOWN_RULE}; no pit-stop or strategy offers.")
        elif fin := self._finish_status():
            # #172: on the cool-down lap -- on track, a fresh snapshot, no row yet.
            text += (f"\nYou have {fin} (the game's live finish status), so this race is "
                     "over (its result log isn't read yet): talk about it as over, not "
                     f"live -- {_COOL_DOWN_RULE}; no pit-stop or strategy offers.")
        elif not self.on_track or (self.race is None and not self.race_note):
            text += ("\nNo race is live now, so the race these positions describe is over "
                     "(its result log isn't read yet): talk about it as a finished race -- "
                     f"{_COOL_DOWN_RULE}; no pit-stop or strategy offers.")
        if waited:
            # Measured: no result row is LINKED after the wait -- not that LMU hasn't
            # written it (no alias, a name/car/mode mismatch). LMU writes the log when the
            # driver leaves the session (ingest.POLL_INTERVAL_S), so re-asking while still
            # in it gets nothing new.
            text += ("\nThe result log isn't imported yet -- this review is PROVISIONAL (no "
                     "class result, grid slot or car-to-car contacts). LMU writes the log "
                     "when the driver leaves the session; tell them to ask again after that.")
        return "\n".join([h for h in head if h] + [text] + self._race_lap_positions(win, rr))

    def _await_result_log(self, picked, n) -> bool | None:
        """#209: the latest race is over with no live race attached (get_race_result's
        'No race is live now' case) and its result log isn't imported yet: nudge the
        importer and re-check until it is, bounded (see _RESULT_LOG_WAIT_S). None: no
        wait. True: the log still isn't in after a wait -- this one, or (once per
        context: a re-call must not block the turn again) an earlier one. False: it
        landed during this wait."""
        win = self._current_race()
        if (self.scan_results is None or win is None or picked not in (None, win)
                or (self.on_track and (self.race is not None or self.race_note))
                or self.store.race_session(self.session_id, n) is not None):
            return None
        if self._result_log_waited:
            return True
        end = min(time.time() + _RESULT_LOG_WAIT_S, win["end_ts"] + _RESULT_LOG_RECENT_S)
        if time.time() >= end:
            return None
        self._result_log_waited = True
        while time.time() < end:
            self.scan_results()
            time.sleep(_RESULT_LOG_POLL_S)
            if self.store.race_session(self.session_id, n) is not None:
                return False
        return True

    def _race_lap_positions(self, win, rr=None) -> list[str]:
        """#180: the race's per-lap positions from race lap 1 (session_laps rows, no
        telemetry), so a race recap has the arc even when the model never makes the
        race-scoped incident call -- and the pointer to that call. `win`: the race
        window get_race_result describes (the same one `_race_line` names).
        #204: with a result row `rr`, also only laps inside its race_ts..next_race_ts,
        as the arc is -- a legacy window (no lap_no to split on, Monza 2026-09-07) can
        hold two races."""
        if win is None:
            return []
        ids = set(win["lap_ids"])
        start = rr.get("race_ts") if rr else None
        end = rr.get("next_race_ts") if rr else None
        label_by_id, _ = self._lap_numbering()
        rows = [r for r in self._session_laps()
                if r["id"] in ids and r.get("position") is not None
                and not _pre_green(r["phase"], r.get("lap_no"))
                and (start is None or r["ts"] >= start) and (end is None or r["ts"] < end)]
        if not rows:
            return []
        # #181: a whole lap LMU invalidated (an off, a cut) has no time, like a cool-down
        # -- name it, so an untimed last lap is never read as the lap after the flag.
        # Live capture only: a result-log row with no time may be anything untimed.
        bad = [r.get("source") == "live" and not r["valid"] and not r.get("partial")
               and r["lap_time"] is None for r in rows]
        # #204: a lap and its in-lap share a label ('race 3 lap 9' twice after the
        # flag); the repeat names its kind. Tagged, not deduped: at Long Beach
        # (2026-09-24, DNF) the lap-4 in-lap is P14 after lap 4's P12.
        # #208: a multiclass race lists class, the lap's overall place in brackets --
        # when the laps carry a class place at all (older laps never sampled one).
        multi = (_multiclass(((r["position"], r.get("class_position")) for r in rows), rr)
                 and any(r.get("class_position") is not None for r in rows))
        laps, seen = [], set()
        for r, b in zip(rows, bad):
            name = label_by_id.get(r["id"], "lap id " + str(r["id"]))
            kind = _PARTIAL_WORD.get(r.get("partial"), "") if name in seen else ""
            seen.add(name)
            cls = r.get("class_position") if multi else None
            place = (f"P{cls} (P{r['position']})" if cls is not None
                     else _place(r["position"], None, True) if multi else f"P{r['position']}")
            laps.append(f"{name} {place}" + (" (invalidated)" if b else "")
                        + (f" ({kind})" if kind else ""))
        # #204: the header names where the list starts -- capture can miss race lap 1,
        # and a legacy window's second race numbers on from the first.
        first = label_by_id.get(rows[0]["id"], "lap id " + str(rows[0]["id"]))
        start = "race lap 1" if first.endswith(" lap 1") else first
        which = "in class, the overall place in brackets" if multi else "overall"
        out = [f"Per-lap positions ({which}) from {start}: {', '.join(laps)}."]
        if any(bad):
            out.append("An invalidated lap was RACED -- a racing lap LMU didn't time (an off "
                       "or a cut), never the cool-down.")
        # #187: with several races the pointer names this one -- phase='race' alone reads
        # the latest, which is not this race when the result log is an earlier race's.
        sel = f", race={win['index']}" if len(self._race_windows()) > 1 else ""
        pointer = ("Lap-by-lap incidents for this race: get_stint_events with "
                   f"phase='race'{sel}.")
        return out + [pointer]

    def _no_live_race(self) -> str | None:
        """#152 guard for the race tools: a snapshot exists only in a live race, from its
        first completed lap. Never a guess."""
        if self.on_track and self.race is None and self.race_note:
            return self.race_note
        if not self.on_track or self.race is None:
            return ("No live race: race state, strategy and live battle reads exist only during "
                    "a live race session, from its first completed lap.")
        return None

    def _live(self):
        """#186: (field, race_sample, the player's VehicleState) of one scoring frame
        read as the tool answers -- the class ranked by `race_sample`, exactly as the lap
        commit ranks it. One bounded shared-memory copy (read_field_snapshot never
        raises, and reads a dead buffer as not live, #147). None -- the snapshot is said,
        as of its lap -- with no reader, no live placed player, not a race, or a frame
        from another race than the snapshot's (same lap rule as race.live_race)."""
        if self.live_field is None or self.race is None:
            return None
        try:
            field = self.live_field()
            sample = race_sample(field)
            me = next((c for c in field.cars if c.is_player), None)
            lap = field.session.current_lap
        except Exception:
            return None
        if (sample is None or sample.class_position is None or me is None
                or field.session.phase != "race" or lap is None
                or lap - 1 not in (self.race.as_of_lap, self.race.as_of_lap + 1)):
            return None
        return field, sample, me

    def _finish_status(self) -> str | None:
        """#172: what the player's live finish status says he has done, once his race is
        over -- read from one #186 live frame (lmu.live: mFinishStatus is 1 from the
        line at the flag through the cool-down lap). None: still racing (0), an unknown
        value, no reader, off track, or not a race."""
        if not self.on_track or self.live_field is None:
            return None
        try:
            sess = self.live_field().session
        except Exception:
            return None
        return _FINISH_WORDS.get(sess.finish_status) if sess.phase == "race" else None

    def _race_over(self) -> str | None:
        """#172: the race tools' answer once the live finish status ends the race."""
        fin = self._finish_status()
        return (f"This race is over: you have {fin} (the game's live finish status). No "
                "race state, pit stops or strategy to offer -- get_race_result reviews it."
                if fin else None)

    def get_race_state(self):
        if over := self._race_over():
            return over
        text = self._no_live_race()
        if text is None:
            live = self._live()
            text = format_race_state(self.race, (live[2].place, live[1]) if live else None)
            laps = rival_laps_line(live[0]) if live else None
            if laps:
                text += "\n" + laps
        # #162: the game's live penalty count (lmu.live.SessionState.penalties), read
        # when the question came in. What a penalty is for only reaches us in the result
        # log after the race (get_race_result). Only a positive count is said: the channel
        # is unverified at the rig, and a 0 from a channel LMU never fills would read as
        # "none outstanding" -- the dead-channel failure (mGripFract). A c_short outside
        # 1..99 is garbage, not a count.
        sess = self.live_session
        n = getattr(sess, "penalties", None)
        if (self.on_track and sess is not None and sess.phase == "race"
                and isinstance(n, int) and 0 < n < 100):
            text += (f"\nPenalties: {n} outstanding (the game's live count; what it is for "
                     "is not reported until the race's result log).")
        return text

    def get_strategy(self, race_laps=None, race_minutes=None):
        if not self.on_track:
            return self._no_live_race()
        if over := self._race_over():
            return over
        if self.race is not None and self.race.strategy.note is None:
            rec = self._race_lap_record()
            return format_strategy(self.race, *((damage_state(rec.channels), rec.lap_no)
                                                if rec is not None else (None, None)))
        return self._provisional_strategy(race_laps, race_minutes)

    def _live_instance(self) -> str | None:
        """The session id the LIVE laps are stored under. live_lap carries the BASE id
        (smm session_id_for); the store files laps under this weekend's instance
        (`base~YYYYMMDDTHHMM` after a 6 h gap), so the base is resolved -- read-only --
        at the live lap's start time. Without a live lap: the chat's session id.
        #161 red-team round 2: the bare base read ANOTHER weekend's race lap."""
        m = self.live_lap or {}
        if m.get("session_id"):
            try:
                return self.store.resolve_current_instance(m["session_id"], m.get("start_ts"))
            except Exception:
                return None   # never fall back to the bare base: that is another weekend
        return self.session_id

    def _live_row(self, row) -> bool:
        """A lap row of the car being driven NOW: the live track, the live car (raw entry
        name or resolved model), and -- with a live lap -- stored within the store's own
        session gap before that lap started (`start_ts` is the live LAP's start, smm
        `_now()` at its first sample; a stored lap's ts is its end). #161 red-team round
        3: an old-weekend chat as the newest activity resolves to the OLD instance, and
        its race's lap 14 (a flat FL) was stated as this race's."""
        if ((self.track and row.get("track") != self.track)
                or (self.car and self.car not in (row.get("car"), row.get("car_model")))):
            return False
        if not self.live_lap:
            return True
        # No start_ts (smm always sets one): the question is being asked now, mid-lap.
        start = self.live_lap.get("start_ts") or time.time()
        # +60 s: the lap just committed is stamped at its line crossing, the same moment
        # the live lap starts -- never let clock jitter drop it.
        return start - _SESSION_GAP_S <= row["ts"] <= start + 60

    def _race_lap_record(self):
        """The stored lap the live race snapshot is as of (the newest race row of the
        live session with that lap number), or None. A practice lap never stands in:
        its damage was repaired in the garage and its fuel is not what is on board."""
        if self.race is None:
            return None
        sid = self._live_instance()
        rows = [r for r in (self.store.session_laps(sid) if sid else [])
                if r.get("phase") == "race" and r.get("lap_no") == self.race.as_of_lap
                and self._live_row(r)]
        try:
            return self.store.load(rows[-1]["id"]) if rows else None
        except Exception:
            return None

    def _provisional_strategy(self, race_laps, race_minutes) -> str:
        """#161: before the race stint has MIN_STINT_LAPS clean laps -- on the grid, in
        practice/qualifying, or race lap 1 -- plan from this session's clean practice/
        qualifying burn rates and the race length (the live race's, or the one the driver
        gave). Every figure is computed in race.py; this only gathers the inputs."""
        def no(why):
            return (f"No live race projection yet (it needs {MIN_STINT_LAPS} clean race "
                    f"laps in this stint), and no provisional plan: {why}.")
        sess = self.live_session
        if (sess is not None and sess.phase == "race" and self.race is None
                and (getattr(sess, "current_lap", None) or 0) > 1):
            # #161 red-team: mid-race with no snapshot (a sidecar restart, a stale one)
            # nothing on board is known -- never plan it from lap 0 on a full fill.
            return self.race_note or NO_RACE_LAP_YET
        # The LIVE session's laps (the chat may carry another session the UI had
        # selected), and only this track and car (#161 red-team: a Fuji plan at Road
        # Atlanta).
        sid = self._live_instance()
        practice = []
        for row in self.store.session_laps(sid) if sid else []:
            if row.get("phase") == "race" or row.get("mapping"):
                continue
            if not self._live_row(row):
                continue
            try:
                f = _lap_reads(self.store, row["id"])[0]
            except Exception:
                continue   # a result-log row with no telemetry
            if f.clean and (f.fuel_used or f.energy_used):
                practice.append((row.get("phase"), f))
        phases = {ph for ph, _ in practice[-RATE_LAPS:]}
        practice, source, notes, energy_sessions = (
            [f for _, f in practice[-RATE_LAPS:]], None, [], {sid})
        ve = None
        cls = self._stored_class()
        if len(practice) >= MIN_STINT_LAPS:
            recent = practice[-PACE_LAPS:]
            pace = sum(f.lap_time for f in recent) / len(recent)
            at_pace = f"at your practice pace of {format_laptime(pace)}"
            # #184 red-team R2: VE per lap follows the fuel ratio, and the pit menu may
            # set another for the race. Once the race sitting shows its fill (a garage or
            # grid row) and a practice sitting used here filled differently, its VE/lap
            # would be off by the change: not projected. Fuel L/lap still holds.
            fills, _ = self._fills([], sid)
            race = fills.get((sid, "race"))
            off = sorted((fills[(sid, ph)], ph) for ph in phases
                         if race and fills.get((sid, ph))
                         and abs(fills[(sid, ph)] / race - 1) > FILL_TOL)
            if off:
                practice = [replace(f, energy_used=None) for f in practice]
                ve = True
                notes.append(
                    f"Virtual energy: not projected -- your race fill is at fuel ratio "
                    f"{race:.2f} L per 1% (read off the tank on the grid), your "
                    + " and ".join(f"{ph} laps ran at {r:.2f}" for r, ph in off)
                    + "; virtual energy per lap changes with the ratio. Fuel per lap "
                    "still holds.")
        else:
            # #184 (Sarthe 2026-09-26: one qualy lap, 'that doesn't help me'): this car's
            # stored burn at this track, every session -- practice, qualifying AND race.
            hist, read, stored = self._burn_history(cls)
            med, notes, energy_sessions = history_pick(hist, *self._fills(hist, sid))
            if med is None:
                return no(f"it needs {MIN_STINT_LAPS} clean laps with a measured fuel/energy "
                          f"burn at this track in this car (found {len(hist)} across all "
                          "your sessions here)")
            # The mean of copies is the median: the same strategy math, on the medians.
            # A VE car stays one when its VE isn't projected: fuel is set by the ratio.
            practice, pace = [med] * MIN_STINT_LAPS, med.lap_time
            ve = any(h.facts.energy_used for h in hist)
            at_pace = f"at {format_laptime(pace)} a lap"
            days = sorted({time.strftime("%Y-%m-%d", time.localtime(h.ts)) for h in hist})
            kinds = Counter(h.phase or "unlabelled" for h in hist)
            source = (f"medians of {len(hist)} clean laps stored here in this car "
                      f"({', '.join(days)}: "
                      + ", ".join(f"{v} {k}" for k, v in sorted(kinds.items())) + ")"
                      + (f", the newest found in {read} of {stored} stored laps"
                         if read < stored else ""))
        start, lap_no, damage, damage_lap = None, 0, None, None
        if self.race is not None:
            # A race lap is done: plan from what was on board at ITS line, never lap 0.
            rec = self._race_lap_record()
            if rec is None:
                return no(f"race lap {self.race.as_of_lap} isn't readable from the store")
            start, lap_no = lap_facts(rec), rec.lap_no
            damage, damage_lap = damage_state(rec.channels), rec.lap_no
        timed = clock_out = False
        if race_laps is not None or race_minutes is not None:
            try:
                if isinstance(race_laps, bool) or isinstance(race_minutes, bool):
                    raise TypeError      # True is not a race length
                if race_laps is not None:
                    total = int(race_laps)
                    if total != float(race_laps):
                        raise ValueError     # 43.9 laps is not a race length
                else:
                    total = laps_left(0, None, float(race_minutes) * 60, pace)
                if not 0 < total <= 1000:
                    raise ValueError
            except (TypeError, ValueError, OverflowError):
                return no("race_laps / race_minutes must be a positive number")
            to_go, timed = max(total - lap_no, 0), race_laps is None
            basis = (f"{total} lap{'s' if total != 1 else ''} (as you said)" if not timed
                     else f"{float(race_minutes):g} minutes (as you said) {at_pace} = "
                          f"{flag_counts(total, total)}")
        elif sess is not None and sess.phase == "race":
            # #184: before race lap 1 is done the clock is not the race length -- in the
            # race session's garage/grid it counts down to the start (Sarthe 2026-09-26,
            # a 20-minute race: 58, 26.8, 8.0 s left on its lap-0 rows). Ask instead.
            clock = (sess.end_time - sess.elapsed) if sess.end_time > 0 and lap_no else None
            to_go = laps_left(lap_no, sess.max_laps, clock, pace)
            by_laps = sess.max_laps and to_go == max(sess.max_laps - lap_no, 0)
            timed = not by_laps and clock is not None
            # #195: the live plan's rule (race.plans_longer), so the two never differ.
            pos = self.race.state.position if self.race is not None else None
            clock_out = timed and clock <= 0
            if clock_out and pos == 1:
                return f"As of race lap {lap_no}. No projection: {RACE_OVER.format(lap_no)}."
            basis = (f"{sess.max_laps} laps (the live race's lap count)" if by_laps else
                     None if not timed else
                     f"the clock ran out before the end of lap {lap_no}" if clock_out else
                     f"{_clock(clock)} on the clock {at_pace} = "
                     + (f"{to_go} lap{'s' if to_go != 1 else ''} to go: "
                        f"{YOU_LEAD.format(lap_no)}" if pos == 1
                        else f"{flag_counts(to_go, lap_no + to_go)} to go"))
            timed = timed and plans_longer(clock, pos)
        else:
            to_go = basis = None
        if to_go is None or basis is None:
            return no("race length unknown. Ask the driver how long the race is, then "
                      "call get_strategy again with race_laps or race_minutes")
        # A timed race is planned on the longer count (flag_counts): running dry is the
        # costly error. Before the start, the formation lap comes off the assumed fill.
        formation = None if start is not None else self._formation(cls, energy_sessions)
        return format_provisional(
            provisional_strategy(practice, to_go + timed, start, ve, formation), lap_no,
            basis, len(practice), damage, damage_lap, source, notes,
            measured=start is not None, timed=timed, ve_car=bool(ve), formation=formation,
            clock_out=clock_out)

    def _stored_class(self, car=None):
        """#184: the stored class this car's laps are filed under (store.timed_laps'
        `car_class`; None = no scraped class), or False when it can't be told: a car is a
        (model, class) pair -- the Oreca 07 races as LMP2 AND LMP2_ELMS, and their burn
        must never merge (#182). The live class picks one stored class; with the live
        class unknown, only a model stored in exactly one class is read. #190: `car`
        (default this car) may name another model a tool was asked about."""
        car = car or self.car
        if not (self.track and car):
            return False
        classes = {g["car_class"] for g in self.store.session_facets()["car_groups"]
                   if car in g["cars"]}
        mine = [c for c in classes
                if not self.match_class or _norm_class(c) == _norm_class(self.match_class)]
        if len(mine) != 1:
            return False
        return None if mine[0] == "Other" else mine[0]

    def _class_scope(self, car=None):
        """#190: the stored class a cross-session read of `car` (default this car) is
        narrowed to: a class, None when there is nothing to narrow (the model is stored
        in at most one class), or False when it is stored in several and which one is
        unknown -- the caller then refuses rather than mix them, like #184's history."""
        cls = self._stored_class(car)
        if cls is not False:
            return cls
        car = car or self.car
        return False if sum(car in g["cars"] for g in
                            self.store.session_facets()["car_groups"]) > 1 else None

    def _mixed_classes(self, car):
        """#190: what a cross-session read says instead of mixing a model's classes."""
        classes = sorted(g["car_class"] for g in self.store.session_facets()["car_groups"]
                         if car in g["cars"])
        return (f"Can't tell which class of {car} this is: it is stored as "
                f"{' and '.join(classes)}, and their data must not be mixed.")

    def _burn_history(self, cls) -> tuple[list, int, int]:
        """#184: (the newest HISTORY_LAPS clean laps with a measured fuel/energy burn at
        this track in this car, from every session, as race.HistLap; laps read; laps
        stored). Stops after HISTORY_LOADS reads (latency) even with fewer."""
        if cls is False:
            return [], 0, 0
        rows = sorted((r for r in self.store.timed_laps(self.track, self.car, cls)
                       if r["telemetry"]), key=lambda r: r["ts"], reverse=True)
        out, read = [], 0
        for row in rows[:HISTORY_LOADS]:
            read += 1
            try:
                f, _, _, meta = _lap_reads(self.store, row["id"])
            except Exception:
                continue
            if f.clean and (f.fuel_used or f.energy_used):
                out.append(HistLap(*meta, f))
                if len(out) == HISTORY_LAPS:
                    break
        return out, read, len(rows)

    def _fills(self, hist, sid) -> tuple[dict, float | None]:
        """#184: ({(session, phase): fuel ratio}, the live session's ratio or None) for
        the sittings of `hist` and of the live session -- race.fill_ratio over the
        departures of each sitting's pit/out/in/partial rows, where fills happen (at most
        FILL_ROWS a sitting, newest first). Only those: a ratio read off clean laps alone
        drifts +11% / -4% (the 09-03..09-13 sittings, stored before those rows were), so
        a sitting without one is "not recorded" and no ratio is ever stated for it. The
        live ratio is its newest sitting's that shows one: a grid or out-lap row is
        enough, no clean lap needed."""
        deps: dict = defaultdict(list)
        live = [r for r in self.store.session_laps(sid) if self._live_row(r)] if sid else []
        mine = list(dict.fromkeys((sid, r["phase"]) for r in reversed(live)))
        want = {(h.session, h.phase) for h in hist} | set(mine)
        for session in {k[0] for k in want}:
            seen: Counter = Counter()
            for r in reversed(live if session == sid else self.store.session_laps(session)):
                key = (session, r["phase"])
                if (key not in want or r.get("mapping") or seen[key] >= FILL_ROWS
                        or not (r.get("pit") or r.get("partial"))):
                    continue
                seen[key] += 1
                try:
                    deps[key].append(_lap_reads(self.store, r["id"])[1])
                except Exception:
                    continue    # a result-log row: no telemetry
        fills = {k: fill_ratio(v) for k, v in deps.items()}
        return fills, next((fills[k] for k in mine if fills.get(k)), None)

    def _formation(self, cls, energy_sessions):
        """#184: race.pick_formation over this car's stored formation laps here, or None
        with none. The formation row is the one committed just before race lap 1 of the
        same race -- never a pit row (it spans garage fuel changes: Long Beach 2026-09-24
        read 99 -> 56.7 L) nor a countdown row, whose clock is below lap 1's (Sarthe
        2026-09-26: 8.0 s against lap 1's 949 s)."""
        if cls is False:
            return None
        rows = self.store.race_start_laps(self.track, self.car, cls)
        found = [a for a, b in pairwise(rows)
                 if a["lap_no"] == 0 and b["lap_no"] == 1 and a["session_id"] == b["session_id"]
                 and not a["pit"] and a["telemetry"]
                 and (a["time_left"] is None or b["time_left"] is None
                      or a["time_left"] >= b["time_left"])]
        out = []
        for r in reversed(found[-HISTORY_LAPS:]):          # newest first
            try:
                burn = _lap_reads(self.store, r["id"])[2]
            except Exception:
                continue
            out.append((r["ts"], r["session_id"], burn))
        return pick_formation(out, energy_sessions) if out else None

    def get_battle(self, rival=None, which=None, direction=None, position=None, race=None):
        """#174: in a live race, the last completed lap's cached read (unchanged). With
        no race live -- or when the driver names a stored lap or 'race' -- the read is
        rebuilt from the battle_laps rows stored at each race lap's commit.
        #185: `direction` ('ahead'/'behind') or `position` (a class position) picks the
        rival instead of the nearest car.
        #204: race=N is the stored read of race N as a whole, like which='race N'."""
        direction = str(direction or "").strip().lower() or None
        if direction not in (None, "ahead", "behind"):
            return f"direction must be 'ahead' or 'behind', not {direction!r}."
        if position is not None:
            if isinstance(position, float) and position.is_integer():
                position = int(position)
            n = "".join(str(position).split()).upper().removeprefix("P")
            if not n.isdecimal() or int(n) < 1:
                return f"position must be a class position like 3, not {position!r}."
            position, direction = int(n), None
        if direction or position:
            rival = None    # the place asked for, never a name's race window
        which = (which or "").strip().lower()
        live = self._no_live_race()
        if live is None and race is None and which in ("", "latest", "last lap", "current",
                                      "last completed lap"):
            # #186: the read is the lap's field (#185). Once his class place has changed
            # since, the car asked for is picked from the live field, and that is said.
            b = self.race.battle
            live = self._live() if b is not None else None
            lap_me = next((c for c in b.laps if c.is_player), None) if b else None
            moved = (live is not None and lap_me is not None
                     and live[1].class_position != lap_me.class_position)
            # 'P3' is a class position: the live one, like position=3, whenever the
            # live car there is not the lap's (#193: also when only others moved).
            ask = position or (rival_position(str(rival)) if rival else None)
            target = None
            if live is not None and lap_me is not None and (direction or ask):
                target = live_battle_target(live[0].cars, direction, ask)
                if not moved:
                    # His place held, but the car there may have changed (a swap
                    # behind him): the live car only when it is not the lap's car.
                    want = target_position(lap_me, direction, ask)
                    was = rival_matches([c for c in b.laps if not c.is_player],
                                        f"P{want}", lap_me) if want else []
                    if target[0] is None or (was and same_car(was[0], target[0])):
                        target = None
            text = format_battle(b, self.race.as_of_lap, self.units, rival,
                                 note=self.race.battle_note, direction=direction,
                                 position=position, live_target=target,
                                 live_cars=live[0].cars if live else None)
            if moved:
                text += (f"\nYou're P{live[1].class_position} in class now (live); this "
                         "read compares the field as it stood at the end of lap "
                         f"{self.race.as_of_lap}.")
            return text
        if which in ("", "latest") and race is None:
            if self.on_track and self.race_note:
                return live   # a race is live, its first lap not in: never the last race's
            if self._current_race() is None:
                return f"{live} This session has no race laps stored to read one from."
            which = "race"
        corners = self.cmap.corners if self.cmap else []
        if not corners:
            return "No battle read: there is no corner map for this track."
        # 'race N' is a whole race only once there are several: in a one-race session
        # 'race 3' is race lap 3 (#77's refs). An unknown N is refused (_race_pick),
        # never read as the latest race's lap N.
        target = None
        if race is not None or (len(self._race_windows()) > 1
                                and re.fullmatch(r"(?:the )?race \d+", which)):
            target, err = self._race_pick(race, which)
            lap = re.fullmatch(r"lap (\d+)", which)
            whole = re.fullmatch(r"(?:the )?race (\d+)", which)
            if err and race is not None and lap:
                return self._race_pick(race)[1] or (
                    f"race={race} is the whole race; for lap {lap[1]} of it pass "
                    f"which='race {race} lap {lap[1]}'.")
            if err and race is None and whole:
                return err + (f" For a lap: 'race {self._current_race()['index']} lap "
                              f"{whole[1]}'.")
            if err:
                return err
        if target is not None or which in _WHOLE_RACE:
            return self._stored_race_battle(rival, target, corners, direction, position)
        label_by_id, _ = self._lap_numbering()
        record, lap_id = self._resolve_lap(which)
        if record is None:
            return self._no_lap_msg(which)
        name = label_by_id.get(lap_id, f"lap id {lap_id}")
        laps = self.store.battle_laps(lap_id)
        b = build_battle(laps, corners) if laps else None
        if b is None or record.lap_no is None:
            return (f"No battle read is stored for {name}: none is stored for a lap outside "
                    "a race, with a stop, an out-/in-lap, a partly captured lap, or a lap "
                    "the field was not recorded on.")
        return f"Stored read for {name}.\n" + format_battle(b, record.lap_no, self.units, rival,
                                                           direction=direction,
                                                           position=position)

    def _stored_race_battle(self, rival, target, corners, direction=None, position=None):
        """#174: a whole race's stored battle read -- `target` (a race window), or the
        latest race with a stored read; a named rival who wasn't in it is looked for in
        this session's earlier races, newest first."""
        label_by_id, _ = self._lap_numbering()
        windows = self._race_windows()
        if not windows:
            return "No race in this session: battle reads are stored for race laps only."

        def race_laps(win):
            # One entry per race lap as the driver counts them: a lap and its in-lap
            # fragment share a label (the one with a read wins), and 'lap 0' rows are
            # the grid/formation (mTotalLaps 0), not a race lap.
            by_label: dict[str, object] = {}
            for i in win["lap_ids"]:
                name = label_by_id.get(i, f"lap id {i}")
                if not name.endswith(" lap 0") and by_label.get(name) is None:
                    laps = self.store.battle_laps(i)
                    by_label[name] = build_battle(laps, corners) if laps else None
            return list(by_label.items())

        read = [] if target is not None else [
            (w, laps) for w, laps in ((w, race_laps(w)) for w in reversed(windows))
            if any(b for _, b in laps)]
        # The latest race with a read; a named rival's most recent race if he wasn't in
        # it (the driver is always in the field, so naming himself never moves on).
        win, laps = (
            (target, race_laps(target)) if target is not None else
            next((r for r in read if not rival
                  or any(rival_matches(b.laps, rival) for _, b in r[1] if b)), None)
            or (read[0] if read else (windows[-1], race_laps(windows[-1]))))
        head = ""
        if len(windows) > 1:
            started = time.strftime("%H:%M", time.localtime(win["start_ts"]))
            head = f"Race {win['index']} of this session (started {started}). "
        if read and win is not read[0][0]:
            head += (f"{rival!r} was not in race {read[0][0]['index']} (the latest with a "
                     f"battle read); this is race {win['index']}, the most recent race he "
                     "was in. ")
        return format_race_battle(laps, self.units, rival, head=head, direction=direction,
                                  position=position)

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

    def get_personal_best(self, track=None, car=None, car_class=None):
        # #182: counts result-log laps (store.timed_laps, the game's own times), and
        # `car` may be a brand/model word ('McLaren', 'Z06') or `car_class` a class
        # ('GT3') -- several matching cars answer with one best per car.
        track = track or self.track
        explicit = bool(car or car_class)
        car = car or (None if car_class else self.car)
        if not (track and (car or car_class)):
            return "No track/car resolved yet — no personal best available."
        # A car is a (model, class) pair: the Oreca 07 races as LMP2 AND LMP2_ELMS, and
        # those times must never merge into one PB. "Other" = no scraped class.
        pairs = list(dict.fromkeys((m, g["car_class"]) for g in self.store.session_facets()
                                   ["car_groups"] for m in g["cars"]))
        laps = lambda p: self.store.timed_laps(track, p[0], None if p[1] == "Other" else p[1])
        label = lambda p: (f"{p[0]} ({p[1]})" if sum(q[0] == p[0] for q in pairs) > 1
                           else p[0])
        matched = (car and not car_class and [p for p in pairs if p[0] == car]) or [
            p for p in pairs if (not car or car.lower() in p[0].lower())
            and (not car_class or _norm_class(p[1]) == _norm_class(car_class))]
        rows = {p: r for p in matched if (r := laps(p))}
        if not matched and car and not car_class and (r := self.store.timed_laps(track, car)):
            rows = {(car, None): r}  # a raw entry name
        # A brand word that also names cars with no laps here says so (car_class= would
        # list the whole class's garage, which is noise).
        others = [label(q) for q in matched if q not in rows] if car and not car_class else []
        if len(rows) > 1 and not car_class and len({p[0] for p in rows}) == 1:
            # One model in several classes: the current car's class picks one, if it
            # matches (REST may spell it differently -- then list each class).
            mine = {p: r for p, r in rows.items() if self.match_class
                    and _norm_class(p[1]) == _norm_class(self.match_class)}
            rows = mine if len(mine) == 1 else rows
        if len(rows) == 1:
            (p, r), = rows.items()
            return self._personal_best_one(track, label(p), r) + (
                f" (Also matched, no laps here: {', '.join(others)}.)" if others else "")
        if not rows:
            here = [f"{p[0]} ({p[1]})" for p in pairs if explicit and laps(p)]
            return (f"No recorded laps yet for {track} / {car or car_class}."
                    + (f" Cars with laps here: {', '.join(here)}." if here else ""))
        bests = sorted(((min(r, key=lambda x: (x["lap_time"], not x["telemetry"])), label(p))
                        for p, r in rows.items()), key=lambda bm: bm[0]["lap_time"])
        return (f"Personal bests at {track} ({' '.join(filter(None, (car, car_class)))}), "
                "one per car: " + "; ".join(
                    f"{m} {format_laptime(b['lap_time'])} (set "
                    f"{time.strftime('%Y-%m-%d', time.localtime(b['ts']))}"
                    f"{'' if b['telemetry'] else ', result log only, no telemetry'})"
                    for b, m in bests) + ".")

    def _personal_best_one(self, track, car, rows):
        best = min(rows, key=lambda r: (r["lap_time"], not r["telemetry"]))
        prefix = f"All-time personal best at {track} in {car}: {format_laptime(best['lap_time'])}"
        if not best["telemetry"]:
            prefix += (" (from the game's result log -- no telemetry, so it can't be analysed "
                       "lap by lap)")
        if self.session_id and best["session_id"] == self.session_id:
            # #130: the PB *is* the lap just driven -- "set <date>" reads as history
            # when it's actually this session, so name the lap and say how long ago.
            label = self._lap_numbering()[0].get(best["id"])
            this_lap = f"this session's {label}" if label else "this session"
            return f"{prefix} — that's {this_lap}, set {_lap_age(best['ts'])}."
        when = time.strftime("%Y-%m-%d", time.localtime(best["ts"]))
        out = f"{prefix}, set {when}."
        session = [r["lap_time"] for r in rows if self.session_id
                   and r["session_id"] == self.session_id]
        if session and min(session) != best["lap_time"]:
            out += f" This session's best so far: {format_laptime(min(session))}."
        return out

    def get_progress(self, track=None, car=None):
        track, car = track or self.track, car or self.car
        if not (track and car):
            return "No track/car resolved yet — no progress to show."
        if (cls := self._class_scope(car)) is False:
            return self._mixed_classes(car)
        summary = self.store.session_summary(track=track, car=car, model_class=cls)
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
        if (cls := self._class_scope(car)) is False:
            return self._mixed_classes(car)
        rows = self.store.race_sessions_page(track=track, car=car, limit=int(limit or 5),
                                             model_class=cls)["rows"]
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
            result = (f", finished {_place(rr['position'], rr['class_position'], _multiclass([], rr))}"
                      if rr and rr.get("position") is not None else "")
            out.append(f"{date} ({kind}, {r['laps']} laps, best {best}{result})")
        return f"Recent sessions at {track} / {car}:\n" + "\n".join(out)

    def get_last_setup(self, track=None, car=None):
        track, car = track or self.track, car or self.car
        if not (track and car):
            return "No track/car resolved yet — no setup on record."
        # Exclude the current session so this returns the PRIOR session's setup
        # ("what did we run here last time?"), not the one this session recorded.
        # #188: the previous session's fastest lap's setup; sessions from before per-lap
        # setups existed fall back to the old per-session autosave, named as such.
        if (cls := self._class_scope(car)) is False:
            return self._mixed_classes(car)
        fast = self.store.previous_fastest_setup(track, car, exclude_session_id=self.session_id,
                                                 car_class=cls)
        row = self.store.last_setup(track, car, exclude_session_id=self.session_id,
                                    car_class=cls)
        # A legacy autosave from a MORE RECENT, different session is the previous session.
        if fast is not None and not (row is not None and row["session_id"] != fast["session_id"]
                                     and row["updated_ts"] > fast["ts"]):
            when = time.strftime("%Y-%m-%d", time.localtime(fast["ts"]))
            lines = _setup_lines(fast["setup"])
            which = "fastest lap" if fast["fastest"] else "fastest lap with a recorded setup"
            lap = f"lap {fast['lap_no']}, " if fast["lap_no"] is not None else ""
            head = (f"Setup on the {which} ({lap}{format_laptime(fast['lap_time'])}) of your "
                    f"previous session at {track} / {car} ({when})")
            if not lines:
                return f"{head}: stored, but no adjustable parameters were captured."
            return f"{head}:\n" + "\n".join(lines)
        if row is None:
            return f"No stored setup for {track} / {car} yet."
        when = time.strftime("%Y-%m-%d", time.localtime(row["updated_ts"]))
        lines = _setup_lines(row["setup"])
        if not lines:
            return f"Setup stored for {track} / {car} ({when}) but no adjustable parameters were captured."
        return (f"Last autosaved setup at {track} / {car} ({when}; no per-lap setup "
                "recorded, so not tied to a lap):\n" + "\n".join(lines))

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
            "get_time_on_table": lambda: self.get_time_on_table(
                args.get("which", "best"), bool(args.get("detail", False))),
            "get_stint_events": lambda: self.get_stint_events(args.get("laps"),
                                                              args.get("phase"),
                                                              args.get("race"),
                                                              args.get("which")),
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
            "get_race_result": lambda: self.get_race_result(args.get("race"), args.get("which")),
            "get_race_state": lambda: self.get_race_state(),
            "get_strategy": lambda: self.get_strategy(args.get("race_laps"),
                                                      args.get("race_minutes")),
            "get_battle": lambda: self.get_battle(args.get("rival"), args.get("which"),
                                                  args.get("direction"), args.get("position"),
                                                  args.get("race")),
            "get_car_status": lambda: self.get_car_status(),
            "get_setup": lambda: self.get_setup(),
            "record_driver_learning": lambda: self.record_driver_learning(
                args.get("text", ""), args.get("track"), args.get("car"), args.get("replaces")),
            "get_driver_memories": lambda: self.get_driver_memories(
                args.get("track"), args.get("car"), args.get("topic")),
            "get_personal_best": lambda: self.get_personal_best(args.get("track"), args.get("car"),
                                                                args.get("car_class")),
            "get_progress": lambda: self.get_progress(args.get("track"), args.get("car")),
            "list_past_sessions": lambda: self.list_past_sessions(
                args.get("track"), args.get("car"), args.get("limit", 5)),
            "get_last_setup": lambda: self.get_last_setup(args.get("track"), args.get("car")),
        }.get(name)
        if handler is None:
            return f"error: unknown tool {name!r}"
        return handler()
