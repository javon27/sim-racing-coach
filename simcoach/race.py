"""Race state + strategy for the coach's PTT race tools (#152).

Governing constraint (spec 2026-09-22 §3): a push-to-talk answer is heard seconds
after the question, so every figure here must stay true for LAPS -- lap numbers,
per-lap targets, multi-lap trends -- and it is computed once per committed lap,
never while the driver waits. Code extracts, the model narrates: each projection is
computed here and reaches the model as a fact, stamped with the lap it is as of.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, replace
from itertools import pairwise

import numpy as np

from simcoach.analysis import _MIN_TIRE_TREND_LAPS, _WHEELS, is_incident_lap
from simcoach.battle import _SIDE, Battle, build_battle, car_laps
from simcoach.car_lookup import resolve_car
from simcoach.corners import CornerMap
from simcoach.format import SUSPENSION_UNSEEN, format_laptime
from simcoach.fuel import (
    SaveAdvice,
    laps_remaining,
    plan_stint,
    ratio_balance,
    stint_save_advice,
    use_per_lap_from_channel,
)
from simcoach.lmu.live import FieldSnapshot, VehicleState
from simcoach.model_types import LapRecord, RaceSample

# Every rate or projection needs at least this many clean laps in the current stint.
MIN_STINT_LAPS = 2
# The gap trend fits at most this many latest laps spent behind (or ahead of) the SAME car.
GAP_TREND_LAPS = 5
# A gap slope under this, either way, reads as "holding steady". A judgement, not a
# measurement: the class gap is sampled once per lap from the scoring feed. PENDING RIG.
GAP_STEADY_S_PER_LAP = 0.05
# A lap-to-lap class-gap jump above this (s) is an event -- his pit stop, a spin, a
# penalty -- not pace, so it starts a new gap trend like a change of car does. PENDING
# RIG: the size of a real lap-to-lap swing in close racing.
GAP_STEP_BREAK_S = 5.0
# "Recent clean-lap pace" = the mean of at most this many latest clean laps.
PACE_LAPS = 3


def _who(car: VehicleState) -> str:
    # The raw entry name, deliberately NOT car_lookup.resolve_car: this runs on the
    # capture thread, and resolve_car can re-scan every result log on disk.
    return f"{car.driver} ({car.car})"


def class_neighbours(cars: list[VehicleState]):
    """(ahead, gap_ahead, behind, gap_behind): the player's in-class neighbours.

    The class gap is the SUM of `gap_next` links along the overall order between the
    two cars. In a multiclass race the car one place up is often another class, so the
    player's own gap_next is not the class gap. A lapped link (read_field sets gap_next
    None when laps_behind_next > 0) makes the gap None: that car is a lap or more away,
    and a seconds figure would be false. #192: so does a negative or non-finite link
    (the rig read -1.853 after the flag) and a summed 0.0, which is unset, not side by
    side. Unplaced cars (place 0) are ignored."""
    # (place, id): read_field's class-rank order, so the class neighbours' class places
    # are the player's -/+ 1 even on a tied place (#208: format_race_state states them).
    order = sorted((c for c in cars if c.place > 0), key=lambda c: (c.place, c.id))
    me = next((i for i, c in enumerate(order) if c.is_player), None)
    if me is None:
        return None, None, None, None
    cls = order[me].cls

    def _gap(lo: int, hi: int) -> float | None:
        links = [order[k].gap_next for k in range(lo + 1, hi + 1)]
        if any(g is None or not math.isfinite(g) or g < 0 for g in links):
            return None
        return float(sum(links)) or None

    a = next((j for j in range(me - 1, -1, -1) if order[j].cls == cls), None)
    b = next((j for j in range(me + 1, len(order)) if order[j].cls == cls), None)
    return (None if a is None else order[a], None if a is None else _gap(a, me),
            None if b is None else order[b], None if b is None else _gap(me, b))


def race_sample(snap: FieldSnapshot) -> RaceSample | None:
    """The race situation in one scoring frame, or None when not live / no player."""
    if not snap.session.live:
        return None
    player = next((c for c in snap.cars if c.is_player), None)
    if player is None:
        return None
    s = snap.session
    # mEndET <= 0 = no session clock. PENDING RIG: mEndET semantics in a timed race.
    clock = (s.end_time - s.elapsed) if s.end_time > 0 else None
    if player.place <= 0:
        # Unplaced (formation, a reset): no class order to read -- the clock only.
        return RaceSample(time_left=clock, max_laps=s.max_laps)
    ahead, gap_a, behind, gap_b = class_neighbours(snap.cars)
    return RaceSample(
        class_position=player.class_position,
        class_ahead=_who(ahead) if ahead else None, class_gap_ahead=gap_a,
        class_behind=_who(behind) if behind else None, class_gap_behind=gap_b,
        time_left=clock,
        max_laps=s.max_laps,
    )


def rival_laps_line(field: FieldSnapshot) -> str | None:
    """#213: the best and last lap of the class cars ahead and behind -- the same
    neighbours `class_neighbours` picks -- and the driver's own, from one live scoring
    frame. LMU's per-session times, so nothing carries across sessions. A time the game
    hasn't set (<= 0) is left out, never '0:00.000'; None when there is none to state."""
    ahead, _, behind, _ = class_neighbours(field.cars)
    parts = []
    for who, best, last in (
            *((f"car {side} in class {_who(c)}", c.best_lap, c.last_lap)
              for side, c in (("ahead", ahead), ("behind", behind)) if c),
            ("yours", field.session.best_lap, field.session.last_lap)):
        t = [f"{k} {format_laptime(v)}" for k, v in (("best", best), ("last", last))
             if v and v > 0]
        if t:
            parts.append(f"{who}: {', '.join(t)}")
    return "Lap times now (live, this session): " + "; ".join(parts) + "." if parts else None


def live_battle_target(cars: list[VehicleState], direction: str | None,
                       position: int | None) -> tuple[VehicleState | None, str]:
    """#186: the car a get_battle `direction`/`position` asks for in the LIVE field --
    used once the driver's class place has changed since the battle's lap, so the car
    he just passed is never 'the car ahead'. (car, why it was picked), or (None, why
    there is none). Class order as the lap commit ranks it (read_field)."""
    me = next(c for c in cars if c.is_player)
    if position:
        if position == me.class_position:
            return None, f"P{position} is you now (live), not a rival."
        hit = next((c for c in cars if c.place > 0 and c.cls == me.cls
                    and c.class_position == position), None)
        return hit, (f"P{position} in class now (live)" if hit else
                     f"No car in your class is P{position} now (live).")
    ahead, _, behind, _ = class_neighbours(cars)
    hit = ahead if direction == "ahead" else behind
    if hit is None:
        return None, (f"You're P{me.class_position} in class now (live): nobody in your "
                      f"class is {'ahead of' if direction == 'ahead' else 'behind'} you.")
    return hit, f"{_SIDE[direction]} in class now (live), you're P{me.class_position}"


def gap_slope(points: list[tuple[int, float]]) -> float | None:
    """Least-squares gap change in s/lap over (lap_no, gap) points; None when fewer
    than MIN_STINT_LAPS distinct laps."""
    if len({x for x, _ in points}) < MIN_STINT_LAPS:
        return None
    xs, ys = zip(*points)
    return float(np.polyfit(xs, ys, 1)[0])


def catch_lap(lap_no: int, gap: float, slope: float, final_lap: int | None) -> int | None:
    """The lap a shrinking gap reaches zero at `slope` (s/lap, negative = shrinking),
    or None: not shrinking faster than the steady band, or not before the flag."""
    if slope > -GAP_STEADY_S_PER_LAP or gap <= 0:
        return None
    # round() first: polyfit's slope carries BLAS-dependent ulp noise, so an exact
    # 6.0 laps could ceil to 7 on one machine and 6 on another (#223).
    lap = lap_no + math.ceil(round(gap / -slope, 9))
    return lap if final_lap is None or lap <= final_lap else None


def laps_left(lap_no: int, max_laps: int | None, time_left: float | None,
              pace: float | None) -> int | None:
    """Laps still to drive after lap `lap_no`: a lap-count race's remainder, a timed
    race's clock over the recent clean-lap pace (a lap already started when the clock
    runs out is still driven, so at least 1), or the smaller when both apply. None when
    neither is known -- a timed race with no clean pace yet included.

    The driver's own laps, not the leader's. A timed race ends at the driver's first
    crossing after the OVERALL LEADER takes the flag, and the leader takes it at his
    first crossing after zero -- so a car he is ahead of on the road can drive one lap
    MORE than this (never fewer). Both plans -- pre-race (#184) and live (#195) -- plan
    on that longer count, stating both (`flag_counts`, `plans_longer`)."""
    cands = []
    if max_laps:
        cands.append(max(max_laps - lap_no, 0))
    if time_left is not None and pace:
        cands.append(max(1, math.ceil(time_left / pace)))
    return min(cands) if cands else None


