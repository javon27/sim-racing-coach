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
import math
import re
import statistics
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
# The most trace any car keeps while the driver never crosses the line (in the garage,
# monitoring): long enough for the slowest lap behind a safety car, bounded for a
# sitting that goes on for hours.
TRACE_MAX_S = 900.0
# No car covers more than this between two scoring frames (540 km/h). A frame implying
# more is the lap count and lap distance disagreeing at S/F (see FieldTrace.feed).
MAX_PLAUSIBLE_SPEED_MS = 150.0


class FieldTrace:
    """Same-class car positions over the driver's lap, from each NEW scoring frame.

    Pure: capture feeds it the FieldSnapshot it already reads; it never reads memory
    itself. Distance is unwrapped (laps * track length + lap distance), so each car's
    trace rises monotonically across the line, whenever that car crosses it. A sample is
    (time, unwrapped distance, km/h, in the pit lane)."""

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
            e = self._cars.setdefault(c.id, {"pts": []})
            # Every frame: a driver swap or a reused slot id must not keep a stale name.
            e["driver"], e["car"], e["class_position"] = c.driver, c.car, c.class_position
            # At S/F the lap count and lap distance may not update in the same frame,
            # in either order: distance reset first reads a lap backwards, the count
            # ticking first reads a lap ahead. Either frame is dropped, not repaired.
            # ponytail: if a car's very FIRST sample is a lap-ahead frame, its trace
            # stays blocked until it really gets there; the next take() tail limits it.
            if e["pts"]:
                t0, s0 = e["pts"][-1][:2]
                if s < s0 or s - s0 > MAX_PLAUSIBLE_SPEED_MS * (t - t0):
                    continue
            pts = e["pts"]
            pts.append((t, s, c.speed * 3.6, bool(c.in_pits)))
            if pts[0][0] < t - TRACE_MAX_S:
                del pts[:bisect.bisect_left(pts, t - TRACE_MAX_S, key=lambda p: p[0])]

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
    # Which of this car's laps (unwrapped lap index) each corner was timed on: a
    # section is only compared between two crossings on the SAME lap.
    lap: dict[int, int] = field(default_factory=dict)
    # (from, to) scoring-clock spans this car spent in the pit lane: no zone that
    # overlaps one is compared (a stop is not "slower through that section").
    pit: list[tuple[float, float]] = field(default_factory=list)


def _time_at(pts: list, s: float) -> float | None:
    """When the trace reached unwrapped distance `s`, interpolated; None outside it."""
    i = bisect.bisect_left(pts, s, key=lambda p: p[1])
    if i < len(pts) and pts[i][1] == s:
        return pts[i][0]
    if i == 0 or i == len(pts):
        return None
    (t0, s0), (t1, s1) = pts[i - 1][:2], pts[i][:2]
    return t0 + (t1 - t0) * (s - s0) / (s1 - s0)


def _speed_at(pts: list, s: float) -> float | None:
    """Speed (km/h) at unwrapped distance `s`, interpolated between the samples either
    side -- never the nearest raw sample, which at a 5 Hz feed can sit metres from a
    V-shaped apex and read a speed neither car had there."""
    i = bisect.bisect_left(pts, s, key=lambda p: p[1])
    if i < len(pts) and pts[i][1] == s:
        return pts[i][2]
    if i == 0 or i == len(pts):
        return None
    (_, s0, v0), (_, s1, v1) = pts[i - 1][:3], pts[i][:3]
    return v0 + (v1 - v0) * (s - s0) / (s1 - s0)


