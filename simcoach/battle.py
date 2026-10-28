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
import dataclasses
import itertools
import math
import re
import statistics
from collections import Counter
from dataclasses import dataclass, field

from simcoach.corners import Corner
from simcoach.format import unit_fns
from simcoach.lmu.live import FieldSnapshot, VehicleState

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
# #238: a rival this many seconds either side of the driver at a contact is named as
# the nearest car there. Swept over 52 real contact laps (#238 review): true partners
# sat 0.00-0.58 s away; at 1.0 s 7 of 16 wall/no-car hits still named a car, at 0.6 s 2.
CONTACT_NEAR_S = 0.6
# #250: a car whose frames have read a lap or more BEHIND its trace for this long is not
# at an S/F count/distance mismatch (one frame) -- its trace start was stale. Real case:
# the formation lap ends with the grid at 3,777 m on lap 0, then the start reads -302 m
# on lap 0, so every car's race lap 1 was dropped until it passed 3,777 m again.
STALE_TRACE_S = 1.0
# #250 review: no time or speed is interpolated between two samples further apart than
# this (scoring is a few Hz): a long gap -- a lap-count lag, a restarted trace -- bridged
# by one straight line invents a crossing time for a car that wasn't there.
MAX_SAMPLE_GAP_S = 2.0
# #250 review: an impact joins the contact before it when it is within CONTACT_CHAIN_S of
# the previous impact, or within CONTACT_CHAIN_M AND CONTACT_NEAR_GAP_S of it -- a scrape
# runs on (7688: steps up to 1.94 s), a stopped car hit again ~5 s later is a new contact
# at the same spot (7768: 830 m then 822 m 5.9 s on; 7737: 743 m then 757 m 5.2 s on).
CONTACT_CHAIN_M = 30.0
CONTACT_NEAR_GAP_S = 2.0
CONTACT_CHAIN_S = 1.0


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
            # A run of lap-behind frames lasting STALE_TRACE_S means the trace was the
            # bad side (#250): it restarts there. A car reversing a few metres never does.
            if e["pts"]:
                t0, s0 = e["pts"][-1][:2]
                if s0 - s > self._track_len / 2:
                    if t - e.setdefault("behind_since", t) <= STALE_TRACE_S:
                        continue
                    e["pts"] = []
                else:
                    e.pop("behind_since", None)
                    if s < s0 or s - s0 > MAX_PLAUSIBLE_SPEED_MS * (t - t0):
                        continue
            e.pop("behind_since", None)
            pts = e["pts"]
            pts.append((t, s, c.speed * 3.6, bool(c.in_pits)))
            if pts[0][0] < t - TRACE_MAX_S:
                del pts[:bisect.bisect_left(pts, t - TRACE_MAX_S, key=lambda p: p[0])]

    def peek(self) -> dict:
        """The trace so far, copied, for a read off the capture thread (#250: the lap in
        progress). The capture thread keeps feeding; nothing is trimmed."""
        return {"player_id": self._player_id, "track_len": self._track_len,
                "cars": {cid: {**e, "pts": e["pts"][:]}
                         for cid, e in list(self._cars.items())}}

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
    if t1 - t0 > MAX_SAMPLE_GAP_S:
        return None
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
    (t0, s0, v0), (t1, s1, v1) = pts[i - 1][:3], pts[i][:3]
    if t1 - t0 > MAX_SAMPLE_GAP_S:
        return None
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


def _time_at_dist(car: CarLap, corners: list[Corner], dist: float) -> float | None:
    """When `car` passed lap distance `dist`, interpolated between the two stored corner
    crossings either side of it (entries and exits) -- only when both are on ONE of its
    laps and in order. None outside the timed corners (no S/F seam bracket: the far side
    is in another lap's row)."""
    pts = sorted(((d, t, car.lap.get(c.number)) for c in corners if c.number in car.entry
                  for d, t in ((c.start_dist, car.entry[c.number]),
                               (c.end_dist, car.exit[c.number]))), key=lambda p: p[0])
    for (d0, t0, k0), (d1, t1, k1) in itertools.pairwise(pts):
        if d0 <= dist <= d1 and d1 > d0 and k0 == k1 and t1 >= t0:
            return t0 + (t1 - t0) * (dist - d0) / (d1 - d0)
    return None


