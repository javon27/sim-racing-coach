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

from simcoach.analysis import analyze_lap
from simcoach.coach import _setup_lines
from simcoach.format import (
    find_corner,
    format_corner_line,
    format_lap_facts,
    format_laptime,
    format_tire_report,
    format_wheel_report,
)
from simcoach.fuel import fuel_per_lap_from_channel, laps_remaining

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
                       "the lap's in-session number (per phase, e.g. 'race lap 3' — the "
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
                                                    "lap number like 'race lap 3'."}},
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
                       "(e.g. 'Village'). Use this for a question about a specific corner "
                       "instead of pulling the whole lap. Pass `which` to target a specific "
                       "lap — do this whenever the conversation is about a particular lap "
                       "(e.g. 'race lap 2'); it defaults to the latest lap otherwise.",
        "input_schema": {
            "type": "object",
            "properties": {
                "corner": {"type": "string", "description": "Corner number or name."},
                "which": {"type": "string",
                          "description": "Which lap: 'latest' (default), 'best', or an "
                                         "in-session lap number like 'race lap 2'."},
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
                       "specific changes. Includes which adjustments are legal this tier.",
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


class CoachContext:
    """Live data bound for one question; tools read from it. Rebuilt each turn."""

    def __init__(self, store, units="metric", record=None, lap_id=None, cmap=None,
                 setup=None, track=None, car=None, on_track=None,
                 allowed_adjustments=None, session_id=None):
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
        """Per-phase, in-session lap numbering matching the UI (laps numbered 1..N
        within each phase, in ts order). Returns (label_by_id, id_by_ref):
        label_by_id[lap_id] -> e.g. 'race lap 3'; id_by_ref maps textual refs the
        model might use ('race 3', 'race lap 3', or a bare number in the current
        phase) back to the DB lap_id. Cached per turn."""
        if self._lap_numbering_cache is not None:
            return self._lap_numbering_cache
        label_by_id: dict[int, str] = {}
        id_by_ref: dict[str, int] = {}
        if self.session_id:
            cur_rec, _ = self._current_lap()
            cur_phase = cur_rec.phase if cur_rec is not None else None
            counts: dict = {}
            for lap in self.store.session_laps(self.session_id):
                ph = lap.get("phase")
                counts[ph] = counts.get(ph, 0) + 1
                n = counts[ph]
                label_by_id[lap["id"]] = f"{ph} lap {n}" if ph else f"lap {n}"
                if ph:
                    id_by_ref[f"{ph} {n}".lower()] = lap["id"]
                    id_by_ref[f"{ph} lap {n}".lower()] = lap["id"]
                    if ph == cur_phase:
                        id_by_ref[str(n)] = lap["id"]  # bare number -> current phase
                else:
                    id_by_ref[str(n)] = lap["id"]
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
        an in-session lap reference ('race lap 3', 'race 3', or a bare number in the
        current phase) -> that lap; a bare number falls back to a raw DB id."""
        which = (which or "latest").strip().lower()
        if which in ("latest", "current", ""):
            return self._current_lap()
        if which == "best":
            if not (self.track and self.car):
                return None, None
            best = self.store.best_lap(self.track, self.car)
            # best_lap returns the record; find its id via recent list match on time
            return best, None
        # In-session per-phase reference (matches how laps are named to the driver).
        _, id_by_ref = self._lap_numbering()
        if which in id_by_ref:
            lid = id_by_ref[which]
            try:
                return self.store.load(lid), lid
            except Exception:
                return None, None
        if which.isdigit():
            try:
                return self.store.load(int(which)), int(which)
            except Exception:
                return None, None
        return None, None

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
            return "No lap available for that request (none captured yet, or unknown id)."
        if self.cmap is None:
            return "No corner map for this track yet — can't break the lap into corners."
        analysis = self._analysis_for(record, lap_id)
        return format_lap_facts(analysis, self.units)

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
            return "No lap available for that request (none captured yet, or unknown lap)."
        if self.cmap is None:
            return "No corner map for this track yet — can't break the lap into corners."
        analysis = self._analysis_for(record, lap_id)
        cm = find_corner(analysis, corner)
        if cm is None:
            names = ", ".join(f"T{c.label or c.corner_no} {c.name}" for c in analysis.corners)
            return f"No corner matching {corner!r}. Corners this track: {names}"
        line = format_corner_line(cm, self.units)
        note = self._corner_note(cm.label or cm.corner_no)
        return f"{line}\nAuthored note: {note}" if note else line

    def _corner_note(self, label) -> str | None:
        """Authored note for one corner label from the merged map, or None.

        Notes may be keyed by a hyphen range ("10-11") covering several
        corners, while corner maps label corners individually ("10", "11").
        Fall back to a range match when the label is a plain integer.
        """
        notes = getattr(self.cmap, "notes", None) or {}
        by_corner = notes.get("corners") or {}
        label = str(label)
        if label in by_corner:
            return by_corner[label]
        if not label.isdigit():
            return None
        n = int(label)
        for key, text in by_corner.items():
            m = re.fullmatch(r"(\d+)-(\d+)", key)
            if m and int(m.group(1)) <= n <= int(m.group(2)):
                return text
        return None

    def get_track_notes(self, corner=None):
        notes = getattr(self.cmap, "notes", None) or {}
        overview = (notes.get("overview") or "").strip()
        by_corner = notes.get("corners") or {}
        if not overview and not by_corner:
            return f"No authored notes for {self.track or 'this track'} yet."
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
        rr = self.store.race_session(self.session_id) if self.session_id else None
        # Race stint only — a session_id spans practice/qualy/race, so an all-phase arc
        # would read the start/worst from an earlier phase (wrong start position).
        arc = self.store.session_position_arc(self.session_id, phase="race") if self.session_id else None
        if not rr and not arc:
            return ("No race result recorded for this session (practice/qualy, or not a "
                    "completed race).")
        lines = []
        if rr:
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
            lines.append(
                f"In-race: started P{arc['start']}, as low as P{arc['worst']}, "
                f"currently/finished P{arc['last']}."
            )
        return "\n".join(lines)

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
        row = self.store.last_setup(track, car)
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
            "list_track_corners": lambda: self.list_track_corners(),
            "get_corner": lambda: self.get_corner(args.get("corner", ""), args.get("which", "latest")),
            "get_tire_report": lambda: self.get_tire_report(),
            "get_wheel_report": lambda: self.get_wheel_report(),
            "get_track_notes": lambda: self.get_track_notes(args.get("corner")),
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
