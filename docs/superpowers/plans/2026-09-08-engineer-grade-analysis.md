# Engineer-grade Lap & Setup Analysis — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the coach the reads a real race engineer works from — *where* time is lost (delta-t by bucket), dead time (coasting), a handling balance read, grip-envelope use, stint consistency and tire-deg trend — then extend capture by the setup channels the struct already exposes and add one setup-diagnostics tool that names the lever.

**Architecture:** Every item is the same shape: a **pure deterministic function** in `simcoach/analysis.py`, a **formatter** in `simcoach/format.py`, and either a new **agentic tool** in `simcoach/coach_tools.py` (`TOOLS` + a `CoachContext` method + `dispatch` wiring) or an extra fact line folded into the existing corner/lap formatters. Phase 0 is a `sample_from_telem()` extension in `simcoach/lmu/smm.py`. **No SQLite schema change** (verified — see Global Constraints), **no server/UI change**, no new dependency.

**Tech Stack:** Python 3 / numpy / pytest. Model behind the `Model` protocol; tools exercised with `simcoach.model.fake.FakeModel` (no network).

**Issue:** GitHub #38. **Spec:** `docs/superpowers/specs/2026-09-08-engineer-grade-analysis-design.md`. **Research:** `docs/research/2026-09-08-race-engineer-lap-analysis-gaps.md`.

---

## What I verified vs. assumed

Stated up front because the brief asked, and because three findings change the spec.

**CONFIRMED by reading the code:**

- `simcoach/lmu/rf2structs.py` `TelemWheelV01` (lines 41-76) declares `mSuspensionDeflection`, `mRideHeight`, `mSuspForce`, `mLateralPatchVel`, `mLongitudinalPatchVel`, `mLateralGroundVel`, `mLongitudinalGroundVel`, `mCamber`, `mToe`, `mTemperature[3]`, `mVerticalTireDeflection`. `simcoach/lmu/smm.py` `sample_from_telem()` (lines 255-264) reads **only** `mTemperature[1]`, `mPressure`, `mTireLoad`, `mWear`, `mGripFract`, `mRotation`, `mBrakeTemp`, `mBrakePressure`. **The research note is correct: Phase 0 is a cheap `sample()` addition, no struct work.**
- `store.save()` does `np.savez_compressed(path, **record.channels)` and `store.load()` does `{k: npz[k] for k in npz.files}` (`simcoach/store.py:321,360`). Channels pass through **generically** — new channels need no migration. `SCHEMA_VERSION = 11` (`store.py:41`); **this plan adds no migration.** If a per-corner metrics cache is ever needed (see Task 8 ceiling), **v12** is the next number.
- **There is no `time` channel in captured laps.** `sample_from_telem` emits no time key; only the `tests/conftest.py` `synthetic_lap` fixture fabricates one. So delta-t **must** be integrated from `ds / v` over `lap_distance`/`speed` — exactly as the spec says, and the reason it has to.
- **`speed` is km/h**, not m/s (`smm.py:233`, `|mLocalVel| * 3.6`). The integration is `dt = ds / (v_kmh / 3.6)`. Getting this wrong silently scales every delta by 3.6.
- The `tests/conftest.py` `synthetic_lap` fixture has **no** `long_g`, `wheel_rot_*`, `gear`, or `rpm` channels. Tasks needing them add their own local fixture rather than mutating the shared one (several suites assert on its current output).

**CONFIRMED against the authoritative header** (`S:\SteamLibrary\steamapps\common\Le Mans Ultimate\Support\SharedMemoryInterface\InternalsPlugin.hpp`, lines 140-180 — the same file `rf2structs.py` was transcribed from):

| Field | Header comment | Consequence |
|---|---|---|
| `mGripFract` | *"an approximation of what fraction of the contact patch is **sliding**"* | **Higher = more sliding.** `analysis.py:25,186` and `format.py:53` both document it as the **opposite** ("near 0 = sliding", "1.0 = full grip"). See Open Question 1. |
| `mTemperature[3]` | *"left/center/right (**not to be confused with inside/center/outside!**)"* | The spec's `tire_temp_in_{w}` ← `[0]` / `tire_temp_out_{w}` ← `[2]` mapping is **wrong for right-side wheels**. See Open Question 2. |
| `mWear` | *"0.0-1.0, **fraction of maximum**"* | Reads as fraction **worn**, while `analysis.py:73` treats it as remaining and `format.py:99` prints "Tire wear remaining". See Open Question 3. |
| `mBrakeTemp` | *"**Celsius**"* | `smm.py:263` subtracts 273.15. See Open Question 4. |
| `mSuspensionDeflection`, `mRideHeight` | *meters* | Thresholds in Task 11 are in meters, not mm. |
| `mSuspForce` | *"pushrod load in Newtons"* | — |
| `mBrakePressure` | *"currently 0.0-1.0 … will convert to true kPa in future"* | It is a **fraction**, not a pressure. Don't print kPa. |
| `mCamber`, `mToe` | radians | **Live dynamic camber is captured-able and the spec never mentions it** — a direct measurement, strictly better than inferring camber from tread spread. See Open Question 2. |
| `mElapsedTime` / `mLapStartET` | *"game session time" / "time this lap was started"* | An **exact** in-lap time axis is a one-line capture add. See Open Question 5. |

**ASSUMED (not verified):** that the header comments describe LMU's *runtime* behaviour. rf2structs.py's own docstring says every convention is "PENDING LIVE VALIDATION", and the `mGripFract` / `mWear` / `mBrakeTemp` items above are places where the shipped code and the header disagree — one of them is wrong and only the rig settles it. **Task 13 is that check**, and every task below is written so a wrong guess there does not invalidate the work.

---

## Global Constraints

- **Two-stage invariant.** Each function is pure (`LapRecord` channels + `CornerMap` in, numbers out), tested with synthetic channels and a golden assertion. Tools return **computed fact strings**. Raw ~50 Hz arrays never reach the model.
- **Corners stay owned data.** Bucket boundaries and corner identity come from `CornerMap` / `CornerMetrics`; direction from `compute_direction`. No new code path asks the model to infer a corner, a direction, or a name.
- **Storage split unchanged.** New channels ride the per-lap `.npz` on the existing lap-distance axis. SQLite untouched; no migration in this plan.
- **`Model` interface untouched.** Tools are dispatched by the existing agentic loop; no SDK call outside `simcoach/model/`.
- **UI-agnostic core.** Nothing in this plan touches `simcoach/server/`, `frontend/`, or `src-tauri/`. Tools surface through the existing coach loop both clients already share.
- **Graceful degradation is a tested requirement, not a nicety.** Older laps lack the new channels; every tool returns a clear *"not captured for this lap (older capture)"* line — mirror `get_wheel_report`'s `None` → message pattern. Each such path gets its own test.
- **Sign/semantics conventions live behind one named constant each**, with a `ponytail:` comment and a pointer to Task 13. No heuristic hard-codes a disputed sign inline.
- **Env:** tests run `./.venv/Scripts/python.exe -m pytest` from the repo root (verified working). Lint `./.venv/Scripts/python.exe -m ruff check <files>` (line-length 100); only avoid **new** findings — the repo carries pre-existing lint debt. `git status --short simcoach/tracks/` must stay empty (shipped maps are not to be rewritten).
- **`[RIG]` / `[LIVE-LLM]` tasks are off the critical path.** Tasks 1-12 and 14's offline steps are completable unattended with no sim and no API key.