def flag_counts(n: int, last: int) -> str:
    """#184: a timed race's two counts (see laps_left) -- `n` laps to go, the flag at the
    end of lap `last` or one later."""
    return (f"{n} lap{'s' if n != 1 else ''} if the overall leader takes the flag "
            f"before you finish lap {last}, {n + 1} if not")


# #195: said instead of flag_counts when the driver is P1 overall at the line: the
# leader's flag is his own, so the clock's count is exact.
YOU_LEAD = ("you lead overall (P1 at the end of lap {}), so the flag falls at your first "
            "crossing after the clock runs out")
# #195 round 1: the clock had run out at the end of lap {} -- the leader may already have
# taken the flag (every stored timed race ended on that lap). For P1 he took it there.
CLOCK_OUT = ("the clock ran out before the end of lap {0}: one more lap unless the overall "
             "leader has already taken the flag (then lap {0} was your last)")
RACE_OVER = ("the race is over: you lead overall and crossed the line after the clock ran "
             "out (the end of lap {}) — that was the chequered flag")
# A stop scheduled at the end of the shorter count's flag lap (planned on the longer one).
FLAG_LAP_STOP = ("a stop at the end of lap {0} applies only if the overall leader hasn't "
                 "taken the flag by then — if he has, lap {0} is your last")


def plans_longer(time_left: float, position: int | None) -> bool:
    """#195: a timed race's plan counts laps_left + 1 (both plans, pre-race and live) --
    unless the driver is P1 overall at the line (`position`, unknown = not the leader),
    or the clock had already run out there: the leader's flag then comes within one of
    his laps, never after this car's next crossing (every stored timed race ended on the
    lap it ran out on)."""
    return position != 1 and time_left > 0


# Tire life runs the worst tire down to this much tread REMAINING. A chosen threshold,
# not a measured limit: the coach says "to 30% tread", never "the tires are done by
# lap N" as a fact (spec §3 get_strategy).
WEAR_FLOOR_PCT = 30.0
# Burn and wear rates average at most this many latest clean laps.
RATE_LAPS = 5
# A save target above this fraction of the per-lap burn is not a plan: the driver is
# told a stop is needed instead (and Stops says "one", never "none if you save").
# #195 round 2: 5% is the observed ceiling at a 1-3% pace cost -- every full non-pit lap
# in the store, burn saved vs its sitting's median: p90 3.2%, max 7.0% at +1-3% pace;
# only laps >15% slow (yellows, incidents) saved more than ~8%. Road Atlanta #2 was
# offered a 12.6% cut and pitted. PENDING RIG: no deliberate lift-and-coast stint is
# stored, so the real ceiling of a committed save is unmeasured.
MAX_SAVE_FRACTION = 0.05
# The reserve kept on fuel/energy: the stint-1 stop lap and every refill figure plan
# with it -- the same 5% `fuel.plan_stint` plans with by default. A judgement.
REFILL_MARGIN = 0.05
# The largest believable fuel tank, L. mFuelCapacity's offset is unverified (PENDING
# RIG): a reading that is not finite, not above one lap's burn, below the fuel on board
# or above this is treated as "tank size not reported", never stated.
MAX_FUEL_CAPACITY_L = 250.0


@dataclass
class LapFacts:
    """The few numbers per lap the race tools need, taken at commit so the tracker never
    holds a lap's ~100 full-rate channels in memory."""
    lap_no: int
    lap_time: float | None
    clean: bool
    position: int | None = None
    race: RaceSample | None = None
    fuel_used: float | None = None        # L this lap; None = no clear burn (e.g. topped up)
    fuel_end: float | None = None         # L on board at the line
    fuel_capacity: float | None = None    # L, a full tank; None = not reported
    energy_used: float | None = None      # virtual energy, fraction of a full charge
    energy_end: float | None = None
    wear_end: dict[str, float] | None = None   # tread remaining per wheel, 1.0 = new


@dataclass
class Resource:
    name: str               # "fuel" | "energy"
    per_lap: float          # L/lap, or fraction of a charge per lap
    on_board: float         # L, or fraction of a charge left
    laps: float             # laps what is on board lasts
    per_fill: float | None  # laps a full tank/charge lasts; None = capacity unknown


@dataclass
class PitWindow:
    latest: int             # the last lap to pit at the end of before `binding` runs out
    earliest: int | None    # the first lap one full service still reaches the flag from
    binding: str            # "fuel" | "energy" | "tires"
    one_stop: bool          # False: even a full service at `earliest` can't reach the flag


@dataclass
class Strategy:
    note: str | None             # why nothing is projected (the guards); None = projected
    laps_to_go: int | None
    resources: list[Resource]
    advice: SaveAdvice | None
    save_per_lap: float | None   # in the binding resource's unit; None = it makes the flag
    tire_wheel: str | None       # the tire that reaches WEAR_FLOOR_PCT first
    tire_remaining_pct: float | None
    tire_rate_pct: float | None  # % tread per lap on that tire
    tire_laps: float | None      # laps until it reaches WEAR_FLOOR_PCT
    window: PitWindow | None     # None = no stop needed on these rates
    # #164: laps to WEAR_FLOOR_PCT per wheel that is wearing -- which tires to change.
    tire_laps_by_wheel: dict[str, float] | None = None
    # A measured per-lap burn whose amount ON BOARD is unknown (fuel before race lap 1
    # on a virtual-energy car): stated as a rate, never projected (#161 red-team).
    burn_only: dict[str, float] | None = None


def is_clean(record: LapRecord) -> bool:
    """A lap a rate may be measured on: timed, valid, not an out-/in-/partial lap and
    never in the pits (spec: those are excluded from every rate). #251: nor a lap the
    game kept valid through a spin, a stop on track or contact -- the same detection
    get_stint_events reports -- or a 16 s stop sets the pace. No corner map here, so
    an off (read per corner) doesn't count; a spin or stop it caused does."""
    return (bool(record.lap_time) and record.valid and record.partial is None
            and not record.pit and not is_incident_lap(record, CornerMap([]), None))


def _burn(ch: dict, key: str) -> tuple[float | None, float | None]:
    """(used this lap, left at the line) for a depleting channel; used is None when the
    lap shows no clear burn (flat, or topped up)."""
    if key not in ch or len(ch[key]) < 2:
        return None, None
    end = float(ch[key][-1])
    try:
        return use_per_lap_from_channel(ch[key]), end
    except ValueError:
        return None, end


# A lap whose channels start further round than this was picked up mid-lap (the app or
# capture started, or shared memory reconnected, during it): its lap time is the game's
# own and still true, but its fuel/energy burn covers only part of the lap.
PARTIAL_START_M = 200.0


def picked_up_mid_lap(record: LapRecord) -> bool:
    """True when the lap's channels start past PARTIAL_START_M (see above)."""
    dist = record.channels.get("lap_distance")
    return dist is not None and len(dist) > 0 and float(dist[0]) > PARTIAL_START_M


def lap_facts(record: LapRecord) -> LapFacts:
    ch = record.channels
    fuel_used, fuel_end = _burn(ch, "fuel")
    energy_used, energy_end = _burn(ch, "virtual_energy")
    if picked_up_mid_lap(record):
        # Half a lap's burn read as a full lap's would say "makes the flag" for a car
        # laps short. The amounts AT the line are still right; only the rates go.
        fuel_used = energy_used = None
    cap = float(ch["fuel_capacity"][-1]) if len(ch.get("fuel_capacity", ())) else 0.0
    if not (math.isfinite(cap) and 0 < cap <= MAX_FUEL_CAPACITY_L):
        cap = 0.0
    wear = ({w: float(ch[f"tire_wear_{w}"][-1]) for w in _WHEELS}
            if all(len(ch.get(f"tire_wear_{w}", ())) for w in _WHEELS) else None)
    return LapFacts(lap_no=record.lap_no or 0, lap_time=record.lap_time,
                    clean=is_clean(record), position=record.position, race=record.race,
                    fuel_used=fuel_used, fuel_end=fuel_end, fuel_capacity=cap or None,
                    energy_used=energy_used, energy_end=energy_end, wear_end=wear)


def pit_window(lap_no: int, laps_to_go: int,
               limits: list[tuple[str, float, float | None]],
               margin: float = 0.0) -> PitWindow | None:
    """The stop window from each limit's (name, laps left on it, laps a full fill or a
    new set lasts). None when every limit reaches the flag.

    latest = the lap the first-running-out limit is still good for. earliest = the
    first lap from which one full service reaches the flag on every limit.
    ponytail: a full service (fuel, energy AND tires) is assumed; a no-tire-change stop
    is not modelled.

    `margin` (fuel/energy only): latest keeps that reserve in the tank -- Road Atlanta's
    raw "latest" left 0.3% virtual energy. When that pushes latest before `earliest`,
    the reserve can't be kept on both stints and latest is `earliest` (the refill line
    then says how thin it is)."""
    short = [(name, laps) for name, laps, _ in limits if laps < laps_to_go]
    if not short:
        return None
    binding, laps = min(short, key=lambda x: x[1])
    raw_latest = lap_no + math.floor(laps)
    latest = (raw_latest if binding == "tires"
              else lap_no + math.floor(laps / (1 + margin)))
    fills = [fill for _, _, fill in limits]
    earliest = (None if any(f is None for f in fills)
                else max(lap_no + 1, lap_no + laps_to_go - math.floor(min(fills))))
    if earliest is not None and latest < earliest <= raw_latest:
        latest = earliest
    # Under a lap left on the binding limit: latest == lap_no, i.e. "pit at the next
    # opportunity" -- the first lap a stop can happen is lap_no + 1 either way.
    return PitWindow(latest, earliest, binding,
                     earliest is None or earliest <= max(latest, lap_no + 1))


