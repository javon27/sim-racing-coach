"""Race state + strategy for the coach's PTT race tools (#152).

Governing constraint (spec 2026-09-22 §3): a push-to-talk answer is heard seconds
after the question, so every figure here must stay true for LAPS -- lap numbers,
per-lap targets, multi-lap trends -- and it is computed once per committed lap,
never while the driver waits. Code extracts, the model narrates: each projection is
computed here and reaches the model as a fact, stamped with the lap it is as of.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from simcoach.analysis import _WHEELS, LapAnalysis, stint_tire_trend
from simcoach.fuel import SaveAdvice, laps_remaining, stint_save_advice, use_per_lap_from_channel
from simcoach.lmu.live import FieldSnapshot, VehicleState
from simcoach.model_types import LapRecord, RaceSample

# Every rate or projection needs at least this many clean laps in the current stint.
MIN_STINT_LAPS = 2
# The gap trend fits at most this many latest laps spent behind (or ahead of) the SAME car.
GAP_TREND_LAPS = 5
# A gap slope under this, either way, reads as "holding steady". A judgement, not a
# measurement: the class gap is sampled once per lap from the scoring feed. PENDING RIG.
GAP_STEADY_S_PER_LAP = 0.05
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
    and a seconds figure would be false. Unplaced cars (place 0) are ignored."""
    order = sorted((c for c in cars if c.place > 0), key=lambda c: c.place)
    me = next((i for i, c in enumerate(order) if c.is_player), None)
    if me is None:
        return None, None, None, None
    cls = order[me].cls

    def _gap(lo: int, hi: int) -> float | None:
        links = [order[k].gap_next for k in range(lo + 1, hi + 1)]
        return None if any(g is None for g in links) else float(sum(links))

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
    ahead, gap_a, behind, gap_b = class_neighbours(snap.cars)
    s = snap.session
    return RaceSample(
        class_position=player.class_position,
        class_ahead=_who(ahead) if ahead else None, class_gap_ahead=gap_a,
        class_behind=_who(behind) if behind else None, class_gap_behind=gap_b,
        # mEndET <= 0 = no session clock. PENDING RIG: mEndET semantics in a timed race.
        time_left=(s.end_time - s.elapsed) if s.end_time > 0 else None,
        max_laps=s.max_laps,
    )


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
    if slope > -GAP_STEADY_S_PER_LAP:
        return None
    lap = lap_no + math.ceil(gap / -slope)
    return lap if final_lap is None or lap <= final_lap else None


def laps_left(lap_no: int, max_laps: int | None, time_left: float | None,
              pace: float | None) -> int | None:
    """Laps still to drive after lap `lap_no`: a lap-count race's remainder, a timed
    race's clock over the recent clean-lap pace (a lap already started when the clock
    runs out is still driven, so at least 1), or the smaller when both apply. None when
    neither is known -- a timed race with no clean pace yet included.

    ponytail: the driver's own laps, not the leader's. A lapped car's race ends when
    the LEADER takes the flag, so this can over-count by a lap for a lapped car."""
    cands = []
    if max_laps:
        cands.append(max(max_laps - lap_no, 0))
    if time_left is not None and pace:
        cands.append(max(1, math.ceil(time_left / pace)))
    return min(cands) if cands else None


# Tire life runs the worst tire down to this much tread REMAINING. A chosen threshold,
# not a measured limit: the coach says "to 30% tread", never "the tires are done by
# lap N" as a fact (spec §3 get_strategy).
WEAR_FLOOR_PCT = 30.0
# Burn and wear rates average at most this many latest clean laps.
RATE_LAPS = 5


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


def is_clean(record: LapRecord) -> bool:
    """A lap a rate may be measured on: timed, valid, not an out-/in-/partial lap and
    never in the pits (spec: those are excluded from every rate)."""
    return (bool(record.lap_time) and record.valid and record.partial is None
            and not record.pit)


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


def lap_facts(record: LapRecord) -> LapFacts:
    ch = record.channels
    fuel_used, fuel_end = _burn(ch, "fuel")
    energy_used, energy_end = _burn(ch, "virtual_energy")
    cap = float(ch["fuel_capacity"][-1]) if len(ch.get("fuel_capacity", ())) else 0.0
    wear = ({w: float(ch[f"tire_wear_{w}"][-1]) for w in _WHEELS}
            if all(len(ch.get(f"tire_wear_{w}", ())) for w in _WHEELS) else None)
    return LapFacts(lap_no=record.lap_no or 0, lap_time=record.lap_time,
                    clean=is_clean(record), position=record.position, race=record.race,
                    fuel_used=fuel_used, fuel_end=fuel_end, fuel_capacity=cap or None,
                    energy_used=energy_used, energy_end=energy_end, wear_end=wear)


def pit_window(lap_no: int, laps_to_go: int,
               limits: list[tuple[str, float, float | None]]) -> PitWindow | None:
    """The stop window from each limit's (name, laps left on it, laps a full fill or a
    new set lasts). None when every limit reaches the flag.

    latest = the lap the first-running-out limit is still good for. earliest = the
    first lap from which one full service reaches the flag on every limit.
    ponytail: a full service (fuel, energy AND tires) is assumed; a no-tire-change stop
    is not modelled."""
    short = [(name, laps) for name, laps, _ in limits if laps < laps_to_go]
    if not short:
        return None
    binding, laps = min(short, key=lambda x: x[1])
    latest = lap_no + math.floor(laps)
    fills = [fill for _, _, fill in limits]
    earliest = (None if any(f is None for f in fills)
                else max(lap_no + 1, lap_no + laps_to_go - math.floor(min(fills))))
    return PitWindow(latest, earliest, binding, earliest is None or earliest <= latest)


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
    resources = []
    for name, used, end, cap in (
            ("fuel", [f.fuel_used for f in clean], latest.fuel_end, latest.fuel_capacity),
            ("energy", [f.energy_used for f in clean], latest.energy_end, 1.0)):
        rates = [u for u in used if u]
        if len(rates) < MIN_STINT_LAPS or end is None:
            continue
        per = sum(rates) / len(rates)
        resources.append(Resource(name, per, end, laps_remaining(end, per),
                                  cap / per if cap else None))
    advice = save = None
    if resources:
        by = {r.name: r for r in resources}
        inf = float("inf")
        advice = stint_save_advice(by["fuel"].laps if "fuel" in by else inf,
                                   by["energy"].laps if "energy" in by else inf, laps_to_go)
        if not advice.make_it:
            save = advice.save_per_lap_fraction * by[advice.binding_resource].per_lap
    wheel = rem = rate = tire_laps = None
    worn = [f for f in clean if f.wear_end and f.lap_time]
    trend = stint_tire_trend(
        [LapAnalysis(f.lap_time, [], False, wheels={"tire_wear_end": f.wear_end})
         for f in worn],
        [f.lap_time for f in worn])
    if trend["ok"] and latest.wear_end:
        cands = [((latest.wear_end[w] * 100 - WEAR_FLOOR_PCT) / r, w, r)
                 for w, r in trend["wear_pct_per_lap"].items() if r > 0]
        if cands:
            tire_laps, wheel, rate = min(cands)
            rem = latest.wear_end[wheel] * 100
    limits = [(r.name, r.laps, r.per_fill) for r in resources]
    if tire_laps is not None:
        limits.append(("tires", max(tire_laps, 0.0), (100 - WEAR_FLOOR_PCT) / rate))
    window = pit_window(latest.lap_no, laps_to_go, limits) if limits else None
    return Strategy(None, laps_to_go, resources, advice, save, wheel, rem, rate, tire_laps,
                    window)
