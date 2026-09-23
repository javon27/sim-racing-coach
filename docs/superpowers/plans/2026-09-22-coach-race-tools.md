# Coach Race Tools Implementation Plan (#152)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The coach answers race questions over PTT with three tools: `get_race_state` (position, class position, remaining, the in-class car ahead and behind with the gap trend and catch lap), `get_strategy` (fuel and energy to the flag, the pit window, tire life to a named wear threshold) and `get_battle` (where a same-class rival gains or loses on the last completed lap). All three read one snapshot. The snapshot is recomputed once per committed race lap and stamped `as_of_lap`, so no work happens while the driver waits and every figure stays true for laps.

**Architecture:** The pure math lives in two new core modules. `simcoach/race.py` holds the race sample, the gap trend, the remaining laps, fuel and energy to the flag, the pit window, tire life, `RaceTracker` and the text formatters. `simcoach/battle.py` holds the field trace, corner crossings, zone deltas, rival resolution and its formatter. Capture (`LmuSource._poll_once`) already reads the full scoring struct on every poll. At commit it runs `read_field` on that same frame and puts a `RaceSample` on the `LapRecord` (Part A). On every new scoring frame it also feeds a `FieldTrace` with same-class cars (Part B). No second reader is added. The sidecar's `_capture_loop` saves the lap (the new race columns are schema v21, the `battle_laps` table is v22). It then hands the lap to a one-worker pool that calls `RaceTracker.commit`, never on the capture thread. The tools read `ctx.race`, the cached `RaceSnapshot`. `coach_ws` passes it in only while the game is live.

**Tech Stack:** Python 3.13, numpy, SQLite (stdlib `sqlite3`), FastAPI sidecar, pytest, ruff 0.16.8.

**Spec:** `docs/superpowers/specs/2026-09-22-coach-absorbs-pit-wall-design.md` §3 ("Strategy tools") governs, including its latency constraint. §1/§2 shipped in #151 and are out of scope.

## Global Constraints

- **Work in a worktree on a branch.** The main checkout holds the driver's uncommitted files (`simcoach/tracks/weathertech_raceway_laguna_seca.json`, `src-tauri/Cargo.toml`, untracked `simcoach/tracks/fuji_speedway.json` and `simcoach/tracks/silverstone_national_circuit.json`). Never touch, stage, stash or reset them. Set up the worktree like this: `git worktree add ../sim-racing-coach-152 -b feat/152-coach-race-tools main`. Then, inside it, run `python -m venv .venv && .venv/Scripts/python.exe -m pip install -e ".[dev]"`. Before the first test run of every task, run `.venv/Scripts/python.exe -c "import simcoach; print(simcoach.__file__)"` and confirm the path is inside `sim-racing-coach-152` (memory: "stale worktrees + venv crosstalk").
- **Stage by name only.** Never `git add -A`, `git add .`, `git stash`, `git reset` or `git checkout -- <file>`. Commit each task's work before running anything that could revert it.
- **Every commit uses `Refs #152`, never `Fixes #152`.** This work is rig-verify: the noise floor, `end_time` and the class gaps must be checked at the rig (see "Rig check"), and the issue is closed by hand after that. Commit trailer, exactly:
  ```
  Refs #152

  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01Pywcy5ZyceRZNDLyjn3F35
  ```
- **Code extracts, the model narrates.** Every rate, projection, ranking and rival choice is computed in `race.py`/`battle.py`. Tool text is compact facts and always starts with `As of lap N`.
- **Only measured values are stated.** A value that was not measured is left out, not hedged: a lapped class gap becomes "a lap or more away", and a rival's untimed corner is skipped. `WEAR_FLOOR_PCT` is always labelled as a chosen threshold. Constants that are pending the rig (`GAP_STEADY_S_PER_LAP`, `BATTLE_NOISE_FLOOR_S`, `APEX_SPEED_FLOOR_KPH`, `BATTLE_RANGE_S`) say so in their comments.
- **Latency rule (spec §3).** Figures stay valid for laps: lap numbers, per-lap targets, multi-lap trends. Never report an instantaneous gap or a tactical call. PTT only: no proactive callouts.
- **UI-agnostic core.** All logic lives in `simcoach/race.py` and `simcoach/battle.py`. The sidecar only wires the pool, and the tools only format.
- **Never slow the capture thread.** `source.laps()` is suspended while `_capture_loop`'s body runs. Race work goes to `app.state.race_pool`. On the capture thread, only `read_field` runs (pure, no I/O), and only on a new scoring frame. `car_lookup.resolve_car` is never called there, because it can re-scan every result log.
- **Gates for every task:** `.venv/Scripts/python.exe -m pytest -q` green and `ruff check simcoach tests scripts` clean.
- **Part A (Tasks 1–6) ships on its own:** race state and strategy. **Part B (Tasks 7–11)** adds the battle read on top of Part A. Each task leaves the tree green and is reviewable alone.

## Decisions made while planning (recorded; the driver was not available)