---

# Slice 1 (the vertical slice) — "Where am I losing time, and how much of it is coasting?"

**This is the recommended first ship.** Tasks 1-4. It is useful standing alone: the coach can answer the single highest-value question in the research spike ("where am I losing time?") with a ranked, numeric, corner-named answer, and immediately explain part of it ("0.4 s of the 0.6 s you lose at T4 is coasting between brake release and throttle"). Tasks 1-2 also build the distance-alignment helper that every later lap-vs-lap read reuses.

**It defers:** handling read, G-G, consistency, tire trend, gear/lockup (Slice 2-3), and the whole setup track (Slice 4). Nothing in Slice 1 blocks them.

### Task 1: Distance-keyed time axis + two-lap alignment (pure helpers)

**Files:** Modify `simcoach/analysis.py`; Test: `tests/test_analysis.py`

**Interfaces — produces, in `analysis.py`:**
- `lap_time_axis(record) -> tuple[np.ndarray, np.ndarray]` → `(dist, t)`, cumulative elapsed seconds along `lap_distance`, trapezoidally integrating `ds / (speed_kmh / 3.6)`. `t[0] == 0.0`. Clamps speed to `_MIN_SPEED_KMH = 1.0` so a stationary sample can't divide by ~0. Drops non-increasing `lap_distance` samples (pit re-entry / S-F wrap / sensor jitter) before integrating — monotonic distance is a precondition of the whole comparison.
- `align_time_axes(record, reference) -> tuple[np.ndarray, np.ndarray] | None` → `(dist_grid, delta_t)` where `delta_t = t_cur(d) - t_ref(d)` on the **overlapping** distance range of both laps, sampled on the current lap's (filtered) distance grid via `np.interp`. Returns `None` when the overlap is shorter than `_MIN_OVERLAP_FRAC = 0.5` of the current lap (a partial/out lap can't be compared honestly).

- [ ] **Step 1: Write the failing tests** in `tests/test_analysis.py`:

```python
def _flat_lap(speed_kmh, n=500, length=1000.0, **extra):
    """A constant-speed lap on a clean distance axis."""
    import numpy as np
    from simcoach.model_types import LapRecord
    dist = np.linspace(0.0, length, n)
    ch = {"lap_distance": dist, "speed": np.full(n, float(speed_kmh)),
          "throttle": np.ones(n), "brake": np.zeros(n), "steering": np.zeros(n)}
    ch.update(extra)
    return LapRecord("lmu", "t", "c", 0.0, True, 0.0, ch)

def test_lap_time_axis_constant_speed_is_distance_over_speed():
    from simcoach.analysis import lap_time_axis
    d, t = lap_time_axis(_flat_lap(72.0, length=1000.0))   # 72 km/h = 20 m/s
    assert t[0] == 0.0
    assert abs(t[-1] - 50.0) < 0.05        # 1000 m / 20 m/s = 50 s  (km/h NOT m/s)

def test_lap_time_axis_ignores_non_increasing_distance():
    import numpy as np
    from simcoach.analysis import lap_time_axis
    lap = _flat_lap(72.0, n=100, length=1000.0)
    lap.channels["lap_distance"][50] = 10.0          # a backwards jitter sample
    d, t = lap_time_axis(lap)
    assert np.all(np.diff(d) > 0)                     # filtered out
    assert abs(t[-1] - 50.0) < 0.5

def test_lap_time_axis_survives_a_stopped_sample():
    from simcoach.analysis import lap_time_axis
    lap = _flat_lap(72.0)
    lap.channels["speed"][10] = 0.0
    _, t = lap_time_axis(lap)
    import math; assert math.isfinite(t[-1])           # no div-by-zero / inf

def test_align_time_axes_injected_loss_shows_as_delta():
    from simcoach.analysis import align_time_axes
    ref = _flat_lap(72.0, length=1000.0)
    cur = _flat_lap(72.0, length=1000.0)
    # slow to 36 km/h (10 m/s) between 400-600 m: 200 m costs 20 s instead of 10 s
    m = (cur.channels["lap_distance"] >= 400) & (cur.channels["lap_distance"] <= 600)
    cur.channels["speed"][m] = 36.0
    d, delta = align_time_axes(cur, ref)
    assert abs(delta[-1] - 10.0) < 0.3                # +10 s lost, net
    assert abs(delta[d < 400][-1]) < 0.1              # nothing lost before the slow bit
    assert abs(delta[d > 600][0] - 10.0) < 0.3        # all of it inside the window

def test_align_time_axes_rejects_insufficient_overlap():
    from simcoach.analysis import align_time_axes
    assert align_time_axes(_flat_lap(72.0, length=300.0), _flat_lap(72.0, length=3000.0)) is None
```

- [ ] **Step 2: Run to verify they fail.** `./.venv/Scripts/python.exe -m pytest tests/test_analysis.py -k "time_axis or align_time" -v` → FAIL (ImportError).
- [ ] **Step 3: Implement** both helpers in `analysis.py` near the top (they are lap-level, not corner-level). Use `np.trapz`-style cumulative integration (`np.cumsum` of midpoint `ds/v`), `np.interp` for resampling. Add a `ponytail:` comment naming the ceiling: *trapezoidal integration of ~50 Hz ds/v; exact only to sampling resolution — `mElapsedTime` would be exact (see plan Open Question 5)*.
- [ ] **Step 4: Run to verify they pass.**
- [ ] **Step 5: Full analysis suite + ruff.** `./.venv/Scripts/python.exe -m pytest tests/test_analysis.py -q` && `./.venv/Scripts/python.exe -m ruff check simcoach/analysis.py`
- [ ] **Step 6: Commit** — `git commit -m "feat(analysis): distance-keyed lap time axis + two-lap delta-t alignment"`

