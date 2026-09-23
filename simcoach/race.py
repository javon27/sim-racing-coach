"""Race state + strategy for the coach's PTT race tools (#152).

Governing constraint (spec 2026-09-22 §3): a push-to-talk answer is heard seconds
after the question, so every figure here must stay true for LAPS -- lap numbers,
per-lap targets, multi-lap trends -- and it is computed once per committed lap,
never while the driver waits. Code extracts, the model narrates: each projection is
computed here and reaches the model as a fact, stamped with the lap it is as of.
"""
from __future__ import annotations

import math

import numpy as np

from simcoach.lmu.live import FieldSnapshot, VehicleState
from simcoach.model_types import RaceSample

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