def strategy(stint: list[LapFacts], laps_to_go: int | None) -> Strategy:
    """Fuel/energy and tires to the flag from the current stint's clean laps."""
    clean = [f for f in stint if f.clean][-RATE_LAPS:]
    empty = Strategy(None, laps_to_go, [], None, None, None, None, None, None, None)
    if len(clean) < MIN_STINT_LAPS:
        empty.note = (f"not enough laps yet: needs {MIN_STINT_LAPS} clean laps in this "
                      "stint (out-, in- and partial laps don't count)")
        return empty
    if laps_to_go is None:
        empty.note = ("race length unknown: no lap count or clock reported, or no clean "
                      "pace yet for a timed race")
        return empty
    latest = stint[-1]
    resources, burn_only = [], {}
    for name, used, end, cap, most in (
            ("fuel", [f.fuel_used for f in clean], latest.fuel_end, latest.fuel_capacity,
             MAX_FUEL_CAPACITY_L),
            ("energy", [f.energy_used for f in clean], latest.energy_end, 1.0, 1.0)):
        rates = [u for u in used if u]
        if len(rates) < MIN_STINT_LAPS:
            continue
        per = sum(rates) / len(rates)
        if end is None:
            burn_only[name] = per
            continue
        # cap >= end, with a sliver of slack: a reading a hair over the tank is rounding.
        full = (cap is not None and math.isfinite(cap) and per < cap <= most
                and cap >= end - 0.05 * cap)
        resources.append(Resource(name, per, end, laps_remaining(end, per),
                                  cap / per if full else None))
    advice = save = None
    if resources:
        by = {r.name: r for r in resources}
        inf = float("inf")
        advice = stint_save_advice(by["fuel"].laps if "fuel" in by else inf,
                                   by["energy"].laps if "energy" in by else inf, laps_to_go)
        if not advice.make_it and advice.save_per_lap_fraction <= MAX_SAVE_FRACTION:
            save = advice.save_per_lap_fraction * by[advice.binding_resource].per_lap
    wheel = rem = rate = tire_laps = by_wheel = None
    worn = [f for f in clean if f.wear_end]
    if len({f.lap_no for f in worn}) >= _MIN_TIRE_TREND_LAPS and latest.wear_end:
        # Fitted on LAP NUMBER, not sample index: a non-clean lap between two clean
        # ones (track limits, say) is left out of the series but still wore the tire.
        laps = [f.lap_no for f in worn]
        cands = []
        for w in _WHEELS:
            r = -float(np.polyfit(laps, [f.wear_end[w] * 100 for f in worn], 1)[0])
            if r > 0:
                cands.append(((latest.wear_end[w] * 100 - WEAR_FLOOR_PCT) / r, w, r))
        if cands:
            by_wheel = {w: laps_ for laps_, w, _ in cands}
            tire_laps, wheel, rate = min(cands)
            rem = latest.wear_end[wheel] * 100
    limits = [(r.name, r.laps, r.per_fill) for r in resources]
    if tire_laps is not None:
        limits.append(("tires", max(tire_laps, 0.0), (100 - WEAR_FLOOR_PCT) / rate))
    window = (pit_window(latest.lap_no, laps_to_go, limits, REFILL_MARGIN)
              if limits else None)
    return Strategy(None, laps_to_go, resources, advice, save, wheel, rem, rate, tire_laps,
                    window, by_wheel, burn_only or None)


def provisional_strategy(practice: list[LapFacts], laps_to_go: int | None,
                         start: LapFacts | None = None, ve: bool | None = None,
                         formation: tuple | None = None) -> Strategy:
    """#161: a pre-race plan before the race stint has MIN_STINT_LAPS clean laps.

    The same `strategy` math, on burn rates from `practice` (this session's clean
    practice/qualifying laps) and the amounts on board in `start` -- the latest race
    lap's, at the line -- or, before any race lap, an ASSUMED start (`format_provisional`
    says so): 100% virtual energy on a car that has it, with the fuel on board unknown
    (the fuel ratio decides it: Road Atlanta started on 73.8 L, not a 120 L tank); a full
    tank only on a fuel-only car. Tires are not projected: practice runs span tire
    changes, so their wear says nothing about this set. `ve`: the car has virtual energy
    (default: a rate shows a VE burn) -- #184: a VE car whose VE burn is not projected
    still has its fuel set by the ratio, never a full tank. `formation` (fuel L, energy
    fraction, ...): #184, an assumed start loses the measured formation lap before the
    green."""
    rates = [replace(f, wear_end=None) for f in practice if f.clean]
    if start is None:
        cap = next((f.fuel_capacity for f in reversed(practice) if f.fuel_capacity), None)
        ve = any(f.energy_used for f in rates) if ve is None else ve
        form_fuel, form_energy = (formation or (None, None))[:2]
        start = LapFacts(lap_no=0, lap_time=None, clean=False,
                         fuel_end=None if ve or cap is None else cap - (form_fuel or 0.0),
                         fuel_capacity=cap,
                         energy_end=1.0 - (form_energy or 0.0) if ve else None)
    else:
        start = replace(start, clean=False, wear_end=None)
    return strategy(rates + [start], laps_to_go)


# #184: the history fallback uses at most this many of the newest clean laps, and stops
# after HISTORY_LOADS reads even with fewer -- each is a full-rate .npz load (~40 ms on
# the rig) while the driver waits.
HISTORY_LAPS = 20
HISTORY_LOADS = 60
# ...and reads at most this many pit/out/in/partial rows a sitting for its fuel ratio.
FILL_ROWS = 6
# Two sittings' lap pace within this, relative, are "the same". A judgement: lap-to-lap
# scatter in one sitting is ~1-2%; Silverstone BMW practice vs race was 17%.
REGIME_TOL = 0.03
# Two fuel ratios (L on board per 1% virtual energy, read after a fill) within this are
# the same setting. Measured: one setting reads the same to 0.1-0.5% at every fill of a
# session (Road Atlanta 0.75/1.00, Sarthe 0.87, Long Beach 0.99, Algarve 0.85).
FILL_TOL = 0.02
# A sample counts only while the car drives: parked in the garage the channels show
# menu/loading values -- 1.0 L at 100% VE, the previous session's last state (Long Beach
# 2026-09-26 read Sarthe's 56.55 L / 65% for 13 minutes), a 76 L / 100% preview.
MOVING_KMH = 10.0
# A fuel step bigger than this between two samples is not a burn (a refill, a garage
# reset, a new session's state): the tank as last set starts after the last one.
FUEL_STEP_L = 0.5


@dataclass
class HistLap:
    """One stored clean lap the history fallback reads."""
    ts: float
    phase: str | None
    session: str | None
    facts: LapFacts


def departure(record: LapRecord) -> tuple[float, float] | None:
    """#184: (virtual energy, fuel L) at the row's highest-VE sample while the car moves,
    in the last stretch between FUEL_STEP_L steps that has one -- right after a fill,
    the tank as the fuel ratio set it. A session's first row can open on the LAST
    session's car still at speed (Long Beach 2026-09-26 row 7641: Sarthe's 17.29 L / 19%
    at 188 km/h); parked, the garage shows placeholders and previews, and a row can end
    on one. ponytail: a row that is ONLY such a tail then parked would read the tail --
    none seen in the store. None without VE, fuel and speed channels or such a
    sample."""
    ch = record.channels
    ve, fu, sp = ch.get("virtual_energy"), ch.get("fuel"), ch.get("speed")
    if ve is None or fu is None or sp is None or not len(ve):
        return None
    ve, fu, sp = np.asarray(ve), np.asarray(fu), np.asarray(sp)
    cuts = [0, *(np.flatnonzero(np.abs(np.diff(fu)) > FUEL_STEP_L) + 1), len(fu)]
    for lo, hi in reversed(list(pairwise(cuts))):
        v, f = ve[lo:hi], fu[lo:hi]
        ok = (sp[lo:hi] > MOVING_KMH) & (v > 0)
        if ok.any():
            m = float(v[ok].max())
            return m, float(f[ok & (v >= m - 1e-6)].max())
    return None


def fill_ratio(deps) -> float | None:
    """#184: the fuel ratio a sitting ran at -- L per 1% virtual energy at the fullest of
    its `departure`s (a fill; the on-board ratio drifts as both drain: Road Atlanta 0.75
    at the fill, 0.84 twenty laps on). None without one."""
    deps = [d for d in deps if d]
    if not deps:
        return None
    v, f = max(deps)
    return f / (v * 100)


