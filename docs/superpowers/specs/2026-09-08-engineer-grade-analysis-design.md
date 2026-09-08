# Engineer-grade lap & setup analysis — design (2026-09-08)

Status: spec only. To be built **after** the longitudinal driver-profile work
(`worktree-feat+driver-profile`). No branch yet.
Source: `docs/research/2026-09-08-race-engineer-lap-analysis-gaps.md` (the research spike
this implements). Read that first for the "why."

## Problem

Nestor captures nearly every channel a MoTeC log has, but the coach only sees a per-corner
speed/brake/trail summary. Real race engineers work **delta-t first** (where is time lost),
then diagnose in the traces, and read a distinct set of **car-state channels to drive setup
changes**. Nestor surfaces none of: time-loss-by-mini-sector, coasting, an understeer/
oversteer read, traction-circle use, consistency, tire-deg trend, or any setup diagnostic —
even though the data (or the struct field) already exists.

## Approach

Every item below is the same shape and preserves the two-stage invariant: a **pure,
deterministic analysis function** (tested, no LLM) turns already-captured channels into a
**fact string**, exposed as **one new agentic coach tool** (added to `TOOLS` in
`simcoach/coach_tools.py`, dispatched via `CoachContext`). Corners stay owned data; raw
50 Hz never reaches the model. Most features need **no new capture**; the setup track needs
a cheap `sample()` extension (Phase 0) because the fields are already in the struct.

Analysis code lives in `simcoach/analysis.py` (or a new `simcoach/analysis_extra.py` if it
grows), formatters in `simcoach/format.py`, tools + dispatch in `simcoach/coach_tools.py`.

**Graceful degradation:** new channels only exist on laps captured after the update; every
tool returns a clear "not captured for this lap (older capture)" line when a channel is
missing — same pattern as the existing `get_wheel_report` fallback.

---

## Phase 1 — Driving analysis (no new capture; data already sampled)

### 1. Delta-t by mini-sector — `get_time_loss`  *(highest value)*
The headline "where am I losing time." Deterministic:
- Reference = `store.best_lap(track, car, session_id=…)` same phase (fall back all-time).
- Both laps are distance-keyed; compute elapsed time along distance by integrating
  `dt = ds / max(v, ε)` over the `speed`/`lap_distance` channels → `t(d)` per lap.
- `delta(d) = t_cur(d) − t_ref(d)`; bucket by **corner window** (reuse the corner map's
  start/end per corner — no separate fixed-grid mini-sectors; ponytail).
- Output: total gain/loss + a ranked list of the biggest-loss buckets, e.g.
  `"T4 braking −0.31s, T7 exit −0.18s, T1 entry −0.09s; net −0.6s vs best."`
- Tool: `get_time_loss(reference="best"|lap-ref)`. Input schema mirrors `which`.
- Tests: two synthetic laps with a known injected loss in one window → assert the bucket
  sums and ranking. Ceiling: trapezoidal integration on ~50 Hz is fine; note it.