---

### Task 2: Time-loss buckets over the whole lap + formatter

**Depends on:** Task 1.

**Files:** Modify `simcoach/analysis.py`, `simcoach/format.py`; Test: `tests/test_analysis.py`, `tests/test_format.py`

**Interfaces:**
- Consumes `align_time_axes`, `CornerMap.corners`, `in_window`.
- Produces `analysis.time_loss_buckets(record, reference, cmap) -> list[dict] | None` — one bucket per corner window **plus** one per inter-corner gap, each `{"label": str, "kind": "corner"|"straight", "delta": float, "start_dist": float, "end_dist": float}`, in track order. `net_delta` == sum of bucket deltas (see design decision below). `None` when `align_time_axes` returns `None`.
- Produces `format.format_time_loss(buckets, reference_label) -> str` — net line plus the top-N losses ranked biggest-first (`_TOP_BUCKETS = 5`), e.g. `"Net -0.60s vs your session best. Biggest losses: T4 Village -0.31s, back straight (T6→T7) -0.18s, T1 -0.09s."` Also names the biggest **gain** when one exists, so the coach can reinforce it.

**Design decision (deviates from the spec — flagged, with reasoning):** the spec says bucket *"by corner window … no separate fixed-grid mini-sectors"*, but corner windows do **not** cover the lap, so the buckets would not sum to the net delta the same output line reports — a coach handing back arithmetic that doesn't add up is worse than no tool. **Covering the whole lap** with corner buckets *plus* the gaps between them keeps the spec's "no fixed grid, reuse owned corner boundaries" intent, makes the numbers reconcile, and adds a genuinely useful read (time lost on a straight = an exit/traction problem, which a corner-only split hides). Straight buckets are labelled from the corners they sit between, so nothing is invented.

**Also deferred deliberately:** the spec's example output splits *within* a corner (`"T4 braking"`, `"T7 exit"`). That is Task 4b — it needs only `brake_point`/`apex_dist`, both already on `CornerMetrics`, and it slots in without changing this function's contract.