def _when(laps: list[HistLap]) -> str:
    first = min(laps, key=lambda h: h.ts)
    return (time.strftime("%Y-%m-%d %H:%M", time.localtime(first.ts))
            + (f" {first.phase}" if first.phase else ""))


def _sittings(laps: list[HistLap]) -> list[list[HistLap]]:
    """`laps` grouped by (session, phase), newest group first: a practice and a race of
    one stored session are two sittings (Silverstone BMW: 56.9 s vs 66.9 s laps)."""
    groups: dict = {}
    for h in sorted(laps, key=lambda h: -h.ts):
        groups.setdefault((h.session, h.phase), []).append(h)
    return list(groups.values())


def _med(xs) -> float | None:
    xs = [x for x in xs if x]
    return float(np.median(xs)) if len(xs) >= MIN_STINT_LAPS else None


def history_pick(hist: list[HistLap], fills: dict, current: float | None
                 ) -> tuple[LapFacts | None, list[str], set]:
    """#184: one lap standing for this car's stored clean laps at this track, the notes
    that say how it was picked, and the sessions its virtual energy came from.

    Fuel L/lap is physical: the median of every lap. Virtual energy per lap is not: the
    pit menu's fuel ratio sets how much VE a litre costs (Road Atlanta 2026-09-23: 2.25
    L/lap at 2.39 %VE with a 1.00 ratio, 3.40 % with 0.75). So VE comes only from laps
    whose sitting's `fills` ratio is within FILL_TOL of `current` -- the live session's,
    read off the tank even before a clean lap. With none (no row of this session, or
    none that shows it), the newest sitting's ratio is used and always stated. A sitting whose
    ratio can't be read is left out once one can; with no ratio read anywhere the VE is
    pooled and that is said. The lap time comes from the same pool (else every lap); when
    its sittings' paces differ by more than REGIME_TOL the FASTEST sitting's median is
    used -- too few laps is the dangerous error. A channel measured on fewer than
    MIN_STINT_LAPS laps is None, never projected (a car with no VE reads a flat 0: DKR
    LMP3, Sarthe). The first item is None when neither channel is. The tank size is the
    newest lap's that reports one (callers pass `hist` newest first)."""
    notes: list[str] = []
    if not any(h.facts.fuel_used for h in hist):
        notes.append("No fuel burn recorded in them (the channel is flat or absent), so "
                     "none is projected.")
    if not any(h.facts.energy_used for h in hist):
        notes.append("No virtual-energy burn recorded in them (the channel is flat or "
                     "absent), so none is projected.")
    energy = [h for h in hist if h.facts.energy_used]

    def fill(h):
        return fills.get((h.session, h.phase))
    ref, head = current, "at your current fuel ratio"  # read off this session's tank
    if ref is None and energy:
        known = [h for h in energy if fill(h)]
        if known:
            newest = max(known, key=lambda h: h.ts)
            ref, head = fill(newest), "at your newest stored fuel ratio"
    if ref is not None:
        keep = [h for h in energy if fill(h) and abs(fill(h) / ref - 1) <= FILL_TOL]
        other = [g for g in _sittings([h for h in energy if fill(h) and h not in keep])]
        unknown = [h for h in energy if not fill(h)]
        energy = keep
        left = "".join([
            "; " + ", ".join(f"{len(g)} at fuel ratio {fill(g[0]):.2f} on {_when(g)}"
                             for g in other) + " left out" if other else "",
            f"; {len(unknown)} whose fuel ratio isn't recorded left out" if unknown else ""])
        if left or current is None:
            notes.append(f"Virtual energy: {len(keep)} stored lap{'s' if len(keep) != 1 else ''}"
                         f" {head} ({ref:.2f} L per 1% on board after a fill, within "
                         f"{FILL_TOL:.0%}){left}"
                         + ("" if current is not None else
                            f" -- measured at {ref:.2f}: at another ratio this figure is "
                            "wrong, so set your fuel ratio before trusting it") + ".")
    elif energy:
        notes.append("Virtual energy: the fuel ratio these laps ran at isn't recorded -- at "
                     "another ratio this figure is wrong.")
    e = _med(h.facts.energy_used for h in energy)
    if e is None and any(h.facts.energy_used for h in hist):
        notes.append(f"Virtual energy: only {len(energy)} clean lap"
                     f"{'s' if len(energy) != 1 else ''} to measure it on at this fuel ratio "
                     f"-- it needs {MIN_STINT_LAPS}, so none is projected yet.")
    f = _med(h.facts.fuel_used for h in hist)
    if f is None and e is None:
        return None, notes, set()
    pool = energy if e is not None else hist
    paces = sorted((float(np.median([h.facts.lap_time for h in g])), _when(g))
                   for g in _sittings(pool))
    if len(paces) > 1 and paces[-1][0] / paces[0][0] - 1 > REGIME_TOL:
        pace = paces[0][0]
        notes.append(f"Lap time: {format_laptime(pace)}, the median of your {paces[0][1]} "
                     "session -- the fastest of your sessions here, which differ by more "
                     f"than {REGIME_TOL:.0%} (too slow a lap would count too few laps).")
    else:
        pace = float(np.median([h.facts.lap_time for h in pool]))
    return (LapFacts(lap_no=0, lap_time=pace, clean=True, fuel_used=f, energy_used=e,
                     fuel_capacity=next((h.facts.fuel_capacity for h in hist
                                         if h.facts.fuel_capacity), None)),
            notes, {h.session for h in energy})


def formation_burn(record: LapRecord) -> dict[str, float | None]:
    """#184: {"fuel": L, "energy": fraction} a formation-lap row burned, first sample to
    last; None where the channel is absent, rose (a refill), or the lap was picked up
    mid-way. 0.0 is a measurement: LMU burned no VE on the formation from 2026-09-23."""
    out = {}
    for name, key in (("fuel", "fuel"), ("energy", "virtual_energy")):
        ch = record.channels.get(key)
        d = float(ch[0] - ch[-1]) if ch is not None and len(ch) >= 2 else -1.0
        out[name] = d if d >= 0 and not picked_up_mid_lap(record) else None
    return out


def pick_formation(rows: list[tuple[float, str | None, dict]], energy_sessions: set | None
                   ) -> tuple[float | None, float | None, str, str | None]:
    """#184: (fuel L, energy fraction, where from, how the energy was picked) for the
    formation lap, from stored formation rows (ts, session, formation_burn), newest first.
    Fuel (physical): the median of every row. Energy: only rows from `energy_sessions`
    (None = any) -- the laps' fuel-ratio partition -- the median, or the LARGEST when they
    disagree, each stated (Sarthe: 2.4% on 09-17/18, 0 on 09-26): an extra 2.4% at the
    start is cheap, running short eats half the margin."""
    fuels = [b["fuel"] for _, _, b in rows if b["fuel"] is not None]
    fuel = float(np.median(fuels)) if fuels else None
    ves = [(b["energy"], ts) for ts, s, b in rows if b["energy"] is not None
           and (energy_sessions is None or s in energy_sessions)]
    vals = [v for v, _ in ves]
    energy, how = (float(np.median(vals)) if vals else None), None
    if vals and max(vals) - min(vals) > REGIME_TOL * max(vals):
        energy = max(vals)
        by: dict = {}
        for v, ts in sorted(ves, key=lambda x: x[1]):
            by.setdefault(round(v * 100, 1), []).append(
                time.strftime("%Y-%m-%d", time.localtime(ts)))
        how = ("stored formation laps differ: " + " or ".join(
            f"{v:.1f}% ({', '.join(dict.fromkeys(d))})" for v, d in by.items())
            + f"; planning {energy * 100:.1f}%")
    days = sorted({time.strftime("%Y-%m-%d", time.localtime(ts)) for ts, _, _ in rows})
    return fuel, energy, (f"{len(rows)} stored formation lap{'s' if len(rows) != 1 else ''} "
                          f"here in this car, {', '.join(days)}"), how


_WHEEL_NAME = {"fl": "front-left", "fr": "front-right", "rl": "rear-left", "rr": "rear-right"}


@dataclass
class GapRead:
    who: str
    gap: float | None           # s now; None = lapped link, or an unset/negative one
    slope: float | None         # s/lap over the trend laps; negative = the gap is shrinking
    catch_lap: int | None       # the lap it reaches zero at that slope, before the flag
                                # (#201: the longer count's, as build_snapshot plans)


@dataclass
class RaceState:
    position: int | None
    class_position: int | None
    max_laps: int | None
    time_left: float | None
    pace: float | None          # recent clean-lap pace, s
    laps_to_go: int | None
    ahead: GapRead | None
    behind: GapRead | None
    sampled: bool = True        # False: the race sample was missing on the latest lap
    timed: bool = False         # #195: the clock, not a lap count, sets laps_to_go


