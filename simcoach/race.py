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
from dataclasses import dataclass, replace

import numpy as np

from simcoach.analysis import _MIN_TIRE_TREND_LAPS, _WHEELS
from simcoach.battle import Battle, build_battle, car_laps
from simcoach.car_lookup import resolve_car
from simcoach.corners import CornerMap
from simcoach.format import format_laptime
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
# A save target above this fraction of the per-lap burn is not a plan: the driver is
# told a stop is needed instead. A judgement (lift-and-coast buys a few percent).
MAX_SAVE_FRACTION = 0.15
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
                         start: LapFacts | None = None) -> Strategy:
    """#161: a pre-race plan before the race stint has MIN_STINT_LAPS clean laps.

    The same `strategy` math, on burn rates from `practice` (this session's clean
    practice/qualifying laps) and the amounts on board in `start` -- the latest race
    lap's, at the line -- or, before any race lap, an ASSUMED start (`format_provisional`
    says so): 100% virtual energy on a car that has it, with the fuel on board unknown
    (the fuel ratio decides it: Road Atlanta started on 73.8 L, not a 120 L tank); a full
    tank only on a fuel-only car. Tires are not projected: practice runs span tire
    changes, so their wear says nothing about this set."""
    rates = [replace(f, wear_end=None) for f in practice if f.clean]
    if start is None:
        cap = next((f.fuel_capacity for f in reversed(practice) if f.fuel_capacity), None)
        ve = any(f.energy_used for f in rates)
        start = LapFacts(lap_no=0, lap_time=None, clean=False,
                         fuel_end=None if ve else cap, fuel_capacity=cap,
                         energy_end=1.0 if ve else None)
    else:
        start = replace(start, clean=False, wear_end=None)
    return strategy(rates + [start], laps_to_go)


_WHEEL_NAME = {"fl": "front-left", "fr": "front-right", "rl": "rear-left", "rr": "rear-right"}