1. **The class gap is summed through the overall order.** In multiclass, the car one place up is often another class, so the player's own `gap_next` is not the class gap. The class gap ahead is the sum of `gap_next` over every car from the player up to the class car ahead. The gap behind is the same sum from the class car behind up to the player. If any link is lapped (`gap_next is None`), the gap is `None`, stated as "a lap or more away", never as a seconds figure. Rig check: when `mTimeBehindNext` updates, and whether class gaps match the in-game standings.
2. **The race sample comes from the scoring frame capture already holds.** The spec says live values come from `read_field_snapshot()`. That function is only `read_field()` over a fresh MMF read. Capture already has the committing frame in hand, so it calls `read_field(scoring)` on it. That is the same mapping on the same frame that holds `mLastLapTime` and `mTotalLaps`, with no second reader.
3. **What is stored:** seven nullable columns on `laps` (v21): `class_position`, `class_ahead`, `class_gap_ahead`, `class_behind`, `class_gap_behind`, `time_left`, `max_laps`. Overall position reuses the existing `position` column. "Who" is stored as `"driver (entry name)"`, raw and not resolved to a car model (see the capture-thread rule).
4. **The ledger is in memory.** `RaceTracker` keeps small per-lap `LapFacts`, not whole `LapRecord`s (about 4 MB of channels per lap), and it does not reload from the store. After a sidecar restart mid-race, the tools say "not enough laps yet" until two clean laps land. The spec's 2-lap guard already covers this. Seeding from the store is a follow-up.
5. **What counts as clean and as a stint.** A clean lap is timed, valid, `partial is None` and `pit` falsy. A new stint starts on a pit or out/in lap, a change of session, track or car, or a `lap_no` that goes backwards (a new race in the same sitting). A non-race commit (practice, qualy, mapping) clears the snapshot.
6. **Remaining laps.** For a lap-count race it is `max_laps − lap_no`. For a timed race it is `ceil(time_left / pace)`, at least 1 (a started lap is still driven), where pace is the mean of the last 3 clean laps. When both apply, the smaller wins. It uses the driver's own laps, not the leader's (a `ponytail:` comment notes the one-lap over-count for a lapped car).
7. **Fuel and energy to the flag use `laps_remaining` + `stint_save_advice`, not `plan_race`/`binding_stint`.** Those two plan from a full tank at the race start. Mid-race, what matters is the amount on board now. `stint_save_advice` already picks the binding resource, gives make-it-or-not, and gives the save fraction. Virtual energy is a 0..1 fraction, so a full charge = 1.0. Fuel capacity comes from a new `fuel_capacity` channel (`mFuelCapacity`, offset unverified, a rig item).
8. **Pit window.** `latest` = `lap_no + floor(laps left on the first limit to run out)`. `earliest` = `max(lap_no + 1, final_lap − floor(min laps-per-full-fill over all limits))`. This assumes a full service at the stop (fuel, energy and tires). A `ponytail:` comment notes that a no-tyre-change stop is not modelled. `one_stop` is False when `earliest > latest`.
9. **Tire life.** It uses `stint_tire_trend`'s `wear_pct_per_lap`, fed minimal `LapAnalysis(wheels={"tire_wear_end": ...})` objects, so no second slope implementation exists. The floor is `WEAR_FLOOR_PCT = 30.0`, the spec's own example. `stint_tire_trend` needs 3 laps, which is stricter than the 2-lap guard. That is honest, and the text says it.
10. **Gap trend.** It is a least-squares fit over at most the last 5 clean laps spent behind (or ahead of) the same car. A change of car starts a new trend. A slope under `GAP_STEADY_S_PER_LAP = 0.05` reads as "holding steady". The catch lap is only given when the gap is closing and the catch lands before the flag.
11. **Where the cache lives.** `RaceTracker` is in the core, holding one attribute (`snapshot`) that is swapped by a single assignment. The sidecar owns `app.state.race` and a one-worker `ThreadPoolExecutor` (`app.state.race_pool`), so commits run in order and off the capture thread. Tools get the snapshot via `CoachContext(race=...)`. `coach_ws` passes it only when `read_session_track_car()` says the game is live.
12. **"No live race".** A tool says "No live race" when the game is not live or when there is no race snapshot yet (practice, qualy, or before the first race lap completes). It never guesses.
13. **The battle trace comes from the capture thread's own scoring read.** It is fed only on a new `mCurrentET` (scoring runs at a few Hz, the poll at 50). Only the player's class is recorded. Distance is unwrapped (`laps × track_len + lap_dist`). The trace is handed over at every S/F wrap, and the last 5 s are kept for the next lap. Capture never needs the corner map: crossings are computed at commit, in the race worker.
14. **Pairing crossings across the line.** A rival's crossing of a spot is paired with the driver's by time: the rival's crossing *nearest in time* to the driver's crossing of the same spot. This handles cars crossing S/F at different times. The player is timed from the same scoring feed, so both sides carry the same error. A corner whose window wraps S/F is skipped. The section from the last corner to the first (across the line) is not built. A rival behind who has not reached a late corner when the lap commits has no time there, so that corner is not compared.
15. **Constants pending the rig.** `BATTLE_NOISE_FLOOR_S = 0.10` is the conservative top of the spec's 0.05–0.1 estimate. `APEX_SPEED_FLOOR_KPH = 5.0` and `BATTLE_RANGE_S = 2.0` are also defaults. All three are named constants, and each comment says "PENDING RIG". Only deltas above the floor are reported.
16. **Who "the rival" is.** By default, at each corner entry the nearest rival within 2 s scores a point. The top scorer is the rival. A second is named when it scored at least a third of the timed corners. A named rival: `P<n>` is the **class** position (the field is one class). Anything else is a case-insensitive substring of the driver, entry or resolved model name, with a leading "the"/"that" dropped.
17. **The battle is computed for race laps that were not out/in/partial or pit laps.** An invalidated (track-limits) lap still counts, because the fight was real.
18. **Battle storage.** SQLite table `battle_laps` (v22): one row per (driver's lap, same-class car, driver included), with the crossings as JSON. It is not in the `.npz`, because it is a few dozen numbers per car and not a full-rate channel.
19. **Prompt.** The RACECRAFT paragraph's "never state gaps or traffic — they are not measured" is now false. It becomes "never state traffic, and state a gap only as get_race_state reports it". One RACE TOOLS routing paragraph is added, and Part B extends it with `get_battle`.

## File Structure

- `simcoach/model_types.py`: add `RaceSample`, `LapRecord.race` (Task 1) and `LapRecord.field_trace` (Task 10, transient).
- `simcoach/race.py` (new): Task 1 adds the class gap, race sample, gap slope, catch lap and remaining laps. Task 2 adds `LapFacts`, fuel/energy/tires and the pit window. Task 5 adds `RaceTracker`, the snapshot and the formatters. Task 11 adds the battle hook.
- `simcoach/battle.py` (new): Task 7 adds `FieldTrace` and `car_laps`. Task 8 adds the zone deltas, rivals, `Battle` and `format_battle`.
- `simcoach/store.py`: v21 race columns plus `save` (Task 3); v22 `battle_laps`, `save_battle`/`battle_laps` and `clear_all` (Task 9).
- `simcoach/lmu/smm.py`: the `fuel_capacity` channel and the race sample at commit (Task 4); the field-trace feed (Task 10).
- `simcoach/server/app.py`: `app.state.race`/`race_pool` and `_race_commit` (Task 5); `CoachContext(race=...)` in `coach_ws` (Task 6); `app.state.map_for` and battle persistence (Task 11).
- `simcoach/coach_tools.py`: the `get_race_state`/`get_strategy` schemas, handlers and dispatch (Task 6); `get_battle` (Task 11).
- `simcoach/coach.py`: RACECRAFT amendment plus the RACE TOOLS routing paragraph (Task 6), extended in Task 11.
- `CLAUDE.md`: Layout line for `race.py` (Task 6) and `battle.py` (Task 11).
- Tests: `tests/test_race.py` (new; Tasks 1, 2, 5), `tests/test_battle.py` (new; Tasks 7, 8, 11), `tests/test_store.py` (3, 9), `tests/test_lmu_smm.py` (4, 10), `tests/test_server_capture.py` (5, 11), `tests/test_coach_tools.py` (6, 11), `tests/test_server_ws.py` (6), `tests/test_coach.py` (6, 11).

---

# Part A: race state + strategy

### Task 1: Pure race-state math (class gap, race sample, gap trend, remaining laps)

**Files:**
- Modify: `simcoach/model_types.py`
- Create: `simcoach/race.py`
- Test: `tests/test_race.py` (new)

**Interfaces:**
- Consumes: `simcoach.lmu.live.FieldSnapshot`, `VehicleState` (fields `id, place, class_position, driver, car, cls, lap_dist, speed, laps, is_player, gap_next, laps_behind_next`), `SessionState` (`live, elapsed, end_time, max_laps`).
- Produces:
  - `model_types.RaceSample` (dataclass, all fields default `None`): `class_position: int|None, class_ahead: str|None, class_gap_ahead: float|None, class_behind: str|None, class_gap_behind: float|None, time_left: float|None, max_laps: int|None`.
  - `LapRecord.race: RaceSample | None = None`.
  - `race.MIN_STINT_LAPS = 2`, `GAP_TREND_LAPS = 5`, `GAP_STEADY_S_PER_LAP = 0.05`, `PACE_LAPS = 3`.
  - `class_neighbours(cars: list[VehicleState]) -> tuple[VehicleState|None, float|None, VehicleState|None, float|None]`.
  - `race_sample(snap: FieldSnapshot) -> RaceSample | None`.
  - `gap_slope(points: list[tuple[int, float]]) -> float | None`.
  - `catch_lap(lap_no: int, gap: float, slope: float, final_lap: int | None) -> int | None`.
  - `laps_left(lap_no: int, max_laps: int|None, time_left: float|None, pace: float|None) -> int | None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_race.py
"""#152: pure race-state + strategy math for the coach's race tools."""
import dataclasses

import pytest

from simcoach.lmu.live import FieldSnapshot, VehicleState, empty_snapshot
from simcoach.race import catch_lap, class_neighbours, gap_slope, laps_left, race_sample


def _car(cid, place, cls, gap_next, *, player=False, driver=None, car="Entry",
         class_position=0):
    return VehicleState(
        id=cid, place=place, class_position=class_position, driver=driver or f"D{cid}",
        car=car, cls=cls, lap_dist=0.0, speed=0.0, sector=0, laps=10, in_pits=False,
        is_player=player, best_lap=None, last_lap=None, best_s1=None, best_s2=None,
        gap_leader=None, laps_behind_leader=0, gap_next=gap_next,
        laps_behind_next=0 if gap_next is not None else 1)


# Overall order: P3 GT3 rival, P4 a Hypercar, P5 the player (GT3), P6 GT3 chaser.
# gap_next is each car's gap to the car ONE PLACE UP, as the game reports it.
FIELD = [
    _car(3, 5, "GT3", 1.0, player=True, driver="You", class_position=2),
    _car(1, 3, "GT3", 4.0, driver="A. Rival", car="Rival Entry", class_position=1),
    _car(2, 4, "Hypercar", 0.8),
    _car(4, 6, "GT3", 2.4, driver="B. Chaser", car="Chaser Entry", class_position=3),
]


def _snap(cars, **session):
    s = dataclasses.replace(empty_snapshot().session, live=True, phase="race",
                            track_len=4000.0, **session)
    return FieldSnapshot(session=s, cars=cars)


def test_class_gap_sums_the_links_through_a_car_of_another_class():
    ahead, gap_a, behind, gap_b = class_neighbours(FIELD)
    assert ahead.driver == "A. Rival" and gap_a == pytest.approx(1.8)   # 0.8 + 1.0, not 1.0
    assert behind.driver == "B. Chaser" and gap_b == pytest.approx(2.4)


def test_a_lapped_link_makes_the_class_gap_none_not_a_false_seconds_figure():
    field = [dataclasses.replace(c, gap_next=None, laps_behind_next=1) if c.id == 2 else c
             for c in FIELD]
    ahead, gap_a, _, gap_b = class_neighbours(field)
    assert ahead.driver == "A. Rival" and gap_a is None
    assert gap_b == pytest.approx(2.4)


def test_the_class_leader_has_no_car_ahead_and_the_last_car_none_behind():
    ahead, gap_a, behind, _ = class_neighbours([c for c in FIELD if c.id != 1])
    assert ahead is None and gap_a is None and behind.driver == "B. Chaser"
    _, _, behind, gap_b = class_neighbours([c for c in FIELD if c.id != 4])
    assert behind is None and gap_b is None


def test_race_sample_reads_class_position_neighbours_and_the_clock():
    rs = race_sample(_snap(FIELD, elapsed=1200.0, end_time=3600.0, max_laps=None))
    assert rs.class_position == 2
    assert rs.class_ahead == "A. Rival (Rival Entry)"
    assert rs.class_gap_ahead == pytest.approx(1.8)
    assert rs.class_behind == "B. Chaser (Chaser Entry)"
    assert rs.class_gap_behind == pytest.approx(2.4)
    assert rs.time_left == pytest.approx(2400.0) and rs.max_laps is None


def test_race_sample_has_no_clock_without_an_end_time_and_is_none_when_not_live():
    rs = race_sample(_snap(FIELD, elapsed=1200.0, end_time=0.0, max_laps=24))
    assert rs.time_left is None and rs.max_laps == 24
    assert race_sample(empty_snapshot()) is None


def test_gap_slope_is_the_least_squares_change_per_lap_and_needs_two_laps():
    assert gap_slope([(10, 3.1), (11, 2.8), (12, 2.5)]) == pytest.approx(-0.3)
    assert gap_slope([(12, 2.5)]) is None
    assert gap_slope([]) is None


def test_catch_lap_only_when_closing_faster_than_steady_and_before_the_flag():
    assert catch_lap(12, 2.5, -0.3, 24) == 21        # 2.5 / 0.3 = 8.3 -> 9 laps
    assert catch_lap(12, 2.5, -0.3, 20) is None      # not before the flag
    assert catch_lap(12, 2.5, -0.3, None) == 21      # race length unknown: still a lap
    assert catch_lap(12, 2.5, -0.02, 24) is None     # inside the steady band
    assert catch_lap(12, 2.5, 0.2, 24) is None       # opening, not closing


def test_laps_left_for_lap_count_timed_and_both():
    assert laps_left(12, 24, None, None) == 12
    assert laps_left(12, None, 2400.0, 100.0) == 24
    assert laps_left(12, None, 2350.0, 100.0) == 24   # a started lap is still driven
    assert laps_left(12, 30, 2400.0, 100.0) == 18     # whichever ends the race first
    assert laps_left(12, None, -5.0, 100.0) == 1      # the clock ran out: the last lap
    assert laps_left(12, None, 2400.0, None) is None  # timed, no clean pace yet
    assert laps_left(12, None, None, 100.0) is None
    assert laps_left(30, 24, None, None) == 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_race.py -q`
Expected: FAIL at collection with `ModuleNotFoundError: No module named 'simcoach.race'`.

- [ ] **Step 3: Add `RaceSample` and `LapRecord.race`**

In `simcoach/model_types.py`, insert this class **above** `class LapRecord` (the module has no `from __future__ import annotations`, so the name must exist before `LapRecord`'s annotations are evaluated):

```python
@dataclass
class RaceSample:
    """The race situation at a lap's commit (#152), read from the same scoring frame
    that committed the lap. Every field is None when the game did not report it."""
    class_position: int | None = None
    class_ahead: str | None = None        # "driver (entry)" one place up in class
    class_gap_ahead: float | None = None  # s; None = a lap or more, or no car ahead
    class_behind: str | None = None
    class_gap_behind: float | None = None
    time_left: float | None = None        # s to the session clock's end; None = no clock
    max_laps: int | None = None           # a lap-count race's length; None = not one
```

Append this as the last field of `LapRecord` (after `partial`):

```python
    # The race situation at this lap's commit (#152); stored on the lap row (v21).
    race: RaceSample | None = None
```

- [ ] **Step 4: Create `simcoach/race.py`**

```python
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
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_race.py -q`
Expected: PASS (8 passed).

- [ ] **Step 6: Full gate**

Run: `.venv/Scripts/python.exe -m pytest -q` and `ruff check simcoach tests scripts`
Expected: all green; ruff `All checks passed!`.

- [ ] **Step 7: Commit**

```bash
git add simcoach/model_types.py simcoach/race.py tests/test_race.py
git commit -m "feat(race): #152 - pure class gap, race sample, gap trend and remaining laps

Refs #152

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Pywcy5ZyceRZNDLyjn3F35"
```

---

### Task 2: Pure strategy math (lap facts, fuel/energy to the flag, tire life, pit window)

**Files:**
- Modify: `simcoach/race.py`
- Test: `tests/test_race.py`

**Interfaces:**
- Consumes: `fuel.use_per_lap_from_channel`, `fuel.laps_remaining`, `fuel.stint_save_advice` → `SaveAdvice(binding_resource, binding_laps_remaining, laps_to_go, save_per_lap_fraction, make_it)`; `analysis.stint_tire_trend(analyses, lap_times) -> {"ok", "wear_pct_per_lap": {wheel: %/lap}, ...}`; `analysis.LapAnalysis(lap_time, corners, has_reference, wheels=...)`; `analysis._WHEELS`; `LapRecord` (`lap_time, valid, partial, pit, lap_no, position, race, channels`).
- Produces:
  - `WEAR_FLOOR_PCT = 30.0`, `RATE_LAPS = 5`.
  - `LapFacts(lap_no: int, lap_time: float|None, clean: bool, position=None, race=None, fuel_used=None, fuel_end=None, fuel_capacity=None, energy_used=None, energy_end=None, wear_end: dict[str, float]|None = None)`.
  - `Resource(name, per_lap, on_board, laps, per_fill)`, `PitWindow(latest: int, earliest: int|None, binding: str, one_stop: bool)`.
  - `Strategy(note, laps_to_go, resources, advice, save_per_lap, tire_wheel, tire_remaining_pct, tire_rate_pct, tire_laps, window)`.
  - `is_clean(record: LapRecord) -> bool`, `lap_facts(record: LapRecord) -> LapFacts`.
  - `pit_window(lap_no: int, laps_to_go: int, limits: list[tuple[str, float, float|None]]) -> PitWindow | None`.
  - `strategy(stint: list[LapFacts], laps_to_go: int | None) -> Strategy`.

- [ ] **Step 1: Write the failing tests.** Merge these imports into the file's top import block. Do not put them mid-file: ruff E402 fails on that. The result is `import numpy as np` beside `import pytest`, plus one `from simcoach.race import (...)` that lists every name.

```python
import numpy as np

from simcoach.race import LapFacts, PitWindow, is_clean, lap_facts, pit_window, strategy
```

Then append to `tests/test_race.py`:

```python
def test_is_clean_excludes_pit_out_in_partial_invalid_and_untimed(synthetic_lap):
    assert is_clean(synthetic_lap)
    for over in ({"pit": True}, {"partial": "out"}, {"partial": "in"}, {"valid": False},
                 {"lap_time": None}):
        assert not is_clean(dataclasses.replace(synthetic_lap, **over)), over


def test_lap_facts_takes_burn_level_capacity_and_tread_at_the_line(synthetic_lap):
    ch = {**synthetic_lap.channels, "fuel_capacity": np.full(10, 110.0)}
    f = lap_facts(dataclasses.replace(synthetic_lap, channels=ch, lap_no=7, position=4))
    assert f.lap_no == 7 and f.position == 4 and f.clean
    assert f.fuel_used == pytest.approx(2.6) and f.fuel_end == pytest.approx(77.4)
    assert f.fuel_capacity == pytest.approx(110.0)
    assert f.energy_used is None and f.energy_end is None      # no virtual_energy channel
    assert f.wear_end["fl"] == pytest.approx(0.97)


def test_pit_window_from_the_binding_limit():
    assert pit_window(12, 12, [("fuel", 7.4, 40.0)]) == PitWindow(19, 13, "fuel", True)
    assert pit_window(12, 12, [("fuel", 7.4, 40.0), ("tires", 5.2, 30.0)]) == \
        PitWindow(17, 13, "tires", True)
    assert pit_window(12, 12, [("fuel", 7.4, 4.0)]) == PitWindow(19, 20, "fuel", False)
    assert pit_window(12, 12, [("fuel", 7.4, None)]) == PitWindow(19, None, "fuel", True)
    assert pit_window(12, 12, [("fuel", 30.0, 40.0)]) is None


def _clean(lap_no, fuel_end, wear_fl, wear_rest=0.9, **over):
    wear = {"fl": wear_fl, "fr": wear_rest, "rl": wear_rest, "rr": wear_rest}
    return LapFacts(lap_no=lap_no, lap_time=100.0, clean=True, fuel_used=2.5,
                    fuel_end=fuel_end, fuel_capacity=100.0, wear_end=wear, **over)


STINT = [_clean(10, 30.0, 0.80, 0.90), _clean(11, 27.5, 0.78, 0.89),
         _clean(12, 25.0, 0.76, 0.88)]


def test_strategy_fuel_short_save_target_tire_life_and_window():
    s = strategy(STINT, laps_to_go=12)
    assert s.note is None
    (fuel,) = s.resources
    assert fuel.name == "fuel" and fuel.laps == pytest.approx(10.0)
    assert fuel.per_fill == pytest.approx(40.0)
    assert not s.advice.make_it and s.advice.binding_resource == "fuel"
    assert s.save_per_lap == pytest.approx(2.5 / 6)            # stretch 10 laps over 12
    assert s.tire_wheel == "fl" and s.tire_rate_pct == pytest.approx(2.0)
    assert s.tire_remaining_pct == pytest.approx(76.0)
    assert s.tire_laps == pytest.approx(23.0)                  # (76 - 30) / 2
    assert s.window == PitWindow(22, 13, "fuel", True)


def test_strategy_makes_it_with_no_stop():
    s = strategy(STINT, laps_to_go=8)
    assert s.advice.make_it and s.save_per_lap is None and s.window is None


def test_strategy_needs_two_clean_laps_in_this_stint_and_a_race_length():
    out_lap = LapFacts(lap_no=12, lap_time=None, clean=False, fuel_end=90.0)
    assert strategy([_clean(11, 27.5, 0.78), out_lap], 12).note.startswith(
        "not enough laps yet")
    assert strategy(STINT, None).note.startswith("race length unknown")


def test_energy_binds_when_it_runs_out_first():
    stint = [dataclasses.replace(f, energy_used=0.1, energy_end=e)
             for f, e in zip(STINT, (0.9, 0.8, 0.75))]     # 7.5 laps of energy vs 10 of fuel
    s = strategy(stint, laps_to_go=12)
    assert s.advice.binding_resource == "energy"
    assert s.save_per_lap == pytest.approx(0.1 * (1 - 7.5 / 12))
    assert s.window.binding == "energy" and s.window.latest == 19
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_race.py -q`
Expected: FAIL at collection with `ImportError: cannot import name 'LapFacts' from 'simcoach.race'`.

- [ ] **Step 3: Implement.** In `simcoach/race.py`, replace the import block with:

```python
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from simcoach.analysis import _WHEELS, LapAnalysis, stint_tire_trend
from simcoach.fuel import SaveAdvice, laps_remaining, stint_save_advice, use_per_lap_from_channel
from simcoach.lmu.live import FieldSnapshot, VehicleState
from simcoach.model_types import LapRecord, RaceSample
```

Then append to the end of `simcoach/race.py`:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_race.py -q`
Expected: PASS (15 passed).

- [ ] **Step 5: Full gate**

Run: `.venv/Scripts/python.exe -m pytest -q` and `ruff check simcoach tests scripts`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add simcoach/race.py tests/test_race.py
git commit -m "feat(race): #152 - fuel/energy to the flag, tire life to a named floor, pit window

Refs #152

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Pywcy5ZyceRZNDLyjn3F35"
```

---

### Task 3: Store the race sample on the lap row (schema v21)

**Files:**
- Modify: `simcoach/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: `LapRecord.race: RaceSample | None` (Task 1).
- Produces: `store._RACE_COLUMNS: tuple[tuple[str, str], ...]` (column, SQL type) in `RaceSample` field order; `SCHEMA_VERSION = 21`; `LapStore.save` writes the seven columns (NULL when `record.race is None`).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_store.py`)

```python
# --- issue #152: the race situation at each lap's commit --------------------------

def test_v20_schema_migrates_race_sample_columns_to_v21(tmp_path, synthetic_lap):
    """#152: a real v20 laps table (no race columns) gains them on open; the old row
    keeps NULLs, which is exactly what it is (captured before the sample existed)."""
    from simcoach.store import _RACE_COLUMNS, SCHEMA_VERSION, LapStore
    db = str(tmp_path / "c.sqlite")
    s = LapStore(db, str(tmp_path / "telem"))
    old_id = s.save(synthetic_lap)
    for col, _ in _RACE_COLUMNS:
        s.conn.execute(f"ALTER TABLE laps DROP COLUMN {col}")
    s.conn.execute("PRAGMA user_version = 20")
    s.conn.commit(); s.conn.close()

    s2 = LapStore(db, str(tmp_path / "telem"))
    assert s2.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    cols = {r[1] for r in s2.conn.execute("PRAGMA table_info(laps)")}
    assert {c for c, _ in _RACE_COLUMNS} <= cols
    row = s2.conn.execute("SELECT class_position, class_gap_ahead FROM laps WHERE id = ?",
                          (old_id,)).fetchone()
    assert row == (None, None)
    assert s2.load(old_id).lap_time == synthetic_lap.lap_time


def test_save_stores_the_race_sample_on_the_lap_row(tmp_path, synthetic_lap):
    import dataclasses

    from simcoach.model_types import RaceSample
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "telem"))
    rs = RaceSample(class_position=2, class_ahead="A. Rival (Rival Entry)",
                    class_gap_ahead=1.8, class_behind="B. Chaser (Chaser Entry)",
                    class_gap_behind=None, time_left=2400.0, max_laps=None)
    lid = s.save(dataclasses.replace(synthetic_lap, race=rs))
    row = s.conn.execute(
        "SELECT class_position, class_ahead, class_gap_ahead, class_behind, "
        "class_gap_behind, time_left, max_laps FROM laps WHERE id = ?", (lid,)).fetchone()
    assert row == (2, "A. Rival (Rival Entry)", 1.8, "B. Chaser (Chaser Entry)", None,
                   2400.0, None)
    none_id = s.save(synthetic_lap)                  # no sample -> all NULL, still saves
    assert s.conn.execute("SELECT class_position FROM laps WHERE id = ?",
                          (none_id,)).fetchone() == (None,)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_store.py -q -k "race_sample"`
Expected: FAIL with `ImportError: cannot import name '_RACE_COLUMNS'` and `sqlite3.OperationalError: no such column: class_position`.

- [ ] **Step 3: Implement.** In `simcoach/store.py`:

(a) Change `SCHEMA_VERSION = 20` to `SCHEMA_VERSION = 21`.

(b) Directly after the `_USAGE_COLUMNS = (...)` definition, add:

```python
# The race situation at each lap's commit (#152), as (laps column, SQL type), in
# RaceSample field order. One list so the migration, the fresh schema check and the
# INSERT can never drift apart.
_RACE_COLUMNS = (("class_position", "INTEGER"), ("class_ahead", "TEXT"),
                 ("class_gap_ahead", "REAL"), ("class_behind", "TEXT"),
                 ("class_gap_behind", "REAL"), ("time_left", "REAL"),
                 ("max_laps", "INTEGER"))
```

(c) In `_SCHEMA`'s `laps` table, replace

```
    pit INTEGER,
    partial TEXT
);
```

with

```
    pit INTEGER,
    partial TEXT,
    class_position INTEGER,
    class_ahead TEXT,
    class_gap_ahead REAL,
    class_behind TEXT,
    class_gap_behind REAL,
    time_left REAL,
    max_laps INTEGER
);
```

(d) Replace

```python
                if cols and "partial" not in cols:
                    _add_column(self.conn, "laps", "partial TEXT")
        if current_version != stamp:
```

with

```python
                if cols and "partial" not in cols:
                    _add_column(self.conn, "laps", "partial TEXT")
            if current_version < 21:
                # v20 -> v21: the race situation at each lap's commit (#152) -- class
                # position, the in-class car ahead/behind and their gaps, time left and
                # a lap-count race's length -- so the gap trend has a stored history.
                # Existing rows stay NULL. Guarded like the v18-v20 steps.
                cols = [r[1] for r in self.conn.execute("PRAGMA table_info(laps)")]
                if cols:
                    for c, typ in _RACE_COLUMNS:
                        if c not in cols:
                            _add_column(self.conn, "laps", f"{c} {typ}")
        if current_version != stamp:
```

(e) In `LapStore.save`, replace

```python
                pit = None if record.pit is None else int(record.pit)
                cur = self.conn.execute(
                    "INSERT INTO laps (ts, sim, track, car, lap_time, valid, telemetry_path, "
                    "phase, session_id, s1, s2, s3, position, sector_bounds, lap_no, pit, "
                    "partial) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (record.timestamp, record.sim, record.track, record.car,
                     record.lap_time, int(record.valid), None,
                     record.phase, sid, _sec(0), _sec(1), _sec(2),
                     record.position, bounds, record.lap_no, pit, record.partial),
                )
```

with

```python
                pit = None if record.pit is None else int(record.pit)
                race = [getattr(record.race, c) if record.race else None
                        for c, _ in _RACE_COLUMNS]
                cur = self.conn.execute(
                    "INSERT INTO laps (ts, sim, track, car, lap_time, valid, telemetry_path, "
                    "phase, session_id, s1, s2, s3, position, sector_bounds, lap_no, pit, "
                    "partial, " + ", ".join(c for c, _ in _RACE_COLUMNS) + ") "
                    "VALUES (" + ", ".join(["?"] * (17 + len(_RACE_COLUMNS))) + ")",
                    (record.timestamp, record.sim, record.track, record.car,
                     record.lap_time, int(record.valid), None,
                     record.phase, sid, _sec(0), _sec(1), _sec(2),
                     record.position, bounds, record.lap_no, pit, record.partial, *race),
                )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_store.py -q`
Expected: PASS (the whole file, including every older migration test).

- [ ] **Step 5: Full gate**

Run: `.venv/Scripts/python.exe -m pytest -q` and `ruff check simcoach tests scripts`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add simcoach/store.py tests/test_store.py
git commit -m "feat(store): #152 - v21 per-lap race sample columns (class position, class gaps, clock)

Refs #152

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Pywcy5ZyceRZNDLyjn3F35"
```

---

### Task 4: Capture puts the race sample (and the tank size) on each committed lap

**Files:**
- Modify: `simcoach/lmu/smm.py`
- Test: `tests/test_lmu_smm.py`

**Interfaces:**
- Consumes: `race.race_sample(snap) -> RaceSample | None` (Task 1), `live.read_field(scoring) -> FieldSnapshot` (pure).
- Produces: every committed `LapRecord` from `LmuSource._poll_once` carries `record.race`, read from the committing scoring frame. `sample_from_telem` adds a `fuel_capacity` channel (`telem.mFuelCapacity`, L).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_lmu_smm.py`)

