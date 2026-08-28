# LMU setup guide — general methodology + cause→effect

Freeform reference knowledge, not telemetry. Every claim below is tagged with
a source id from `sources.md` and a confidence note. Two tags recur:
**[broadly agreed]** = corroborated across ≥2 independent sources or matches
general sim-racing convention; **[single-source]** = one creator's stated
opinion/number, not independently confirmed — verify against the user's own
setup sheets/logs before treating as fact. Numeric values (percentages, mm,
°C) are as stated by the cited source, not validated against current LMU
patches/BoP.

## Recommended order of setup changes

Two sources give an explicit sequence; they agree on the shape (mechanical
grip → aero/rotation → fine electronics) even though S8 is LMP3-specific and
S9 is general/Hypercar-leaning.

1. Fuel load + tire pressures — establish the performance baseline (S8, S9)
2. Brake bias + max pedal force — calibrate braking feel/balance (S8)
3. Brake ducting — lock in tire/brake temperature window (S8)
4. Springs — address the primary understeer/oversteer bias (S8)
5. Ride height / rake — balance the aero platform (S8, S9)
6. Packers — kill local bottoming without touching ride height (S8)
7. Dampers (bump/rebound) — fine-tune transient response per circuit (S8, S9)
8. Toe + ARBs — refine turn-in response and rotational balance (S8)
9. Differential preload/lock — last-stage entry rotation tuning (S8, S9)
10. Wing — final aero adjustment, coordinated with ride height (S8, S9)

Cross-cutting methodology, **[broadly agreed]**:
- Change one parameter at a time, then run several consistent laps before
  reading the data (S9).
- Test long runs, not just qualifying pace — "setups usually fall apart once
  the tyres begin to fade"; a race setup should be "what you can repeat for
  100 consecutive laps, not what you can do once on fresh tyres" (S9).
- Don't copy a setup blindly across track variants — track/kerb-profile
  changes can "completely alter what the car needs" (S9).
- Sprint vs. endurance philosophy differs: sprint favors lower ride heights
  and sharper rotation "a bit on edge"; endurance favors a calmer, more
  repeatable balance and slightly more rear stability over outright rotation
  (S9).

## Cause→effect by lever

### Tire pressures
- LMU generally rewards running pressures near their lowest viable point —
  predictable grip, better track-surface settling. **[single-source]** (S9)
- LMP3 baseline: start at minimum pressure, target an 85–95°C operating
  window; adjust brake ducting (more open = cooler) to hold it there.
  **[single-source, car-class-specific]** (S8)
- Tire carcass vs. surface temperature modeling differs by class: Hypercars
  are sharper/more temperature-sensitive, LMGT3s more tolerant.
  **[single-source]** (S9)

### Anti-roll bars (ARB)
- Stiffer front ARB → more understeer. Stiffer rear ARB → more rotation but
  less predictable over kerbs. **[broadly agreed shape]** (S9, matches S8's
  per-axis breakdown below)
- LMP3 specifics: soft front ARB = responsive/reactive front but more
  instability; stiff front ARB = more stable but induces understeer. Soft
  rear ARB = planted/stable rear especially on throttle; stiff rear ARB =
  more on-throttle rotation but potential instability. **[single-source,
  LMP3-specific]** (S8)
- Endurance racing tends toward slightly softer bars overall for a
  friendlier, more drivable balance. **[single-source]** (S9)

### Springs
- Softer front springs → reduce understeer. Stiffer rear springs → more
  on-throttle rotation. Too stiff overall → nervous/skittish car. Too soft →
  wallowing. **[broadly agreed shape]** (S8, S9)
- GT3s tolerate/benefit from softer, more compliant springs (absorb kerbs,
  carry mechanical grip); Hypercars need a stiffer, stable aero platform —
  too soft can stall the floor and lose downforce. **[single-source]** (S9)

### Ride height / rake
- Lower rear ride height relative to front (less rake) → sharper/pointier
  front end, more oversteer risk. Raise rear relative to front (more rake) →
  more front-end grip at turn-in. **[single-source, LMP3-specific]** (S8)
- Hypercar ride height is a tight window: too low bottoms out in high-speed
  zones and unsettles the car; too high loses significant downforce.
  **[single-source]** (S9)
- LMU-specific quirk: in-game ride height figures are set **static, without
  fuel** — actual dynamic ride height drops as fuel burns off during a
  stint, so what you set at the low-fuel end of a run rides lower on full
  tanks. **[single-source]** (S8)
- Packers control local bottoming (e.g. one specific kerb/compression) —
  use them instead of raising ride height globally, to avoid losing aero
  everywhere else. **[single-source]** (S8)

### Dampers (bump / rebound)
- Bump controls how suspension handles kerbs/compression and weight
  transfer; rebound controls how it returns to position. Too stiff → nervous/
  skittish; too soft → wallowing. **[broadly agreed shape, general-sim
  convention]** (S8, S9)