def _nearest_line(t_me: float, timed: list[tuple[CarLap, float]], dist: float) -> str | None:
    """#238's line: up to two of `timed` (rival, when it passed `dist`) within
    CONTACT_NEAR_S of the driver's `t_me`, closest first, none in the pit lane then."""
    near = sorted(((t - t_me, o) for o, t in timed
                   if abs(t - t_me) <= CONTACT_NEAR_S and _clear(o, t, t)),
                  key=lambda x: abs(x[0]))[:2]
    if not near:
        return None
    cars = "; ".join(
        f"{o.driver} (P{o.class_position} in class), "
        + ("level" if round(abs(g), 1) == 0 else f"{abs(g):.1f} s {'behind' if g > 0 else 'ahead'}")
        for g, o in near)
    # The field trace records the driver's class only: a car of another class is unseen.
    return (f"Nearest same-class car at the first touch of the contact ({dist:,.0f} m; "
            f"other classes aren't tracked): {cars} -- the nearest car, not a measured contact partner "
            "(who made contact is not measured).")


def contact_touch_dist(hits: list[dict], newest: bool) -> float | None:
    """#250 (driver's call): where the nearest-car line is read -- the FIRST touch (by
    et) of one contact. A contact chains placed impacts in et order while each is close
    to the PREVIOUS one (CONTACT_CHAIN_S, or CONTACT_CHAIN_M within CONTACT_NEAR_GAP_S:
    a scrape runs on -- 7688 went 699 -> 757 m over 5 s); unplaced impacts are skipped.
    Which contact: the newest on a live lap ("who hit me?" just now -- not an earlier tap
    on the same lap), else the one holding the heaviest impact (#238). Ties: newest is
    the last on equal et, heaviest the first on equal magnitude. Race 2 lap 3 shows why
    the first touch: at 2,071 m two cars were alongside; at the spin's heaviest knock a
    second later, nobody within 0.6 s. None when no impact is placed, or on a live read
    whose newest impact is unplaced (never an older contact in its place)."""
    if newest and hits and not math.isfinite(
            max(reversed(hits), key=lambda e: e["et"])["lap_distance"]):
        return None
    placed = sorted((e for e in hits if math.isfinite(e["lap_distance"])),
                    key=lambda e: e["et"])          # stable: equal et keeps capture order
    contacts: list[list[dict]] = []
    for e in placed:
        p = contacts[-1][-1] if contacts else None
        gap = e["et"] - p["et"] if p is not None else math.inf
        if gap <= CONTACT_CHAIN_S or (
                gap <= CONTACT_NEAR_GAP_S
                and abs(e["lap_distance"] - p["lap_distance"]) <= CONTACT_CHAIN_M):
            contacts[-1].append(e)
        else:
            contacts.append([e])
    if not contacts:
        return None
    if newest:
        pick = contacts[-1]
    else:   # max() keeps the first of equal magnitudes
        pick = max(contacts, key=lambda c: max(e["magnitude"] for e in c))
    return pick[0]["lap_distance"]


def contact_nearest(laps: list[CarLap], corners: list[Corner], dist: float) -> str | None:
    """#238: the same-class car(s) nearest the driver as they passed `dist` (a contact's
    first touch, contact_touch_dist) on a stored lap: up to two within CONTACT_NEAR_S, closest first.
    Both times are the field trace's own scoring-clock crossings, so no two clocks are
    mixed. None when the driver or nobody near is timed there. It is the NEAREST car,
    never "who hit you": contact attribution is not measured."""
    me = next((c for c in laps if c.is_player), None)
    t_me = _time_at_dist(me, corners, dist) if me else None
    if t_me is None:
        return None
    return _nearest_line(t_me, [(o, t) for o in laps if not o.is_player
                                and (t := _time_at_dist(o, corners, dist)) is not None], dist)