```python
def test_sample_from_telem_maps_fuel_capacity():
    """#152: the tank size, for the pit window's earliest lap."""
    t = roundtrip(make_telem(mFuelCapacity=110.0))
    assert sample_from_telem(t, lap_dist=0.0)["fuel_capacity"] == pytest.approx(110.0)


def test_committed_lap_carries_the_race_sample_from_the_same_scoring_frame():
    """#152: at commit, _poll_once reads the race situation from the scoring frame it
    already holds -- class position, the class gap THROUGH a car of another class, and
    the clock -- and puts it on the LapRecord. No second reader."""
    import mmap

    from simcoach.lmu import smm
    from simcoach.lmu.rf2structs import rF2Scoring, rF2Telemetry

    tag = uuid.uuid4().hex
    tn, sn = f"$simcoach_test_telem_{tag}$", f"$simcoach_test_scoring_{tag}$"
    tmap = mmap.mmap(-1, C.sizeof(rF2Telemetry), tagname=tn, access=mmap.ACCESS_WRITE)
    smap = mmap.mmap(-1, C.sizeof(rF2Scoring), tagname=sn, access=mmap.ACCESS_WRITE)

    # (id, place, class, driver, entry, gap to the car one place up)
    field = [(1, 5, b"GT3", b"You", b"Car", 1.0),
             (2, 3, b"GT3", b"A. Rival", b"Rival Entry", 4.0),
             (3, 4, b"Hyper", b"H. Car", b"Hyper Entry", 0.8),
             (4, 6, b"GT3", b"B. Chaser", b"Chaser Entry", 2.4)]

    def write(dist, last_lap, total_laps):
        t = rF2Telemetry(); t.mVersionUpdateBegin = t.mVersionUpdateEnd = 1; t.mNumVehicles = 1
        v = t.mVehicles[0]; v.mID = 1; v.mLocalVel.z = -50.0
        v.mTrackName = b"Test Circuit"; v.mVehicleName = b"Car"
        s = rF2Scoring(); s.mScoringInfo.mVersionUpdateBegin = s.mScoringInfo.mVersionUpdateEnd = 1
        s.mScoringInfo.mSession = 12; s.mScoringInfo.mCurrentET = 1200.0
        s.mScoringInfo.mEndET = 3600.0; s.mScoringInfo.mLapDist = 4000.0
        s.mScoringInfo.mNumVehicles = len(field)
        for i, (cid, place, cls, drv, entry, gap) in enumerate(field):
            pv = s.mVehicles[i]; pv.mID = cid; pv.mPlace = place; pv.mVehicleClass = cls
            pv.mDriverName = drv; pv.mVehicleName = entry; pv.mTimeBehindNext = gap
            pv.mTotalLaps = total_laps
        pv = s.mVehicles[0]; pv.mIsPlayer = True; pv.mLapDist = dist; pv.mLastLapTime = last_lap
        tmap.seek(0); tmap.write(bytes(t)); smap.seek(0); smap.write(bytes(s))

    src = smm.LmuSource(poll_hz=60, now_fn=lambda: 1_700_000_000.0)
    records = []
    write(5.0, -1.0, 1)
    for i in range(11):
        write(5.0 + 3895.0 * i / 10, -1.0, 1)
        r = src._poll_once(tn, sn)
        if r is not None:
            records.append(r)
    write(3.0, 92.5, 2)                              # cross S/F with a fresh time: COMMIT
    r = src._poll_once(tn, sn)
    if r is not None:
        records.append(r)
    tmap.close(); smap.close()

    assert len(records) == 1
    rs = records[0].race
    assert rs.class_position == 2                    # GT3 order: rival P3, you P5, chaser P6
    assert rs.class_ahead == "A. Rival (Rival Entry)"
    assert rs.class_gap_ahead == pytest.approx(1.8)  # 0.8 (Hyper) + 1.0 (you)
    assert rs.class_behind == "B. Chaser (Chaser Entry)"
    assert rs.class_gap_behind == pytest.approx(2.4)
    assert rs.time_left == pytest.approx(2400.0) and rs.max_laps is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_lmu_smm.py -q -k "fuel_capacity or race_sample"`
Expected: FAIL: `KeyError: 'fuel_capacity'` and `AttributeError: 'NoneType' object has no attribute 'class_position'`.

- [ ] **Step 3: Implement.** In `simcoach/lmu/smm.py`:

(a) Add to the imports (ruff enforces isort order, and `lmu.live` sorts before `lmu.rf2structs`), directly **before** `from simcoach.lmu.rf2structs import (`:

```python
from simcoach.lmu.live import read_field
```

and directly after `from simcoach.model_types import LapRecord`:

```python
from simcoach.race import race_sample
```

(b) In `sample_from_telem`, replace

```python
        "fuel": telem.mFuel,
```

with

```python
        "fuel": telem.mFuel,
        # #152: a full tank (L), for the pit window's earliest lap. PENDING LIVE
        # VALIDATION: mFuelCapacity's offset has not been checked against a running sim.
        "fuel_capacity": telem.mFuelCapacity,
```

(c) In `LmuSource._poll_once`, replace

```python
                if record is not None and fresh:
                    self._committed_time = lap_time
```

with

```python
                if record is not None:
                    # #152: the race situation at this commit, from the SAME scoring
                    # frame -- no second reader. read_field is pure (no I/O).
                    record.race = race_sample(read_field(scoring))
                if record is not None and fresh:
                    self._committed_time = lap_time
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_lmu_smm.py tests/test_lmu_live.py -q`
Expected: PASS.

- [ ] **Step 5: Full gate**

Run: `.venv/Scripts/python.exe -m pytest -q` and `ruff check simcoach tests scripts`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add simcoach/lmu/smm.py tests/test_lmu_smm.py
git commit -m "feat(capture): #152 - race sample from the committing scoring frame; fuel_capacity channel

Refs #152

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Pywcy5ZyceRZNDLyjn3F35"
```

---

### Task 5: `RaceTracker`: one snapshot per committed lap, off the capture thread

**Files:**
- Modify: `simcoach/race.py`, `simcoach/server/app.py`
- Test: `tests/test_race.py`, `tests/test_server_capture.py`

**Interfaces:**
- Consumes: `lap_facts`, `strategy`, `gap_slope`, `catch_lap`, `laps_left` (Tasks 1–2); `format.format_laptime(seconds) -> str`; `analysis._MIN_TIRE_TREND_LAPS`.
- Produces:
  - `GapRead(who: str, gap: float|None, slope: float|None, catch_lap: int|None)`.
  - `RaceState(position, class_position, max_laps, time_left, pace, laps_to_go, ahead: GapRead|None, behind: GapRead|None)`.
  - `RaceSnapshot(as_of_lap: int, session_id: str|None, state: RaceState, strategy: Strategy)`.
  - `build_snapshot(stint: list[LapFacts], session_id: str | None) -> RaceSnapshot`.
  - `RaceTracker()` with `.snapshot: RaceSnapshot | None` and `.commit(record: LapRecord) -> RaceSnapshot | None`.
  - `format_race_state(snap: RaceSnapshot) -> str`, `format_strategy(snap: RaceSnapshot) -> str`. Both start with `As of lap N`.
  - Sidecar: `app.state.race: RaceTracker`, `app.state.race_pool: ThreadPoolExecutor(max_workers=1, thread_name_prefix="race-state")`, and module-level `_race_commit(app_state, lap) -> None`.

- [ ] **Step 1: Write the failing tests.** In `tests/test_race.py`'s top import block, add `from simcoach.model_types import LapRecord, RaceSample`, and add `RaceTracker, format_race_state, format_strategy` to the `simcoach.race` import. Imports stay at the top because of ruff E402. Then append:

```python
_W = ("fl", "fr", "rl", "rr")


