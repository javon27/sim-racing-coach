"""Battle read for the coach's get_battle tool (#152): where a same-class rival is
faster or slower than the driver, on the last completed lap.

Capture feeds FieldTrace every NEW scoring frame (every same-class car's unwrapped lap
distance and speed); at lap commit the trace becomes per-corner crossing times per car
(`car_laps`), and code -- never the model -- ranks where a rival gains or loses.
Scoring updates at a few Hz, so every time here is interpolated between samples a few
tenths of a second apart: BATTLE_NOISE_FLOOR_S hides anything smaller than that error.
"""
from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field

from simcoach.corners import Corner
from simcoach.format import unit_fns
from simcoach.lmu.live import FieldSnapshot

# A rival within this many seconds of the driver at a corner entry is "in the fight"
# there. PENDING RIG (#152).
BATTLE_RANGE_S = 2.0
# Per-zone deltas at or under this are timing noise and are never reported. PENDING RIG
# (#152; spec: "measured at the rig before shipping the threshold"): a conservative
# default at the top of the spec's 0.05-0.1 s estimate until the rig measures the
# scoring feed's real update rate and per-corner error.
BATTLE_NOISE_FLOOR_S = 0.10
# Apex-speed differences at or under this are not reported. PENDING RIG (#152): at a few
# Hz the sample nearest the apex can sit ~10 m from it.
APEX_SPEED_FLOOR_KPH = 5.0
# How much trace a new lap inherits from the last one, so a rival who crossed an early
# corner just before the driver crossed the line is still found.
TRACE_TAIL_S = 5.0
# No car covers more than this between two scoring frames (540 km/h). A frame implying
# more is the lap count and lap distance disagreeing at S/F (see FieldTrace.feed).
MAX_PLAUSIBLE_SPEED_MS = 150.0


class FieldTrace:
    """Same-class car positions over the driver's lap, from each NEW scoring frame.

    Pure: capture feeds it the FieldSnapshot it already reads; it never reads memory
    itself. Distance is unwrapped (laps * track length + lap distance), so each car's
    trace rises monotonically across the line, whenever that car crosses it."""

    def __init__(self) -> None:
        self.last_t: float | None = None
        self._player_id: int | None = None
        self._track_len = 0.0
        self._cars: dict[int, dict] = {}

    def feed(self, t: float, snap: FieldSnapshot) -> None:
        if t == self.last_t:
            return          # the capture polls at 50 Hz, scoring updates at a few
        self.last_t = t
        if not snap.session.live or snap.session.track_len <= 0:
            return
        player = next((c for c in snap.cars if c.is_player), None)
        if player is None:
            return
        self._player_id, self._track_len = player.id, snap.session.track_len
        for c in snap.cars:
            if c.cls != player.cls:
                continue    # other classes are not recorded (spec)
            s = c.laps * self._track_len + c.lap_dist
            e = self._cars.setdefault(c.id, {"driver": c.driver, "car": c.car, "pts": []})
            e["class_position"] = c.class_position
            # At S/F the lap count and lap distance may not update in the same frame,
            # in either order: distance reset first reads a lap backwards, the count
            # ticking first reads a lap ahead. Either frame is dropped, not repaired.
            # ponytail: if a car's very FIRST sample is a lap-ahead frame, its trace
            # stays blocked until it really gets there; the next take() tail limits it.
            if e["pts"]:
                t0, s0, _ = e["pts"][-1]
                if s < s0 or s - s0 > MAX_PLAUSIBLE_SPEED_MS * (t - t0):
                    continue
            e["pts"].append((t, s, c.speed * 3.6))

    def take(self) -> dict:
        """Hand over the trace so far; keep the last TRACE_TAIL_S seconds for the next
        lap. Called at every S/F wrap, committed or not, so a trace never piles up."""
        out = {"player_id": self._player_id, "track_len": self._track_len,
               "cars": self._cars}
        cut = (self.last_t or 0.0) - TRACE_TAIL_S
        self._cars = {cid: {**e, "pts": [p for p in e["pts"] if p[0] >= cut]}
                      for cid, e in self._cars.items()}
        return out


@dataclass
class CarLap:
    """One same-class car's timing through the driver's lap: when it crossed each
    corner's entry and exit (scoring-clock seconds) and its speed nearest the apex."""
    car_id: int
    driver: str
    car: str                # the game's entry name
    class_position: int
    is_player: bool
    entry: dict[int, float] = field(default_factory=dict)   # corner number -> time
    exit: dict[int, float] = field(default_factory=dict)
    apex_kph: dict[int, float] = field(default_factory=dict)
    model: str = ""         # the car model, resolved off the capture thread