def live_contact_nearest(trace: dict, lap_no: int, dist: float) -> str | None:
    """#250: contact_nearest for the lap IN PROGRESS (`lap_no`, the live meta's: the
    driver's mTotalLaps + 1), read straight off the field trace so far (FieldTrace.peek):
    every car's own scoring-clock samples interpolated at `dist`, so it needs no corner
    crossing after the hit and holds across S/F. Each rival is read on the lap of his
    that passed `dist` nearest in time to the driver (as car_laps pairs corners)."""
    length, cars = trace.get("track_len") or 0.0, trace.get("cars") or {}
    me = cars.get(trace.get("player_id"))
    if not length or me is None or not me["pts"]:
        return None
    t_me = _time_at(me["pts"], (lap_no - 1) * length + dist)
    if t_me is None:
        return None
    timed = []
    for cid, e in cars.items():
        pts = e["pts"]
        if e is me or not pts or (k := _crossing_lap(pts, dist, length, t_me)) is None:
            continue
        o = CarLap(cid, e["driver"], e["car"], e.get("class_position", 0), False,
                   pit=_pit_spans(pts))
        timed.append((o, _time_at(pts, k * length + dist)))
    return _nearest_line(t_me, timed, dist)


def _rival_query(query: str) -> str:
    return re.sub(r"^(the|that)\s+", "", query.strip().lower())


def rival_position(query: str) -> int | None:
    """The class position a rival query names ('P4', 'the p4', 'that P 4' -> 4), or None
    when it names a car -- the one grammar for both get_battle and rival_matches."""
    pos = re.fullmatch(r"p\s*(\d+)", _rival_query(query))
    return int(pos[1]) if pos else None