@dataclass
class GapRead:
    who: str
    gap: float | None           # s now; None = a lap or more away
    slope: float | None         # s/lap over the trend laps; negative = the gap is shrinking
    catch_lap: int | None       # the lap it reaches zero at that slope, before the flag


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
    final = latest.lap_no + to_go if to_go is not None else None
    state = RaceState(latest.position, r.class_position, r.max_laps, r.time_left, pace, to_go,
                      _gap_read(stint, "ahead", final), _gap_read(stint, "behind", final),
                      latest.race is not None)
    try:
        strat = strategy(stint, to_go)
    except Exception:
        # Computed apart from the race state, so one bad channel can't cost it too.
        logging.getLogger(__name__).warning("race strategy not computed", exc_info=True)
        strat = Strategy("strategy unavailable this lap (a calculation failed)", to_go, [],
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


def _gap_line(label: str, g: GapRead | None, side: str, placed: bool) -> str:
    if not placed:
        return f"{label}: not sampled this lap."
    if g is None:
        return f"{label}: none" + (" — you lead your class." if side == "ahead" else ".")
    if g.gap is None:
        return f"{label}: {g.who}, a lap or more away."
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
    if g.catch_lap is not None:
        trend += f"; at this rate {reach} around lap {g.catch_lap}"
    elif closing:
        trend += "; not before the flag at this rate"
    return f"{head}; {trend}."


def format_race_state(snap: RaceSnapshot) -> str:
    st = snap.state
    pos = f"P{st.position}" if st.position else "unknown"
    cls = f"P{st.class_position}" if st.class_position else "unknown"
    lines = [f"As of lap {snap.as_of_lap} (race).", f"Position: {pos} overall, {cls} in class."]
    if not st.sampled:
        lines.append("Remaining: not sampled this lap.")
    if st.max_laps:
        lines.append(f"Remaining: {max(st.max_laps - snap.as_of_lap, 0)} of {st.max_laps} laps.")
    if st.time_left is not None:
        clock_laps = max(1, math.ceil(st.time_left / st.pace)) if st.pace else None
        if st.time_left <= 0:
            lines.append("Remaining: the clock has run out — this is the last lap.")
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
    lines.append(_gap_line("Car ahead in class", st.ahead, "ahead", placed))
    lines.append(_gap_line("Car behind in class", st.behind, "behind", placed))
    return "\n".join(lines)


def format_strategy(snap: RaceSnapshot, damage: dict | None = None,
                    damage_lap: int | None = None) -> str:
    """The race stint's projection plus the stop as pit-menu settings (#164).
    `damage` is `analysis.damage_state` of lap `damage_lap` (None/{} = not captured)."""
    s = snap.strategy
    head = f"As of lap {snap.as_of_lap}."
    if s.note:
        return f"{head} No projection: {s.note}."
    return "\n".join([head] + _strategy_lines(s, snap.as_of_lap)
                     + _pit_menu_lines(s, snap.as_of_lap, damage, damage_lap, True))


def format_provisional(s: Strategy, as_of_lap: int, basis: str, n_practice: int,
                       damage: dict | None = None, damage_lap: int | None = None) -> str:
    """#161: `provisional_strategy`'s plan, labelled as an estimate. `as_of_lap` 0 = no
    race lap yet (a full fill is assumed); `basis` says where the race length came from."""
    head = (f"PROVISIONAL — from your last {n_practice} clean practice/qualifying laps this "
            f"session; firms up after {MIN_STINT_LAPS} clean race laps.")
    if s.note:
        return f"{head} No projection: {s.note}."
    names = {r.name for r in s.resources}
    if as_of_lap:
        start = (f"As of race lap {as_of_lap}: what was on board at the line, burned at "
                 "your practice/qualifying rates.")
    elif "energy" in names:
        start = ("Assumes you start on 100% virtual energy; the fuel on board is unknown "
                 "until race lap 1.")
    elif "fuel" in names:
        start = "Assumes you start on a full tank."
    else:
        start = "No start amount can be assumed (no tank size reported)."
    # No fuel ratio read off an ASSUMED start: it needs what is really on board.
    return "\n".join([head, f"Race length: {basis}.", start]
                     + _strategy_lines(s, as_of_lap)
                     + _pit_menu_lines(s, as_of_lap, damage, damage_lap, as_of_lap > 0))


def _pit_menu_lines(s: Strategy, as_of_lap: int, damage: dict | None,
                    damage_lap: int | None, ratio: bool) -> list[str]:
    """#164: a stop as the PIT-MENU settings the driver makes before it -- LMU has no crew
    radio. Every figure is computed here from the measured rates.

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
    r = by.get("energy") or by.get("fuel")
    what = "virtual energy" if r is not None and r.name == "energy" else "fuel"
    if r is None:
        lines.append("Pit menu: no fuel/energy burn measured — no refill figure.")
    elif not w.one_stop:
        lines.append(f"Pit menu: set {what} to "
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
            lines.append(f"Pit menu: set {what} to {full} for a stop at the end of lap "
                         f"{stop} — a full fill covers {cap / r.per_lap:.1f} laps for the "
                         f"{after} from there to the flag: only {left} spare, under the "
                         f"{REFILL_MARGIN:.0%} margin.")
        else:
            amt, step = plan.start_amount, r.per_lap * (1 + REFILL_MARGIN)
            amount, step = ((f"{math.ceil(amt * 100 - 1e-9)}%", f"{step * 100:.1f}%") if pct
                            else (f"{math.ceil(amt - 1e-9)} L", f"{step:.1f} L"))
            lines.append(f"Pit menu: set {what} to {amount} for a stop at the end of lap "
                         f"{stop} (the {after} laps from there to the flag, "
                         f"+{REFILL_MARGIN:.0%} margin); each lap earlier needs about "
                         f"{step} more.")
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
                     "adds isn't measured here.")
    else:
        lines.append(f"Repairs: none measured as of lap {damage_lap} — leave repairs off.")
    return lines


def _strategy_lines(s: Strategy, as_of_lap: int) -> list[str]:
    final = as_of_lap + s.laps_to_go
    lines = [f"Laps to go: {s.laps_to_go} (flag at the end of lap {final})."]
    binding = s.advice.binding_resource if s.advice and not s.advice.make_it else None
    for r in s.resources:
        if r.name == "fuel":
            txt = f"Fuel: {r.per_lap:.2f} L/lap, {r.on_board:.1f} L on board = {r.laps:.1f} laps"
            save = f"save {s.save_per_lap:.2f} L/lap" if s.save_per_lap is not None else ""
        else:
            txt = (f"Virtual energy: {r.per_lap * 100:.1f}%/lap, {r.on_board * 100:.0f}% left "
                   f"= {r.laps:.1f} laps")
            save = (f"save {s.save_per_lap * 100:.1f}% energy/lap"
                    if s.save_per_lap is not None else "")
        margin = r.laps - s.laps_to_go
        if margin >= 0.05:
            txt += f": makes the flag with {margin:.1f} laps spare."
        elif margin >= 0:
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
    # #162: the stop COUNT is stated here, never left to the model -- "a stop is needed"
    # beside a one-stop window read as "one stop won't be enough" (Road Atlanta lap 11).
    if w is None and s.resources:
        lines.append("Stops to the flag: none on these rates.")
    elif w is not None:
        lines.append("Stops to the flag: " + (
            "more than one." if not w.one_stop else "one." if w.earliest is not None
            else "not known (tank size not reported)."))
    return lines
