# LMU class notes (Hypercar, LMGT3, LMP2, LMP3)

Per-class behavior and setup-surface notes, cited against `sources.md`.
See `lmu-roster.md` for the current car list per class. Confidence tags as
in `lmu-setup-guide.md`: **[broadly agreed]** vs **[single-source]**.

## Hypercar (LMDh)

- Adjustable surface is the deepest in the game: full aero (wing, ride
  height, rake), hybrid deployment maps, regen settings, brake migration (a
  dynamic bias system on top of static brake bias), plus the usual mechanical
  levers (springs, ARB, dampers, diff, toe). **[single-source]** (S9)
- No native ABS — braking calibration (max pedal force, static bias) matters
  more here than in LMGT3, since there's no ABS to mask an imperfect setup.
  **[single-source, needs confirmation]** (S8, S9 — see the ABS gap noted in
  `lmu-setup-guide.md`)
- Energy/fuel: hybrid deployment/regen maps let a driver strategically
  conserve or deploy energy per stint or per corner. This is a
  Hypercar-exclusive lever — no other class has it. **[single-source, not
  detailed further — needs deeper research]** (S9)
- Aero balance is more front/rear-sensitive than GT3: more front aero gives
  crisper turn-in at the cost of rear stability; more rear aero is safer but
  harder to rotate mid-corner. Ride height window is tight — too low bottoms
  out in high-speed sections, too high loses significant downforce.
  **[single-source]** (S9)
- Diff can get lively if run too open, especially with the hybrid system
  active. **[single-source]** (S9)
- Driver-category (Bronze/Silver/etc.) legal aid restrictions for Hypercar
  were **not found** in this research pass — do not assume Hypercar allows
  or disallows any specific aid without checking LMU's current rules.

## LMGT3

- Setup surface: mechanical only (springs, ARB, dampers, diff, ride
  height/toe, brake bias/ducts) — no hybrid/energy-deployment layer.
  Far less aero-dependent than Hypercar, and generally described as more
  forgiving to aero changes; GT3s "reward good mechanical balance."
  **[single-source]** (S9)
- Driver aids: LMGT3 is reported as the **only** LMU class with native ABS
  (two axes: intervention level, front/rear distribution), plus a
  three-facet TC (TC level, TC power cut, TC slip angle) adjustable on the
  fly via MFD. **[LMU-specific, single prior source — see the existing car
  note]**: `simcoach/knowledge/ferrari_296_gt3.md`, sourced from OverTake.gg;
  not re-verified in this research pass.
- Because of ABS, LMGT3 can run more rearward brake bias than a
  non-ABS class. **[single-source]** (S9)
- Energy/fuel: no hybrid system; standard fuel-load setup considerations
  only (weight, brake-bias/pack trim as fuel burns). No LMGT3-specific fuel
  strategy notes found in this pass.
- Bronze-legal aid subset (which of TC/brake-bias-range/ABS a Bronze-rated
  driver is specifically permitted to run): **not confirmed** by this
  research pass — the existing Ferrari 296 car note already flags this same
  gap. Verify against LMU's current driver-category rules before coaching on
  it.

## LMP2

- No dedicated source was mined for LMP2 in this pass — the two priority
  text guides (S8, S9) don't cover it, and none of the broadened searches
  surfaced an LMP2-specific setup guide. **[gap — do not infer LMP2 behavior
  from LMP3 or Hypercar notes above without flagging it as an assumption.]**
- What's known only from the roster: 2 cars in-class (Oreca 07 Gibson,
  standard + ELMS de-restricted variant) — see `lmu-roster.md`.
- Bronze-legal aid subset: not researched.

## LMP3

- Setup surface (from the LMU official LMP3 quick-setup guide, S8):
  fuel/radiator/TC/diff, tire pressure/camber/brake bias/pedal force/brake
  ducts, spring rates/packers/ride height, dampers, toe/ARB/rear wing — see
  `lmu-setup-guide.md` for the full per-lever cause→effect table, all of
  which is LMP3-sourced.
- Two cars profiled in S8 with contrasting behavior: Ligier JS P325 (more
  accessible, stronger brakes allowing rearward bias, RHD visibility issue)
  and Ginetta G61 LT-P3 Evo (better high-speed stability, low-speed
  understeer tendency, LHD, harder to extract pace from). **[single-source,
  car-model-specific]** (S8). Note: `lmu-roster.md` lists 4 LMP3 cars total
  (Adess AD25, Duqueine D09, Ginetta G61-LT-P3 Evo, Ligier JS P325) — S8 only
  covers 2 of the 4.
- No hybrid/energy-deployment layer; fuel is a straight weight/strategy
  factor. Radiator cover position is a real lever — LMP3 engines are managed
  toward a <120°C coolant target by closing radiator covers incrementally.
  **[single-source, LMP3-specific]** (S8)
- No ABS mentioned for LMP3 in S8 (silence, not a confirmed negative) — if
  the coach needs a definitive answer, verify against LMU's current
  driver-aid docs rather than assuming from this gap.
- Bronze-legal aid subset: not confirmed by this research pass.

## Cross-class gap: driver-category (Bronze/Silver/Gold/Pro) legal aids

None of the 9 supplied sources nor the broadened search turned up a single
authoritative table of which aids (TC, ABS, brake-bias range) each driver
category is permitted to run, per class. This is flagged as an open item in
every class section above rather than guessed at. If this matters for the
coach's Bronze-legal guidance, it needs a dedicated research pass against
LMU's current rulebook/BoP documentation.