def rival_matches(others: list[CarLap], query: str,
                  me: CarLap | None = None) -> list[CarLap]:
    """Every car the driver's words name, the one nearest the driver on the lap first
    (then class order): 'P4' is a CLASS position (the field here is one class); anything
    else matches part of a driver, entry or car-model name."""
    q = _rival_query(query)
    pos = rival_position(query)
    hits = [o for o in others
            if (o.class_position == pos if pos is not None else
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


def _no_match(rival: str, others: list[CarLap]) -> str:
    field_txt = "; ".join(f"{o.driver} ({o.model or o.car}), P{o.class_position} in class"
                          for o in sorted(others, key=lambda o: o.class_position))
    return (f"No car in your class matches {rival!r}. In your class: "
            f"{field_txt or 'nobody else was recorded'}.")


def _zone_text(z: Zone, units: str) -> str:
    spd, _, spd_u, _ = unit_fns(units)
    txt = f"{z.label} by {abs(z.delta):.2f} s"
    if z.apex_gain_kph is not None and abs(z.apex_gain_kph) > APEX_SPEED_FLOOR_KPH:
        more = "more" if z.apex_gain_kph > 0 else "less"
        txt += f", you carry {abs(spd(z.apex_gain_kph)):.0f} {spd_u} {more} at the apex"
    return txt


_SIDE = {"ahead": "directly ahead of you", "behind": "directly behind you"}


def target_position(me: CarLap, direction: str | None, position: int | None) -> int | None:
    """#185: the class position asked for on the lap -- `position` itself, or one place
    ahead of / behind the driver as the positions were recorded when he finished it;
    None when his own was not recorded."""
    if position:
        return position
    if me.class_position < 1:
        return None
    return me.class_position + (-1 if direction == "ahead" else 1)


def _nobody_at(b: Battle, lap: str, direction: str | None,
               position: int | None) -> str | None:
    """#185: why no rival holds the asked-for place at the end of `lap` ('lap 2',
    'race lap 7'), or None when one does."""
    me = next(c for c in b.laps if c.is_player)
    others = [c for c in b.laps if not c.is_player]
    want = target_position(me, direction, position)
    if want is None:
        return (f"Your class position was not recorded on {lap}, so the car "
                f"{_SIDE[direction]} is unknown; name the car or its position.")
    if want == me.class_position:
        return f"P{want} is you, not a rival."
    if any(o.class_position == want for o in others):
        return None
    if position:
        return _no_match(f"P{want}", others)
    if want < 1 or want > max(o.class_position for o in b.laps):
        return (f"You were P{me.class_position} in class at the end of {lap}: nobody in "
                f"your class was {'ahead of' if direction == 'ahead' else 'behind'} you.")
    return (f"You were P{me.class_position} in class at the end of {lap}; no car was "
            f"recorded at P{want} in class.")


def same_car(o: CarLap, car: VehicleState) -> bool:
    """#186: the lap's car `o` is the live `car` -- the mID AND the entry: a slot reused
    by a new car is not the lap's car."""
    return o.car_id == car.id and o.car == car.car


def format_battle(b: Battle | None, as_of_lap: int, units: str = "metric",
                  rival: str | None = None, note: str | None = None,
                  direction: str | None = None, position: int | None = None,
                  live_target: tuple[VehicleState | None, str] | None = None,
                  live_cars: list[VehicleState] | None = None) -> str:
    """The battle text; `note` is why there is no battle (RaceSnapshot.battle_note).
    #185: `direction` ('ahead'/'behind') compares the car one class position away,
    `position` the car at that class position; either overrides `rival`.
    #186: `live_target` (race.live_battle_target) replaces them when the live field
    picks another car: the car picked in the LIVE field, found in the lap's timing by id
    and entry and stated at its live class place -- or why there is none. `live_cars`
    (the live field, whenever there is one): every rival is stated at its live class
    place, or at its place 'at the end of the lap' when it is not in the live field --
    never a lap-old place unlabelled. Where no place changed, the text is #185's."""
    head = f"As of lap {as_of_lap}."
    if b is None:
        return (f"{head} No battle read for lap {as_of_lap}: "
                f"{note or 'none was computed for that lap'}.")
    me = next(c for c in b.laps if c.is_player)
    others = [c for c in b.laps if not c.is_player]

    def _place(o: CarLap) -> str:
        if live_cars is None:
            return f"P{o.class_position} in class"
        now = next((c for c in live_cars if same_car(o, c)), None)
        if now is None:
            return f"P{o.class_position} in class at the end of lap {as_of_lap}"
        # #193: a place changed since the lap is said in the present, never under
        # "As of lap N." unlabelled.
        return (f"P{now.class_position} in class"
                + (" now (live)" if now.class_position != o.class_position else ""))

    side_why = hits = None
    if live_target is not None:
        car, side_why = live_target
        if car is None:
            return f"{head} {side_why}"
        hits = [dataclasses.replace(c, class_position=car.class_position)
                for c in others if same_car(c, car)]
        if not hits:
            return (f"{head} Rival ({side_why}): {car.driver} ({car.car}), "
                    f"P{car.class_position} in class, was not timed on lap {as_of_lap}: "
                    "it is not in that lap's field.")
    elif direction or position:
        nobody = _nobody_at(b, f"lap {as_of_lap}", direction, position)
        if nobody:
            return f"{head} {nobody}"
        want = target_position(me, direction, position)
        rival = f"P{want}"
        side_why = (f"{_SIDE[direction]} in class at the end of lap {as_of_lap}, when you "
                    f"were P{me.class_position}" if direction else
                    f"P{want} in class at the end of lap {as_of_lap}")
    if rival and hits is None:
        hits = rival_matches(others, rival, me)
        if not hits and rival_matches([me], rival):
            return f"{head} {rival} is you, not a rival."
        if not hits:
            # Labelled only when a place in it (or his own) has changed since the lap.
            moved = any(_place(o) != f"P{o.class_position} in class" for o in b.laps)
            return (f"{head} {_no_match(rival, others)}"
                    + (f" Those are the places at the end of lap {as_of_lap}."
                       if moved else ""))
    if hits:
        hit = hits[0]
        if not hit.entry:
            return (f"{head} {f'Rival ({side_why}): ' if side_why else ''}"
                    f"{hit.driver} ({hit.model or hit.car}), {_place(hit)}, "
                    f"was not timed on lap {as_of_lap}: its trace does not cover "
                    "your lap's corners.")
        why = side_why or ("the car you named" if len(hits) == 1 else
                           f"{len(hits)} cars in your class match {rival!r}; this is the one "
                           f"nearest you on lap {as_of_lap}")
        picks = [hit]
    else:
        picks, why = b.rivals, f"nearest to you on lap {as_of_lap}"
        if not picks:
            return (f"{head} Nobody in your class was within {BATTLE_RANGE_S:.0f} s of you "
                    f"on lap {as_of_lap}; no battle to compare.")
    def _zone(z: Zone) -> str:
        return _zone_text(z, units)

    lines = [head]
    for o in picks:
        lines.append(f"Rival ({why}): {o.driver} ({o.model or o.car}), {_place(o)}.")
        all_zones = zone_deltas(me, o, b.corners)
        if not all_zones:
            # Unmeasured is not "even": nothing timed for both is nothing compared.
            lines.append(f"No corner was timed for both of you on lap {as_of_lap}.")
            continue
        if not _was_near(me, o):
            far = (f"{o.driver} ({o.model or o.car}), {_place(o)}, "
                   f"wasn't near you on lap {as_of_lap} (never within "
                   f"{BATTLE_RANGE_S:.0f} s) -- no corner-by-corner comparison.")
            if side_why:
                lines.append(far)       # keep which car it is (#185)
            else:
                lines[-1] = far
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


def format_race_battle(laps: list[tuple[str, Battle | None]], units: str = "metric",
                       rival: str | None = None, head: str = "",
                       direction: str | None = None, position: int | None = None) -> str:
    """#174: get_battle after the race, from every race lap's STORED battle read.
    `laps` is (the lap's in-session name, its Battle or None when none was stored) for
    every race lap, oldest first -- a lap with none is skipped and counted, never guessed.

    Each lap is compared exactly as the live read compares it (zone_deltas' same-lap and
    pit rules, _was_near's battle range); per zone the result is the MEDIAN of those
    per-lap deltas. BATTLE_NOISE_FLOOR_S is one lap's timing error, and the median stays
    on that one-lap scale, so the same floor applies; a sum would add every lap's error
    (and one lap stuck in traffic would dominate it) and need a floor of its own."""
    have = [(name, b) for name, b in laps if b is not None]
    if not have:
        return (f"{head}No battle read is stored for any of the race's {len(laps)} laps "
                "(none is stored for a lap with a stop, an out-/in-lap, a partly captured "
                "lap, or a lap the field was not recorded on).")

    def ident(c: CarLap) -> tuple[str, str]:
        return (c.driver, c.car)     # a slot id can be reused; the names can't

    def me_of(b: Battle) -> CarLap:
        return next(c for c in b.laps if c.is_player)

    field_: dict[tuple, CarLap] = {}    # each car as last recorded
    for _, b in have:
        field_.update((ident(c), c) for c in b.laps if not c.is_player)
    holds = None
    if direction or position:
        # #185: the car at that place at the end of the race (the last stored lap), and
        # only the laps he held it on are compared -- positions change over a race.
        def at(b: Battle) -> set[tuple]:
            want = target_position(me_of(b), direction, position)
            return {ident(c) for c in b.laps if not c.is_player and c.class_position == want}
        last_name, last = have[-1]
        nobody = _nobody_at(last, last_name, direction, position)
        if nobody:
            return (f"{head}{last_name} is the last race lap with a stored battle read. "
                    + nobody)
        what = _SIDE[direction] if direction else f"P{position}"
        held = [at(b) for _, b in have]
        key = min(held[-1])     # one car per place; min() only makes a duplicate stable
        votes = Counter({key: 1})
        why = (f"{what} in class at the end of {last_name}; he held that place on "
               f"{sum(key in h for h in held)} of the {len(have)} race laps with a stored "
               "battle read")
        if len(set().union(*held)) > 1:
            why += f", {len(set().union(*held))} different cars over the race"
        holds = dict(zip((name for name, _ in have), held))
    elif rival:
        hits = [rival_matches([c for c in b.laps if not c.is_player], rival, me_of(b))
                for _, b in have]
        votes = Counter(ident(h[0]) for h in hits if h)
        if not votes:
            if rival_matches([me_of(have[-1][1])], rival):
                return f"{head}{rival} is you, not a rival."
            return head + _no_match(rival, list(field_.values()))
        n = len({ident(c) for h in hits for c in h})
        why = ("the car you named" if n == 1 else
               f"{n} cars in your class match {rival!r}; this is the one nearest you on the "
               "most laps")
    else:
        votes = Counter(ident(b.rivals[0]) for _, b in have if b.rivals)
        if not votes:
            return (f"{head}Nobody in your class was within {BATTLE_RANGE_S:.0f} s of you "
                    f"on any of the {len(have)} race laps with a stored battle read; no "
                    "battle to compare.")
        why = "nearest to you on the most race laps"
    key = votes.most_common(1)[0][0]
    o = field_[key]
    per_zone: dict[str, list[tuple[str, Zone]]] = {}
    compared, far, untimed, absent, pitted, elsewhere = [], 0, 0, 0, 0, 0
    for name, b in have:
        me = me_of(b)
        his = next((c for c in b.laps if not c.is_player and ident(c) == key), None)
        zones = zone_deltas(me, his, b.corners) if his is not None else []
        if his is None:
            absent += 1
        elif holds is not None and key not in holds[name]:
            elsewhere += 1  # #185: that lap he was not at the place asked about
        elif not zones and zone_deltas(dataclasses.replace(me, pit=[]),
                                       dataclasses.replace(his, pit=[]), b.corners):
            pitted += 1     # timed for both, but every zone overlapped a pit-lane span
        elif not zones:
            untimed += 1
        elif not _was_near(me, his):
            far += 1
        else:
            compared.append(name)
            for z in zones:
                per_zone.setdefault(z.label, []).append((name, z))
    skipped = [f"{n} {why_}" for n, why_ in (
        (len(laps) - len(have), "with no stored battle read"),
        (absent, "with him not recorded"),
        (elsewhere, f"with him not {_SIDE[direction] if direction else f'P{position}'}"),
        (pitted, "with you or him in the pit lane"),
        (far, f"with him never within {BATTLE_RANGE_S:.0f} s of you"),
        (untimed, "with no corner timed for both of you")) if n]
    lines = [(f"{head}Rival ({why}): {o.driver} ({o.model or o.car}), P{o.class_position} "
              "in class on the last lap he was recorded."),
             f"Compared on {len(compared)} of {len(laps)} race laps"
             + (f" ({', '.join(compared)})" if compared else "")
             + (f"; skipped {', '.join(skipped)}" if skipped else "") + "."]
    if not compared:
        return "\n".join(lines + ["No race lap to compare him on."])
    rows = []
    for label, ents in per_zone.items():
        d = statistics.median(z.delta for _, z in ents)
        if round(abs(d), 2) <= BATTLE_NOISE_FLOOR_S:
            continue
        apex = [z.apex_gain_kph for _, z in ents if z.apex_gain_kph is not None]
        beyond = [n for n, z in ents
                  if (z.delta > 0) == (d > 0) and round(abs(z.delta), 2) > BATTLE_NOISE_FLOOR_S]
        rows.append((d, _zone_text(Zone(label, d, statistics.median(apex) if apex else None),
                                   units)
                     + f" (median over {len(ents)} lap{'s' if len(ents) > 1 else ''}; beyond "
                       f"the noise on {', '.join(beyond)})"))
    his_zones = [t for d, t in sorted(rows) if d < 0]
    mine = [t for d, t in sorted(rows, reverse=True) if d > 0]
    lines.append("That car is quicker: " + ("; ".join(his_zones)
                                            or "nowhere by more than the timing noise") + ".")
    lines.append("You are quicker: " + ("; ".join(mine)
                                        or "nowhere by more than the timing noise") + ".")
    lines.append("Each difference is the median over the laps compared; a median of "
                 f"{BATTLE_NOISE_FLOOR_S:.2f} s or less is within the timing noise and is not "
                 "listed.")
    return "\n".join(lines)
