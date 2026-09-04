"""On-demand data tools for the agentic coach. The model calls these to fetch
exactly what a question needs instead of receiving the whole lap every turn.

Each tool returns a deterministic text summary built from the analysis/store
layer — the two-stage invariant holds: raw ~50 Hz telemetry never reaches the
model, only computed per-corner/lap facts. Corner identity comes from the map,
never guessed. `CoachContext` is rebuilt per question so tools always read the
current session (track/car/lap), which the live-session re-resolution keeps fresh.
"""
from __future__ import annotations

from simcoach.analysis import analyze_lap
from simcoach.coach import _setup_lines
from simcoach.fuel import fuel_per_lap_from_channel, laps_remaining
from simcoach.format import (
    find_corner,
    format_corner_line,
    format_lap_facts,
    format_laptime,
    format_tire_report,
    format_wheel_report,
)

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
        "name": "get_corner",
        "description": "The telemetry facts for ONE corner of the latest lap, by number "
                       "(e.g. '10') or name (e.g. 'Village'). Use this for a question about "
                       "a specific corner instead of pulling the whole lap.",
        "input_schema": {
            "type": "object",
            "properties": {"corner": {"type": "string",
                                      "description": "Corner number or name."}},
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

    # --- helpers ---------------------------------------------------------
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
            cur_phase = self.record.phase if self.record is not None else None
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
        if self.record is None:
            return None
        return self._analysis_for(self.record, self.lap_id)

    def _resolve_lap(self, which):
        """which -> (record, lap_id). 'latest'/None -> current; 'best' -> best lap;
        an in-session lap reference ('race lap 3', 'race 3', or a bare number in the
        current phase) -> that lap; a bare number falls back to a raw DB id."""
        which = (which or "latest").strip().lower()
        if which in ("latest", "current", ""):
            return self.record, self.lap_id
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
        lap = "yes" if self.record is not None else "no lap captured yet"
        phase = self.record.phase if self.record else None
        laps_txt = (f"this session: {n_sess}, all-time this track/car: {n_all}"
                    if n_sess is not None else f"this track/car: {n_all} (no session split)")
        return (f"Track: {self.track or 'unknown'} | Car: {self.car or 'unknown'} | "
                f"Session type: {phase or 'unknown'} | State: {on} | Current lap: {lap} | "
                f"Laps ({laps_txt}) | Reference (best) lap available: {'yes' if has_ref else 'no'}")

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

    def get_corner(self, corner):
        analysis = self._latest_analysis()
        if analysis is None:
            return "No lap captured yet (or no corner map for this track)."
        cm = find_corner(analysis, corner)
        if cm is None:
            names = ", ".join(f"T{c.corner_no} {c.name}" for c in analysis.corners)
            return f"No corner matching {corner!r}. Corners this track: {names}"
        return format_corner_line(cm, self.units)

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
        arc = self.store.session_position_arc(self.session_id) if self.session_id else None
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

    def dispatch(self, name: str, args: dict) -> str:
        """Execute one tool call by name. Unknown tool -> error string."""
        args = args or {}
        self.tool_calls.append({"name": name, "input": args})
        handler = {
            "get_session_status": lambda: self.get_session_status(),
            "list_recent_laps": lambda: self.list_recent_laps(args.get("limit", 10)),
            "get_lap_summary": lambda: self.get_lap_summary(args.get("which", "latest")),
            "get_corner": lambda: self.get_corner(args.get("corner", "")),
            "get_tire_report": lambda: self.get_tire_report(),
            "get_wheel_report": lambda: self.get_wheel_report(),
            "get_energy_report": lambda: self.get_energy_report(),
            "get_race_result": lambda: self.get_race_result(),
            "get_setup": lambda: self.get_setup(),
        }.get(name)
        if handler is None:
            return f"error: unknown tool {name!r}"
        return handler()
