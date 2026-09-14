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

from simcoach.analysis import analyze_lap, geometry_clause, time_loss_buckets
from simcoach.coach import _setup_lines
from simcoach.format import (
    find_corner,
    format_corner_line,
    format_lap_facts,
    format_laptime,
    format_time_loss,
    format_tire_report,
    format_wheel_report,
)
from simcoach.fuel import fuel_per_lap_from_channel, laps_remaining
from simcoach.track_lookup import load_track_knowledge, note_key_binds

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
                          "description": "Which lap: 'latest' (default), 'best', or an "
                                         "in-session lap number like 'lap 2'."},
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
                       "track: a short overview plus per-corner notes, as OWNED data. Call this "
                       "before describing how a track or corner drives. Pass `corner` (number or "
                       "label) to get just that corner's note. Works with NO laps captured. "
                       "Never invent track character from memory — quote these notes.",
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
        since = (rr.get("race_ts") if rr
                 else store.unlogged_race_since(session_id))
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


class CoachContext:
    """Live data bound for one question; tools read from it. Rebuilt each turn."""

    def __init__(self, store, units="metric", record=None, lap_id=None, cmap=None,
                 setup=None, track=None, car=None, on_track=None,
                 allowed_adjustments=None, session_id=None,
                 car_notes=None, setup_concepts=None):
        self.store = store
        self.units = units
        self.record = record
        self.lap_id = lap_id
        self.cmap = cmap
        self.setup = setup
        self.track = track or (record.track if record else None)
        self.car = car or (record.car if record else None)
        self.on_track = on_track
        self.allowed_adjustments = allowed_adjustments
        # Authored car knowledge (merged shipped+overlay `notes` dict) and the shared
        # LMU setup-concepts doc; loaded per turn like cmap. Empty {} when none.
        self.car_notes = car_notes or {}
        self.setup_concepts = setup_concepts or {}
        # The current on-track stint (practice/qualy/race), for scoping laps and the
        # reference to this session rather than all-time. From the live lap's stamp.
        self.session_id = session_id or (record.session_id if record else None)
        self.tool_calls: list = []   # (name, input) invoked this turn, for the chat log
        self._analysis_cache: dict = {}
        self._lap_numbering_cache: tuple[dict, dict] | None = None
        self._current_lap_cache: tuple | None = None

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
            rows = self.store.recent_laps(self.track, self.car, 1, session_id=self.session_id)
            if rows:
                lid = rows[0]["lap_id"]
                try:
                    found = (self.store.load(lid), lid)
                except Exception:
                    found = (None, None)
        self._current_lap_cache = found
        return found
    def _lap_numbering(self) -> tuple[dict, dict]:
        """In-session lap numbering matching the UI (LapList.svelte): laps are
        grouped into CONSECUTIVE same-phase runs in ts order, numbered 1..N within
        each run -- a run restarts at 1 if the phase value changes and later comes
        back (e.g. practice -> race -> practice), same as the UI's groups. Returns
        (label_by_id, id_by_ref): label_by_id[lap_id] -> e.g. 'race lap 3';
        id_by_ref maps textual refs the model might use ('race 3', 'race lap 3',
        and -- for the current phase's run -- the UI's unprefixed 'lap 3' or a bare
        '3') back to the DB lap_id.
        Cached per turn."""
        if self._lap_numbering_cache is not None:
            return self._lap_numbering_cache
        label_by_id: dict[int, str] = {}
        id_by_ref: dict[str, int] = {}
        if self.session_id:
            cur_rec, _ = self._current_lap()
            cur_phase = cur_rec.phase if cur_rec is not None else None
            prev_phase: object = object()  # sentinel -- never equals a real phase value
            n = 0
            for lap in self.store.session_laps(self.session_id):
                ph = lap.get("phase")
                n = n + 1 if ph == prev_phase else 1
                prev_phase = ph
                label_by_id[lap["id"]] = f"{ph} lap {n}" if ph else f"lap {n}"
                if ph:
                    id_by_ref[f"{ph} {n}".lower()] = lap["id"]
                    id_by_ref[f"{ph} lap {n}".lower()] = lap["id"]
                    if ph == cur_phase:
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
            self._analysis_cache[key] = analyze_lap(record, self.cmap, reference=ref)
        return self._analysis_cache[key]

    def _latest_analysis(self):
        record, lap_id = self._current_lap()
        if record is None:
            return None
        return self._analysis_for(record, lap_id)

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

    # --- tools -----------------------------------------------------------
    def get_session_status(self):
        n_all = len(self.store.list_lap_ids(self.track, self.car)) if (self.track and self.car) else 0
        n_sess = len(self.store.session_lap_ids(self.session_id)) if self.session_id else None
        has_ref = bool(self.track and self.car and self.store.best_lap(self.track, self.car))
        on = "on track" if self.on_track else ("unknown" if self.on_track is None else "not on track")
        cur_rec, _ = self._current_lap()
        lap = "yes" if cur_rec is not None else "no lap captured yet"
        phase = cur_rec.phase if cur_rec else None
        # Current-phase count: "8 laps into the race" must be race laps, not the
        # whole session (qualy + race). Grouped like _lap_numbering's counts[ph].
        n_phase = (sum(1 for r in self.store.session_laps(self.session_id) if r.get("phase") == phase)
                   if self.session_id and phase else None)
        if n_sess is not None:
            head = f"this {phase}: {n_phase}, " if n_phase is not None else ""
            laps_txt = f"{head}session total: {n_sess}, all-time this track/car: {n_all}"
        else:
            laps_txt = f"this track/car: {n_all} (no session split)"
        n_corners = len(self.cmap.corners) if (self.cmap and self.cmap.corners) else 0
        cmap_txt = (f"yes ({n_corners} corners — use list_track_corners)" if n_corners
                    else "no")
        return (f"Track: {self.track or 'unknown'} | Car: {self.car or 'unknown'} | "
                f"Session type: {phase or 'unknown'} | State: {on} | Current lap: {lap} | "
                f"Laps ({laps_txt}) | Corner map: {cmap_txt} | "
                f"Reference (best) lap available: {'yes' if has_ref else 'no'}")

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
            out.append(f"{label}: {format_laptime(r['lap_time'])} {valid}")
        return "\n".join(out)

    def get_lap_summary(self, which="latest"):
        record, lap_id = self._resolve_lap(which)
        if record is None:
            return self._no_lap_msg(which)
        if self.cmap is None:
            return "No corner map for this track yet — can't break the lap into corners."
        analysis = self._analysis_for(record, lap_id)
        return format_lap_facts(analysis, self.units)

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
        return format_time_loss(buckets, ref_label)

    def list_track_corners(self):
        if self.cmap is None or not self.cmap.corners:
            return "No corner map for this track yet — no corner list available."
        parts = []
        for c in self.cmap.corners:
            label = c.label or str(c.number)
            multi = " (double-apex)" if c.apexes and len(c.apexes) > 1 else ""
            parts.append(f"T{label} {c.name} — {c.direction}{multi}")
        return (f"Corners for {self.track or 'this track'} ({len(parts)}), from the saved map:\n"
                + "\n".join(parts))

    def get_corner(self, corner, which="latest"):
        record, lap_id = self._resolve_lap(which)
        if record is None:
            return self._no_lap_msg(which)
        if self.cmap is None:
            return "No corner map for this track yet — can't break the lap into corners."
        analysis = self._analysis_for(record, lap_id)
        cm = find_corner(analysis, corner)
        if cm is None:
            names = ", ".join(f"T{c.label or c.corner_no} {c.name}" for c in analysis.corners)
            return f"No corner matching {corner!r}. Corners this track: {names}"
        line = format_corner_line(cm, self.units)
        label = cm.label or cm.corner_no
        note = self._corner_note(label)
        if note:
            line += f"\nAuthored note: {note}"
        geo = self._corner_geometry_clause(label, cm.direction)
        if geo:
            line += f"\nGeometry (baked from the track map): {geo}"
        return line

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
        analysis = self._latest_analysis()
        if analysis is None:
            return "No lap captured yet (or no corner map for this track)."
        report = format_tire_report(analysis, self.units)
        return report or "No tire-temperature data was captured for this lap."

    def get_wheel_report(self):
        analysis = self._latest_analysis()
        if analysis is None:
            return "No lap captured yet (or no corner map for this track)."
        report = format_wheel_report(analysis, self.units)
        return report or "No brake/wear data was captured for this lap (older capture)."

    def get_energy_report(self):
        if self.record is None:
            return "No lap captured yet — no fuel/energy usage to report."
        ch = self.record.channels
        lines = []
        if "fuel" in ch:
            remaining = float(ch["fuel"][-1])
            try:
                fpl = fuel_per_lap_from_channel(ch["fuel"])
                lines.append(f"Fuel: {fpl:.2f} L used this lap; {remaining:.1f} L in tank "
                             f"(~{laps_remaining(remaining, fpl):.1f} laps left).")
            except ValueError:
                lines.append(f"Fuel: {remaining:.1f} L in tank (no clear per-lap burn this lap).")
        if "virtual_energy" in ch:
            ve = ch["virtual_energy"]
            used, cur = float(ve[0] - ve[-1]), float(ve[-1])
            if used > 0:
                lines.append(f"Virtual energy: {used * 100:.1f}% used this lap; {cur * 100:.0f}% "
                             f"remaining (~{cur / used:.1f} laps left).")
            else:
                lines.append(f"Virtual energy: {cur * 100:.0f}% remaining.")
        if "soc" in ch:
            lines.append(f"Battery state of charge: {float(ch['soc'][-1]) * 100:.0f}%.")
        return "\n".join(lines) if lines else "No fuel/energy data captured for this lap."

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
        return f"Session track: {track}.\n{text}" if track else text

    def get_setup(self):
        if not self.setup:
            return "No setup data available (REST unavailable or open setup)."
        lines = _setup_lines(self.setup)
        if not lines:
            return "No adjustable setup parameters exposed for this car."
        out = "Current setup:\n" + "\n".join(lines)
        if self.allowed_adjustments is not None:
            out += "\nLegal to change this tier: " + ", ".join(self.allowed_adjustments)
        return out

    def record_driver_learning(self, text, track=None, car=None):
        self.store.add_learning(text, status="pending", track=track or self.track,
                                car=car or self.car, source="coach_tool",
                                session_id=self.session_id)
        return "Saved for your review."

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
        when = time.strftime("%Y-%m-%d", time.localtime(best.timestamp))
        out = f"All-time personal best at {track} in {car}: {format_laptime(best.lap_time)}, set {when}."
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
        """Execute one tool call by name. Unknown tool -> error string."""
        args = args or {}
        self.tool_calls.append({"name": name, "input": args})
        handler = {
            "get_session_status": lambda: self.get_session_status(),
            "list_recent_laps": lambda: self.list_recent_laps(args.get("limit", 10)),
            "get_lap_summary": lambda: self.get_lap_summary(args.get("which", "latest")),
            "get_time_loss": lambda: self.get_time_loss(args.get("which", "latest"),
                                                        args.get("reference", "best")),
            "list_track_corners": lambda: self.list_track_corners(),
            "get_corner": lambda: self.get_corner(args.get("corner", ""), args.get("which", "latest")),
            "get_tire_report": lambda: self.get_tire_report(),
            "get_wheel_report": lambda: self.get_wheel_report(),
            "get_track_notes": lambda: self.get_track_notes(args.get("corner")),
            "get_car_notes": lambda: self.get_car_notes(),
            "get_energy_report": lambda: self.get_energy_report(),
            "get_race_result": lambda: self.get_race_result(),
            "get_setup": lambda: self.get_setup(),
            "record_driver_learning": lambda: self.record_driver_learning(
                args.get("text", ""), args.get("track"), args.get("car")),
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