def _crossing_lap(pts: list, dist: float, length: float, t_ref: float) -> int | None:
    """Which of this car's laps (unwrapped lap index) crossed lap distance `dist`
    NEAREST IN TIME to `t_ref` -- the driver's own crossing of the same spot. That, not
    "the same lap number", pairs two cars: it holds across the line whichever of them
    crosses it first, and for a car on another lap count. Only a corner's ENTRY is
    paired this way; its apex and exit are read on the same lap as its entry."""
    best = None
    for k in range(int(pts[0][1] // length), int(pts[-1][1] // length) + 1):
        t = _time_at(pts, k * length + dist)
        if t is not None and (best is None or abs(t - t_ref) < abs(best[0] - t_ref)):
            best = (t, k)
    return None if best is None else best[1]


def _pit_spans(pts: list) -> list[tuple[float, float]]:
    """Runs of pit-lane samples, widened to the neighbouring samples on each side:
    scoring is a few Hz, so the car entered/left the lane somewhere in between."""
    spans, i, n = [], 0, len(pts)
    while i < n:
        if len(pts[i]) > 3 and pts[i][3]:
            j = i
            while j + 1 < n and pts[j + 1][3]:
                j += 1
            spans.append((pts[max(i - 1, 0)][0], pts[min(j + 1, n - 1)][0]))
            i = j + 1
        else:
            i += 1
    return spans


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
        lap = CarLap(cid, e["driver"], e["car"], e.get("class_position", 0), cid == me_id,
                     pit=_pit_spans(pts))
        for n, (c, te, _, _) in ref.items():
            k = lap_no - 1 if cid == me_id else _crossing_lap(pts, c.start_dist, length, te)
            if k is None:
                continue
            ent, apx, ext = (_time_at(pts, k * length + d)
                             for d in (c.start_dist, c.apex_dist, c.end_dist))
            if ent is None or ext is None or ext <= ent:
                continue
            lap.entry[n], lap.exit[n], lap.lap[n] = ent, ext, k
            if apx is not None:
                kph = _speed_at(pts, k * length + c.apex_dist)
                if kph is not None:
                    lap.apex_kph[n] = kph
        out.append(lap)     # untimed too: "not timed on lap N", never "no such car"
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


def _clear(car: CarLap, a: float, b: float) -> bool:
    """True when `car` was out of the pit lane for the whole span a..b."""
    return not any(p <= b and q >= a for p, q in car.pit)


def zone_deltas(me: CarLap, rival: CarLap, corners: list[Corner]) -> list[Zone]:
    """Per-corner (entry to exit) and per-section (one corner's exit to the next timed
    corner's entry) time differences, over the zones BOTH cars were timed through.

    A section is only compared when each car's two crossings are on ONE of its laps and
    in order; no zone is compared across a car's time in the pit lane."""
    cs = sorted((c for c in corners if c.number in me.entry and c.number in rival.entry),
                key=lambda c: c.start_dist)
    out = []
    for i, c in enumerate(cs):
        n = c.number
        apex = (me.apex_kph[n] - rival.apex_kph[n]
                if n in me.apex_kph and n in rival.apex_kph else None)
        if all(_clear(x, x.entry[n], x.exit[n]) for x in (me, rival)):
            out.append(Zone(_corner_label(c),
                            (rival.exit[n] - rival.entry[n]) - (me.exit[n] - me.entry[n]),
                            apex))
        if i + 1 < len(cs):
            m = cs[i + 1].number
            if all(x.lap.get(n) == x.lap.get(m) and x.exit[n] < x.entry[m]
                   and _clear(x, x.exit[n], x.entry[m]) for x in (me, rival)):
                out.append(Zone(f"{_corner_label(c)} exit to {_corner_label(cs[i + 1])}",
                                (rival.entry[m] - rival.exit[n])
                                - (me.entry[m] - me.exit[n]), None))
    return out


def _was_near(me: CarLap, o: CarLap) -> bool:
    """Within BATTLE_RANGE_S of the driver at some corner entry on the lap (spec: only
    a car in battle range during the lap is compared)."""
    return any(abs(o.entry[n] - t) <= BATTLE_RANGE_S for n, t in me.entry.items()
               if n in o.entry)


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


def _gap(me: CarLap, o: CarLap) -> float:
    """How far `o` was from the driver on the lap: the median entry-time gap over the
    corners both were timed at (inf when none)."""
    gaps = [abs(o.entry[n] - t) for n, t in me.entry.items() if n in o.entry]
    return statistics.median(gaps) if gaps else math.inf


def rival_matches(others: list[CarLap], query: str,
                  me: CarLap | None = None) -> list[CarLap]:
    """Every car the driver's words name, the one nearest the driver on the lap first
    (then class order): 'P4' is a CLASS position (the field here is one class); anything
    else matches part of a driver, entry or car-model name."""
    q = re.sub(r"^(the|that)\s+", "", query.strip().lower())
    pos = re.fullmatch(r"p\s*(\d+)", q)
    hits = [o for o in others
            if (o.class_position == int(pos[1]) if pos else
                bool(q) and any(q in name.lower() for name in (o.driver, o.car, o.model)))]
    return sorted(hits, key=lambda o: (_gap(me, o) if me else 0.0, o.class_position))


def match_rival(others: list[CarLap], query: str, me: CarLap | None = None) -> CarLap | None:
    """The rival the driver named; among several matches ("the Ferrari"), the one the
    driver was nearest on the lap -- the car being fought, not the first in class order."""
    hits = rival_matches(others, query, me)
    return hits[0] if hits else None


@dataclass
class Battle:
    laps: list[CarLap]        # every same-class car timed on the lap, the driver included
    corners: list[Corner]
    rivals: list[CarLap]      # the default rival(s): nearest for most of the lap


def build_battle(laps: list[CarLap], corners: list[Corner]) -> Battle | None:
    me = next((c for c in laps if c.is_player), None)
    if me is None or not me.entry:
        return None
    return Battle(laps, corners, nearest_rivals(me, [c for c in laps if not c.is_player]))


def format_battle(b: Battle | None, as_of_lap: int, units: str = "metric",
                  rival: str | None = None, note: str | None = None) -> str:
    """The battle text; `note` is why there is no battle (RaceSnapshot.battle_note)."""
    head = f"As of lap {as_of_lap}."
    if b is None:
        return (f"{head} No battle read for lap {as_of_lap}: "
                f"{note or 'none was computed for that lap'}.")
    me = next(c for c in b.laps if c.is_player)
    others = [c for c in b.laps if not c.is_player]
    if rival:
        hits = rival_matches(others, rival, me)
        if not hits and rival_matches([me], rival):
            return f"{head} {rival} is you, not a rival."
        if not hits:
            field_txt = "; ".join(f"P{o.class_position} {o.driver} ({o.model or o.car})"
                                  for o in sorted(others, key=lambda o: o.class_position))
            return (f"{head} No car in your class matches {rival!r}. In your class: "
                    f"{field_txt or 'nobody else was recorded'}.")
        hit = hits[0]
        if not hit.entry:
            return (f"{head} {hit.driver} ({hit.model or hit.car}), P{hit.class_position} "
                    f"in class, was not timed on lap {as_of_lap}: its trace does not cover "
                    "your lap's corners.")
        why = ("the car you named" if len(hits) == 1 else
               f"{len(hits)} cars in your class match {rival!r}; this is the one nearest "
               f"you on lap {as_of_lap}")
        picks = [hit]
    else:
        picks, why = b.rivals, f"nearest to you on lap {as_of_lap}"
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
        if not _was_near(me, o):
            lines[-1] = (f"{o.driver} ({o.model or o.car}), P{o.class_position} in class, "
                         f"wasn't near you on lap {as_of_lap} (never within "
                         f"{BATTLE_RANGE_S:.0f} s) -- no corner-by-corner comparison.")
            continue
        # The floor applies to the delta AS STATED (2 dp): 0.1000001 s reads "0.10 s",
        # which the closing line says is never listed.
        zones = [z for z in all_zones if round(abs(z.delta), 2) > BATTLE_NOISE_FLOOR_S]
        his = sorted((z for z in zones if z.delta < 0), key=lambda z: z.delta)
        mine = sorted((z for z in zones if z.delta > 0), key=lambda z: -z.delta)
        lines.append("That car is quicker: " + ("; ".join(map(_zone, his))
                                                or "nowhere by more than the timing noise")
                     + ".")
        lines.append("You are quicker: " + ("; ".join(map(_zone, mine))
                                            or "nowhere by more than the timing noise") + ".")
    lines.append(f"Differences of {BATTLE_NOISE_FLOOR_S:.2f} s or less are within the timing "
                 "noise and are not listed.")
    return "\n".join(lines)