def _rec(lap_no, gap_ahead, fuel_start, wear, *, phase="race", pit=False, partial=None,
         lap_time=100.0, ahead="A. Rival (Rival Entry)"):
    n = 50
    ch = {"fuel": np.linspace(fuel_start, fuel_start - 2.5, n),
          "fuel_capacity": np.full(n, 100.0)}
    for w in _W:
        ch[f"tire_wear_{w}"] = np.linspace(wear + 0.01, wear, n)
    race = RaceSample(class_position=3, class_ahead=ahead, class_gap_ahead=gap_ahead,
                      class_behind="B. Chaser (Chaser Entry)", class_gap_behind=2.0,
                      time_left=None, max_laps=24)
    return LapRecord("lmu", "Spa", "GT3", lap_time, True, float(lap_no), ch, phase=phase,
                     session_id="spa--gt3--offline", position=7, lap_no=lap_no, pit=pit,
                     partial=partial, race=race)


def _tracker_at_lap_12():
    tr = RaceTracker()
    for lap_no, gap, fuel, wear in ((10, 3.1, 32.5, 0.80), (11, 2.8, 30.0, 0.78),
                                    (12, 2.5, 27.5, 0.76)):
        tr.commit(_rec(lap_no, gap, fuel, wear))
    return tr


def test_tracker_snapshot_is_stamped_and_projects_from_the_stint():
    snap = _tracker_at_lap_12().snapshot
    assert snap.as_of_lap == 12 and snap.state.laps_to_go == 12
    assert snap.state.ahead.slope == pytest.approx(-0.3) and snap.state.ahead.catch_lap == 21
    assert snap.state.behind.slope == pytest.approx(0.0) and snap.state.behind.catch_lap is None
    assert snap.strategy.window == PitWindow(22, 13, "fuel", True)


def test_a_new_car_ahead_starts_a_new_gap_trend():
    tr = _tracker_at_lap_12()
    tr.commit(_rec(13, 0.9, 25.0, 0.74, ahead="C. Other (Other Entry)"))
    assert tr.snapshot.state.ahead.who == "C. Other (Other Entry)"
    assert tr.snapshot.state.ahead.slope is None            # one lap behind this car


def test_a_pit_stop_starts_a_new_stint_and_the_guard_holds_until_two_clean_laps():
    tr = _tracker_at_lap_12()
    tr.commit(_rec(13, 2.5, 25.0, 0.76, pit=True, partial="in", lap_time=None))
    tr.commit(_rec(14, 20.0, 100.0, 1.0, pit=True, partial="out", lap_time=None))
    tr.commit(_rec(15, 19.5, 97.5, 0.99))
    snap = tr.snapshot
    assert snap.strategy.note.startswith("not enough laps yet")
    assert snap.state.ahead.slope is None


def test_leaving_the_race_clears_the_snapshot():
    tr = _tracker_at_lap_12()
    assert tr.commit(_rec(1, 3.0, 50.0, 0.9, phase="practice")) is None
    assert tr.snapshot is None


def test_the_cached_snapshot_only_changes_on_a_lap_commit():
    tr = _tracker_at_lap_12()
    snap = tr.snapshot
    first = (format_race_state(snap), format_strategy(snap))
    assert (format_race_state(snap), format_strategy(snap)) == first
    assert tr.snapshot is snap                        # reading never recomputes
    tr.commit(_rec(13, 2.2, 25.0, 0.74))
    assert tr.snapshot is not snap and tr.snapshot.as_of_lap == 13


def test_format_race_state_leads_with_the_lap_stamp_and_states_only_trends():
    text = format_race_state(_tracker_at_lap_12().snapshot)
    assert text.startswith("As of lap 12 (race).")
    assert "Position: P7 overall, P3 in class." in text
    assert "Remaining: 12 of 24 laps." in text
    assert ("Car ahead in class: A. Rival (Rival Entry), 2.5 s ahead; you are closing "
            "0.30 s/lap; at this rate you reach him around lap 21.") in text
    assert "Car behind in class: B. Chaser (Chaser Entry), 2.0 s behind; holding steady" in text


def test_format_strategy_states_margin_save_target_threshold_and_window():
    text = format_strategy(_tracker_at_lap_12().snapshot)
    assert text.startswith("As of lap 12.")
    assert ("Fuel: 2.50 L/lap, 25.0 L on board = 10.0 laps: short by 2.0 laps; "
            "save 0.42 L/lap") in text
    assert "about 23 laps to the 30% threshold (a chosen threshold, not a measured limit)" in text
    assert "Pit window: laps 13 to 22 (latest set by fuel;" in text


def test_format_strategy_guard_reads_as_not_enough_laps():
    tr = RaceTracker()
    tr.commit(_rec(10, 3.1, 32.5, 0.80))
    assert format_strategy(tr.snapshot) == (
        "As of lap 10. No projection: not enough laps yet: needs 2 clean laps in this stint "
        "(out-, in- and partial laps don't count).")
```

Append to `tests/test_server_capture.py`:

```python
class _ListSource:
    def __init__(self, laps):
        self._laps = laps

    def laps(self):
        yield from self._laps


def test_capture_loop_refreshes_the_race_snapshot_off_the_capture_thread(tmp_path, synthetic_lap):
    """#152: each stored race lap refreshes the race tools' cached snapshot on the race
    pool -- never inline on the capture thread, whose generator is suspended while the
    loop body runs."""
    import threading
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    app = create_app(store, Coach(FakeModel()))
    seen = []
    real_commit = app.state.race.commit

    def spy(*args):
        seen.append(threading.current_thread().name)
        return real_commit(*args)

    app.state.race.commit = spy
    laps = [dataclasses.replace(synthetic_lap, phase="race", lap_no=n, timestamp=float(n),
                                session_id="spa--gt3--offline") for n in (1, 2)]
    _capture_loop(_ListSource(laps), store, app.state)
    app.state.race_pool.shutdown(wait=True)
    assert app.state.race.snapshot.as_of_lap == 2
    assert seen and all(name.startswith("race-state") for name in seen)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_race.py tests/test_server_capture.py -q`
Expected: FAIL: `ImportError: cannot import name 'RaceTracker'` and `AttributeError: 'State' object has no attribute 'race'`.

- [ ] **Step 3: Implement the core.** In `simcoach/race.py`, change the analysis import to

```python
from simcoach.analysis import _MIN_TIRE_TREND_LAPS, _WHEELS, LapAnalysis, stint_tire_trend
```

and add `from simcoach.format import format_laptime` directly **before** the `simcoach.fuel` import (isort order). Append:

```python
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


@dataclass
class RaceSnapshot:
    """Everything the race tools answer from, as of one committed lap."""
    as_of_lap: int
    session_id: str | None
    state: RaceState
    strategy: Strategy


def _gap_read(stint: list[LapFacts], side: str, final_lap: int | None) -> GapRead | None:
    """The in-class car ahead/behind now, and the gap trend over the laps spent with
    that SAME car there -- a new car starts a new trend. Clean laps only."""
    who_key, gap_key = f"class_{side}", f"class_gap_{side}"
    latest = stint[-1]
    who = getattr(latest.race, who_key) if latest.race else None
    if who is None:
        return None
    points = []
    for f in reversed(stint):
        if f.race is None or getattr(f.race, who_key) != who:
            break
        gap = getattr(f.race, gap_key)
        if f.clean and gap is not None:
            points.append((f.lap_no, gap))
    gap = getattr(latest.race, gap_key)
    slope = gap_slope(points[:GAP_TREND_LAPS])
    caught = (catch_lap(latest.lap_no, gap, slope, final_lap)
              if slope is not None and gap is not None else None)
    return GapRead(who, gap, slope, caught)


def build_snapshot(stint: list[LapFacts], session_id: str | None) -> RaceSnapshot:
    latest = stint[-1]
    r = latest.race or RaceSample()
    clean = [f for f in stint if f.clean]
    recent = clean[-PACE_LAPS:]
    pace = (sum(f.lap_time for f in recent) / len(recent)
            if len(clean) >= MIN_STINT_LAPS else None)
    to_go = laps_left(latest.lap_no, r.max_laps, r.time_left, pace)
    final = latest.lap_no + to_go if to_go is not None else None
    state = RaceState(latest.position, r.class_position, r.max_laps, r.time_left, pace, to_go,
                      _gap_read(stint, "ahead", final), _gap_read(stint, "behind", final))
    return RaceSnapshot(latest.lap_no, session_id, state, strategy(stint, to_go))


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

    def commit(self, record: LapRecord) -> RaceSnapshot | None:
        if record.phase != "race" or record.lap_no is None:
            # Practice, qualy, a mapping lap: there is no live race to answer about.
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
        self.snapshot = build_snapshot(self._stint, record.session_id)
        return self.snapshot


def _clock(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60}:{s % 60:02d}"


def _gap_line(label: str, g: GapRead | None, side: str) -> str:
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
    if st.max_laps:
        lines.append(f"Remaining: {max(st.max_laps - snap.as_of_lap, 0)} of {st.max_laps} laps.")
    if st.time_left is not None:
        if st.time_left <= 0:
            lines.append("Remaining: the clock has run out — this is the last lap.")
        elif st.pace:
            lines.append(f"Remaining: {_clock(st.time_left)} on the clock, about "
                         f"{st.laps_to_go} laps at your recent clean pace "
                         f"({format_laptime(st.pace)}/lap).")
        else:
            lines.append(f"Remaining: {_clock(st.time_left)} on the clock; laps left need "
                         f"{MIN_STINT_LAPS} clean laps in this stint for a pace.")
    if not st.max_laps and st.time_left is None:
        lines.append("Remaining: the game reports no lap count or clock for this race.")
    lines.append(_gap_line("Car ahead in class", st.ahead, "ahead"))
    lines.append(_gap_line("Car behind in class", st.behind, "behind"))
    return "\n".join(lines)


def format_strategy(snap: RaceSnapshot) -> str:
    s = snap.strategy
    head = f"As of lap {snap.as_of_lap}."
    if s.note:
        return f"{head} No projection: {s.note}."
    final = snap.as_of_lap + s.laps_to_go
    lines = [head, f"Laps to go: {s.laps_to_go} (flag at the end of lap {final})."]
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
        if margin >= 0:
            txt += f": makes the flag with {margin:.1f} laps spare."
        else:
            txt += f": short by {-margin:.1f} laps"
            txt += f"; {save} to finish without stopping." if r.name == binding else "."
        lines.append(txt)
    if not s.resources:
        lines.append("Fuel/energy: no clear per-lap burn measured in this stint.")
    if s.tire_laps is not None:
        lines.append(f"Tires: worst is the {_WHEEL_NAME[s.tire_wheel]} at "
                     f"{s.tire_remaining_pct:.0f}% tread, wearing {s.tire_rate_pct:.1f}%/lap: "
                     f"about {max(s.tire_laps, 0):.0f} laps to the {WEAR_FLOOR_PCT:.0f}% "
                     "threshold (a chosen threshold, not a measured limit).")
    else:
        lines.append(f"Tires: no wear rate yet (needs {_MIN_TIRE_TREND_LAPS} clean laps with "
                     "wear data in this stint).")
    w = s.window
    if w is None:
        lines.append("Pit window: no stop needed on these rates"
                     + (" (tires not yet measured)." if s.tire_laps is None else "."))
    elif not w.one_stop:
        lines.append(f"Pit window: pit by lap {w.latest} ({w.binding} runs out first); one "
                     "stop can't reach the flag from there — more than one stop is needed.")
    elif w.earliest is None:
        lines.append(f"Pit window: pit by lap {w.latest} ({w.binding} runs out first); the "
                     "earliest lap is unknown (tank size not reported).")
    else:
        lines.append(f"Pit window: laps {w.earliest} to {w.latest} (latest set by "
                     f"{w.binding}; earliest is the first lap one full service reaches the "
                     "flag from).")
    return "\n".join(lines)