- No universal per-track numbers given by either source — both treat this as
  iterative, circuit-by-circuit tuning based on kerb behavior.

### Differential (preload / power / coast lock)
- More preload/on-power lock → more stable off-corner traction, harder to
  rotate the car. Less lock → better rotation but livelier/more reactive,
  especially on worn tires. **[broadly agreed shape]** (S9)
- LMP3: excessive rotation on turn-in → reduce diff preload; understeer on
  entry → increase diff lock. **[single-source, LMP3-specific]** (S8)
- Class sensitivity: GT3 diffs feel "friendlier"/more forgiving; Hypercars,
  especially with hybrid systems, can get very lively if the diff is run too
  open. **[single-source]** (S9)

### Aero / wing
- More front aero (Hypercar) → crisper turn-in, less rear stability. More
  rear aero → safer/more stable but harder to rotate mid-corner.
  **[single-source]** (S9)
- LMGT3 is far less aero-dependent than Hypercar and generally more
  forgiving to aero changes. **[single-source]** (S9)
- Wing angle and ride-height rake interact — coordinate them rather than
  tuning wing in isolation (raising wing adds downforce but adds drag; a
  car can trade wing angle for a lower/more stable rear-ride-height platform
  instead). **[single-source]** (S8)

### Toe (front / rear)
LMP3-specific, no corroboration found elsewhere — **[single-source]** (S8):
- More front toe-in → better high-speed stability, more tire scrub/heat.
  Toe-out → more responsive turn-in, less scrub, less stable.
- Less rear toe-in → better entry rotation/speed, more rear instability.
  More rear toe-in → more rear stabilization on entry.

### Brake bias
- General shape: bias too far forward → entry understeer. Bias too far
  rearward → rear lockups, worse late-stint as rears lose grip. Target the
  lowest (most rearward) bias the driver can control. **[broadly agreed]**
  (S9)
- One specific number: LMU's *default* brake bias tends to run too far
  forward; move it rearward into the 54–56% (front) range to engage all
  four tires, then trim forward when tires are cold / rearward once warm.
  **[single-source — treat as a starting point, not a target]** (S10)
- Hypercar-specific complication: "brake migration" — bias shifts
  dynamically with brake pressure — is a distinct system layered on top of
  static brake bias. **[single-source, flagged for deeper research]** (S9;
  a dedicated brake-migration explainer exists at S12 but was not mined —
  see sources.md)
- LMP3 max pedal-force calibration differs by car and input device: use
  default for controllers; for pedal users, Ligier ~85–91%, Ginetta
  ~80–85%. **[single-source, LMP3-specific, car-model-specific]** (S8)
- Class difference: GT3s can run more rearward bias because of their ABS
  behavior; Hypercars (no ABS) need slightly safer/more forward values.
  **[single-source]** (S9)

### Brake ducts
- More open ducting → cooler tires/brakes; use to hold the 85–95°C tire
  window once bias and pressures are set. **[single-source, LMP3-specific]**
  (S8)

### Traction control (TC)
- Higher TC → safer but slower; lower TC → faster but punishes mistakes.
  This is the primary electronic aid GT3s have to lean on (no hybrid
  deployment layer). **[broadly agreed, general-sim convention]** (S9)
- See the existing Ferrari 296 GT3 car note
  (`simcoach/knowledge/ferrari_296_gt3.md`) for LMU's specific TC axes
  (TC level, TC power cut, TC slip angle) — not re-derived here to avoid
  duplication.

### ABS
- **[LMU-specific, needs confirmation]** LMGT3 is reported as the only LMU
  class with native ABS (per the existing Ferrari 296 car note, sourced
  separately from OverTake.gg — not re-verified in this pass). Hypercars and
  the prototype classes (LMP2/LMP3) do not have it, which is why brake bias
  and pedal-force calibration matter more for those classes (S8, S9).

### Engine braking / hybrid deployment (Hypercar only)
- Hypercar-class-only lever: hybrid deployment maps and regen settings let a
  driver strategically conserve or deploy energy per stint/corner — a layer
  GT3 and prototype classes don't have. **[single-source, not detailed
  further]** (S9). See `lmu-classes.md` for the class-level energy note.

## Common mistakes (broadly agreed, S9)
- Chasing single-lap qualifying pace when setting up for a race stint.
- Changing more than one parameter between test runs.
- Copying a setup across track variants without retesting.

## Gaps / not covered by this pass
- No numeric spring-rate, ARB, or damper-click starting points for any car —
  neither text guide gives them, and the brief prohibits inventing specific
  numeric values. Those come from the user's own setup sheets.
- The Reddit thread (S1) and 5 of 6 YouTube videos (S3–S7) could not be
  text-mined — see `sources.md` for what's known about each (title/creator
  only) and why.
- Brake migration mechanics (Hypercar-specific) are flagged but not detailed
  — S12 (OverTake.gg brake migration guide, found via search, not fetched)
  is a good next source to mine.