@dataclass
class RaceSnapshot:
    """Everything the race tools answer from, as of one committed lap."""
    as_of_lap: int
    session_id: str | None
    state: RaceState
    strategy: Strategy
    # Whose race this is (#152 red-team): a snapshot outlives its race, so the sidecar
    # only serves it while the live track/car match (see live_race).
    track: str | None = None
    car: str | None = None
    battle: Battle | None = None   # #152 get_battle: the lap's same-class timing, or None
    battle_note: str | None = None  # why there is no battle, said instead of one


def _gap_read(stint: list[LapFacts], side: str, final_lap: int | None) -> GapRead | None:
    """The in-class car ahead/behind now, and the gap trend over the laps spent with
    that SAME car there -- a new car starts a new trend. Clean laps only."""
    who_key, gap_key = f"class_{side}", f"class_gap_{side}"
    latest = stint[-1]
    who = getattr(latest.race, who_key) if latest.race else None
    if who is None:
        return None
    points = []
    newer = None                     # the gap one lap later than f, walking back
    for f in reversed(stint):
        if f.race is None or getattr(f.race, who_key) != who:
            break
        gap = getattr(f.race, gap_key)
        if gap is not None and newer is not None and abs(newer - gap) > GAP_STEP_BREAK_S:
            break                    # a step, not a trend: his stop, a spin, a penalty
        if gap is not None:
            newer = gap
        if f.clean and gap is not None:
            points.append((f.lap_no, gap))
    gap = getattr(latest.race, gap_key)
    slope = gap_slope(points[:GAP_TREND_LAPS])
    caught = (catch_lap(latest.lap_no, gap, slope, final_lap)
              if slope is not None and gap is not None else None)
    return GapRead(who, gap, slope, caught)


def build_snapshot(stint: list[LapFacts], session_id: str | None,
                   track: str | None = None, car: str | None = None,
                   battle: Battle | None = None,
                   battle_note: str | None = None) -> RaceSnapshot:
    latest = stint[-1]
    r = latest.race or RaceSample()
    clean = [f for f in stint if f.clean]
    recent = clean[-PACE_LAPS:]
    pace = (sum(f.lap_time for f in recent) / len(recent)
            if len(clean) >= MIN_STINT_LAPS else None)
    to_go = laps_left(latest.lap_no, r.max_laps, r.time_left, pace)
    timed = bool(to_go is not None and pace and r.time_left is not None
                 and not (r.max_laps and to_go == max(r.max_laps - latest.lap_no, 0)))
    # #195: a timed race is planned on the longer count, as the pre-race plan is (#184);
    # #201: so is the gap trend's catch cutoff.
    plan = to_go + 1 if timed and plans_longer(r.time_left, latest.position) else to_go
    final = latest.lap_no + plan if plan is not None else None
    state = RaceState(latest.position, r.class_position, r.max_laps, r.time_left, pace, to_go,
                      _gap_read(stint, "ahead", final), _gap_read(stint, "behind", final),
                      latest.race is not None, timed)
    try:
        strat = strategy(stint, plan)
    except Exception:
        # Computed apart from the race state, so one bad channel can't cost it too.
        logging.getLogger(__name__).warning("race strategy not computed", exc_info=True)
        strat = Strategy("strategy unavailable this lap (a calculation failed)", plan, [],
                         None, None, None, None, None, None, None)
    return RaceSnapshot(latest.lap_no, session_id, state, strat, track, car, battle,
                        battle_note)


class RaceTracker:
    """The current race stint's per-lap facts, and the ONE cached snapshot the race
    tools read.

    `commit` runs once per stored lap, off the capture thread (the sidecar hands it to a
    one-worker pool); the tools only read `snapshot`, so nothing is computed while the
    driver waits. The snapshot is replaced by a single assignment, so a reader on another
    thread sees the old one or the new one, never a half-built one.

    ponytail: in memory only. After a sidecar restart mid-race the tools say "not enough
    laps yet" until two clean laps land; seed from the store if that ever matters."""

    def __init__(self) -> None:
        self._stint: list[LapFacts] = []
        self._key: tuple | None = None
        self.snapshot: RaceSnapshot | None = None

    def commit(self, record: LapRecord,
               cmap: CornerMap | None = None) -> RaceSnapshot | None:
        if record.phase != "race" or record.lap_no is None:
            # Practice, qualy: there is no live race to answer about.
            self._stint, self._key, self.snapshot = [], None, None
            return None
        facts = lap_facts(record)
        key = (record.session_id, record.track, record.car)
        restarted = key != self._key or bool(
            self._stint and facts.lap_no <= self._stint[-1].lap_no)
        # A stop starts a new stint: rates from before it describe other tires and fuel.
        if restarted or record.pit or record.partial in ("out", "in"):
            self._stint = []
        self._key = key
        self._stint.append(facts)
        battle, note = self._battle(record, cmap, facts.lap_no)
        self.snapshot = build_snapshot(self._stint, record.session_id, record.track,
                                       record.car, battle, note)
        return self.snapshot

    @staticmethod
    def _battle(record: LapRecord, cmap: CornerMap | None,
                n: int) -> tuple[Battle | None, str | None]:
        """(the lap's battle, or None and the reason said instead). One lap of driving
        is compared: never an out-/in-/partial lap or a stop (an invalidated lap still
        counts -- the fight was real). A failure costs the battle, never the snapshot."""
        if cmap is None or not cmap.corners:
            return None, "there is no corner map for this track"
        kind = {"out": f"lap {n} was an out-lap", "in": f"lap {n} was an in-lap"}
        if record.partial is not None:
            return None, kind.get(record.partial, f"lap {n} was only partly captured")
        if record.pit:
            return None, f"lap {n} included a pit stop"
        if not record.field_trace:
            return None, f"the field of cars was not recorded on lap {n}"
        try:
            laps = car_laps(record.field_trace, cmap.corners, n)
            for c in laps:
                c.model = resolve_car(c.car)   # off the capture thread: this can scan logs
            battle = build_battle(laps, cmap.corners)
        except Exception:
            logging.getLogger(__name__).warning("battle read not built", exc_info=True)
            return None, f"the battle read failed on lap {n}"
        if battle is None:
            return None, f"none of lap {n}'s corners were timed from the scoring feed"
        return battle, None


NO_RACE_LAP_YET = ("Not enough laps yet: no completed lap of this race has been measured "
                   "since the app started -- race state and strategy start from the next "
                   "completed race lap.")


def live_race(snap: RaceSnapshot | None, *, track: str | None, car: str | None,
              phase: str, current_lap: int | None) -> tuple[RaceSnapshot | None, str | None]:
    """(the snapshot to answer from, or the note to say instead), for the race being
    driven NOW. A cached snapshot outlives its race: after a race at Spa it is still
    there in the garage at Monza, and on the formation lap of the next race at Spa.

    Served only when the live session is a race at the snapshot's track/car and the lap
    in progress (`current_lap` = completed laps + 1, as live.read_field reports it) is
    the one right after the snapshot's lap -- or one more, while that lap's commit is
    still in flight on the race pool. Not a race -> (None, None): "no live race". A race
    with no matching snapshot (a new race, or stale) -> (None, NO_RACE_LAP_YET)."""
    if phase != "race":
        return None, None
    fresh = (snap is not None and current_lap is not None
             and (snap.track or "").strip() == (track or "").strip()
             and (snap.car or "").strip() == (car or "").strip()
             and current_lap - 1 in (snap.as_of_lap, snap.as_of_lap + 1))
    return (snap, None) if fresh else (None, NO_RACE_LAP_YET)


def _clock(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60}:{s % 60:02d}"


def _gap_line(label: str, g: GapRead | None, side: str, placed: bool,
              counts: tuple[int, int] | None = None, catch: bool = True) -> str:
    """`counts` = flag_counts' (laps to go, flag lap) when the race is planned on the
    longer count (#201): a catch after that flag lap is said to be conditional.
    `catch` False (#192: a live seconds gap is stated beside this line) cuts the catch-lap
    projection, which is off the lap's gap: only the per-lap rate is said."""
    if not placed:
        return f"{label}: not sampled this lap."
    if g is None:
        return f"{label}: none" + (" — you lead your class." if side == "ahead" else ".")
    if g.gap is None:
        return f"{label}: {g.who}, no seconds figure (a lap or more away, or the game hasn't set it)."
    head = f"{label}: {g.who}, {g.gap:.1f} s {side}"
    if g.slope is None:
        return (f"{head}; trend: not enough laps yet (needs {MIN_STINT_LAPS} clean laps with "
                "this car there, in this stint).")
    if abs(g.slope) < GAP_STEADY_S_PER_LAP:
        return f"{head}; holding steady (under {GAP_STEADY_S_PER_LAP:.2f} s/lap either way)."
    closing = g.slope < 0
    if side == "ahead":
        trend = f"you are {'closing' if closing else 'losing'} {abs(g.slope):.2f} s/lap"
        reach = "you reach him"
    else:
        trend = f"he is {'closing' if closing else 'dropping back'} {abs(g.slope):.2f} s/lap"
        reach = "he reaches you"
    if not catch:
        return f"{head}; {trend}."
    if g.catch_lap is not None:
        trend += f"; at this rate {reach} around lap {g.catch_lap}"
        if counts and g.catch_lap > counts[1]:
            trend += f", only if the race runs that long (laps to go: {flag_counts(*counts)})"
    elif closing:
        trend += "; not before the flag at this rate"
    return f"{head}; {trend}."