- [ ] **Step 1: Write the failing tests.** In `tests/test_analysis.py`, build two laps on `simcoach/tracks/test_track.json` geometry (corners at 800-1200 and 2000-2400, lap ≈ 3000 m — reuse the `synthetic_lap` fixture's shape or `_flat_lap(length=3000)`), inject a slowdown **inside corner 1's window only**, and assert:

```python
def test_time_loss_buckets_attribute_the_loss_to_the_right_corner():
    # ... cur = ref but slower between 900-1100 m (inside T1's 800-1200 window)
    b = time_loss_buckets(cur, ref, cmap)
    by = {x["label"]: x["delta"] for x in b}
    t1 = [x for x in b if x["kind"] == "corner" and x["label"].startswith("T1")][0]
    assert t1["delta"] > 0.5                                  # the loss landed in T1
    assert all(abs(x["delta"]) < 0.05 for x in b if x is not t1)   # and nowhere else

def test_time_loss_buckets_sum_to_the_net_delta():
    b = time_loss_buckets(cur, ref, cmap)
    _, delta = align_time_axes(cur, ref)
    assert abs(sum(x["delta"] for x in b) - float(delta[-1])) < 0.02

def test_time_loss_buckets_cover_corners_and_straights():
    b = time_loss_buckets(cur, ref, cmap)
    assert {x["kind"] for x in b} == {"corner", "straight"}
    assert len([x for x in b if x["kind"] == "corner"]) == len(cmap.corners)

def test_time_loss_buckets_none_without_usable_reference():
    assert time_loss_buckets(short_lap, long_ref, cmap) is None
```

In `tests/test_format.py`, assert `format_time_loss` ranks biggest-loss-first, signs losses negative, and includes the net.

- [ ] **Step 2: Run to verify fail.** `./.venv/Scripts/python.exe -m pytest tests/test_analysis.py tests/test_format.py -k time_loss -v`
- [ ] **Step 3: Implement.** Bucket delta = `delta_t` at the bucket's end distance minus at its start (interpolated) — no re-integration. Handle an S/F-seam-wrapped corner window (`start_dist > end_dist`, `in_window` is wrap-aware) by splitting it into the two distance runs and summing; if that is awkward, treat a wrapped corner as one bucket spanning to lap end and note the ceiling with a `ponytail:` comment. Corner labels via `corner.label or str(corner.number)` + `corner.name`, exactly as `format_corner_line` does.
- [ ] **Step 4: Run to verify pass.**
- [ ] **Step 5: Full suite + ruff** on the two files.
- [ ] **Step 6: Commit** — `git commit -m "feat(analysis): whole-lap time-loss buckets by corner window + formatter"`

---

### Task 3: `get_time_loss` coach tool

**Depends on:** Task 2.

**Files:** Modify `simcoach/coach_tools.py`, `simcoach/coach.py` (prompt); Test: `tests/test_coach_tools.py`

**Interfaces:**
- `CoachContext.get_time_loss(which="latest", reference="best") -> str`. Resolves the lap with the existing `_resolve_lap`. Resolves the reference **the same way `_analysis_for` already does** — this session's best first (same-phase fairness), all-time best as fallback — so `get_time_loss` and the per-corner `min vs best` deltas never disagree about what "best" means. Never compares a lap to itself (pass `exclude_lap_id`).
- `TOOLS` entry `get_time_loss` with `which` + `reference` (both optional strings), described as *the* first tool for "where am I losing time".
- `dispatch` wiring.
- Honest failure strings: no corner map → the existing wording; no reference lap → `"No reference lap to compare against yet — you need a second valid lap on this track/car."`; unusable overlap → `"That lap can't be compared to the reference (partial lap or a different track length)."`

- [ ] **Step 1: Write the failing tests** in `tests/test_coach_tools.py`, extending the `_ctx` helper pattern (it builds a real `LapStore` + `simcoach/tracks/test_track.json` map):

```python
def test_get_time_loss_names_the_worst_corner(tmp_path, synthetic_lap):
    # store a fast reference lap and a slower current lap (slow inside T1's window)
    ...
    out = ctx.dispatch("get_time_loss", {})
    assert "T1" in out and "-" in out            # the loss is attributed and signed
    assert "Net" in out

def test_get_time_loss_is_honest_without_a_reference(tmp_path, synthetic_lap):
    ctx, _, _ = _ctx(tmp_path, synthetic_lap)    # only one lap stored
    assert "reference" in ctx.get_time_loss().lower()

def test_get_time_loss_registered_in_tools_and_dispatch(tmp_path, synthetic_lap):
    assert any(t["name"] == "get_time_loss" for t in TOOLS)
    ctx, _, _ = _ctx(tmp_path, synthetic_lap)
    assert not ctx.dispatch("get_time_loss", {}).startswith("error:")

def test_agentic_loop_can_call_get_time_loss(tmp_path, synthetic_lap):
    # FakeModel scripted tool call, mirroring test_agentic_loop_dispatches_tool_calls
    ...
```

- [ ] **Step 2: Run to verify fail.** `./.venv/Scripts/python.exe -m pytest tests/test_coach_tools.py -k time_loss -v`
- [ ] **Step 3: Implement** the method, `TOOLS` spec and `dispatch` entry.
- [ ] **Step 4: Prompt guidance** in `coach.py` `AGENTIC_SYSTEM_PROMPT`: for any "where am I losing time / why am I slower / what should I work on" question, call `get_time_loss` **first**, then `get_corner` on the worst bucket to explain *why*. Keep it to a sentence or two — the prompt is already long.
- [ ] **Step 5: Run to verify pass**, then full `tests/test_coach_tools.py tests/test_coach.py` + ruff.
- [ ] **Step 6: Commit** — `git commit -m "feat(coach): get_time_loss tool - ranked delta-t by corner bucket"`

---

### Task 4: Coasting / dead-time, folded into the corner + lap facts

**Depends on:** nothing (parallelisable with 1-3), but ship after so Slice 1 reads as one feature.

**Files:** Modify `simcoach/analysis.py`, `simcoach/format.py`; Test: `tests/test_analysis.py`, `tests/test_format.py`

**Interfaces:**
- New `CornerMetrics` fields, defaulting to `float("nan")` like the existing extended ones: `coast_time` (s), `coast_dist` (m), `brake_to_throttle_dist` (m — last brake-off to first meaningful throttle, `nan` when there's no braking phase).
- New `LapAnalysis` field `coast_total: tuple[float, float] | None` → `(seconds, meters)` over the whole lap.
- Threshold constants `_COAST_THROTTLE = 0.05`, `_COAST_BRAKE = 0.05` (the spec's values), with a `ponytail:` note that they're a tuning knob.
- `format_corner_line` appends `", coasting 0.4s (18 m)"` when `coast_time > 0.05` s (silent below that — a clean corner shouldn't carry a noise line), and `", 18 m brake→throttle gap"` when finite and > 1 m.
- `format_lap_facts` appends a lap-level `"Coasting this lap: 2.1 s (86 m) with neither pedal."` line when known.

Coast time inside a corner window uses the same `ds / (v/3.6)` integration as Task 1, restricted to the coasting mask — import and reuse, don't re-derive.

- [ ] **Step 1: Write the failing tests.** A synthetic corner with an explicit coast plateau (brake released at 900 m, throttle picked up at 1000 m, constant 36 km/h = 10 m/s between → exactly 100 m / 10 s) → assert `coast_dist ≈ 100`, `coast_time ≈ 10`, `brake_to_throttle_dist ≈ 100`; a corner with no coasting → `coast_time == 0.0` and **no** coasting text in `format_corner_line`; a lap missing `throttle`/`brake` → `nan`, no crash, no line.
- [ ] **Step 2: Run to verify fail.**
- [ ] **Step 3: Implement** in `_metrics_for_corner` (window-scoped) and `analyze_lap` (lap-scoped), plus the two formatter lines.
- [ ] **Step 4: Run to verify pass.** Also re-run `tests/test_coach_tools.py` and `tests/test_coach.py` — **the corner line is asserted in several places**; expect to update substring assertions that now have text appended.
- [ ] **Step 5: Full suite + ruff.**
- [ ] **Step 6: Commit** — `git commit -m "feat(analysis): coasting / dead-time + brake-to-throttle gap per corner and lap"`

### Task 4b (optional, same slice): sub-corner phase split for time loss

Split each corner bucket into `braking` (bucket start → `brake_point`… apex) / `apex` / `exit` (apex → bucket end) using `CornerMetrics.brake_point` and `apex_dist`, so the output reads like the spec's `"T4 braking -0.31s, T7 exit -0.18s"`. Same test shape as Task 2 (inject the loss into the braking run only, assert it lands in the `braking` sub-bucket). Additive to Task 2's contract — buckets gain a `phase` key; the net still reconciles. **Do this only if Slice 1 lands early**; the whole-corner attribution already answers the question.

---

# Slice 2 — Handling, envelope and input sanity (still fully offline)

### Task 5: `get_handling` — understeer / oversteer read

**Depends on:** Tasks 1-4 merged (no code dependency; avoids formatter conflicts).

**Files:** Modify `simcoach/analysis.py`, `simcoach/format.py`, `simcoach/coach_tools.py`, `simcoach/coach.py`; Test: `tests/test_analysis.py`, `tests/test_coach_tools.py`

**Interfaces:** `analysis.handling_read(record, corner) -> dict` → `{"label": "entry understeer"|"exit oversteer"|"neutral"|"unknown", "evidence": [str, ...], "confidence": "low"|"medium"}`; `format.format_handling(reads) -> str`; tool `get_handling(corner=None, which="latest")` (all corners when `corner` is omitted, worst-first).

**The primary signal is steering vs lat_g only** — unambiguous and offline-verifiable:
- **Understeer:** `|steering|` still increasing through the corner while `|lat_g|` has plateaued at/near the lap's observed peak (within `_LATG_PLATEAU = 0.95` of it).
- **Oversteer:** steering **reducing** or **reversing sign** (countersteer) while `|lat_g|` is still high.
- **Lockup / wheelspin** from `wheel_rot_*`: near-zero rotation while `speed` is well above zero under braking; a spike above the rolling-speed-implied rate on throttle. (`mRotation` is rad/s and `mStaticUndeflectedRadius` is *not* sampled, so compare each wheel against the **four-wheel median**, not an absolute rolling rate — that sidesteps needing a tire radius.)

**The per-wheel `grip_*` front-vs-rear axis is gated behind one constant** because its direction is disputed (Open Question 1):

```python
# ponytail: the LMU/rF2 header says mGripFract is the fraction of the contact
# patch that is SLIDING (higher = worse), while analysis.py/format.py have long
# documented the opposite. Until the rig check settles it (plan Task 13), the
# handling label rests on steering-vs-lat_g alone and the grip axis only adds a
# hedged evidence line. Flip this one constant when it's confirmed.
_GRIP_HIGHER_MEANS_MORE_SLIDING = True
```

- [ ] **Step 1: Write the failing tests** — a synthetic understeer trace (steering ramping while `lat_g` flat at peak) → `"understeer"` in the label; a synthetic oversteer trace (steering reversing sign at high `lat_g`) → `"oversteer"`; a clean corner → `"neutral"`; a lap with **no** `lat_g` → `"unknown"` and an honest message, not a crash; a locked wheel (`wheel_rot_fl ≈ 0`, others rolling, under brake) → a lockup evidence line naming FL. Plus tool registration + `FakeModel` dispatch tests as in Task 3.
- [ ] **Step 2-4:** verify fail → implement → verify pass. Keep `handling_read` out of `_metrics_for_corner` (it's a separate tool's concern and `CornerMetrics` is already wide).
- [ ] **Step 5:** Prompt line: call `get_handling` for any balance complaint ("won't turn in", "snappy on exit") and quote its evidence; never assert a balance diagnosis without it.
- [ ] **Step 6: Full suite + ruff. Commit** — `git commit -m "feat(coach): get_handling - understeer/oversteer read from steering vs lat_g"`

### Task 6: Traction-circle (G-G) utilization, folded into `get_lap_summary`

**Depends on:** Task 4 (shares the formatter).

**Files:** `simcoach/analysis.py`, `simcoach/format.py`; Test: `tests/test_analysis.py`, `tests/test_format.py`

**Interfaces:** `analysis.gg_envelope(record) -> dict | None` → `{"radius_g": float, "pct_used_mean": float, "dead_middle_pct": float}`. Envelope radius = the lap's observed max `hypot(lat_g, long_g)` (the spec's naive per-car peak); `pct_used` = mean of `hypot(...)/radius` over the **cornering phase only** (`|lat_g| > _CORNERING_LATG = 0.3`); `dead_middle_pct` = fraction of cornering-phase samples below `_DEAD_MIDDLE = 0.5` of the envelope. New `LapAnalysis.gg` field; `format_lap_facts` adds `"Grip envelope: peak 1.6g combined; using 74% of it through corners; 11% of cornering time below half-envelope (coasting / inputs not blended)."` Omitted entirely when `long_g` is absent (older laps).

No new tool — the spec offers `get_gg` as an alternative and folding it in is the lazier, equally useful choice (it rides every whole-lap question for free).

- [ ] **Step 1:** Failing tests — a synthetic lap with a known max combined g and a known low-g plateau in the corner → golden `radius_g`, `pct_used_mean`, `dead_middle_pct`; a lap with no `long_g` → `None`, no line, no crash.
- [ ] **Steps 2-4:** verify fail → implement → verify pass. `ponytail:` comment: *observed-max envelope, per lap; a fitted circle over recent laps is the upgrade path.*
- [ ] **Step 5: Full suite + ruff. Commit** — `git commit -m "feat(analysis): traction-circle envelope utilization + dead-middle in the lap summary"`

### Task 7: Gear / RPM sanity + lockup flags in the corner facts

**Depends on:** Tasks 4, 5 (reuses the lockup helper from Task 5).

**Files:** `simcoach/analysis.py`, `simcoach/format.py`; Test: `tests/test_analysis.py`, `tests/test_format.py`

**Interfaces:** `CornerMetrics.apex_gear` (int-valued float, `nan` when `gear` absent), `min_rpm_on_throttle`, `lockup_wheels: str` (e.g. `"FL"`, `""`). `format_corner_line` appends `", gear 3 @ apex"`, `", bogging (4800 rpm on exit)"` when `min_rpm_on_throttle` is below `_BOG_RPM_FRAC = 0.55` of the lap's observed max rpm, and `", FL lockup"` when flagged.

**Deliberate scope cut:** "wrong gear for the corner" and "short-shift" need a per-car power band the app does not have. `_BOG_RPM_FRAC` of observed max rpm is the honest proxy — it flags *bogging*, which is what the driver can act on. `ponytail:` comment says so; a per-car power-band table is the upgrade path.

- [ ] **Step 1:** Failing tests — synthetic lap with `gear`/`rpm` (a **local** fixture; do not extend the shared `synthetic_lap`) → golden `apex_gear` and a bog flag at a known low-rpm exit; lap without `gear`/`rpm` → `nan`, no line.
- [ ] **Steps 2-4:** verify fail → implement → verify pass (re-run the corner-line assertion suites).
- [ ] **Step 5: Full suite + ruff. Commit** — `git commit -m "feat(analysis): apex gear, exit bog and lockup flags in the corner facts"`

---

# Slice 3 — Stint-level reads

### Task 8: `get_consistency`

**Depends on:** Tasks 1-4 merged.

**Files:** `simcoach/analysis.py`, `simcoach/format.py`, `simcoach/coach_tools.py`, `simcoach/coach.py`; Test: `tests/test_analysis.py`, `tests/test_coach_tools.py`

**Interfaces:** `analysis.consistency(records, cmap) -> dict` → `{"lap_time_sigma": float, "n_laps": int, "per_corner": {label: {"min_spd_sigma": float, "brake_point_sigma": float}}}` (pure — takes already-loaded records, no store). Tool `get_consistency()` loads this session's valid laps via `store.session_laps` + `store.load`, **scoped to the current phase** (a qualy hot lap and a race stint lap are not the same population), formats `"Lap time σ 0.41s over 8 race laps. Least consistent: T4 min speed ±6 km/h, T1 brake point ±14 m."` Needs ≥ 3 laps; below that, say so.

**Ceiling:** re-analyses every lap per call (N `.npz` loads + `analyze_lap`). Fine for a stint of ~10-30 laps at conversational cadence. `ponytail:` comment: *the `corner_metrics` SQLite table already caches a subset; promote the cache (schema v12) only if this is measurably slow.*

- [ ] **Step 1:** Failing tests — a synthetic set of laps where exactly one corner's min speed is noisy and the rest are identical → assert that corner is ranked least consistent and the others' σ ≈ 0; a 2-lap session → the "need more laps" message; laps from a different phase excluded. Tool registration + `FakeModel` dispatch.
- [ ] **Steps 2-4:** verify fail → implement → verify pass.
- [ ] **Step 5:** Prompt line: use for "am I consistent / what's inconsistent" and before advising on a single lap's outlier.
- [ ] **Step 6: Full suite + ruff. Commit** — `git commit -m "feat(coach): get_consistency - lap-time and per-corner variance across the stint"`

### Task 9: `get_tire_trend`

**Depends on:** Task 8 (reuses its load-the-stint helper — factor that out in Task 8 if not already).

**Files:** `simcoach/analysis.py`, `simcoach/format.py`, `simcoach/coach_tools.py`, `simcoach/coach.py`; Test: `tests/test_analysis.py`, `tests/test_coach_tools.py`

**Interfaces:** `analysis.tire_trend(records) -> dict | None` → per-wheel least-squares slope per lap of `tire_temp_*`, `tire_press_*`, `tire_wear_*` plus the lap-time slope. Tool `get_tire_trend()` → `"Over 8 race laps: pace +0.15 s/lap; RL wear 2.1%/lap (hottest wheel, mean 104 °C); pressures +0.8 kPa/lap. Reads deg-limited."` `None` → `"Not enough laps with tire data in this stint yet."`

**Wear-direction caveat:** the header says `mWear` is *fraction of maximum* (worn), the code treats it as remaining (Open Question 3). So **report the direction measured from the data** and derive the wording from one constant:

```python
# ponytail: mWear direction is unsettled (plan Open Question 3 / Task 13). Slope
# sign is reported as measured; this constant only picks the WORDING.
_WEAR_IS_REMAINING = True   # True -> a falling channel means "wearing"
```

- [ ] **Step 1:** Failing tests — synthetic laps with a linear wear ramp and a linear pace drift → golden slopes (sign included); laps without tire channels → `None` + honest message; single lap → `None`.
- [ ] **Steps 2-4:** verify fail → implement → verify pass.
- [ ] **Step 5:** Prompt line: use for deg/stint-length questions alongside `get_wheel_report` (single lap) — and note this feeds the longitudinal profile.
- [ ] **Step 6: Full suite + ruff. Commit** — `git commit -m "feat(coach): get_tire_trend - per-stint tire and pace degradation slopes"`

---

# Slice 4 — Capture extension + setup diagnostics

### Task 10: Phase 0 — sample the setup channels the struct already exposes

**Depends on:** nothing. **Can run in parallel with Slices 1-3** (it touches only `smm.py`).

**Files:** Modify `simcoach/lmu/smm.py` (`sample_from_telem` + its docstring); Test: `tests/test_lmu_smm.py`

**Interfaces — new per-wheel channels** (`{w}` ∈ fl/fr/rl/rr), all from `TelemWheelV01`, keeping every existing key unchanged (`tire_temp_{w}` stays `mTemperature[1]`, the centre):

| Channel | Source | Unit |
|---|---|---|
| `ride_height_{w}` | `mRideHeight` | m |
| `susp_defl_{w}` | `mSuspensionDeflection` | m |
| `susp_force_{w}` | `mSuspForce` | N |
| `tire_defl_{w}` | `mVerticalTireDeflection` | m |
| `lat_patch_vel_{w}` | `mLateralPatchVel` | m/s |
| `long_ground_vel_{w}` | `mLongitudinalGroundVel` | m/s |
| `tire_temp_l_{w}` | `mTemperature[0]` | °C |
| `tire_temp_r_{w}` | `mTemperature[2]` | °C |
| `camber_{w}` | `mCamber` | rad |

**Three deliberate deviations from the spec, all flagged above:**
1. **`tire_temp_l_` / `tire_temp_r_`, not `_in_`/`_out_`** — the header says `mTemperature` is *left/center/right, "not to be confused with inside/center/outside"*. Naming them in/out at the capture layer bakes in a wrong mapping for one side of the car. Capture faithfully; map left/right → inner/outer **per wheel side** in `analysis.py`, where it is testable (Open Question 2).
2. **`camber_{w}` added** — dynamic camber is a direct measurement and a strictly better camber diagnostic than tread spread. One line, same struct read.
3. **`long_ground_vel_{w}` added** — with `lat_patch_vel` it yields a real **slip angle in degrees** (`atan2`), not just the spec's "proxy". Needed by Task 12.

Also consider the rake pair `mFrontRideHeight` / `mRearRideHeight` on `TelemInfoV01` (chassis-level, not per-wheel) as `ride_height_front`/`ride_height_rear` — free, and the direct source for a rake read.

- [ ] **Step 1: Write the failing test** in `tests/test_lmu_smm.py`, extending `make_telem()` (lines ~95-108) to set the new wheel fields, then:

```python
def test_sample_from_telem_maps_setup_channels():
    t = roundtrip(make_telem())           # make_telem now sets the new wheel fields
    s = sample_from_telem(t, lap_dist=0.0)
    for w in ("fl", "fr", "rl", "rr"):
        assert 0.0 < s[f"ride_height_{w}"] < 0.5        # meters, plausible
        assert s[f"susp_defl_{w}"] == pytest.approx(0.03)
        assert s[f"susp_force_{w}"] == pytest.approx(2500.0)
        assert s[f"tire_defl_{w}"] == pytest.approx(0.012)
        assert s[f"lat_patch_vel_{w}"] == pytest.approx(1.5)
        assert s[f"long_ground_vel_{w}"] == pytest.approx(-55.0)
        assert s[f"tire_temp_l_{w}"] == pytest.approx(90.0)   # K -> C
        assert s[f"tire_temp_r_{w}"] == pytest.approx(70.0)
        assert s[f"camber_{w}"] == pytest.approx(-0.052)

def test_existing_channels_are_unchanged_by_the_setup_add():
    s = sample_from_telem(roundtrip(make_telem()), lap_dist=0.0)
    assert s["tire_temp_fl"] == pytest.approx(80.0)   # still mTemperature[1], the centre
    assert s["tire_press_rr"] == 165.0 and s["grip_fr"] == 0.95
```

- [ ] **Step 2: Run to verify fail.** `./.venv/Scripts/python.exe -m pytest tests/test_lmu_smm.py -k setup_channels -v`
- [ ] **Step 3: Implement** — add the lines to the per-wheel loop and document the units + sources in the `sample_from_telem` docstring's convention block (it is the file's contract; keep the `PENDING LIVE VALIDATION` framing and point at Task 13).
- [ ] **Step 4: Run to verify pass.** Then the full `tests/test_lmu_smm.py` and `tests/test_server_capture.py` — sample width changes ripple into the capture path.
- [ ] **Step 5: Full suite + ruff.**
- [ ] **Step 6: Commit** — `git commit -m "feat(capture): sample ride height, suspension, tread temps, patch velocity and camber"`
- [ ] **Step 7 `[RIG]`: live plausibility check** — with LMU running, capture one lap and confirm the new channels are in physical ranges (ride height ~0.02-0.15 m and dipping at compressions; `susp_defl` moving with kerbs; `lat_patch_vel` near 0 on a straight and growing with cornering; tread temps ordered sensibly vs the centre). **This is a verification step, not a blocker** — Tasks 11-12 build on the channels either way, and a wrong-scale channel shows up as an out-of-range threshold, fixable by constant.

---

### Task 11: `get_setup_diagnostics` part A — the threshold reads

**Depends on:** Task 10.

**Files:** `simcoach/analysis.py` (or a new `simcoach/analysis_setup.py` if `analysis.py` gets unwieldy — it is already ~360 lines; **recommend the new module** for the setup track), `simcoach/format.py`, `simcoach/coach_tools.py`, `simcoach/coach.py`; Test: `tests/test_analysis_setup.py` (new), `tests/test_coach_tools.py`

**Interfaces:** `setup_diagnostics(record, cmap) -> list[dict]` → findings as `{"lever": str, "finding": str, "where": str, "direction": str}`, grouped by lever in the formatter. Part A covers the four threshold reads:

1. **Bottoming** — `ride_height_{w}` minima below `_BOTTOMING_M = 0.005`, or a `tire_defl_{w}` spike above `_TIRE_DEFL_SPIKE_M = 0.025`; report the **corner** it happens in (owned data — `in_window` against the map) → *"raise ride height / stiffen the bumpstop"*.
2. **Camber from tread spread** — per wheel, map `tire_temp_l_`/`tire_temp_r_` → **inner/outer by wheel side** (left-side wheels: `l` = outer, `r` = inner; right-side: reversed) and flag a spread beyond `_TREAD_SPREAD_C = 8.0` → *"reduce/add camber on <wheel>"*. **This mapping is the deliverable of Open Question 2 and gets its own dedicated test per side.**
3. **Pressure window** — hot `tire_press_{w}` mean vs `_TARGET_PRESS_KPA` (a named per-axle constant) → *"raise/lower cold pressure"*.
4. **Brake balance** — front vs rear `brake_temp` mean → *"duct / bias / migration"*. Uses a **difference**, so it is immune to the `mBrakeTemp` unit question (Open Question 4) — note that in the comment so nobody "fixes" it later.

Tool `get_setup_diagnostics()` renders the findings grouped by lever, **prefixed with the honest framing**: these are heuristic reads on per-car thresholds, and the coach must cross-check `get_setup` for what is actually adjustable on this car/tier before naming a change. Missing channels (pre-Task-10 laps) → `"Setup channels weren't captured for this lap (older capture) — drive a lap with the current version for setup diagnostics."`

All thresholds are module-level named constants with one `ponytail:` comment: *per-car heuristics; a per-car calibration table is the upgrade path.*

- [ ] **Step 1: Write the failing tests** in a new `tests/test_analysis_setup.py`: a synthetic lap with a ride-height dip inside corner 1's window → a bottoming finding **naming T1**; a flat lap → none; an FL tread spread with the hot side on `tire_temp_l_fl` → *outer* hot for a left-side wheel, and the **mirrored** case on `rr` → *inner* hot (two separate assertions — this is the spec-error fix); an over-target pressure → a pressure finding; front brake temps 150 °C above rear → a brake-balance finding; a lap with no setup channels → `[]` and the honest tool message. Plus tool registration + `FakeModel` dispatch in `tests/test_coach_tools.py`.
- [ ] **Steps 2-4:** verify fail → implement → verify pass.
- [ ] **Step 5:** Prompt line: for setup questions, call `get_setup_diagnostics` **and** `get_setup` (legal adjustments) before proposing a change; quote the evidence; never propose an adjustment the tier doesn't allow.
- [ ] **Step 6: Full suite + ruff. Commit** — `git commit -m "feat(coach): get_setup_diagnostics - bottoming, camber, pressure and brake-balance reads"`

### Task 12: `get_setup_diagnostics` part B — damper velocity + slip-angle balance

**Depends on:** Tasks 10, 11.

**Files:** as Task 11; Test: `tests/test_analysis_setup.py`, `tests/test_coach_tools.py`

**Interfaces — two findings added to `setup_diagnostics`:**
5. **Damper velocity** — `d(susp_defl)/dt` per wheel. With no time channel, derive dt from the distance axis and speed (`dt = ds / (v/3.6)`, reusing Task 1's helper) — **this is the one place where `mElapsedTime` (Open Question 5) would materially improve accuracy**, so write it behind a small helper that an exact time channel can replace. Report a coarse histogram (low/high-speed bump/rebound as four buckets against `_DAMPER_HIGH_SPEED_MPS = 0.05`) and flag kerb packing (excess high-speed bump in a kerbed corner) → damping levers.
6. **Slip-angle balance** — per wheel `slip_deg = degrees(atan2(lat_patch_vel, max(|long_ground_vel|, ε)))`; front-axle mean vs rear-axle mean through the cornering phase → understeer/oversteer bias → *ARB / differential / aero balance / pressures*. Cross-check against Task 5's steering-vs-lat_g label and **say so when they disagree** rather than picking a winner.

- [ ] **Step 1:** Failing tests — a synthetic kerb strike (a fast `susp_defl` transient) → a high-speed-bump finding; a smooth lap → none; front slip 2° above rear → an understeer-bias finding, mirrored for oversteer; a lap without `lat_patch_vel` → those findings absent while Part A's still appear (**partial degradation**, the important case).
- [ ] **Steps 2-4:** verify fail → implement → verify pass.
- [ ] **Step 5: Full suite + ruff. Commit** — `git commit -m "feat(coach): damper-velocity histogram + slip-angle balance in setup diagnostics"`

---

### Task 13 `[RIG]`: settle the sign/semantics conventions at the rig

**Depends on:** Task 10 shipped. **Not on the critical path** — every task above is written so that a flip here is a one-constant change plus a doc comment.

Needs a live LMU session. Capture one representative lap, then check:

- [ ] **`mGripFract` direction** (Open Question 1) — on a clean straight at moderate speed, is `grip_*` ≈ 0 or ≈ 1? During a deliberate big slide, does it rise or fall? The header says it's the **sliding** fraction (→ rises). If confirmed, **`analysis.py:25,186` and `format.py:53` are documenting it backwards and `min_grip` is reporting the least-sliding wheel while calling it "lowest grip"** — fix the comments, rename to `max_slip`/invert the metric, and flip `_GRIP_HIGHER_MEANS_MORE_SLIDING`. **This is a pre-existing bug in shipped coach output, not something this issue introduces** — worth its own issue and commit.
- [ ] **`mWear` direction** (Open Question 3) — over a long stint, does `tire_wear_*` rise or fall? Set `_WEAR_IS_REMAINING` and fix `analysis.py:73` / `format.py:99` if inverted.
- [ ] **`mBrakeTemp` unit** (Open Question 4) — are reported brake temps plausible (300-800 °C under braking in a GT/LMP car) or ~273 too low? The header says Celsius while `smm.py:263` subtracts 273.15. Front-vs-rear *differences* are unaffected, so Task 11's brake-balance read stands either way.
- [ ] **New-channel plausibility** — Task 10 Step 7, if not already done.
- [ ] **`lat_patch_vel` sign** — does a left-hand corner give front wheels a consistent sign? Only the front-vs-rear *magnitude* comparison is used, so this is a refinement.
- [ ] **Commit** the resulting constant flips and comment corrections as their own change: `git commit -m "fix(capture): correct <field> sign/unit conventions confirmed at the rig"`

### Task 14: Final verification

- [ ] **Step 1:** `./.venv/Scripts/python.exe -m pytest -q` → green.
- [ ] **Step 2:** `./.venv/Scripts/python.exe -m ruff check simcoach/ tests/` → no **new** findings vs the pre-change baseline.
- [ ] **Step 3:** `git status --short simcoach/tracks/` → empty (no shipped map was rewritten).
- [ ] **Step 4:** `cd frontend && npm test` → green (should be untouched; this confirms the UI-agnostic claim).
- [ ] **Step 5:** Confirm the `TOOLS` list still matches `dispatch` — every new tool name present in both, no orphans. A one-line test asserting `{t["name"] for t in TOOLS}` is fully dispatchable is worth adding permanently.
- [ ] **Step 6 `[LIVE-LLM]`:** with a real API key, ask the coach *"where am I losing time?"*, *"why is the car understeering at T3?"* and *"what should I change on the setup?"* and confirm it **selects** the new tools unprompted and quotes their numbers. Tool *selection* is model behaviour and is not unit-testable; if it doesn't reach for them, the fix is the tool description and the prompt line, not the analysis.
- [ ] **Step 7 `[RIG]`:** a live shakedown lap confirming the new capture channels land in the `.npz` and the setup diagnostics read plausibly on real data.

---

## Open questions needing the user's decision

1. **`mGripFract` is documented as the SLIDING fraction — the codebase says the opposite.** Header: *"an approximation of what fraction of the contact patch is sliding"*; `analysis.py:25` says *"near 0 = sliding"* and `format.py:53` prints *"1.0 = full grip"*. One of these is wrong, and if the header is right the shipped `min grip` line has been telling the driver the opposite of the truth. **My recommendation:** ship Task 5 resting on steering-vs-lat_g (unambiguous), gate the grip axis behind `_GRIP_HIGHER_MEANS_MORE_SLIDING`, verify at the rig (Task 13), and open a **separate bug issue** for the existing `min_grip` output — it is a shipped-output defect, not part of #38.
2. **The spec's tread-temp mapping is wrong for one side of the car.** `mTemperature` is *left/center/right* with an explicit header warning *"not to be confused with inside/center/outside"*, so `tire_temp_in_{w}` ← `[0]` mislabels every right-side wheel. **My recommendation:** capture as `tire_temp_l_`/`tire_temp_r_` and do the side-aware inner/outer mapping in analysis where it's tested (Tasks 10, 11). **Also:** `mCamber` is right there in the struct — measured dynamic camber beats inferring camber from tread spread. Should Task 11 lead with measured camber and keep tread spread as corroboration? I'd say yes; the spec doesn't mention `mCamber` at all, so this is your call.
3. **`mWear` direction.** Header: *"0.0-1.0, fraction of maximum"* (worn); `analysis.py:73` comments *"wear is a remaining fraction (1.0 = new)"* and `format.py` prints *"Tire wear remaining"*. Affects the **sign** of Task 9's wear slope. Recommendation as above: report measured direction, wording behind a constant, settle at the rig.
4. **`mBrakeTemp` unit.** Header says Celsius; `smm.py:263` subtracts 273.15. If the header is right, every brake temp the coach has ever reported is ~273 too low. Low risk to this plan (Task 11 uses differences) but a real question for the existing `get_wheel_report`. Separate issue?
5. **Sample `mElapsedTime` / `mLapStartET` for an exact time axis?** Not in the spec. One line in `sample_from_telem` gives an exact in-lap time channel, which would make delta-t and damper velocity exact instead of integration-accurate. Existing laps would still need the `ds/v` path, so this is additive, not a replacement. **My recommendation: yes, add it in Task 10** — it's the cheapest accuracy win in the whole plan. Flagging rather than doing it because it widens Phase 0 beyond what the spec scoped.
6. **Bucketing scope for `get_time_loss`.** The spec says corner windows only; I propose corner windows **plus** inter-corner straights so the buckets sum to the net delta and straight-line losses are visible (Task 2). Confirm you want whole-lap coverage rather than corner-only.
7. **Does Slice 1 ship on its own, or is Slice 2 wanted in the same branch?** Slice 1 (Tasks 1-4) is the genuinely useful minimum and is ~4 commits. Slices 2-4 are additive and independently shippable.

## Self-review notes

- **Spec coverage:** P1.1 → Tasks 1-3 (+4b); P1.2 → Task 4; P1.3 → Task 5; P1.4 → Task 6; P1.5 → Task 8; P1.6 → Task 9; P1.7 → Task 7; Phase 0 → Task 10; Phase 2 → Tasks 11-12. Testing section folded per task; invariants → Global Constraints.
- **Deliberately deferred (not tasked):** the two-lap trace-overlay UI (research §4C — the `.npz` substrate stays ready); weather/track-state-aware reference selection (belongs to `[[race-context-gaps]]`); a per-car calibration table for the setup thresholds; a fitted G-G envelope across laps; per-car power bands for true gear/short-shift checks; promoting the per-corner metrics cache to SQLite (v12) unless Task 8 proves slow.
- **Name consistency:** `lap_time_axis` / `align_time_axes` / `time_loss_buckets` / `handling_read` / `gg_envelope` / `consistency` / `tire_trend` / `setup_diagnostics` used identically throughout; tool names `get_time_loss` / `get_handling` / `get_consistency` / `get_tire_trend` / `get_setup_diagnostics` match their `TOOLS` specs and `dispatch` keys.
- **Offline completability:** Tasks 1-12 and Task 14 Steps 1-5 need no sim and no API key. Only Task 13, Task 10 Step 7 and Task 14 Steps 6-7 are gated, and none of them blocks a merge of the analysis work.
- **Formatter-conflict note:** Tasks 4, 6 and 7 all append to `format_corner_line` / `format_lap_facts`, and several existing suites assert on that text. Land them sequentially, not in parallel, and expect to update substring assertions in `tests/test_format.py`, `tests/test_coach_tools.py`, `tests/test_coach.py`.
