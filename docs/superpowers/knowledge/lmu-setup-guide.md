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

A third, much shorter sequence for a driver making a *default/fixed-feeling*
setup driveable rather than doing a full ground-up build, **[single-source,
worked example]** (S4): brakes (bias + migration + max pedal force) → TC
(TC, TC cut, TC slip) → ARB (front, then rear) — each stage tuned to its
limit with the one-click-at-a-time/test-lap loop below before moving to the
next, since brake and throttle confidence have to exist before ARB rotation
changes are meaningful to judge. Not a contradiction of the 10-step order
above — it's a compressed subset (electronics + ARB only) aimed at getting
most of the time gain with the fewest, safest changes.

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
- Hypercar per-axis mechanics, corroborated by two independent creators
  **[broadly agreed shape, Hypercar-specific]** (S3, S5): softer front ARB →
  more turn-in rotation but less stability entering a corner and slower
  direction changes (worse in fast direction-change sections like an S);
  stiffer front ARB → more stable/quicker-reacting entry but more understeer.
  Softer rear ARB → more planted/stable exit but less rotation; stiffer rear
  ARB → more exit rotation but the rear can swing out and become unstable in
  fast corners.
- Recommended tuning process, **[single-source, worked example]** (S5): keep
  the *other* axis fixed, change one ARB one click at a time, drive several
  laps, and stop at the first click where instability (not just discomfort)
  appears. Worked example (BMW, Monza, default rear ARB): lowering front ARB
  from 5→2 cut ~0.2s (more rotation) but introduced a near-loss of the rear
  through a fast section; raising rear ARB 2→3 recovered rear stability
  *and* gained another ~0.2s (a more stable rear allowed a tighter,
  better-exit line) — stacking to ~0.5s total over the default.
- One creator's stated general preference **[single-source]** (S3, S4): keep
  rear ARB low/soft as a stability baseline and do most of the rotation
  tuning via the front ARB — both explicitly note diminishing/negative
  returns from raising rear ARB further once the car already rotates enough.

### Springs
- Softer front springs → reduce understeer. Stiffer rear springs → more
  on-throttle rotation. Too stiff overall → nervous/skittish car. Too soft →
  wallowing. **[broadly agreed shape]** (S8, S9)
- GT3s tolerate/benefit from softer, more compliant springs (absorb kerbs,
  carry mechanical grip); Hypercars need a stiffer, stable aero platform —
  too soft can stall the floor and lose downforce. **[single-source]** (S9)
- Worked Hypercar extreme (front spring max / rear spring min) → strong
  understeer, more stable, less pitch-sensitive; the opposite (softer front,
  stiffer rear) → easier rotation but noticeably more pitch-sensitive, and
  car-to-car sensitivity to this swing varies a lot. **[single-source,
  Hypercar-specific]** (S3) One named exception in the same source: the
  Mercedes AMG reportedly wants the opposite pairing (stiff rear + soft
  front) to balance — flagged as one creator's specific-car claim, not
  generalized.
- Spring rate, ride height, and wing angle are one coupled system, not three
  independent levers — a low-wing setup wants a lower rear ride height and
  softer rear spring to compensate for the lost stability; a high-wing setup
  can run a stiffer, more aggressive spring rate. **[single-source,
  Hypercar-specific]** (S3)

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
- Corroborating detail from a second, Hypercar-specific source **[single-
  source, Hypercar-specific]** (S3): higher (front) packer values push
  toward more understeer, same as raising ride height; that source's own
  practice is to leave packers at minimum on both ends by default (near-zero
  rear packers specifically, to avoid rear instability), and identifies an
  over-high packer value combined with high front ride height as a concrete
  cause of a slow fixed setup they diagnosed. Higher rear ride height (more
  rake) trades stability at corner entry/fast corners for turn-in
  reluctance; a very flat (low, minimal-rake) platform needs a stiffer rear
  spring to stay stable instead of relying on ride height for it.

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
- Preload specifically (Hypercar): higher preload → more stable off-throttle
  but more understeer; lower preload → less stable off-throttle but more
  natural rotation. Interacts with wing/downforce level — a low-wing setup
  can want higher preload for off-throttle stability, a high-downforce setup
  can afford to run preload low since the aero itself provides stability.
  **[single-source, Hypercar-specific]** (S3)

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
- Thin Hypercar data point, not enough detail to generalize a cause→effect
  claim from: one creator's own setups mostly run front toe at/near zero and
  rear toe at zero, with a small amount of front toe added specifically to
  help turn-in on one car. **[single-source, low detail]** (S3)

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
- Brake migration mechanics, now mined **[single-source, detailed]** (S7):
  as the brake pedal is pressed, bias gradually moves *forward* (toward
  50/50 or beyond, relieving rear-brake pressure); releasing the pedal moves
  it back to the static rearward bias. Range is off–2.5% (LMU's scale — e.g.
  a 1% migration at half pedal travel shifts a 51/49 static bias to roughly
  51.5/48.5, and to ~52/48 at full pedal). Net effect: it lets a driver run
  a more-rearward static bias (more rotation) than they otherwise could,
  while still getting front-loaded stability *during* the heavy-braking
  phase itself — "many setups used by high level drivers" run migration at
  its 2.5% max for exactly this reason. Tradeoff/risk: at max migration
  there's no further front-shift headroom left as a fallback if the rear
  tires degrade mid-race and start locking more readily — a driver in that
  situation has to instead pull the *static* bias forward, sacrificing the
  rotation they were relying on.