def _time_at(pts: list, s: float) -> float | None:
    """When the trace reached unwrapped distance `s`, interpolated; None outside it."""
    i = bisect.bisect_left(pts, s, key=lambda p: p[1])
    if i < len(pts) and pts[i][1] == s:
        return pts[i][0]
    if i == 0 or i == len(pts):
        return None
    (t0, s0, _), (t1, s1, _) = pts[i - 1], pts[i]
    return t0 + (t1 - t0) * (s - s0) / (s1 - s0)


def _crossing(pts: list, dist: float, length: float,
              t_ref: float) -> tuple[float, float] | None:
    """(time, unwrapped distance) of this car's crossing of lap distance `dist` NEAREST
    IN TIME to `t_ref` -- the driver's own crossing of the same spot. That, not "the same
    lap number", pairs two cars: it holds across the line whichever of them crosses it
    first, and for a car on another lap count."""
    best = None
    for k in range(int(pts[0][1] // length), int(pts[-1][1] // length) + 1):
        t = _time_at(pts, k * length + dist)
        if t is not None and (best is None or abs(t - t_ref) < abs(best[0] - t_ref)):
            best = (t, k * length + dist)
    return best


def car_laps(trace: dict, corners: list[Corner], lap_no: int) -> list[CarLap]:
    """Every same-class car's corner timing on the driver's lap `lap_no` (the lap that
    just completed: capture's lap_no is mTotalLaps at commit, so the counter read
    `lap_no - 1` while it was driven).

    ponytail: a corner whose window wraps S/F (start > end) is skipped -- its two ends
    fall in different laps of the driver's. A rival behind who has not reached a late
    corner when the lap commits has no time there, so that corner is simply not
    compared (unmeasured, not guessed)."""
    length = trace.get("track_len") or 0.0
    cars = trace.get("cars") or {}
    me_id = trace.get("player_id")
    me = cars.get(me_id)
    if not length or me is None or not me["pts"]:
        return []
    base = (lap_no - 1) * length
    ref = {}
    for c in corners:
        if c.start_dist > c.end_dist:
            continue
        times = [_time_at(me["pts"], base + d) for d in (c.start_dist, c.apex_dist, c.end_dist)]
        if None not in times:
            ref[c.number] = (c, *times)
    out = []
    for cid, e in cars.items():
        pts = e["pts"]
        if not pts:
            continue
        lap = CarLap(cid, e["driver"], e["car"], e.get("class_position", 0), cid == me_id)
        for n, (c, te, ta, tx) in ref.items():
            if cid == me_id:
                ent = (te, base + c.start_dist)
                apx = (ta, base + c.apex_dist)
                ext = (tx, base + c.end_dist)
            else:
                ent = _crossing(pts, c.start_dist, length, te)
                apx = _crossing(pts, c.apex_dist, length, ta)
                ext = _crossing(pts, c.end_dist, length, tx)
            if ent is None or ext is None or ext[0] <= ent[0]:
                continue
            lap.entry[n], lap.exit[n] = ent[0], ext[0]
            if apx is not None:
                lap.apex_kph[n] = min(pts, key=lambda p, s=apx[1]: abs(p[1] - s))[2]
        if lap.entry:
            out.append(lap)
    return out


@dataclass
class Zone:
    label: str
    delta: float                   # rival's time minus yours; + = you are quicker there
    apex_gain_kph: float | None    # your apex speed minus his (corners only)


def _corner_label(c: Corner) -> str:
    turn = f"Turn {c.label or c.number}"
    return f"{turn} ({c.name})" if c.name and c.name not in (turn, str(c.number), c.label) \
        else turn


def zone_deltas(me: CarLap, rival: CarLap, corners: list[Corner]) -> list[Zone]:
    """Per-corner (entry to exit) and per-section (one corner's exit to the next timed
    corner's entry) time differences, over the zones BOTH cars were timed through."""
    cs = sorted((c for c in corners if c.number in me.entry and c.number in rival.entry),
                key=lambda c: c.start_dist)
    out = []
    for i, c in enumerate(cs):
        n = c.number
        apex = (me.apex_kph[n] - rival.apex_kph[n]
                if n in me.apex_kph and n in rival.apex_kph else None)
        out.append(Zone(_corner_label(c),
                        (rival.exit[n] - rival.entry[n]) - (me.exit[n] - me.entry[n]), apex))
        if i + 1 < len(cs):
            m = cs[i + 1].number
            out.append(Zone(f"{_corner_label(c)} exit to {_corner_label(cs[i + 1])}",
                            (rival.entry[m] - rival.exit[n]) - (me.entry[m] - me.exit[n]),
                            None))
    return out


def nearest_rivals(me: CarLap, others: list[CarLap]) -> list[CarLap]:
    """The same-class car(s) the driver was nearest to for most of the lap, decided in
    code: at each corner entry the closest rival within BATTLE_RANGE_S scores a point.
    The top scorer is the rival; a second is named too when it scored at least a third
    of the timed corners -- the fight changed hands. Empty when nobody was in range."""
    score: dict[int, int] = {}
    for n, t in me.entry.items():
        near = [(abs(o.entry[n] - t), o.car_id) for o in others
                if n in o.entry and abs(o.entry[n] - t) <= BATTLE_RANGE_S]
        if near:
            cid = min(near)[1]
            score[cid] = score.get(cid, 0) + 1
    ranked = sorted(score, key=lambda c: -score[c])
    picks = ranked[:1] + [c for c in ranked[1:2] if score[c] * 3 >= len(me.entry)]
    by_id = {o.car_id: o for o in others}
    return [by_id[c] for c in picks]


def match_rival(others: list[CarLap], query: str) -> CarLap | None:
    """The rival the driver named: 'P4' is a CLASS position (the field here is one
    class); anything else matches part of a driver, entry or car-model name."""
    q = re.sub(r"^(the|that)\s+", "", query.strip().lower())
    pos = re.fullmatch(r"p\s*(\d+)", q)
    for o in sorted(others, key=lambda o: o.class_position):
        if pos:
            if o.class_position == int(pos[1]):
                return o
        elif q and any(q in name.lower() for name in (o.driver, o.car, o.model)):
            return o
    return None


@dataclass
class Battle:
    laps: list[CarLap]        # every same-class car timed on the lap, the driver included
    corners: list[Corner]
    rivals: list[CarLap]      # the default rival(s): nearest for most of the lap


def build_battle(laps: list[CarLap], corners: list[Corner]) -> Battle | None:
    me = next((c for c in laps if c.is_player), None)
    if me is None:
        return None
    return Battle(laps, corners, nearest_rivals(me, [c for c in laps if not c.is_player]))


def format_battle(b: Battle | None, as_of_lap: int, units: str = "metric",
                  rival: str | None = None) -> str:
    head = f"As of lap {as_of_lap}."
    if b is None:
        return (f"{head} No battle read for that lap: it needs a corner map for this track "
                "and a lap that was not an out-, in- or partial lap or a pit stop.")
    me = next(c for c in b.laps if c.is_player)
    others = [c for c in b.laps if not c.is_player]
    if rival:
        hit = match_rival(others, rival)
        if hit is None:
            field_txt = "; ".join(f"P{o.class_position} {o.driver} ({o.model or o.car})"
                                  for o in sorted(others, key=lambda o: o.class_position))
            return (f"{head} No car in your class matches {rival!r}. In your class: "
                    f"{field_txt or 'nobody else was timed'}.")
        picks, why = [hit], "the car you named"
    else:
        picks, why = b.rivals, f"nearest to you for most of lap {as_of_lap}"
        if not picks:
            return (f"{head} Nobody in your class was within {BATTLE_RANGE_S:.0f} s of you "
                    f"on lap {as_of_lap}; no battle to compare.")
    spd, _, spd_u, _ = unit_fns(units)

    def _zone(z: Zone) -> str:
        txt = f"{z.label} by {abs(z.delta):.2f} s"
        if z.apex_gain_kph is not None and abs(z.apex_gain_kph) > APEX_SPEED_FLOOR_KPH:
            more = "more" if z.apex_gain_kph > 0 else "less"
            txt += f", you carry {abs(spd(z.apex_gain_kph)):.0f} {spd_u} {more} at the apex"
        return txt

    lines = [head]
    for o in picks:
        lines.append(f"Rival ({why}): {o.driver} ({o.model or o.car}), "
                     f"P{o.class_position} in class.")
        all_zones = zone_deltas(me, o, b.corners)
        if not all_zones:
            # Unmeasured is not "even": nothing timed for both is nothing compared.
            lines.append(f"No corner was timed for both of you on lap {as_of_lap}.")
            continue
        # The floor applies to the delta AS STATED (2 dp): 0.1000001 s reads "0.10 s",
        # which the closing line says is never listed.
        zones = [z for z in all_zones if round(abs(z.delta), 2) > BATTLE_NOISE_FLOOR_S]
        his = sorted((z for z in zones if z.delta < 0), key=lambda z: z.delta)
        mine = sorted((z for z in zones if z.delta > 0), key=lambda z: -z.delta)
        lines.append("He is quicker: " + ("; ".join(map(_zone, his))
                                          or "nowhere by more than the timing noise") + ".")
        lines.append("You are quicker: " + ("; ".join(map(_zone, mine))
                                            or "nowhere by more than the timing noise") + ".")
    lines.append(f"Differences of {BATTLE_NOISE_FLOOR_S:.2f} s or less are within the timing "
                 "noise and are not listed.")
    return "\n".join(lines)