def format_race_state(snap: RaceSnapshot,
                      live: tuple[int, RaceSample] | None = None) -> str:
    """`live` = (overall place, race_sample) of the scoring frame read as the tool
    answers (#186), or None. The snapshot is up to a lap old: when the place or a class
    neighbour has changed since its lap, the position now leads the answer -- never the
    lap-old one against the driver's own word -- and a neighbour that is a different car
    now is named with its lap-old gap and trend CUT (a stale value is cut, not hedged).
    A neighbour still the same car keeps the lap's gap and trend, labelled with the lap.
    #192: every live neighbour gets the frame's class gap, labelled live -- the scoring
    gaps update every tick (rig, Laguna 2026-10-02), so the frame's sum of links is the
    gap now. A lapped link has no seconds figure. Trends and catch laps stay per-lap."""
    st = snap.state
    who = {side: g.who if g else None for side, g in (("ahead", st.ahead),
                                                        ("behind", st.behind))}
    now = None
    moved = False
    if live is not None and live[1].class_position is None:
        live = None     # #208: an unplaced frame (formation, a reset) has no class order
    if live is not None:
        now = {"ahead": live[1].class_ahead, "behind": live[1].class_behind}
        moved = (live[0], live[1].class_position, now) != (st.position, st.class_position,
                                                            who)
    lines = [f"As of lap {snap.as_of_lap} (race)."]
    if not moved:
        pos = f"P{st.position}" if st.position else "unknown"
        cls = f"P{st.class_position}" if st.class_position else "unknown"
        lines.append(f"Position: {pos} overall, {cls} in class.")
    else:
        lines.insert(0, f"Position now (live): P{live[0]} overall, "
                        f"P{live[1].class_position} in class.")
    if not st.sampled:
        lines.append("Remaining: not sampled this lap.")
    if st.max_laps:
        lines.append(f"Remaining: {max(st.max_laps - snap.as_of_lap, 0)} of {st.max_laps} laps.")
    if st.time_left is not None:
        clock_laps = max(1, math.ceil(st.time_left / st.pace)) if st.pace else None
        if st.time_left <= 0:
            # #195 round 1: after the clock, the leader may already have taken the flag.
            lines.append(f"Remaining: none — {RACE_OVER.format(snap.as_of_lap)}."
                         if st.position == 1 else
                         f"Remaining: {CLOCK_OUT.format(snap.as_of_lap)}.")
        elif st.pace and st.max_laps and max(st.max_laps - snap.as_of_lap, 0) < clock_laps:
            lines.append(f"Remaining: {_clock(st.time_left)} on the clock (the lap count "
                         "ends the race first).")
        elif st.pace:
            lines.append(f"Remaining: {_clock(st.time_left)} on the clock, about "
                         f"{clock_laps} laps at your recent clean pace "
                         f"({format_laptime(st.pace)}/lap).")
        else:
            lines.append(f"Remaining: {_clock(st.time_left)} on the clock; laps left need "
                         f"{MIN_STINT_LAPS} clean laps in this stint for a pace.")
    if st.sampled and not st.max_laps and st.time_left is None:
        lines.append("Remaining: the game reports no lap count or clock for this race.")
    placed = st.sampled and st.class_position is not None
    # A catch past the shorter count's flag lap is only ever projected when the plan is
    # the longer count (build_snapshot's cutoff), so `timed` is enough here.
    counts = (st.laps_to_go, snap.as_of_lap + st.laps_to_go) if st.timed else None
    for side, g in (("ahead", st.ahead), ("behind", st.behind)):
        label = f"Car {side} in class"
        # #208: his class place, stated so the model never makes one up ('he's P16 while
        # you're P16'). He is the next car of the class by place, and class places rank
        # by place (lmu.live.read_field): yours -/+ 1.
        step = -1 if side == "ahead" else 1
        if now is None:
            his = f" (P{st.class_position + step})" if g and placed else ""
            lines.append(_gap_line(label + his, g, side, placed, counts))
        elif now[side] is None:
            lines.append(f"{label} now (live): none"
                         + (" — you lead your class." if side == "ahead" else "."))
        else:
            gap = getattr(live[1], f"class_gap_{side}")
            head = (f"{label} now (live, P{live[1].class_position + step}): {now[side]}, "
                    + (f"{gap:.1f} s {side}" if gap is not None else
                       "no seconds figure (a lap or more away, or the game hasn't set it)"))
            if now[side] == who[side]:      # an unsampled lap says so, not a trend rule
                lines.append(head + ".")
                his = f" (P{st.class_position + step})" if st.class_position else ""
                lines.append(_gap_line(f"{label}{his} at the end of lap {snap.as_of_lap}",
                                       g, side, placed, counts, catch=gap is None))
            else:
                lines.append(f"{head}; no trend yet — a trend needs {MIN_STINT_LAPS} clean "
                             "laps with him there.")
    return "\n".join(lines)


def format_strategy(snap: RaceSnapshot, damage: dict | None = None,
                    damage_lap: int | None = None) -> str:
    """The race stint's projection plus the stop as pit-menu settings (#164).
    `damage` is `analysis.damage_state` of lap `damage_lap` (None/{} = not captured)."""
    s, st, lap = snap.strategy, snap.state, snap.as_of_lap
    head = f"As of lap {lap}."
    if st.timed and st.time_left <= 0 and st.position == 1:
        return f"{head} No projection: {RACE_OVER.format(lap)}."
    if s.note:
        return f"{head} No projection: {s.note}."
    longer = st.timed and s.laps_to_go != st.laps_to_go
    first = lap + st.laps_to_go if longer else None
    plan = _strategy_lines(s, lap, first)
    if longer:
        # #195: planned on the longer count (build_snapshot), worded as the pre-race plan.
        plan[0] = (f"Laps to go: planned on {s.laps_to_go}, the longer count (the flag at "
                   f"the end of lap {lap + s.laps_to_go} at the latest): "
                   f"{flag_counts(st.laps_to_go, lap + st.laps_to_go)}.")
    elif st.timed and st.time_left <= 0:
        plan[0] = f"Laps to go: 1 — {CLOCK_OUT.format(lap)}."
    elif st.timed and st.position == 1:
        plan[0] = plan[0][:-1] + ": " + YOU_LEAD.format(lap) + "."
    return "\n".join([head] + plan + _pit_menu_lines(s, lap, damage, damage_lap, True, first))


def format_provisional(s: Strategy, as_of_lap: int, basis: str, n_practice: int,
                       damage: dict | None = None, damage_lap: int | None = None,
                       source: str | None = None, notes: tuple | list = (),
                       measured: bool | None = None, timed: bool = False,
                       formation: tuple | None = None, ve_car: bool = False,
                       clock_out: bool = False) -> str:
    """#161: `provisional_strategy`'s plan, labelled as an estimate. `basis` says where
    the race length came from. #184: `source` where the rates came from (default this
    session's practice laps) and `notes` how they were picked; `measured` whether the
    start amounts were read off a stored race row (default: after race lap 1) -- else a
    full fill is ASSUMED, less the formation lap; `timed` a timed race, which can run a
    lap longer than counted (see laps_left) -- `s` is then planned on the LONGER count
    and the need lines give both; `formation` pick_formation's (fuel L, energy
    fraction, where from, how) for the formation lap; `ve_car` the car has virtual
    energy (the fuel ratio sets its fuel); `clock_out` (#195) the clock had run out at
    the end of race lap `as_of_lap`."""
    measured = as_of_lap > 0 if measured is None else measured
    source = source or (f"your last {n_practice} clean practice/qualifying laps this "
                        "session")
    head = f"PROVISIONAL — from {source}; firms up after {MIN_STINT_LAPS} clean race laps."
    if s.note:
        return "\n".join([f"{head} No projection: {s.note}.", *notes])
    by = {r.name: r for r in s.resources}
    if as_of_lap:
        start = (f"As of race lap {as_of_lap}: what was on board at the line, burned at "
                 "those rates.")
    elif measured:
        start = ("As of the start line: " + ", ".join(
            f"{r.on_board * 100:.0f}% virtual energy" if r.name == "energy"
            else f"{r.on_board:.1f} L" for r in sorted(s.resources, key=lambda r: r.name))
            + " on board.")
    elif "energy" in by:
        start = ("Assumes you start on 100% virtual energy"
                 + (f" ({by['energy'].on_board * 100:.1f}% at the green, after the "
                    "formation lap)" if by["energy"].on_board < 1 else "")
                 + "; the fuel on board is unknown until race lap 1.")
    elif "fuel" in by:
        start = ("Assumes you start on a full tank"
                 + (f" ({by['fuel'].on_board:.1f} L at the green, after the formation lap)"
                    if (formation or (None,))[0] else "") + ".")
    elif ve_car:
        start = ("No start amount is assumed: on a virtual-energy car the fuel ratio sets "
                 "the fuel on board, unknown until race lap 1.")
    else:
        start = "No start amount can be assumed (no tank size reported)."
    first = as_of_lap + s.laps_to_go - 1 if timed else None
    plan = _strategy_lines(s, as_of_lap, first)
    if timed:
        # The longer count is planned: a line saying the flag falls at its end as a fact
        # would read the shorter race as ruled out.
        plan[0] = (f"Laps to go: planned on {s.laps_to_go}, the longer count (the flag at "
                   f"the end of lap {as_of_lap + s.laps_to_go} at the latest).")
    elif clock_out:
        plan[0] = f"Laps to go: 1 — {CLOCK_OUT.format(as_of_lap)}."
    return "\n".join([head, *notes, f"Race length: {basis}.", start]
                     + ([] if measured else _need_lines(s, timed, formation))
                     + plan
                     # No fuel ratio read off an ASSUMED start: it needs what is on board.
                     + _pit_menu_lines(s, as_of_lap, damage, damage_lap, measured, first))