### 2. Coasting / dead-time — extend `get_corner` + `get_lap_summary`
Per corner and whole lap: fraction/time/distance with `throttle < 0.05 AND brake < 0.05`,
plus the **brake→throttle transition gap** (distance from last-brake to first-throttle).
Reuse the existing formatters (don't add a tool — fold into the corner/lap facts).
- Data: `throttle`, `brake`, `speed`, `lap_distance`.
- Output line: `"coasting 0.4s (18 m) between brake release and throttle."`
- Tests: synthetic corner with a coasting plateau.

### 3. Handling read (understeer / oversteer) — `get_handling`
Per corner, a heuristic balance read:
- **Understeer** signal: `|steering|` still climbing while `lat_g` has plateaued near the
  car's observed max, and **front `grip_fraction` saturated above rear**.
- **Oversteer** signal: corrective/reducing steering (or sign reversal = countersteer) with
  **rear grip below front**; `wheel_rot` collapse = lockup, spike = wheelspin.
- Output: `"T3: entry understeer — front grip 0.98 vs rear 0.90, steering still winding at
  min speed."`
- Data: `steering`, `lat_g`, `grip_{fl,fr,rl,rr}`, `wheel_rot_*`; sharper once Phase 0 adds
  `lat_patch_vel` (front-vs-rear slip proxy).
- Tests: synthetic understeer trace vs oversteer trace → assert the label.
- Ceiling: a heuristic index, **not** a vehicle model — mark it (`ponytail:` comment) and
  leave a threshold constant for tuning.

### 4. Traction-circle (G-G) utilization — extend `get_lap_summary` (or `get_gg`)
Per corner/lap: estimate the grip envelope radius from the car's observed peak combined g,
then report **% of envelope used** and the **"dead middle"** (low combined g during the
cornering phase = coasting / inputs not combined = weak trail-brake blend).
- Data: `lat_g`, `long_g`.
- Ceiling: envelope radius = naive per-car observed-max (a running max over recent laps);
  refine with a fitted circle later. Leave the estimate behind one function.

### 5. Consistency — `get_consistency`
Across the stint (session + phase): stddev of lap time and of per-corner min speed / brake
point; flag the highest-variance corners. `"Lap time σ 0.4s over 8 laps; T4 min speed swings
±6 km/h — least consistent."`
- Data: `store.session_laps` + per-corner analysis (cache reuse).
- Tests: synthetic set with one noisy corner.

### 6. Tire-deg trend — `get_tire_trend` (or extend `get_wheel_report`)
Across the stint: per-lap slope of tire temp / pressure / wear and lap-time drift.
`"RL wear +2.1%/lap, pace +0.15s/lap over 8 laps → deg-limited; RL running hottest."`
Feeds the longitudinal profile ([[deep-dive-and-longitudinal-profile]]).

### 7. Lower — fold into `get_corner` / `get_lap_summary`
- **Gear/RPM** sanity: wrong gear for a corner, short-shift, bog on exit (rpm below power
  band at throttle-on). Data: `gear`, `rpm`, `throttle`.
- **Lockup flags**: `wheel_rot ≈ 0` while `speed > 0` under braking.

---

## Phase 0 — Setup-channel capture add  *(prerequisite for Phase 2)*

The fields are **already present** in `simcoach/lmu/rf2structs.py` `TelemWheelV01`; the gap
is that `simcoach/lmu/smm.py` `sample()` doesn't read them. Add per-wheel keys (non-breaking
— keep `tire_temp_{w}` = center):
- `ride_height_{w}` ← `mRideHeight`
- `susp_defl_{w}` ← `mSuspensionDeflection`  (damper velocity = diff over time)
- `susp_force_{w}` ← `mSuspForce`
- `tire_defl_{w}` ← `mVerticalTireDeflection`
- `lat_patch_vel_{w}` ← `mLateralPatchVel`  (slip-angle proxy)
- `tire_temp_in_{w}` ← `mTemperature[0]`, `tire_temp_out_{w}` ← `mTemperature[2]`

Storage: these ride in the per-lap `.npz` (existing mechanism); **no SQLite schema change**.
Confirm `store.load`/`save` pass channels through generically (they should — channels are a
dict). Tests: a capture unit test asserting `sample()` emits the new keys in plausible
ranges from a fixture telem struct.

---

## Phase 2 — Setup diagnostics — `get_setup_diagnostics`  *(needs Phase 0)*

One tool that runs the deterministic reads and **names the lever**, cross-referencing the
current setup values (`get_setup` already exposes them + the legal-adjustment list per tier,
so advice stays legal):

- **Bottoming**: `ride_height_{w}` / `tire_defl_{w}` minima below a threshold, or hard
  vertical spikes → `"bottoming at T7 compression (FL ride height → 2 mm) — raise ride
  height or stiffen the bumpstop."`
- **Damper-velocity histogram**: difference `susp_defl` over time → velocity distribution
  per corner (bump/rebound, low/high speed); flag kerb packing (excess high-speed bump).
  → damping levers.
- **Camber from tread spread**: `tire_temp_in_{w}` vs `tire_temp_out_{w}` per wheel →
  `"FL inner 14 °C hotter than outer → reduce front-left camber."`
- **Slip-angle balance**: front vs rear `lat_patch_vel` → understeer/oversteer bias → ARB /
  differential / aero balance / pressures.
- **Pressure window**: hot `tire_press_{w}` vs target → cold-pressure change.
- **Brake balance**: front vs rear `brake_temp` → duct / bias / migration.

Output groups findings by lever with a direction. Ceiling: thresholds/targets are per-car
heuristics — leave them as named constants with a `ponytail:` note (a per-car calibration
table is the upgrade path). This is a driving/setup *coach*, not a full setup optimizer.

---

## Invariants preserved
Two-stage (tools return computed facts, never raw telemetry); corners owned (identity from
the map); swappable model (tools sit behind `Model.run`'s dispatch, unchanged); storage
split (new channels in `.npz`, SQLite untouched).

## Testing
Each deterministic function gets a unit test with **synthetic channels + a golden assertion**
(the smallest trace that fails if the math breaks). Each new tool is added to `TOOLS` and
exercised end-to-end by a **fake-model scripted tool call** (no network), matching the
existing `coach_tools` test pattern. Older-capture graceful-degradation paths get a test too.

## Suggested build order
1. **P1.1 `get_time_loss`** (highest value, no capture change) →
2. **P1.2 coasting** + **P1.3 handling** (driving core) →
3. **P0 capture add** (unblocks setup) →
4. **P2 `get_setup_diagnostics`** →
5. **P1.4–1.7** (G-G, consistency, tire-deg, gear/lockup) as polish.

Each is independently shippable — a deterministic fn + one tool + tests — so it parallelizes
cleanly across agents if wanted. Relates to [[north-star-auto-context]],
[[coaching-output-redesign]], [[race-context-gaps]].
