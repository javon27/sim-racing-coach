# Research spike: how real race engineers analyze laps — and Nestor's gaps

**Date:** 2026-09-08
**Purpose:** Establish how professional race engineers (and the serious sim-racing
data crowd) analyze laps and what data they lean on, then map that against what Nestor
captures and surfaces today, and rank the gaps worth closing.

**TL;DR:** Nestor already captures nearly every channel a MoTeC log has, and its
two-stage pipeline is the right shape. The gaps are almost all in *analysis surfaced to
the coach*, not in data capture — and most are cheap deterministic features because the
raw channels are already in the per-lap `.npz`. The single highest-value missing piece
is a **where-am-I-losing-time (delta-t by mini-sector)** read, followed by **coasting /
dead-time**, a **handling read (understeer/oversteer)**, and **traction-circle
utilization**. On the **setup** side (a first-class goal, not just driving), the wins are
**bottoming/ride-height**, **damper-velocity histograms**, and **camber-from-tread-spread**
— some of which need new capture from the shared-memory struct (§4B).

---

## 1. How professionals do it — the method

The professional loop is remarkably consistent across MoTeC i2, Cosworth Pi Toolbox, AiM,
Autosport Labs Podium, and the sim tooling built on them (Coach Dave Delta, etc.):

1. **Pick a reference lap** — the driver's best, or "a sensible step ahead," on the
   *same car, same track, comparable conditions*. Honest comparisons matter more than
   chasing an untouchable lap.
2. **Overlay the current lap on the reference, plotted against DISTANCE** (not time) so
   the two laps align spatially corner-for-corner.
3. **Read the time-delta (delta-t) channel first** — a running "gained/lost time vs the
   reference" trace that shows *exactly where* on track time is won or lost.
4. **Diagnose the "why" in the underlying traces** at each loss point — speed, throttle,
   brake, steering, g's — and reconcile with the driver's qualitative balance feedback.