def _need_lines(s: Strategy, timed: bool, formation: tuple | None) -> list[str]:
    """#184: before the start, 'how much energy for this race' -- what the race needs,
    with the arithmetic's inputs, so the model narrates it, never works it out. A timed
    race gives both counts, N and the planned N+1 (laps_left). The amount to start with
    rounds UP, as the pit-menu line does."""
    fuel_f, energy_f, where, how = formation or (None, None, "", None)
    rates = {r.name: r.per_lap for r in s.resources} | (s.burn_only or {})
    form = {"fuel": fuel_f, "energy": energy_f}
    got = [n for n in rates if form[n] is not None]
    lines = []
    if got:
        lines.append("Formation lap: " + ", ".join(
            f"{form[n] * 100:.1f}% virtual energy" + (f" ({how})" if how else "")
            if n == "energy" else f"{form[n]:.2f} L" for n in got) + f" (from {where}).")
    ks = (s.laps_to_go - 1, s.laps_to_go) if timed else (s.laps_to_go,)
    for name, per in rates.items():
        pct, fm = name == "energy", form[name]

        def amt(x, pct=pct):
            return f"{x * 100:.1f}%" if pct else f"{x:.1f} L"
        parts = []
        for i, k in enumerate(ks):
            tot = per * k + (fm or 0.0)
            full = tot * (1 + REFILL_MARGIN)
            rate = f" × {per * 100:.1f}%/lap" if pct else f" × {per:.2f} L/lap"
            parts.append(f"{k} lap{'s' if k != 1 else ''}{rate if i == 0 else ''} = "
                         f"{amt(per * k)}"
                         + (f" + {amt(fm)} formation lap = {amt(tot)}" if fm is not None else "")
                         + " → start with at least "
                         + (f"{math.ceil(full * 100 - 1e-9)}%" if pct
                            else f"{math.ceil(full - 1e-9)} L")
                         + (" — more than a full charge" if pct and full > 1 else ""))
        lines.append(("Virtual energy" if pct else "Fuel") + " needed to the flag: "
                     + "; or ".join(parts) + f" (with the {REFILL_MARGIN:.0%} margin"
                     + ("" if fm is not None else "; plus the formation lap, not measured")
                     + ").")
    return lines


def _pit_menu_lines(s: Strategy, as_of_lap: int, damage: dict | None,
                    damage_lap: int | None, ratio: bool,
                    first_flag: int | None = None) -> list[str]:
    """#164: a stop as the PIT-MENU settings the driver makes before it -- LMU has no crew
    radio. Every figure is computed here from the measured rates. #195: a stop a save
    avoids is set only in case he doesn't save, and one at the end of `first_flag` (the
    shorter count's flag lap, when planned on the longer) only if the flag hasn't fallen.

    Virtual energy % is the level the stop fills to (research on #164: TinyPedal reads
    the menu's "VIRTUAL ENERGY:" entry as the absolute refill). The fuel ratio is given as
    a DIRECTION only (`ratio_balance`): its unit/formula is unconfirmed, so no number."""
    w = s.window
    # #164 red-team: a wheel the car reports flat or detached is named, measured.
    dmg = damage or {}
    hurt = {k: "reads flat" for k in dmg.get("wheels_flat", ())}
    hurt.update({k: "reads detached" for k in dmg.get("wheels_detached", ())})
    hurt_line = (("Tyres: change " + ", ".join(f"{k.upper()} — {why}" for k, why in
                                               hurt.items())
                  + f" on lap {damage_lap}.") if hurt else None)
    if w is None:
        head = ("Pit menu: no stop needed on these rates — nothing to set."
                if s.resources else "Pit menu: no fuel/energy projection — nothing computed.")
        if hurt_line:
            head = head.removesuffix(" — nothing to set.") + " for fuel/energy or wear, but:"
        return [head] + ([hurt_line] if hurt_line else [])
    by = {r.name: r for r in s.resources}
    lines = []
    stop = max(w.latest, as_of_lap + 1)
    after = as_of_lap + s.laps_to_go - stop
    pm = f"Pit menu, {IF_NOT_SAVING}:" if _save_instead(s) else "Pit menu:"
    tail = f" Note: {FLAG_LAP_STOP.format(stop)}." if stop == first_flag else ""
    r = by.get("energy") or by.get("fuel")
    what = "virtual energy" if r is not None and r.name == "energy" else "fuel"
    if r is None:
        lines.append("Pit menu: no fuel/energy burn measured — no refill figure.")
    elif not w.one_stop:
        lines.append(f"{pm} set {what} to "
                     + ("100%" if r.name == "energy" else "a full tank")
                     + " — one stop can't reach the flag from there.")
    else:
        cap = 1.0 if r.name == "energy" else (r.per_fill * r.per_lap if r.per_fill
                                               else math.inf)
        plan = plan_stint(r.per_lap, cap, 0.0, 1.0, margin=REFILL_MARGIN,
                          laps_override=after, resource=r.name)
        pct = r.name == "energy"
        if plan.total_needed > cap:
            # plan_stint caps at the tank: say how thin a full fill is, never hide it.
            spare = cap - r.per_lap * after
            full = "100%" if pct else f"a full tank ({cap:.0f} L)"
            left = f"{spare * 100:.1f}%" if pct else f"{spare:.1f} L"
            lines.append(f"{pm} set {what} to {full} for a stop at the end of lap {stop} "
                         f"— a full fill covers {cap / r.per_lap:.1f} laps for the "
                         f"{after} from there to the flag: only {left} spare, under the "
                         f"{REFILL_MARGIN:.0%} margin.{tail}")
        else:
            amt, step = plan.start_amount, r.per_lap * (1 + REFILL_MARGIN)
            amount, step = ((f"{math.ceil(amt * 100 - 1e-9)}%", f"{step * 100:.1f}%") if pct
                            else (f"{math.ceil(amt - 1e-9)} L", f"{step:.1f} L"))
            lines.append(f"{pm} set {what} to {amount} for a stop at the end of lap {stop} "
                         f"(the {after} laps from there to the flag, "
                         f"+{REFILL_MARGIN:.0%} margin); each lap earlier needs about "
                         f"{step} more.{tail}")
    if "energy" in by and ("fuel" in by or "fuel" in (s.burn_only or {})):
        if not ratio or "fuel" not in by:
            lines.append("Fuel ratio: not read before a race lap (needs the fuel and "
                         "energy actually on board).")
        else:
            fuel, nrg = by["fuel"].laps, by["energy"].laps
            ra = ratio_balance(fuel, nrg)
            txt = f"Fuel ratio: fuel on board lasts {fuel:.1f} laps, virtual energy {nrg:.1f}"
            if ra.shift_toward == "balanced":
                txt += " — within half a lap: leave the ratio as it is."
            elif ra.shift_toward == "fuel":
                txt += (" — fuel runs out first: shift the ratio toward fuel (more fuel "
                        "per % of virtual energy).")
            else:
                txt += (" — energy runs out first: shift the ratio toward energy (less "
                        "fuel per % of virtual energy).")
            lines.append(txt + " Direction only: the ratio's unit isn't confirmed, so no "
                               "number.")
    if s.tire_laps is None:
        wear = (f"wear not measured in this race stint yet (needs {_MIN_TIRE_TREND_LAPS} "
                "clean laps with wear data) — no call" + (" on the others" if hurt else ""))
    else:
        short = {k for k, v in (s.tire_laps_by_wheel or {}).items()
                 if v < s.laps_to_go and k not in hurt}
        change = [k.upper() for k in _WHEELS if k in short]
        keep = [k.upper() for k in _WHEELS if k not in short and k not in hurt]
        if change:
            wear = (f"change {', '.join(change)} "
                    f"({'it reaches' if len(change) == 1 else 'they reach'} the "
                    f"{WEAR_FLOOR_PCT:.0f}% threshold before the flag on the measured wear)"
                    + (f"; keep {', '.join(keep)}" if keep else ""))
        else:
            wear = (("the others stay" if hurt else "no change needed — every tire stays")
                    + f" above the {WEAR_FLOOR_PCT:.0f}% threshold to the flag on the "
                    "measured wear")
    # One Tyres line: the measured flat/detached wheel first, then the wear call.
    lines.append(f"{hurt_line[:-1]}; {wear}." if hurt_line else f"Tyres: {wear}.")
    if not damage:
        lines.append("Repairs: no damage data captured.")
    elif (damage.get("dent_max", 0) > 0 or damage.get("detached")
          or damage.get("wheels_flat") or damage.get("wheels_detached")):
        lines.append(f"Repairs: damage measured on lap {damage_lap} (dent severity max "
                     f"{damage.get('dent_max', 0):.0f}, raw 0\N{EN DASH}255 scale, panel "
                     "unknown) — whether to set repairs on is your call; the time a repair "
                     f"adds isn't measured here; {SUSPENSION_UNSEEN}.")
    else:
        # #206: no verdict from dent severity -- real hits of magnitude 9k-18k ended
        # at dent 0, heavier than the 3,336 that cost FR suspension at Long Beach.
        lines.append(f"Repairs: no bodywork damage or flat/detached wheel measured as of "
                     f"lap {damage_lap}; {SUSPENSION_UNSEEN} — check the game's damage "
                     "display before leaving repairs off.")
    return lines