- Worked example, Ferrari 499P at Bahrain **[single-source]** (S7): default
  static bias 52.3/47.7 (front/rear) produced persistent entry understeer
  and missed apexes; moving to 51/49 gained "a lot more rotation" and ~0.5s
  a lap, at the cost of needing correspondingly careful rear-lockup
  management.
- Systematic tuning-from-default method, corroborating and detailing the
  general "target the most rearward bias the driver can control" principle
  above, **[single-source, worked example]** (S4): set max pedal force to
  100% and brake migration to its max (2.5%) first, then move static bias
  rearward one click at a time — testing each click on track — until rear
  lockups appear, then back off one click to find the limit. Only *after*
  the bias limit is found, address any remaining minor lockups by trimming
  migration or max pedal force down a click or two (never the reverse order
  — changing max pedal force first reduces braking performance and can mask
  where the bias limit actually is).
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
- How the three axes actually interact, **[single-source, detailed]** (S6):
  TC is the overall system; TC slip sets how much wheel slip is *allowed*
  before the system intervenes (lower TC slip = intervenes sooner = less
  rotation available but more rear-tire life; a bit of allowed slip is a
  genuine rotation tool, not just a safety net); TC cut sets how
  aggressively power is cut to a slipping wheel once the system activates
  (visible as flashing LEDs — more flashes/harder cut sound at higher
  values). TC cut mainly earns its keep in the wet: a low-grip track wants
  a high TC cut (e.g. 8–10) so the driver can get back on the throttle
  earlier while still under control; in the dry it's mostly a stability
  margin, at the cost of some acceleration.
- Numeric starting points, **[single-source — starting point, not a
  target]**: TC 6–10, TC slip/TC cut ~5–6 as a dry-Hypercar baseline, tuned
  from there by feel (S6). A second creator's more conservative
  beginner-safe starting point for the same class: TC 8 / TC cut 7 / TC slip
  9, then reduce one click at a time (brakes first, then TC, then ARB) until
  the car starts to struggle on throttle exit, then back up one click (S4).
  A third creator's fixed-setup McLaren 720S GT3 (bronze daily race) example
  values ranged from 3-3-3 up to 5-4-5 across different drivers, i.e. GT3
  fixed-setup TC values run noticeably lower than the Hypercar open-setup
  baselines above (S2) — **do not conflate the two classes' numeric ranges**.
- Fixed vs. open setup difference **[single-source]** (S3): default/fixed
  setups tend to run TC (especially TC slip) higher than an open setup
  needs, because in an open setup some of what TC would otherwise be asked
  to fix (excess rotation, instability) gets addressed via the mechanical
  setup instead — so TC can usually be run lower once the rest of the setup
  is tuned.

### ABS
- **[LMU-specific, needs confirmation]** LMGT3 is reported as the only LMU
  class with native ABS (per the existing Ferrari 296 car note, sourced
  separately from OverTake.gg — not re-verified in this pass). Hypercars and
  the prototype classes (LMP2/LMP3) do not have it, which is why brake bias
  and pedal-force calibration matter more for those classes (S8, S9).
- LMGT3's ABS is a single 0–9 dial that mixes two things at once — activation
  timing *and* front/rear bias-like behavior — rather than a simple "more
  ABS = safer" scale. **[single-source, community-sourced from Reddit
  discussion, not verified against official patch notes]** (S1): 1–3 =
  earlier/more rear-slip tolerance ("oversteer" settings, rear can slip more
  than front before ABS cuts in); 4–6 = balanced (front/rear equally
  sensitive); 7–9 = more front-slip tolerance ("understeer" settings). Within
  each band, the sub-value (e.g. 1 vs. 2 vs. 3) sets *how early* ABS
  activates (late → early), not a second oversteer/understeer axis. Community
  discussion in the same thread flags this as confusing/non-orthogonal
  compared to TC's separate slip/cut axes, and notes real driving-feel
  reports: setting rearward brake bias together with a low ABS value (1–3)
  produces a very aggressive, easy-to-spin car; some drivers found ABS 8
  stabilized the Porsche noticeably under braking.
- Fixed-setup GT3 example values actually driven, from a bronze daily race
  **[single-source]** (S2): McLaren 720S GT3, ABS set to either 6 or 9 —
  ABS 6 felt "more nervous"/easier to rotate under trail-braking and was
  reported as the faster leaderboard choice despite feeling worse to the
  driver; brake bias run 44.5–48% (front) depending on how aggressive a lap
  the driver wanted, with max pedal force left near 100% (98% found ~no
  difference).

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
  numeric values. Those come from the user's own setup sheets. (The R-3 pass
  added several creators' *own* reported values as single-source worked
  examples, tagged as such — these are one driver's numbers for one
  car/track, not validated starting points.)
- Brake migration mechanics (Hypercar-specific) — **resolved this pass**, see
  the Brake bias section (S7). S12 (OverTake.gg brake migration guide) is
  still unfetched and could serve as independent corroboration later.
- R-3 pass (2026-08-28) retrieved and mined the Reddit thread (S1) and all 6
  YouTube videos (S2–S7) via `youtube-transcript-api` captions and a
  chrome-devtools browser session — see `sources.md` for method/content per
  source. Remaining known gaps: LMP2 has no dedicated source at all;
  Bronze/Silver/Gold/Pro legal-aid tables are still unfound (see
  `lmu-classes.md`); S11/S13/S14/S15 (found via search, not fetched) remain
  candidate leads for a future pass.