```

- [ ] **Step 4: Wire the sidecar.** In `simcoach/server/app.py`:

(a) Imports: add `from concurrent.futures import ThreadPoolExecutor` directly before `from dataclasses import asdict`. Add `from ..race import RaceTracker` directly after `from ..pronunciations import table_for`.

(b) Directly above `def _capture_loop(`, add:

```python
def _race_commit(app_state, lap) -> None:
    """Refresh the race tools' cached snapshot for a stored lap (#152). A failure costs
    the race tools one lap of freshness, never a lap of capture -- logged, not raised
    (an exception inside a pool future would otherwise vanish silently)."""
    try:
        app_state.race.commit(lap)
    except Exception:
        logging.getLogger(__name__).warning("race snapshot not refreshed", exc_info=True)
```

(c) At the end of `_capture_loop`, replace

```python
        lap_id = store.save(lap)
        app_state.latest = {"record": lap, "lap_id": lap_id}
```

with

```python
        lap_id = store.save(lap)
        app_state.latest = {"record": lap, "lap_id": lap_id}
        if getattr(app_state, "race", None) is not None:
            # #152: refresh the race tools' snapshot OFF this thread -- source.laps() is
            # suspended while this body runs, so work here would delay the next poll.
            app_state.race_pool.submit(_race_commit, app_state, lap)
```

(d) In `create_app`, directly after `app.state.latest = None  # set by the capture thread ...`, add:

```python
    app.state.race = RaceTracker()   # #152: the race tools' snapshot, one per lap commit
    # One worker: commits are processed in lap order, and never on the capture thread.
    app.state.race_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="race-state")
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_race.py tests/test_server_capture.py tests/test_mapcapture_durable.py tests/test_server_reads.py -q`
Expected: PASS. The `SimpleNamespace` app states in `test_server_reads.py` have no `race`, so they skip the hook.

- [ ] **Step 6: Full gate**

Run: `.venv/Scripts/python.exe -m pytest -q` and `ruff check simcoach tests scripts`
Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add simcoach/race.py simcoach/server/app.py tests/test_race.py tests/test_server_capture.py
git commit -m "feat(race): #152 - RaceTracker caches one as-of-lap snapshot per race lap commit, off the capture thread

Refs #152

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Pywcy5ZyceRZNDLyjn3F35"
```

---

### Task 6: `get_race_state` + `get_strategy` tools and the one routing paragraph

**Files:**
- Modify: `simcoach/coach_tools.py`, `simcoach/server/app.py`, `simcoach/coach.py`, `CLAUDE.md`
- Test: `tests/test_coach_tools.py`, `tests/test_server_ws.py`, `tests/test_coach.py`

**Interfaces:**
- Consumes: `race.format_race_state`, `race.format_strategy`, `race.build_snapshot`, `race.LapFacts`, `app.state.race.snapshot` (Task 5).
- Produces: `CoachContext(..., race=None)` → `self.race: RaceSnapshot | None`; `CoachContext.get_race_state() -> str`, `get_strategy() -> str`, and `_no_live_race() -> str | None`; two new `TOOLS` entries and two dispatch entries. `coach_ws` passes `race=app.state.race.snapshot` only when the game is live.

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_coach_tools.py`:

```python
# --- #152: the race tools read the cached snapshot only -----------------------

def _race_snapshot():
    from simcoach.model_types import RaceSample
    from simcoach.race import LapFacts, build_snapshot
    rs = RaceSample(class_position=3, class_ahead="A. Rival (Rival Entry)",
                    class_gap_ahead=2.5, max_laps=24)
    stint = [LapFacts(lap_no=n, lap_time=100.0, clean=True, position=7, race=rs,
                      fuel_used=2.5, fuel_end=e, fuel_capacity=100.0)
             for n, e in ((11, 27.5), (12, 25.0))]
    return build_snapshot(stint, "s")


def test_race_tools_answer_no_live_race_off_track_or_before_a_race_lap(tmp_path, synthetic_lap):
    ctx, store, _ = _ctx(tmp_path, synthetic_lap)
    assert ctx.dispatch("get_race_state", {}).startswith("No live race")   # no snapshot yet
    off = CoachContext(store, on_track=False, race=_race_snapshot())
    for tool in ("get_race_state", "get_strategy"):
        assert off.dispatch(tool, {}).startswith("No live race")


def test_race_tools_read_the_cached_snapshot_and_lead_with_its_lap(tmp_path, synthetic_lap):
    ctx, _, _ = _ctx(tmp_path, synthetic_lap)
    snap = _race_snapshot()
    ctx.race = snap
    state = ctx.dispatch("get_race_state", {})
    strat = ctx.dispatch("get_strategy", {})
    assert state.startswith("As of lap 12 (race).") and "P3 in class" in state
    assert strat.startswith("As of lap 12.") and "Fuel: 2.50 L/lap" in strat
    assert ctx.race is snap and ctx.dispatch("get_race_state", {}) == state
```

Append to `tests/test_server_ws.py`:

```python
def test_ws_coach_race_tool_reads_the_live_race_snapshot(tmp_path, monkeypatch):
    """#152: the live coach turn hands the race tools the cached snapshot -- and only
    while the game is live."""
    from simcoach.lmu import smm
    from simcoach.model_types import RaceSample
    from simcoach.race import LapFacts, build_snapshot
    monkeypatch.setattr(smm, "read_session_track_car", lambda: ("test_track", "test_car"))

    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    fake = FakeModel(reply="ok", tool_script=[("get_race_state", {})])
    app = create_app(store, Coach(fake))
    rs = RaceSample(class_position=3, max_laps=24)
    app.state.race.snapshot = build_snapshot(
        [LapFacts(lap_no=n, lap_time=100.0, clean=True, race=rs) for n in (11, 12)], "s")

    client = TestClient(app, base_url="http://127.0.0.1")
    with client.websocket_connect("/coach", headers={"host": "127.0.0.1"}) as ws:
        ws.send_json({"question": "what position am I?"})
        while True:
            ev = ws.receive_json()
            if ev["type"] in ("done", "error"):
                break

    assert fake.dispatched and fake.dispatched[0][0] == "get_race_state"
    assert fake.dispatched[0][2].startswith("As of lap 12 (race).")
```

In `tests/test_coach.py`, the existing #134 test asserts the sentence this task replaces. Update it: gaps are measured now, traffic still is not. Replace

```python
def test_system_prompt_notes_position_changes_never_gaps_or_traffic():
    """#134: a race lap call may note a position change when it matters, but must
    never invent gaps or traffic -- neither is measured."""
    from simcoach.coach import AGENTIC_SYSTEM_PROMPT
    racecraft = AGENTIC_SYSTEM_PROMPT.split("RACECRAFT:", 1)[1][:600]
    assert "position change" in racecraft
    assert "never state gaps or traffic" in racecraft
```

with

```python
def test_system_prompt_notes_position_changes_never_gaps_or_traffic():
    """#134: a race lap call may note a position change when it matters, but must
    never invent gaps or traffic. #152: a class gap IS measured now -- stated only as
    get_race_state reports it; traffic is still never stated."""
    from simcoach.coach import AGENTIC_SYSTEM_PROMPT
    racecraft = AGENTIC_SYSTEM_PROMPT.split("RACECRAFT:", 1)[1][:600]
    assert "position change" in racecraft
    assert "never state traffic" in racecraft
    assert "a gap only as get_race_state reports it" in racecraft
```

Then append to `tests/test_coach.py`:

```python
def test_agentic_prompt_routes_race_questions_to_one_race_tool():
    """#152: one routing paragraph; the stamp is said; gaps are no longer 'not measured'."""
    from simcoach.coach import AGENTIC_SYSTEM_PROMPT as p
    assert "→ get_race_state" in p and "→ get_strategy" in p
    assert "ONE call answers the question" in p and "As of lap N" in p
    assert "never state gaps or traffic" not in p
    p.format(car="x", speed_unit="km/h", temp_unit="°C")    # no stray braces
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_coach_tools.py tests/test_server_ws.py tests/test_coach.py -q -k "race"`
Expected: FAIL: `TypeError: CoachContext.__init__() got an unexpected keyword argument 'race'`, `error: unknown tool 'get_race_state'`, and the prompt asserts (including the updated #134 test).

- [ ] **Step 3: Implement the tools.** In `simcoach/coach_tools.py`:

(a) Imports: after `from simcoach.fuel import fuel_per_lap_from_channel, laps_remaining`, add

```python
from simcoach.race import format_race_state, format_strategy
```

(b) In `TOOLS`, insert before the `{ "name": "record_driver_learning", ...` entry:

```python
    {
        "name": "get_race_state",
        "description": "LIVE RACE ONLY. Where the driver stands as of the last completed lap: "
                       "overall and class position, laps or time remaining (and laps left at "
                       "the recent clean pace), and the in-class car ahead and behind with the "
                       "gap TREND (s/lap) and the lap he'd be caught at that rate. Use for "
                       "position, gap, 'am I catching him', 'how long is left'. One call "
                       "answers it.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_strategy",
        "description": "LIVE RACE ONLY. Fuel and virtual energy to the flag (makes it or not, "
                       "margin in laps, per-lap save target), the pit window (earliest/latest "
                       "lap and what sets it), and tire life to a named wear threshold, as of "
                       "the last completed lap. Use for 'will I make it', 'when do I pit', "
                       "'how long will the tires last'. One call answers it.",
        "input_schema": {"type": "object", "properties": {}},
    },
```

(c) `CoachContext.__init__`: change the signature line

```python
                 car_notes=None, setup_concepts=None, car_class=None):
```

to

```python
                 car_notes=None, setup_concepts=None, car_class=None, race=None):
```

and directly after `self.car_class = car_class`, add:

```python
        # #152: the RaceSnapshot as of the last committed race lap, or None. The race
        # tools only read it -- nothing is computed while the driver waits.
        self.race = race
```

(d) Insert directly before `    def get_setup(self):`:

```python
    def _no_live_race(self) -> str | None:
        """#152 guard for the race tools: a snapshot exists only in a live race, from its
        first completed lap. Never a guess."""
        if not self.on_track or self.race is None:
            return ("No live race: race state, strategy and battle reads exist only during "
                    "a live race session, from its first completed lap.")
        return None

    def get_race_state(self):
        return self._no_live_race() or format_race_state(self.race)

    def get_strategy(self):
        return self._no_live_race() or format_strategy(self.race)

```

(e) In `dispatch`, directly after `"get_race_result": lambda: self.get_race_result(),`, add:

```python
            "get_race_state": lambda: self.get_race_state(),
            "get_strategy": lambda: self.get_strategy(),
```

- [ ] **Step 4: Pass the snapshot in `coach_ws`.** In `simcoach/server/app.py`, in the worker's `CoachContext(...)` call, replace

```python
                               car_class=car_class)
```

with

```python
                               car_class=car_class,
                               # #152: only while the game is live -- a snapshot
                               # outlives the race it describes.
                               race=app.state.race.snapshot if live else None)
```

- [ ] **Step 5: The routing paragraph.** In `simcoach/coach.py` (`AGENTIC_SYSTEM_PROMPT`), replace

```python
    "change when it matters (a spin's cost, a recovery); never state gaps or traffic — "
    "they are not measured.\n\n"
```

with

```python
    "change when it matters (a spin's cost, a recovery); never state traffic, and state a "
    "gap only as get_race_state reports it.\n\n"
    "RACE TOOLS (a live race; the answer is heard seconds after the question): race "
    "position, gaps or how long is left → get_race_state; fuel, energy, the pit window or "
    "tire life to the end → get_strategy. ONE call answers the question. Every result "
    "starts 'As of lap N' — say that stamp ('as of lap 12, ...'). Give only what the tool "
    "gives — lap numbers, per-lap targets, multi-lap trends — never an instantaneous gap or "
    "an immediate tactical call. The tire wear floor is a chosen threshold: say it as one "
    "('to 30% tread'), never as a measured limit.\n\n"
```

- [ ] **Step 6: CLAUDE.md layout.** In `CLAUDE.md`, replace

```
  `coach_tools.py` (tool-use coach), `model/` (swappable `Model`),
```

with

```
  `coach_tools.py` (tool-use coach), `race.py` (the PTT race tools' race state +
  strategy, cached once per lap commit by `RaceTracker`), `model/` (swappable `Model`),
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_coach_tools.py tests/test_server_ws.py tests/test_coach.py tests/test_coach_stream.py -q`
Expected: PASS, including `test_every_advertised_tool_is_dispatchable`.

- [ ] **Step 8: Full gate**

Run: `.venv/Scripts/python.exe -m pytest -q` and `ruff check simcoach tests scripts`
Expected: all green.

- [ ] **Step 9: Commit**

```bash
git add simcoach/coach_tools.py simcoach/server/app.py simcoach/coach.py CLAUDE.md tests/test_coach_tools.py tests/test_server_ws.py tests/test_coach.py
git commit -m "feat(coach): #152 - get_race_state + get_strategy read the as-of-lap snapshot; one routing line

Refs #152

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Pywcy5ZyceRZNDLyjn3F35"
```

**Part A is shippable here.** Do the rig items 1–9 from "Rig check" before starting Part B if the rig is free.

---

# Part B: battle (where a rival is faster or slower)

### Task 7: Pure field trace + per-corner crossings

**Files:**
- Create: `simcoach/battle.py`
- Test: `tests/test_battle.py` (new)

**Interfaces:**
- Consumes: `lmu.live.FieldSnapshot` / `VehicleState` (`id, driver, car, cls, class_position, laps, lap_dist, speed` m/s, `is_player`), `session.track_len`/`live`; `corners.Corner` (`number, name, start_dist, apex_dist, end_dist, label`).
- Produces:
  - `BATTLE_RANGE_S = 2.0`, `BATTLE_NOISE_FLOOR_S = 0.10`, `APEX_SPEED_FLOOR_KPH = 5.0`, `TRACE_TAIL_S = 5.0`.
  - `FieldTrace()` with `.last_t: float | None`, `.feed(t: float, snap: FieldSnapshot) -> None` and `.take() -> dict`. The dict is `{"player_id": int|None, "track_len": float, "cars": {car_id: {"driver", "car", "class_position", "pts": [(t, unwrapped_s, kph), ...]}}}`.
  - `CarLap(car_id, driver, car, class_position, is_player, entry: dict[int, float], exit: dict[int, float], apex_kph: dict[int, float], model: str = "")`.
  - `car_laps(trace: dict, corners: list[Corner], lap_no: int) -> list[CarLap]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_battle.py
"""#152 get_battle: the same-class field trace and per-corner timing (pure)."""
import dataclasses

import pytest

from simcoach.battle import FieldTrace, car_laps
from simcoach.corners import Corner
from simcoach.lmu.live import FieldSnapshot, VehicleState, empty_snapshot

L = 1000.0
T1 = Corner(1, "La Source", "right", 200.0, 250.0, 300.0, label="1")
T2 = Corner(2, "Eau Rouge", "left", 600.0, 650.0, 700.0, label="2")
T_LINE = Corner(4, "Line Kink", "left", 20.0, 50.0, 80.0, label="4")   # just past S/F
SEAM = Corner(3, "Seam", "right", 980.0, 990.0, 20.0, label="3")       # wraps S/F: skipped


def _vs(cid, cls, laps, dist, *, player=False, speed=50.0, pos=1):
    return VehicleState(
        id=cid, place=pos, class_position=pos, driver=f"D{cid}", car=f"E{cid}", cls=cls,
        lap_dist=dist, speed=speed, sector=0, laps=laps, in_pits=False, is_player=player,
        best_lap=None, last_lap=None, best_s1=None, best_s2=None, gap_leader=None,
        laps_behind_leader=0, gap_next=None, laps_behind_next=0)


def _snap(cars):
    s = dataclasses.replace(empty_snapshot().session, live=True, phase="race", track_len=L)
    return FieldSnapshot(session=s, cars=cars)


def _pts(t0, s0, v, n=140, hz=5.0):
    """A 5 Hz trace at constant speed v (m/s) from unwrapped distance s0 at time t0."""
    return [(t0 + i / hz, s0 + v * i / hz, v * 3.6) for i in range(n)]


def _trace(extra=None):
    """The driver is car 1 on lap 2: unwrapped 750 m at t=95 s, 50 m/s, so he crosses
    the line (1000 m) at t=100 and T1's entry (1200 m) at t=104."""
    cars = {1: {"driver": "You", "car": "E1", "class_position": 3,
                "pts": _pts(95.0, 750.0, 50.0)}}
    cars.update(extra or {})
    return {"player_id": 1, "track_len": L, "cars": cars}


def _rival(s0, *, cid=2, pos=2, driver="A. Rival"):
    return {cid: {"driver": driver, "car": f"E{cid}", "class_position": pos,
                  "pts": _pts(95.0, s0, 50.0)}}


def test_feed_records_the_players_class_once_per_scoring_frame():
    tr = FieldTrace()
    snap = _snap([_vs(1, "GT3", 1, 100.0, player=True), _vs(2, "GT3", 1, 150.0, pos=2),
                  _vs(3, "Hyper", 2, 900.0)])
    tr.feed(10.0, snap)
    tr.feed(10.0, snap)                 # the capture polls faster than scoring updates
    out = tr.take()
    assert out["player_id"] == 1 and out["track_len"] == L
    assert set(out["cars"]) == {1, 2}   # other classes are not recorded
    assert out["cars"][2]["pts"] == [(10.0, 1150.0, pytest.approx(180.0))]
    assert out["cars"][2]["class_position"] == 2


def test_feed_drops_a_frame_where_lap_distance_reset_before_the_lap_count():
    tr = FieldTrace()
    for t, laps, dist in ((10.0, 1, 990.0), (10.2, 1, 5.0), (10.4, 2, 15.0)):
        tr.feed(t, _snap([_vs(1, "GT3", laps, dist, player=True)]))
    assert [s for _, s, _ in tr.take()["cars"][1]["pts"]] == [1990.0, 2015.0]


def test_take_keeps_the_last_seconds_for_the_next_lap():
    tr = FieldTrace()
    for i in range(21):
        tr.feed(float(i), _snap([_vs(1, "GT3", 1, 10.0 * i, player=True)]))
    assert len(tr.take()["cars"][1]["pts"]) == 21
    assert [t for t, _, _ in tr.take()["cars"][1]["pts"]] == [15.0, 16.0, 17.0, 18.0, 19.0,
                                                              20.0]


def test_car_laps_times_each_corner_for_the_driver_and_a_rival():
    by_id = {c.car_id: c for c in car_laps(_trace(_rival(800.0)), [T1, T2, SEAM], lap_no=2)}
    me, rival = by_id[1], by_id[2]
    assert me.is_player and not rival.is_player and rival.class_position == 2
    assert me.entry[1] == pytest.approx(104.0) and me.exit[1] == pytest.approx(106.0)
    assert rival.entry[1] == pytest.approx(103.0) and rival.exit[1] == pytest.approx(105.0)
    assert me.apex_kph[1] == pytest.approx(180.0)
    assert 3 not in me.entry and 3 not in rival.entry      # the S/F-seam corner is skipped


def test_a_rival_is_paired_by_time_across_the_line_whichever_car_crosses_it_first():
    """T_LINE sits just past S/F. A rival 1 s AHEAD crossed the line before the driver
    (his lap counter ticked first); one 1 s BEHIND crosses after. Each is paired with the
    driver's own crossing, not with a crossing a whole lap away."""
    extra = {**_rival(800.0), **_rival(700.0, cid=3, pos=4, driver="B. Chaser")}
    by_id = {c.car_id: c for c in car_laps(_trace(extra), [T_LINE], lap_no=2)}
    assert by_id[1].entry[4] == pytest.approx(100.4)
    assert by_id[2].entry[4] == pytest.approx(99.4)
    assert by_id[3].entry[4] == pytest.approx(101.4)


def test_car_laps_is_empty_without_the_drivers_own_trace():
    assert car_laps({"player_id": 9, "track_len": L, "cars": {}}, [T1], lap_no=2) == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_battle.py -q`
Expected: FAIL at collection with `ModuleNotFoundError: No module named 'simcoach.battle'`.

- [ ] **Step 3: Create `simcoach/battle.py`**

```python
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
from dataclasses import dataclass, field

from simcoach.corners import Corner
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
            # ponytail: a frame where lap distance has reset but mTotalLaps has not yet
            # ticked reads as a whole lap backwards; it is dropped, not repaired.
            if e["pts"] and s < e["pts"][-1][1]:
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
    first."""
    best = None
    for k in range(int(pts[0][1] // length), int(pts[-1][1] // length) + 1):
        t = _time_at(pts, k * length + dist)
        if t is not None and (best is None or abs(t - t_ref) < abs(best[0] - t_ref)):
            best = (t, k * length + dist)
    return best


def car_laps(trace: dict, corners: list[Corner], lap_no: int) -> list[CarLap]:
    """Every same-class car's corner timing on the driver's lap `lap_no` (the lap that
    just completed: the game's lap counter read `lap_no - 1` while it was driven).

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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_battle.py -q`
Expected: PASS (6 passed).

- [ ] **Step 5: Full gate**

Run: `.venv/Scripts/python.exe -m pytest -q` and `ruff check simcoach tests scripts`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add simcoach/battle.py tests/test_battle.py
git commit -m "feat(battle): #152 - pure same-class field trace and per-corner crossings paired by time

Refs #152

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Pywcy5ZyceRZNDLyjn3F35"
```

---

### Task 8: Pure zone deltas, rival resolution and the battle text

**Files:**
- Modify: `simcoach/battle.py`
- Test: `tests/test_battle.py`

**Interfaces:**
- Consumes: `CarLap`, `Corner`, `format.unit_fns(units) -> (speed_fn, temp_fn, speed_label, temp_label)`.
- Produces:
  - `Zone(label: str, delta: float, apex_gain_kph: float | None)`, where `delta` = rival's time − yours (+ = you are quicker).
  - `zone_deltas(me: CarLap, rival: CarLap, corners: list[Corner]) -> list[Zone]`.
  - `nearest_rivals(me: CarLap, others: list[CarLap]) -> list[CarLap]` (0, 1 or 2 cars).
  - `match_rival(others: list[CarLap], query: str) -> CarLap | None`.
  - `Battle(laps: list[CarLap], corners: list[Corner], rivals: list[CarLap])`.
  - `build_battle(laps: list[CarLap], corners: list[Corner]) -> Battle | None`.
  - `format_battle(b: Battle | None, as_of_lap: int, units: str = "metric", rival: str | None = None) -> str` (starts with `As of lap N.`).

- [ ] **Step 1: Write the failing tests.** In `tests/test_battle.py`'s top import block, extend the `simcoach.battle` import with `CarLap, build_battle, format_battle, match_rival, nearest_rivals, zone_deltas`. Imports stay at the top because of ruff E402. Then append:

```python
ME = CarLap(1, "You", "E1", 3, True, entry={1: 104.0, 2: 112.0}, exit={1: 106.0, 2: 114.0},
            apex_kph={1: 150.0, 2: 200.0})
RIVAL = CarLap(2, "A. Rival", "AF Corse #51", 2, False, entry={1: 103.0, 2: 111.5},
               exit={1: 105.3, 2: 113.2}, apex_kph={1: 142.0, 2: 201.0},
               model="Ferrari 296 GT3")


def _lap(cid, entries, pos=2, driver=None):
    return CarLap(cid, driver or f"D{cid}", f"E{cid}", pos, False, entry=entries,
                  exit={n: t + 1.0 for n, t in entries.items()})


def test_zone_deltas_per_corner_and_section_signed_you_quicker_positive():
    zones = {z.label: z for z in zone_deltas(ME, RIVAL, [T1, T2])}
    assert zones["Turn 1 (La Source)"].delta == pytest.approx(0.3)
    assert zones["Turn 1 (La Source)"].apex_gain_kph == pytest.approx(8.0)
    assert zones["Turn 1 (La Source) exit to Turn 2 (Eau Rouge)"].delta == pytest.approx(0.2)
    assert zones["Turn 2 (Eau Rouge)"].delta == pytest.approx(-0.3)


def test_nearest_rivals_names_the_top_scorer_and_a_second_when_the_fight_changed_hands():
    me = CarLap(1, "You", "E1", 3, True, entry={1: 10.0, 2: 20.0, 3: 30.0})
    a = _lap(2, {1: 10.5, 2: 20.5, 3: 31.5})              # nearest at T1 and T2
    b = _lap(3, {1: 12.5, 2: 22.5, 3: 30.2}, pos=4)       # nearest at T3 only: a third
    far = _lap(4, {1: 60.0, 2: 70.0, 3: 80.0}, pos=5)     # never within 2 s
    assert [r.car_id for r in nearest_rivals(me, [a, b, far])] == [2, 3]
    assert [r.car_id for r in nearest_rivals(me, [a, far])] == [2]
    assert nearest_rivals(me, [far]) == []


def test_match_rival_by_class_position_driver_entry_or_model():
    others = [RIVAL, _lap(3, {1: 1.0}, pos=4, driver="B. Chaser")]
    assert match_rival(others, "P4").car_id == 3
    assert match_rival(others, "p 2").car_id == 2
    assert match_rival(others, "chaser").car_id == 3
    assert match_rival(others, "the Ferrari").car_id == 2
    assert match_rival(others, "#51").car_id == 2
    assert match_rival(others, "P9") is None and match_rival(others, "McLaren") is None


def test_format_battle_ranks_zones_above_the_noise_floor_only():
    text = format_battle(build_battle([ME, RIVAL], [T1, T2]), 12)
    assert text.startswith("As of lap 12.")
    assert ("Rival (nearest to you for most of lap 12): A. Rival (Ferrari 296 GT3), "
            "P2 in class.") in text
    assert "He is quicker: Turn 2 (Eau Rouge) by 0.30 s." in text
    assert ("You are quicker: Turn 1 (La Source) by 0.30 s, you carry 8 km/h more at the "
            "apex; Turn 1 (La Source) exit to Turn 2 (Eau Rouge) by 0.20 s.") in text
    assert "0.10 s or less are within the timing noise" in text


def test_a_difference_inside_the_noise_floor_is_never_stated():
    close = CarLap(2, "A. Rival", "E2", 2, False, entry={1: 103.0}, exit={1: 105.08})
    text = format_battle(build_battle([ME, close], [T1, T2]), 12)
    assert "He is quicker: nowhere by more than the timing noise." in text
    assert "You are quicker: nowhere by more than the timing noise." in text
    assert "0.08" not in text


def test_format_battle_units_named_rival_and_the_no_read_cases():
    b = build_battle([ME, RIVAL], [T1, T2])
    assert "you carry 5 mph more at the apex" in format_battle(b, 12, units="imperial")
    named = format_battle(b, 12, rival="P2")
    assert "Rival (the car you named): A. Rival (Ferrari 296 GT3), P2 in class." in named
    assert format_battle(b, 12, rival="McLaren") == (
        "As of lap 12. No car in your class matches 'McLaren'. In your class: "
        "P2 A. Rival (Ferrari 296 GT3).")
    assert format_battle(None, 12).startswith("As of lap 12. No battle read")
    lonely = build_battle([ME, _lap(4, {1: 60.0, 2: 70.0})], [T1, T2])
    assert "Nobody in your class was within 2 s of you on lap 12" in format_battle(lonely, 12)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_battle.py -q`
Expected: FAIL at collection with `ImportError: cannot import name 'build_battle' from 'simcoach.battle'`.

- [ ] **Step 3: Implement.** In `simcoach/battle.py`, change the imports to

```python
import bisect
import re
from dataclasses import dataclass, field

from simcoach.corners import Corner
from simcoach.format import unit_fns
from simcoach.lmu.live import FieldSnapshot
```

and append:

```python
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
        zones = [z for z in zone_deltas(me, o, b.corners)
                 if abs(z.delta) > BATTLE_NOISE_FLOOR_S]
        his = sorted((z for z in zones if z.delta < 0), key=lambda z: z.delta)
        mine = sorted((z for z in zones if z.delta > 0), key=lambda z: -z.delta)
        lines.append(f"Rival ({why}): {o.driver} ({o.model or o.car}), "
                     f"P{o.class_position} in class.")
        lines.append("He is quicker: " + ("; ".join(map(_zone, his))
                                          or "nowhere by more than the timing noise") + ".")
        lines.append("You are quicker: " + ("; ".join(map(_zone, mine))
                                            or "nowhere by more than the timing noise") + ".")
    lines.append(f"Differences of {BATTLE_NOISE_FLOOR_S:.2f} s or less are within the timing "
                 "noise and are not listed.")
    return "\n".join(lines)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_battle.py -q`
Expected: PASS (12 passed).

- [ ] **Step 5: Full gate**

Run: `.venv/Scripts/python.exe -m pytest -q` and `ruff check simcoach tests scripts`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add simcoach/battle.py tests/test_battle.py
git commit -m "feat(battle): #152 - zone deltas above a named noise floor, rival resolution, battle text

Refs #152

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Pywcy5ZyceRZNDLyjn3F35"
```

---

### Task 9: Store battle rows (schema v22)

**Files:**
- Modify: `simcoach/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: `battle.CarLap` (Task 7).
- Produces: `SCHEMA_VERSION = 22`; table `battle_laps(lap_id, car_id, driver, car, model, class_position, is_player, crossings TEXT JSON)`; `LapStore.save_battle(lap_id: int, laps: list[CarLap]) -> None`; `LapStore.battle_laps(lap_id: int) -> list[CarLap]` (ordered by `car_id`); `clear_all` also clears `battle_laps`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_store.py`)

```python
# --- issue #152 get_battle: one row per (lap, same-class car) ----------------------

def test_v21_schema_migrates_to_v22_with_the_battle_table(tmp_path):
    from simcoach.store import SCHEMA_VERSION, LapStore
    db = str(tmp_path / "c.sqlite")
    s = LapStore(db, str(tmp_path / "telem"))
    s.conn.execute("DROP TABLE battle_laps")
    s.conn.execute("PRAGMA user_version = 21")
    s.conn.commit(); s.conn.close()

    s2 = LapStore(db, str(tmp_path / "telem"))
    assert s2.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert s2.battle_laps(1) == []


def test_battle_rows_round_trip_one_per_car_and_reset_clears_them(tmp_path, synthetic_lap):
    from simcoach.battle import CarLap
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "telem"))
    lid = s.save(synthetic_lap)
    me = CarLap(1, "You", "E1", 3, True, entry={1: 104.0}, exit={1: 106.0},
                apex_kph={1: 150.0})
    rival = CarLap(2, "A. Rival", "AF Corse #51", 2, False, entry={1: 103.0},
                   exit={1: 105.3}, model="Ferrari 296 GT3")
    s.save_battle(lid, [me, rival])
    assert s.battle_laps(lid) == [me, rival]
    s.clear_all()
    assert s.battle_laps(lid) == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_store.py -q -k "battle"`
Expected: FAIL: `sqlite3.OperationalError: no such table: battle_laps`.

- [ ] **Step 3: Implement.** In `simcoach/store.py`:

(a) `SCHEMA_VERSION = 21` → `SCHEMA_VERSION = 22`.

(b) Add `from simcoach.battle import CarLap` directly before `from simcoach.car_lookup import resolve_car, resolve_car_class`.

(c) Directly after the `_PROFILE_SCHEMA = """ ... """` block (before `_SCHEMA = """`), add:

```python
# #152 get_battle: one row per (driver's lap, same-class car -- the driver included) with
# that car's corner crossing times and apex speeds on the lap, as JSON. SQLite, not the
# .npz: a few dozen numbers per car, cross-session, not a full-rate channel.
_BATTLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS battle_laps (
    lap_id INTEGER,
    car_id INTEGER,
    driver TEXT,
    car TEXT,
    model TEXT,
    class_position INTEGER,
    is_player INTEGER,
    crossings TEXT
);
"""
```

(d) Change the end of `_SCHEMA` from `""" + _COACH_CHAT_SCHEMA + _PROFILE_SCHEMA` to `""" + _COACH_CHAT_SCHEMA + _PROFILE_SCHEMA + _BATTLE_SCHEMA`.

(e) Replace

```python
                        if c not in cols:
                            _add_column(self.conn, "laps", f"{c} {typ}")
        if current_version != stamp:
```

with

```python
                        if c not in cols:
                            _add_column(self.conn, "laps", f"{c} {typ}")
            if current_version < 22:
                # v21 -> v22: per-lap battle rows for get_battle (#152). A new table, so
                # nothing to backfill: laps stored before it have no battle rows.
                self.conn.executescript(_BATTLE_SCHEMA)
        if current_version != stamp:
```

(f) In `clear_all`, change the table tuple to `("corner_metrics", "coach_chat", "laps", "setups", "race_sessions", "ingested_files", "battle_laps")`.

(g) Add these methods directly after `LapStore.load`:

```python
    def save_battle(self, lap_id: int, laps: list[CarLap]) -> None:
        """#152: every same-class car's corner timing for one of the driver's laps."""
        rows = [(lap_id, c.car_id, c.driver, c.car, c.model, c.class_position,
                 int(c.is_player),
                 json.dumps({"entry": c.entry, "exit": c.exit, "apex_kph": c.apex_kph}))
                for c in laps]
        with self._lock, self.conn:
            self.conn.executemany(
                "INSERT INTO battle_laps (lap_id, car_id, driver, car, model, "
                "class_position, is_player, crossings) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)

    def battle_laps(self, lap_id: int) -> list[CarLap]:
        """The CarLaps save_battle stored for a lap (JSON's string keys back to corner
        numbers), ordered by car id."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT car_id, driver, car, model, class_position, is_player, crossings "
                "FROM battle_laps WHERE lap_id = ? ORDER BY car_id", (lap_id,)).fetchall()
        out = []
        for car_id, driver, car, model, pos, is_player, crossings in rows:
            x = json.loads(crossings)
            timing = {k: {int(n): v for n, v in x[k].items()}
                      for k in ("entry", "exit", "apex_kph")}
            out.append(CarLap(car_id, driver, car, pos, bool(is_player), **timing,
                              model=model or ""))
        return out
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_store.py -q`
Expected: PASS (the whole file).

- [ ] **Step 5: Full gate**

Run: `.venv/Scripts/python.exe -m pytest -q` and `ruff check simcoach tests scripts`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add simcoach/store.py tests/test_store.py
git commit -m "feat(store): #152 - v22 battle_laps, one row per (lap, same-class car)

Refs #152

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Pywcy5ZyceRZNDLyjn3F35"
```

---

### Task 10: Capture feeds the field trace from the scoring it already reads

**Files:**
- Modify: `simcoach/model_types.py`, `simcoach/lmu/smm.py`
- Test: `tests/test_lmu_smm.py`

**Interfaces:**
- Consumes: `battle.FieldTrace` (Task 7), `live.read_field`.
- Produces: `LapRecord.field_trace: dict | None = None` (transient: never stored). `LmuSource` holds `self._trace: FieldTrace` (reset in `_reset`), feeds it on each new `mCurrentET`, calls `take()` at every S/F wrap, and puts the taken trace on the committed record.

- [ ] **Step 1: Write the failing test** (append to `tests/test_lmu_smm.py`)

```python
def test_committed_lap_carries_the_same_class_field_trace():
    """#152 get_battle: _poll_once feeds every NEW scoring frame's same-class cars into
    the lap's field trace (other classes are not recorded; a repeated frame is not
    recorded twice) and hands it over on the committed LapRecord."""
    import mmap

    from simcoach.lmu import smm
    from simcoach.lmu.rf2structs import rF2Scoring, rF2Telemetry

    tag = uuid.uuid4().hex
    tn, sn = f"$simcoach_test_telem_{tag}$", f"$simcoach_test_scoring_{tag}$"
    tmap = mmap.mmap(-1, C.sizeof(rF2Telemetry), tagname=tn, access=mmap.ACCESS_WRITE)
    smap = mmap.mmap(-1, C.sizeof(rF2Scoring), tagname=sn, access=mmap.ACCESS_WRITE)

    def write(dist, last_lap, total_laps, et):
        t = rF2Telemetry(); t.mVersionUpdateBegin = t.mVersionUpdateEnd = 1; t.mNumVehicles = 1
        v = t.mVehicles[0]; v.mID = 1; v.mLocalVel.z = -50.0
        v.mTrackName = b"Test Circuit"; v.mVehicleName = b"Car"
        s = rF2Scoring(); s.mScoringInfo.mVersionUpdateBegin = s.mScoringInfo.mVersionUpdateEnd = 1
        s.mScoringInfo.mSession = 12; s.mScoringInfo.mCurrentET = et
        s.mScoringInfo.mLapDist = 4000.0
        cars = [(1, 5, b"GT3", True, dist), (2, 3, b"GT3", False, min(dist + 100.0, 3999.0)),
                (3, 4, b"Hyper", False, dist)]
        s.mScoringInfo.mNumVehicles = len(cars)
        for i, (cid, place, cls, is_player, d) in enumerate(cars):
            pv = s.mVehicles[i]; pv.mID = cid; pv.mPlace = place; pv.mVehicleClass = cls
            pv.mIsPlayer = is_player; pv.mLapDist = d; pv.mTotalLaps = total_laps
        s.mVehicles[0].mLastLapTime = last_lap
        tmap.seek(0); tmap.write(bytes(t)); smap.seek(0); smap.write(bytes(s))

    src = smm.LmuSource(poll_hz=60, now_fn=lambda: 1_700_000_000.0)
    records, et = [], [1000.0]

    def frame(dist, last, laps):
        et[0] += 0.2
        write(dist, last, laps, et[0])
        for _ in range(2):   # two polls per scoring frame: the repeat must not be recorded
            r = src._poll_once(tn, sn)
            if r is not None:
                records.append(r)

    for i in range(11):
        frame(5.0 + 3895.0 * i / 10, -1.0, 1)
    frame(3.0, 92.5, 2)                              # cross S/F with a fresh time: COMMIT
    tmap.close(); smap.close()

    assert len(records) == 1
    tr = records[0].field_trace
    assert tr["player_id"] == 1 and tr["track_len"] == 4000.0
    assert set(tr["cars"]) == {1, 2}                 # the Hypercar is not recorded
    assert len(tr["cars"][2]["pts"]) == 12           # 12 scoring frames, each once
    ss = [s for _, s, _ in tr["cars"][1]["pts"]]
    assert ss == sorted(ss) and ss[-1] == 8003.0     # unwrapped across the line
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_lmu_smm.py -q -k "field_trace"`
Expected: FAIL: `AttributeError: 'LapRecord' object has no attribute 'field_trace'`.

- [ ] **Step 3: Implement.**

(a) In `simcoach/model_types.py`, append as the last field of `LapRecord`:

```python
    # This lap's same-class field trace (#152 get_battle) -- TRANSIENT: handed from
    # capture to the race tracker, never written to the store or the .npz.
    field_trace: dict | None = None
```

(b) In `simcoach/lmu/smm.py`, add `from simcoach.battle import FieldTrace` directly before `from simcoach.lmu.live import read_field`.

(c) In `LmuSource._reset`, append:

```python
        # #152 get_battle: the same-class field over this lap, from each NEW scoring frame.
        self._trace = FieldTrace()
```

(d) In `_poll_once`, replace

```python
        record = None
        dist = player.mLapDist
        # Finish line / lap boundary: lap distance jumped backward a long way.
        if self._prev_dist is not None and dist < self._prev_dist - _LAP_WRAP_M:
            lap_time = player.mLastLapTime
```

with

```python
        # #152: feed the battle trace from the scoring frame already in hand -- no second
        # reader. Only on a NEW frame: scoring updates at a few Hz, this polls at 50, and
        # read_field walks every car.
        et = float(scoring.mScoringInfo.mCurrentET)
        if et != self._trace.last_t:
            self._trace.feed(et, read_field(scoring))

        record = None
        dist = player.mLapDist
        # Finish line / lap boundary: lap distance jumped backward a long way.
        if self._prev_dist is not None and dist < self._prev_dist - _LAP_WRAP_M:
            # #152: hand over the lap's field trace at EVERY wrap, committed or not, so a
            # trace never spans two of the driver's laps.
            trace = self._trace.take()
            lap_time = player.mLastLapTime
```

(e) Replace the Task 4 block

```python
                if record is not None:
                    # #152: the race situation at this commit, from the SAME scoring
                    # frame -- no second reader. read_field is pure (no I/O).
                    record.race = race_sample(read_field(scoring))
```

with

```python
                if record is not None:
                    # #152: the race situation at this commit, from the SAME scoring
                    # frame -- no second reader. read_field is pure (no I/O).
                    record.race = race_sample(read_field(scoring))
                    record.field_trace = trace
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_lmu_smm.py tests/test_lmu_stale_smm.py -q`
Expected: PASS, including every older capture test.

- [ ] **Step 5: Full gate**

Run: `.venv/Scripts/python.exe -m pytest -q` and `ruff check simcoach tests scripts`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add simcoach/model_types.py simcoach/lmu/smm.py tests/test_lmu_smm.py
git commit -m "feat(capture): #152 - feed the same-class field trace from the scoring capture already reads

Refs #152

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Pywcy5ZyceRZNDLyjn3F35"
```

---

### Task 11: Battle in the snapshot, persisted per lap, and the `get_battle` tool

**Files:**
- Modify: `simcoach/race.py`, `simcoach/server/app.py`, `simcoach/coach_tools.py`, `simcoach/coach.py`, `CLAUDE.md`
- Test: `tests/test_battle.py`, `tests/test_server_capture.py`, `tests/test_coach_tools.py`, `tests/test_coach.py`

**Interfaces:**
- Consumes: `battle.car_laps`, `build_battle`, `format_battle`, `Battle` (Tasks 7–8); `car_lookup.resolve_car(name) -> str` (never raises; raw name when unknown); `LapStore.save_battle` (Task 9); `LapRecord.field_trace` (Task 10); `CornerMap.corners`.
- Produces: `RaceSnapshot.battle: Battle | None = None`; `build_snapshot(stint, session_id, battle=None)`; `RaceTracker.commit(record, cmap: CornerMap | None = None)`; `app.state.map_for`; `_race_commit(app_state, store, lap, lap_id)` (persists `snap.battle.laps`); `CoachContext.get_battle(rival=None) -> str`; a `get_battle` TOOLS entry with an optional `rival` string.

- [ ] **Step 1: Write the failing tests.** Add these to `tests/test_battle.py`'s top import block (ruff E402):

```python
from simcoach.corners import CornerMap
from simcoach.model_types import LapRecord
from simcoach.race import RaceTracker
```

Then append to `tests/test_battle.py`:

```python
def _race_record(lap_no, trace, **over):
    return LapRecord("lmu", "Spa", "GT3", 100.0, True, 0.0, {}, phase="race",
                     session_id="s", lap_no=lap_no, field_trace=trace, **over)


def test_tracker_builds_the_battle_for_a_race_lap(monkeypatch):
    monkeypatch.setattr("simcoach.race.resolve_car", lambda name, *a: f"model of {name}")
    snap = RaceTracker().commit(_race_record(2, _trace(_rival(800.0))), CornerMap([T1, T2]))
    assert [r.driver for r in snap.battle.rivals] == ["A. Rival"]
    assert snap.battle.rivals[0].model == "model of E2"


def test_no_battle_for_an_out_lap_or_without_a_corner_map(monkeypatch):
    monkeypatch.setattr("simcoach.race.resolve_car", lambda name, *a: name)
    tr = RaceTracker()
    out = tr.commit(_race_record(2, _trace(_rival(800.0)), partial="out"), CornerMap([T1]))
    assert out.battle is None
    assert tr.commit(_race_record(3, _trace(_rival(800.0))), None).battle is None
```

Append to `tests/test_server_capture.py`:

```python
def test_capture_loop_stores_battle_rows_for_a_race_lap(tmp_path, monkeypatch):
    """#152: the race worker resolves the lap's corner map, builds the battle and stores
    one battle row per same-class car -- the driver included."""
    import numpy as np

    from simcoach.corners import Corner, CornerMap
    from simcoach.model_types import LapRecord
    monkeypatch.setattr("simcoach.race.resolve_car", lambda name, *a: name)

    def pts(s0):   # 5 Hz at 50 m/s
        return [(95.0 + i / 5, s0 + 10.0 * i, 180.0) for i in range(140)]

    trace = {"player_id": 1, "track_len": 1000.0, "cars": {
        1: {"driver": "You", "car": "E1", "class_position": 3, "pts": pts(750.0)},
        2: {"driver": "A. Rival", "car": "E2", "class_position": 2, "pts": pts(800.0)}}}
    t1 = Corner(1, "La Source", "right", 200.0, 250.0, 300.0, label="1")
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    app = create_app(store, Coach(FakeModel()), cmap=CornerMap([t1]))
    lap = LapRecord("lmu", "Spa", "GT3", 100.0, True, 1.0,
                    {"lap_distance": np.linspace(0.0, 1000.0, 10)}, phase="race",
                    session_id="s", lap_no=2, field_trace=trace)
    _capture_loop(_ListSource([lap]), store, app.state)
    app.state.race_pool.shutdown(wait=True)
    rows = store.battle_laps(app.state.latest["lap_id"])
    assert [r.driver for r in rows] == ["You", "A. Rival"]
    assert app.state.race.snapshot.battle.rivals[0].driver == "A. Rival"
```

Append to `tests/test_coach_tools.py`:

```python
def test_get_battle_reads_the_cached_battle_and_takes_a_named_rival(tmp_path, synthetic_lap):
    from simcoach.battle import CarLap, build_battle
    from simcoach.corners import Corner
    from simcoach.race import LapFacts, build_snapshot
    t1 = Corner(1, "La Source", "right", 200.0, 250.0, 300.0, label="1")
    me = CarLap(1, "You", "E1", 3, True, entry={1: 104.0}, exit={1: 106.0})
    rival = CarLap(2, "A. Rival", "E2", 2, False, entry={1: 103.0}, exit={1: 105.3},
                   model="Ferrari 296 GT3")
    ctx, _, _ = _ctx(tmp_path, synthetic_lap)
    assert ctx.dispatch("get_battle", {}).startswith("No live race")
    ctx.race = build_snapshot([LapFacts(lap_no=12, lap_time=100.0, clean=True)], "s",
                              build_battle([me, rival], [t1]))
    out = ctx.dispatch("get_battle", {})
    assert out.startswith("As of lap 12.")
    assert "You are quicker: Turn 1 (La Source) by 0.30 s." in out
    named = ctx.dispatch("get_battle", {"rival": "P2"})
    assert "Rival (the car you named): A. Rival (Ferrari 296 GT3)" in named
```

In `tests/test_coach.py`, add at the end of `test_agentic_prompt_routes_race_questions_to_one_race_tool`:

```python
    assert "→ get_battle" in p
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_battle.py tests/test_server_capture.py tests/test_coach_tools.py tests/test_coach.py -q -k "battle or race"`
Expected: FAIL: `TypeError: RaceTracker.commit() takes 2 positional arguments but 3 were given`, `error: unknown tool 'get_battle'`, and the prompt assert.

- [ ] **Step 3: Implement the core.** In `simcoach/race.py`:

(a) Add imports (isort order): `from simcoach.battle import Battle, build_battle, car_laps` after the `simcoach.analysis` import; `from simcoach.car_lookup import resolve_car` after it; `from simcoach.corners import CornerMap` after that.

(b) Add the last field of `RaceSnapshot`:

```python
    battle: Battle | None = None   # #152 get_battle: the lap's same-class timing, or None
```

(c) Replace `def build_snapshot(stint: list[LapFacts], session_id: str | None) -> RaceSnapshot:` with

```python
def build_snapshot(stint: list[LapFacts], session_id: str | None,
                   battle: Battle | None = None) -> RaceSnapshot:
```

and its `return` line with

```python
    return RaceSnapshot(latest.lap_no, session_id, state, strategy(stint, to_go), battle)
```

(d) In `RaceTracker.commit`, change the signature to `def commit(self, record: LapRecord, cmap: CornerMap | None = None) -> RaceSnapshot | None:` and replace

```python
        self._stint.append(facts)
        self.snapshot = build_snapshot(self._stint, record.session_id)
        return self.snapshot
```

with

```python
        self._stint.append(facts)
        battle = None
        # One lap of driving is compared: never an out-/in-/partial lap or a stop (an
        # invalidated lap still counts -- the fight was real).
        if (record.field_trace and cmap is not None and cmap.corners
                and record.partial is None and not record.pit):
            laps = car_laps(record.field_trace, cmap.corners, facts.lap_no)
            for c in laps:
                c.model = resolve_car(c.car)   # off the capture thread: this can scan logs
            battle = build_battle(laps, cmap.corners)
        self.snapshot = build_snapshot(self._stint, record.session_id, battle)
        return self.snapshot
```

- [ ] **Step 4: Wire the sidecar.** In `simcoach/server/app.py`:

(a) Replace the whole `_race_commit` function from Task 5 with:

```python
def _race_commit(app_state, store, lap, lap_id) -> None:
    """Refresh the race tools' cached snapshot for a stored lap and keep its battle rows
    (#152). A failure costs the race tools one lap of freshness, never a lap of capture
    -- logged, not raised (an exception inside a pool future would vanish silently)."""
    try:
        map_for = getattr(app_state, "map_for", None)
        snap = app_state.race.commit(lap, map_for(lap.track) if map_for else None)
        if snap is not None and snap.battle is not None:
            store.save_battle(lap_id, snap.battle.laps)
    except Exception:
        logging.getLogger(__name__).warning("race snapshot not refreshed", exc_info=True)
```

(b) In `_capture_loop`, change `app_state.race_pool.submit(_race_commit, app_state, lap)` to `app_state.race_pool.submit(_race_commit, app_state, store, lap, lap_id)`.

(c) In `create_app`, directly after the `_map_for` function body (after its final `return app.state.cmap`), add:

```python
    app.state.map_for = _map_for   # #152: the race worker resolves a lap's corner map
```

- [ ] **Step 5: The tool.** In `simcoach/coach_tools.py`:

(a) Add `from simcoach.battle import format_battle` directly before `from simcoach.coach import _setup_lines, live_electronics`.

(b) In `TOOLS`, directly after the `get_strategy` entry, add:

```python
    {
        "name": "get_battle",
        "description": "LIVE RACE ONLY. Where a same-class rival was faster or slower than "
                       "the driver on the LAST COMPLETED lap: corners and sections ranked by "
                       "time gained or lost, plus apex-speed differences (the passing-chance "
                       "read). By default the rival is the car the driver was nearest for most "
                       "of that lap (it may name two). Use for 'where is he quicker', 'where "
                       "can I pass him'. One call answers it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "rival": {"type": "string",
                          "description": "Optional: a rival the driver named: 'P4' (CLASS "
                                         "position), a driver name, or a car/team name "
                                         "('the Ferrari')."},
            },
        },
    },
```

(c) Directly after `get_strategy` (the method), add:

```python
    def get_battle(self, rival=None):
        return self._no_live_race() or format_battle(self.race.battle, self.race.as_of_lap,
                                                     self.units, rival)
```

(d) In `dispatch`, after the `"get_strategy"` entry, add:

```python
            "get_battle": lambda: self.get_battle(args.get("rival")),
```

- [ ] **Step 6: Extend the routing paragraph.** In `simcoach/coach.py`, replace

```python
    "tire life to the end → get_strategy. ONE call answers the question. Every result "
```

with

```python
    "tire life to the end → get_strategy; where a rival is quicker or slower than the "
    "driver, or where to pass him → get_battle. ONE call answers the question. Every result "
```

- [ ] **Step 7: CLAUDE.md layout.** In `CLAUDE.md`, replace

```
  strategy, cached once per lap commit by `RaceTracker`), `model/` (swappable `Model`),
```

with

```
  strategy, cached once per lap commit by `RaceTracker`), `battle.py` (same-class field
  trace → per-corner rival deltas for `get_battle`), `model/` (swappable `Model`),
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_battle.py tests/test_race.py tests/test_server_capture.py tests/test_coach_tools.py tests/test_coach.py tests/test_server_ws.py -q`
Expected: PASS. The Task 5 spy uses `*args`, so the new `commit(lap, cmap)` call still passes it.

- [ ] **Step 9: Full gate**

Run: `.venv/Scripts/python.exe -m pytest -q` and `ruff check simcoach tests scripts`
Expected: all green.

- [ ] **Step 10: Commit**

```bash
git add simcoach/race.py simcoach/server/app.py simcoach/coach_tools.py simcoach/coach.py CLAUDE.md tests/test_battle.py tests/test_server_capture.py tests/test_coach_tools.py tests/test_coach.py
git commit -m "feat(coach): #152 - get_battle: the last lap's rival read from the cached snapshot; battle rows stored

Refs #152

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Pywcy5ZyceRZNDLyjn3F35"
```

---

## Rig check (why every commit is `Refs #152`, not `Fixes`)

Label #152 `rig-verify` if it isn't already. Close it by hand after this pass.

**Part A**
1. **Timed race.** At a lap commit, `time_left` (`mEndET − mCurrentET`) matches the in-game clock within a second. "about N laps at your recent clean pace" is plausible.
2. **Lap-count race.** `max_laps` is the real race length, not the practice sentinel, and the text reads "N of M laps".
3. **Multiclass.** Class position and the class gaps ahead and behind match the in-game standings, including when a car of another class sits between you and your class rival. Note whether `mTimeBehindNext` updates continuously or only at timing lines.
4. **As-of-lap stamp.** It equals the lap you just completed (the game's lap counter).
5. **Spoken answers.** Ask one PTT question each for fuel to the end, pit window, tire life and gap trend. Each answer is 2–3 sentences and says "as of lap N". Tire life names the 30% as a threshold. No instantaneous gap is given.
6. **Tank size.** The `fuel_capacity` channel is non-zero and equals the car's tank size. `mFuelCapacity` is an unverified offset; if it reads 0, the pit window says "earliest lap is unknown".
7. **Virtual energy.** The per-lap energy % is plausible for a Hypercar and for a GT3.
8. **Pit stop.** After a stop, `get_strategy` says "not enough laps yet" until two clean laps land. Then the burn and wear rates restart.
9. **Known ceiling.** A sidecar restart mid-race gives "not enough laps yet" until two clean laps land (Decision 4).

**Part B**

10. **Scoring update rate.** After a race lap, read `store.battle_laps(<lap_id>)` and the lap's `field_trace` (log `len(pts)` against the lap time) to count scoring frames per second.
11. **Per-corner timing error (sets the noise floor).** For ≥10 corners, compare the driver's own row (`is_player`) entry→exit times against the same corners timed from the full-rate `.npz` (`lap_distance` plus `time_into_lap`, interpolated at the map's `start_dist`/`end_dist`). Set `BATTLE_NOISE_FLOOR_S` to about 2× the RMS error (two cars, each with that error). Set `APEX_SPEED_FLOOR_KPH` from the apex-speed spread. Update the constants and replace "PENDING RIG" with the measured value and date.
12. **Changing opponent.** After a lap-long fight where the opponent changed, one spoken `get_battle` answer names both cars. It states only corners above the floor, and says where he is quicker and where you are.
13. **Named rivals.** "P4", a driver's name and "the Ferrari" each resolve to the right car. Check that `resolve_car` gives model names for the field.

## Follow-ups (not in this plan; file as issues if wanted)

- `get_energy_report` reads `self.record`, but the live `coach_ws` path builds `CoachContext(record=None)`. So in the app it always answers "No lap captured yet". This is pre-existing, not caused by #152, and was found while reading.
- Seed `RaceTracker` from the store (the v21 columns plus the stint's `.npz` fuel/wear) after a sidecar restart mid-race.
- Leader-based finish: a lapped car's remaining laps can be over-counted by one.
- A stop that changes no tires (fuel/energy only) is not modelled in the pit window.

---

## Self-Review

**Spec coverage**

| Spec §3 item | Task |
|---|---|
| Figures valid for laps: lap numbers, per-lap targets, multi-lap trends; no instantaneous gaps | 1, 2, 5 (formatters), 6 (prompt line) |
| Computed at lap boundaries, cached with `as_of_lap`, tools only read the cache | 5 (`RaceTracker`, pool), 6 (tools read `ctx.race`) |
| Completed laps only, never a half-lap | 4 (sample at commit), 5 (commit-driven), 10 (trace handed over at the wrap) |
| One tool call per question + one routing line | 6, 11 (prompt) |
| PTT keeps 2–3 sentences | unchanged; rig item 5 |
| `get_race_state`: overall + class position | 1 (`race_sample`), 4 (capture), 5 (format) |
| Remaining: `max_laps` or `end_time − elapsed` + laps at recent clean pace | 1 (`laps_left`), 5 (format) |
| In-class car ahead/behind, gap trend slope, catch lap, ≥2 laps | 1 (`class_neighbours`, `gap_slope`, `catch_lap`), 5 (`_gap_read`) |
| Per-lap sample stored via a schema migration | 3 (v21), 4 (capture) |
| `get_strategy`: fuel/energy to the end (make it, margin, save target) | 2 (`strategy` via `laps_remaining` + `stint_save_advice`; Decision 7), 5 (format) |
| Pit window bounded by fuel/energy or tires | 2 (`pit_window`) |
| Tire life to a named wear floor, stated as a threshold | 2 (`WEAR_FLOOR_PCT`), 5 (format text), 6 (prompt) |
| `get_battle`: every same-class car recorded every lap, crossing times + apex speed | 7 (`FieldTrace`, `car_laps`), 10 (capture feed) |
| Per-corner and per-section deltas vs cars within battle range | 8 (`zone_deltas`, `nearest_rivals`) |
| Opponent resolved in code: nearest for most of the lap, may name two; or by position/driver/car | 8 (`nearest_rivals`, `match_rival`) |
| Gains/losses ranked + strongest zones (apex speed) | 8 (`format_battle`) |
| Noise floor; only deltas above it reported | 8 (`BATTLE_NOISE_FLOOR_S`), rig item 11 |
| Battle storage: one row per (lap, same-class car), SQLite | 9, 11 (persisted by the worker) |
| Guards: ≥2 clean laps in the stint | 2, 5 |
| Guards: out/in/partial excluded from every rate | 2 (`is_clean`), 5 (stint reset), 11 (no battle on those laps) |
| Guards: offline / not in a race → "no live race" | 5 (non-race clears), 6 (`_no_live_race` + live-only pass-in) |
| Tests: gap slope + catch lap, pit window, timed vs lap-count, wear floor, 2-lap guard | 1, 2, 5 |
| Tests: tool-level with fake snapshot and cache | 6, 11 |
| Tests: migration for the per-lap sample columns | 3 (and 9 for v22) |
| Tests: the cache only changes on lap commit | 5 (`test_the_cached_snapshot_only_changes_on_a_lap_commit`), 6 |
| Rig check | "Rig check" section |

**Placeholder scan:** there is no "TBD", "TODO", "similar to Task N" or "add error handling" anywhere. Every code step has literal code, and every run step has an exact command and an expected result.

**Type consistency:**
- `RaceSample`'s seven fields match `_RACE_COLUMNS`' names and order (Tasks 1, 3). `LapRecord.race` and `LapRecord.field_trace` default to `None` (Tasks 1, 10).
- `LapFacts` fields are defined in Task 2 and used by `_gap_read`/`build_snapshot` (Task 5) and by the test helpers in Tasks 5, 6 and 11.
- `Strategy`, `Resource` and `PitWindow` come from Task 2. `format_strategy` (Task 5) reads `note, laps_to_go, resources, advice, save_per_lap, tire_*, window`. `PitWindow(latest, earliest, binding, one_stop)` is positional everywhere.
- `RaceSnapshot(as_of_lap, session_id, state, strategy[, battle])` is built by `build_snapshot(stint, session_id[, battle])` in Tasks 5 and 11. The tools read `.as_of_lap`, `.battle`, and the formatters' inputs.
- `RaceTracker.commit(record)` becomes `commit(record, cmap=None)` in Task 11. `_race_commit(app_state, lap)` becomes `(app_state, store, lap, lap_id)`, and its single caller is updated in the same task. The Task 5 spy takes `*args`.
- The `FieldTrace.take()` dict keys `player_id`, `track_len` and `cars[id]{driver, car, class_position, pts}` are the same in Task 7 (producer and tests), Task 10 (capture) and Task 11 (the tracker and the inline sidecar test).
- `CarLap(car_id, driver, car, class_position, is_player, entry, exit, apex_kph, model)` is positional-first everywhere, and `battle_laps` round-trips it field for field (Task 9).
- The tool names `get_race_state`, `get_strategy` and `get_battle` (optional `rival`) are identical in `TOOLS`, `dispatch`, the prompt line and the tests.