# Under this many laps either side of the flag a margin isn't measurable: "short by 0.0
# laps; save 0.00 L/lap" beside "Stops: one" (#195 round 1, Algarve lap 3) read as a stop.
ON_LIMIT_LAPS = 0.05
# Heads the pit window and pit menu when a save avoids the stop (`_save_instead`).
IF_NOT_SAVING = "only if you stop instead of saving"


def _save_instead(s: Strategy) -> str | None:
    """#195: what saving avoids the stop with ("save 0.02 L/lap"), or None when a stop
    is needed whatever: no realistic save (MAX_SAVE_FRACTION) or a tire short of the
    flag. Within ON_LIMIT_LAPS the amount is below what the line can state."""
    if s.save_per_lap is None or (s.tire_laps is not None and s.tire_laps < s.laps_to_go):
        return None
    r = next(r for r in s.resources if r.name == s.advice.binding_resource)
    if r.laps - s.laps_to_go > -ON_LIMIT_LAPS:
        return "save a little (you're right on the limit)"
    return _save_amount(s)


def _save_amount(s: Strategy) -> str:
    """The save target and its share of the burn (#195 round 2: "0.3% energy/lap" alone
    read as trivial when it was 13% of the lap's energy)."""
    frac = s.advice.save_per_lap_fraction
    share = "under 1%" if frac < 0.005 else f"{frac:.0%}"
    amt = (f"{s.save_per_lap:.2f} L/lap" if s.advice.binding_resource == "fuel"
           else f"{s.save_per_lap * 100:.1f}% energy/lap")
    return f"save {amt} ({share} of your burn)"


def _strategy_lines(s: Strategy, as_of_lap: int, first_flag: int | None = None) -> list[str]:
    """`first_flag` (#195): the shorter count's flag lap, when `s` is planned on the
    longer -- a stop at its end is said to apply only if the flag hasn't fallen."""
    final = as_of_lap + s.laps_to_go
    lines = [f"Laps to go: {s.laps_to_go} (flag at the end of lap {final})."]
    binding = s.advice.binding_resource if s.advice and not s.advice.make_it else None
    save = _save_amount(s) if s.save_per_lap is not None else ""   # the binding's unit
    avoid = _save_instead(s)
    for r in s.resources:
        if r.name == "fuel":
            txt = f"Fuel: {r.per_lap:.2f} L/lap, {r.on_board:.1f} L on board = {r.laps:.1f} laps"
        else:
            txt = (f"Virtual energy: {r.per_lap * 100:.1f}%/lap, {r.on_board * 100:.0f}% left "
                   f"= {r.laps:.1f} laps")
        margin = r.laps - s.laps_to_go
        if margin >= ON_LIMIT_LAPS:
            txt += f": makes the flag with {margin:.1f} laps spare."
        elif margin > -ON_LIMIT_LAPS:
            txt += ": right on the limit — no measurable margin to the flag."
        elif r.name != binding:
            txt += f": short by {-margin:.1f} laps."
        elif save:
            txt += f": short by {-margin:.1f} laps; {save} to finish without stopping."
        else:
            txt += (f": short by {-margin:.1f} laps; a stop is needed (saving that much — "
                    f"over {MAX_SAVE_FRACTION:.0%} a lap — is not realistic).")
        lines.append(txt)
    for name, per in (s.burn_only or {}).items():
        rate = f"{per:.2f} L/lap" if name == "fuel" else f"{per * 100:.1f}%/lap"
        lines.append(f"{'Fuel' if name == 'fuel' else 'Virtual energy'}: {rate} measured; "
                     "the amount on board is unknown, so no laps-left figure.")
    if not s.resources and not s.burn_only:
        lines.append("Fuel/energy: no clear per-lap burn measured in this stint.")
    if s.tire_laps is not None and s.tire_laps <= 0:
        lines.append(f"Tires: worst is the {_WHEEL_NAME[s.tire_wheel]} at "
                     f"{s.tire_remaining_pct:.0f}% tread, wearing {s.tire_rate_pct:.1f}%/lap: "
                     f"already at or below the {WEAR_FLOOR_PCT:.0f}% threshold (a chosen "
                     "threshold, not a measured limit).")
    elif s.tire_laps is not None:
        lines.append(f"Tires: worst is the {_WHEEL_NAME[s.tire_wheel]} at "
                     f"{s.tire_remaining_pct:.0f}% tread, wearing {s.tire_rate_pct:.1f}%/lap: "
                     f"about {max(s.tire_laps, 0):.0f} laps to the {WEAR_FLOOR_PCT:.0f}% "
                     "threshold (a chosen threshold, not a measured limit).")
    else:
        lines.append(f"Tires: no wear rate yet (needs {_MIN_TIRE_TREND_LAPS} clean laps with "
                     "wear data in this stint).")
    w = s.window
    limit = (f"tires reach the {WEAR_FLOOR_PCT:.0f}% threshold" if w and w.binding == "tires"
             else f"{w.binding} runs out" if w else "")
    if w is None and not s.resources:
        lines.append("Pit window: no fuel/energy projection, so no stop call.")
    elif w is None:
        lines.append("Pit window: no stop needed on these rates"
                     + (" (tires not yet measured)." if s.tire_laps is None else "."))
    elif w.latest <= as_of_lap:
        now = (f"the {_WHEEL_NAME[s.tire_wheel]} tire is at or past the "
               f"{WEAR_FLOOR_PCT:.0f}% threshold" if w.binding == "tires"
               else f"{w.binding} has under a lap left")
        lines.append(f"Pit window: pit at the next opportunity: {now}."
                     + ("" if w.one_stop else " One stop can't reach the flag from there "
                        "— more than one stop is needed."))
    elif not w.one_stop:
        lines.append(f"Pit window: pit by lap {w.latest} ({limit} first); one "
                     "stop can't reach the flag from there — more than one stop is needed.")
    elif w.earliest is None:
        lines.append(f"Pit window: pit by lap {w.latest} ({limit} first); the "
                     "earliest lap is unknown (tank size not reported).")
    else:
        set_by = (f"latest: the lap the {limit}" if w.binding == "tires"
                  else f"latest set by {w.binding}")
        lines.append(f"Pit window: laps {w.earliest} to {w.latest} ({set_by}; earliest is the first lap one full service reaches the "
                     "flag from).")
    if w is not None and first_flag == max(w.latest, as_of_lap + 1):
        lines[-1] = lines[-1][:-1] + f"; {FLAG_LAP_STOP.format(first_flag)}."
    if w is not None and avoid:
        # #195 round 2: the same clause the pit menu carries -- a window a save avoids.
        lines[-1] = lines[-1].replace("Pit window:", f"Pit window, {IF_NOT_SAVING}:", 1)
    # #162: the stop COUNT is stated here, never left to the model -- "a stop is needed"
    # beside a one-stop window read as "one stop won't be enough" (Road Atlanta lap 11).
    if w is None and s.resources:
        lines.append("Stops to the flag: none on these rates.")
    elif w is not None:
        n = ("more than one." if not w.one_stop else "one." if w.earliest is not None
             else "not known (tank size not reported).")
        # #195 round 1: a stop a realistic save avoids is not "one" -- the model repeats
        # this line (Algarve #12 lap 2: "Stops: one" for 0.02 L/lap; he won with 1.1 spare).
        lines.append("Stops to the flag: " + (f"none if you {avoid}; otherwise {n}" if avoid
                                              else n))
    return lines