5. **Quantify with channel/section reports** — min/max/avg/**stddev** per track section,
   and section (mini-sector) times with gained/lost deltas.

Debrief = data (quantitative) + driver feel (qualitative), reconciled. The engineer
works *distance-first, delta-t-first, then traces*.

## 2. The data they look at

**Primary traces (vs distance):** speed, throttle, brake pressure, **steering angle**,
**delta-t / time variance**, gear, RPM, lateral g, longitudinal g.

**Derived / analysis views that do the real work:**

- **Delta-t by mini-sector** — the headline "where am I losing time," corner by corner.
- **Coasting / dead-time** — distance with neither throttle nor brake. Pros want a sharp
  brake→throttle transition; a gentle roll-off before hard braking = wasted time.
- **Braking analysis** — brake point, peak pressure, and whether the deceleration slopes
  are *parallel* across braking zones (consistency); trail-brake release shape.
- **Minimum / apex speed** per corner, and the **throttle application point** on exit
  (how early and how progressively).
- **Understeer / oversteer** — inferred from **steering angle vs lateral g / speed**
  (and true slip angle where available): steering winding on while lateral g plateaus =
  understeer; corrective/countersteer inputs = oversteer.
- **Friction circle / G-G diagram** (lat g vs long g) — how much of the tire's grip
  envelope is actually used, and how cleanly braking blends into cornering (trail-brake
  quality). Dead space in the middle of the G-G = coasting / not combining inputs.
- **Consistency** — lap-to-lap variance / stddev of a channel across a stint.
- **Tire thermal & degradation** — temps, pressures, wear *trend across a stint*, and
  correlating balance drift to deg.
- **Setup correlation** — tie a balance complaint to setup values and channel evidence.

## 3. What Nestor does today

**Captured** (~50 Hz, keyed on lap-distance, stored per lap in `.npz`):
speed, throttle, brake, **steering**, **lat_g**, **long_g**, gear, rpm, ABS-active,
TC-active, fuel, virtual-energy, SoC, battery; per wheel [fl,fr,rl,rr]: tire temp,
pressure, load, wear, **grip fraction**, **wheel rotation** (lockup substrate), brake
temp, brake pressure; plus world position (x/y/z) for the track map.

**Surfaced to the coach** (deterministic per-corner summary + 11 agentic tools):
- Per corner: **entry / apex / exit speed**, min speed, **brake point (distance)**,
  **trail-brake overlap**, apex-relative brake/throttle %, peak g, **peak steering**,
  per-apex min speeds (multi-apex), and **delta vs your best**.
- Tire temps + warm-up trend; brake temps / hot pressures / wear; ABS & TC flags.
- Fuel / virtual-energy / stint-remaining.
- Current setup (+ legal adjustments this tier); race result / incidents / penalties;
  authored track notes + reference times; the authoritative corner list.
- **Agentic**: the model pulls exactly the tool a question needs; raw 50 Hz never
  reaches it.

**Architecture note:** the two-stage invariant (deterministic *measure* → LLM *coaches
on facts*) is exactly how the pros separate data from interpretation. And the raw
full-rate channels are already stored keyed on lap-distance — **the substrate a delta-t
/ trace overlay needs already exists**; it just isn't computed into facts yet.

## 4. Gap analysis

### A. Captured but not surfaced — cheap deterministic wins (data already in the `.npz`)

| Gap | Pro value | Notes / data on hand |
|---|---|---|
| **Delta-t by mini-sector** ("where you lose time") | **Highest** | We give per-corner Δ vs best min-speed, but no continuous time-loss ranking ("−0.3 s in T4 braking"). Distance-keyed channels + a reference lap = everything needed. |
| **Coasting / dead-time** | **High** | throttle & brake both ≈0; not measured. Classic, immediately actionable. |
| **Understeer / oversteer read** | **High** | steering + lat_g + speed captured (only `peak_steer` surfaced today); per-wheel `grip_fraction` + `wheel_rot` add front/rear saturation & lockups. Turns "it won't rotate" into evidence. |
| **Traction-circle (G-G) utilization** | **Med-High** | lat_g/long_g captured; no metric for envelope used or trail-brake blend quality. |
| **Consistency / variance across laps** | **Med** | all laps stored; no lap-to-lap stddev ("your T4 min speed swings ±6 km/h"). |
| **Tire-deg trend across a stint** | **Med** | per-lap temps/press/wear captured & surfaced per-lap, but not as a degradation slope over a stint. |
| **Braking consistency** (parallel decel across zones) | **Med** | long_g captured; not compared zone-to-zone. |
| **Gear / RPM usage** | **Med** | captured, unused — wrong gear, short-shift, bogging on exit. |
| **Lockup detection** | **Med** | `wheel_rot` ≈0 while moving is captured; not flagged. |
| **Throttle-application smoothness / on-throttle point** | **Low-Med** | partially in apex-relative throttle; could be a dedicated exit metric. |

### B. Setup-oriented car data — to inform setup adjustments

This is a first-class direction, not a footnote: Nestor is meant to answer *setup* as
well as driving questions. Race engineers read a distinct set of car-state channels to
decide setup changes, and map each to a specific lever. The value is turning a vague
"the car feels unstable" into "the rear dampers are packing on the kerb at T7 — soften
bump" or "you're bottoming through the compression — raise ride height / stiffen the
bumpstop."

**Data → what it diagnoses → setup lever:**

| Car data | Diagnoses | Setup lever |
|---|---|---|
| **Ride height / bottoming out** (plank & skid wear, hard vertical spikes) | Floor stalling, unpredictable aero, sparks/plank penalty | Raise ride height, stiffen springs/bumpstops, adjust rake |
| **Suspension travel & damper velocity** (damper histograms) | Spring/damper mismatch, kerb packing, poor compliance | Spring rate, bump/rebound damping, bumpstop range |
| **Slip angle, front vs rear** | Handling balance (understeer/oversteer), tire operating range | Anti-roll bars, differential, aero balance, pressures |
| **Vertical tire load per wheel / load transfer** | Roll stiffness distribution, mechanical balance | ARB split, spring split, ride height/rake |
| **Tire temp spread across the tread (inner/mid/outer)** | Camber correctness; front↔rear delta = balance | Camber; pressures; balance devices |
| **Hot tire pressures vs target** | Pressures off optimum window | Cold pressure start |
| **Brake temps front/rear** (+ bias behaviour) | Over/under-worked axle, lockup tendency | Brake ducts, brake bias, migration |
| **Rake / front-vs-rear ride height** | Aero balance, platform | Ride-height split, wing |

**Availability — CONFIRMED against the struct (`simcoach/lmu/rf2structs.py`,
`TelemWheelV01`).** The good news: nearly all of it is already exposed by LMU/rF2 shared
memory — the gap is that `smm.py`'s per-sample dict simply doesn't read it yet. Capture
is a *cheap extension of the `sample()` dict*, not a research risk.

- *Already captured & sampled today* (just need setup-oriented analysis): per-wheel
  **tire load**, **tire pressure** (hot), **tire temp** (center tread only,
  `mTemperature[1]`), **brake temp**, **grip fraction**, plus `lat_g`/`long_g`/steering.
- *Exposed by the struct but NOT yet sampled* (add fields to `sample()` — low effort):
  **`mRideHeight`**, **`mSuspensionDeflection`** (damper travel → velocity by
  differencing), **`mSuspForce`**, **`mVerticalTireDeflection`**, **`mLateralPatchVel`**
  (per-wheel lateral contact-patch velocity → a **slip-angle** proxy), and the **inner /
  outer tread temps** (`mTemperature[0]` / `[2]`, for camber). All present per wheel.
- *Likely unavailable*: explicit tire **slip ratio** and rich aero channels — derive or
  do without.

**Note on the invariant:** setup analysis fits the two-stage pipeline unchanged —
deterministic feature extraction on these channels produces setup-facing facts (e.g.
"bottoming detected at 3 points; RL tread 12 °C hotter inside → add front-left camber"),
and the model coaches on those facts alongside the current setup values it can already
read. Bottoming detection, a damper-velocity histogram, and a camber-from-tread-spread
metric are each a bounded deterministic feature + a coach tool.

### C. Methodology / product gaps

- **No two-lap trace overlay / visual.** The pro workflow is visual (overlaid traces +
  delta-t). Nestor is conversational. A **delta-t-by-mini-sector *summary*** (bucket A)
  delivers most of the coaching value without a chart; a real overlay UI stays the
  bigger, deferred play (the `.npz` substrate is ready for it — see the storage
  invariant).
- **Reference selection.** Pros insist on comparable *conditions*; Nestor compares to
  session/all-time best but doesn't yet factor weather/track state — ties to the open
  race-context work ([[race-context-gaps]]).
- **Corner-phase handling read.** Entry/apex/exit *speeds* exist, but not per-phase
  *input/handling* (entry understeer vs exit oversteer). A natural extension of the
  handling read above.
- **Qualitative ↔ data reconciliation.** The coach is conversational and *could* ask for
  balance feel and reconcile it with channels; there's no structured prompt encouraging
  that debrief pattern.

## 5. Recommendations (prioritized)

Each is a **new deterministic feature + one coach tool** — it fits the two-stage
architecture cleanly (the LLM stays a consumer of computed facts, corners stay owned
data):

1. **Delta-t by mini-sector** → `get_time_loss` / mini-sector tool. Highest pro-value,
   data in hand. Directly answers "where am I losing time" with numbers.
2. **Coasting + brake→throttle transition** per corner/lap.
3. **Handling read (understeer/oversteer)** from steering + lat_g + per-wheel grip;
   extend to entry/apex/exit phases.
4. **Traction-circle utilization** summary.
5. **Consistency** (lap-to-lap variance) and **tire-deg trend** over a stint — also feeds
   the longitudinal driver profile ([[deep-dive-and-longitudinal-profile]]).
6. Lower: gear/RPM checks, lockup flags.

**Setup-analysis track (parallel, §4B — a first-class goal):**

7. **From data already captured:** camber read from front↔rear/tire-temp spread, hot-
   pressure-vs-window, per-axle brake-temp balance, and a steering+lat_g balance read →
   a `get_setup_diagnostics` tool that names the lever (ARB, pressures, camber, bias).
8. **Sample the setup channels the struct already exposes** — add `mRideHeight`,
   `mSuspensionDeflection`, `mSuspForce`, `mVerticalTireDeflection`, `mLateralPatchVel`,
   and inner/outer tread temps (`mTemperature[0]`/`[2]`) to `smm.py`'s `sample()`
   (confirmed present in `rf2structs.py`; low-effort capture add). Then build the
   highest-signal setup features: **bottoming detection** (ride height / vertical
   deflection), a **damper-velocity histogram**, **slip-angle balance** (from
   `mLateralPatchVel`), and true **camber-from-tread-spread**.

This set moves Nestor from "reads your telemetry" to "analyzes it the way an engineer
does," and every item is a concrete, testable deterministic step — the opposite of the
"vibe-coded AI" critique. Relates to [[north-star-auto-context]],
[[coaching-output-redesign]], and [[pit-wall-voice-vision]].

## Sources

- MoTeC i2 lap comparison & delta / section reports — [MoTeC i2](https://www.motec.com.au/products/I2?catId=4);
  [Coach Dave: MoTeC in iRacing](https://coachdaveacademy.com/tutorials/how-to-use-motec-data-in-iracing/);
  [SimRacerCentral MoTeC guide](https://simracercentral.com/motec-sim-racing-telemetry-guide/);
  [Trinacria beginner's guide](https://trinacriasimracing.wordpress.com/beginners-guide-to-telemetry-analysis-motec/)
- Braking, coasting, minimum speed, G-G in debriefs —
  [Autosport Labs: Podium Driver Data Analysis](https://wiki.autosportlabs.com/Podium_Driver_Data_Analysis);
  [Blayze braking masterclass](https://blayze.io/blog/car-racing/braking-masterclass-for-racecar-drivers);
  [Trailbrake: the G-G diagram](https://trailbrake.com/data-analysis-tips/gg-diagram/);
  [VR Performance: traction circle explained](https://vrperfdev.wordpress.com/2016/01/01/traction-circle-g-g-diagram-explained/)
- Understeer / oversteer / slip angle —
  [trophi.ai: slip angle & why your car won't turn](https://www.trophi.ai/post/why-your-car-wont-turn);
  [Wikipedia: understeer and oversteer](https://en.wikipedia.org/wiki/Understeer_and_oversteer)
