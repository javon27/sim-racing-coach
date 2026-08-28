# HANDOFF — overnight autonomous session (2026-08-27 → 28)

**Goal:** Build the deferred v0.1-adjacent follow-up features on branch
`overnight-followups`, each TDD + committed, so the user can review + merge at
lunch (~2026-08-28 midday). User delegated autonomous overnight work: make
educated guesses, stay unblocked, log decisions. Do NOT ping overnight (user
asleep). Batch questions into the "Decisions / Questions for lunch" section
below.

**Branch:** `overnight-followups` (forked from main @ e7a56d2, which already has
the merged v0.1 core + both spike docs). Merge is the USER'S call at lunch.
**Venv:** `.venv` at repo root (`.venv/Scripts/python.exe`). Tests: `.venv/Scripts/python.exe -m pytest -q`.
**Baseline at branch start:** 36 tests passing.

## Working agreement / autonomy rules
- Educated guesses OK; record each in "Decisions" with rationale + how to reverse.
- Keep unblocked: if something needs the live rig (LMU running) or a real API
  key, build + unit-test against fixtures/mocks and mark a `[LUNCH]` item; do
  NOT stall.
- One module at a time: write tests, implement, run the NARROW test, commit,
  update this file. Keep the full suite green.
- No pings overnight. A mobile ping is only allowed AFTER ~07:30 CDT (user up)
  AND only if genuinely blocking. Otherwise log for lunch.
- Stay in spec scope (docs/superpowers/specs/2026-08-27-lmu-ai-coach-design.md).
  Everything below is already in that spec's roadmap.

## Remaining Checklist (ordered; do top-down)
- [x] T-A Fuel calculator — DONE (commit 1450560, 42 passed). simcoach/fuel.py,
      fuel channel in conftest, tests/test_fuel.py.
- [x] T-B Tier-aware coach — DONE (commit 260de08, 47 passed). simcoach/tiers.py
      (Bronze/Gold confirmed, Silver=GUESS), coach allowed_adjustments param.
- [x] T-C Session/phase model — DONE (commit 4f22aac, 52 passed, migration
      review PASS 0 issues). LapRecord +phase/session_id, schema v1->v2 migrate,
      session_lap_ids, best_lap phase filter.
- [x] T-D LMU REST client — DONE (commit 6fc0b91, 58 passed). simcoach/lmu/rest.py
      (stdlib urllib, injectable fetch), unit-tested with recorded spike JSON.
      [LUNCH] live smoke vs localhost:6397 pending.
- [x] T-E LMU shared-memory reader — DONE (commit 1267b45, 64 passed, review
      PASS-WITH-NOTES pack-consistent). rf2structs.py (ctypes _pack_=4), smm.py
      (reader + LmuSource, begin==end reads), self-consistent tests. KNOWN GAP:
      rF2ScoringInfo tail is a placeholder — complete it live at lunch (item 2).
- [x] T-F Energy-as-BoP — DONE (commit bcbd270, 75 passed, diff-reviewed clean).
      plan_stint (resource-agnostic) + binding_stint; plan_race delegates, stays
      FuelPlan (back-compat). CORRECTION: rf2structs TelemInfoV01 DOES have energy
      fields — mVirtualEnergy (c_float, Hypercar BoP energy), mSoC (c_float),
      mBatteryChargeFraction (c_double). -> T-G maps them. Regen-braking coaching
      still [LUNCH]/user-gated.
- [x] T-G Energy channel mapping — DONE (commit 0f094ec, 76 passed).
      virtual_energy/soc/battery_charge mapped in smm.sample_from_telem (no unit
      conversion — assumed fraction-scale, flagged in docstring). Offsets + scale
      still [LUNCH]-validated. energy_per_lap uses fuel.use_per_lap_from_channel.

## Current Status  ===  AUTONOMOUS WORK COMPLETE, RESTING STATE  ===
7 modules done (T-A..T-G) + reviewed + 2 Critical fixes. Branch overnight-followups,
76 tests passing, tree clean. Energy-as-BoP fully accommodated on the buildable side
(T-F planner resource-agnostic; T-G maps virtual_energy/soc/battery_charge). Remaining
is USER-GATED: live-rig validation, app wiring, regen-braking coaching model, and the
adjustable fuel<->energy RATIO coupling — see [LUNCH]. Nothing autonomous remains.

## Next Exact Action  ===  K-1 IN FLIGHT; THEN R-2, THEN T-H  ===
K-1 knowledge base (impl-kb) dispatched. Then:
1. R-2 (brief: scratchpad/overnight/r2-brief.md): mine the user's setup sources
   (07:21) into docs/superpowers/knowledge/ (setup-guide, classes, sources index).
   Sources durably listed in the R-2 brief (Reddit + 6 YouTube + LMU LMP3 guide +
   Coach Dave ultimate LMU setup guide). Dispatch AFTER K-1 commits (both write to
   knowledge/; one committer at a time).
2. T-H energy-strategy/lift advisor (see below).
User's own words: "do more research yourself" — go beyond the 9 links too.

## Queued: T-H energy-strategy / lift-and-coast advisor (research-grounded)
Deterministic module on fuel.py. LMU shows fuel-laps-remaining AND nrg-laps-remaining
separately; no auto lift cue (open ground). Build:
- laps_remaining(amount, use_per_lap).
- save_target(binding_laps_remaining, laps_to_go) -> per-lap save % needed to make
  the stint (0 if already OK). binding = min(fuel_laps, nrg_laps).
- ratio_balance(fuel_laps, nrg_laps) -> direction advice: if nrg runs out first,
  shift ratio toward energy / lift to save NRG (and vice versa); goal fuel≈nrg laps.
Robust DIRECTION + magnitude math (uncertain exact LMU units flagged). The "where to
lift" per-corner cue needs live throttle/consumption traces -> [LUNCH]/later.

## K-1 DONE (commit f0a2bbb, 76 passed): KB scaffold + roster + Spa map + GT3 note.
  README, lmu-roster (14 tracks + cars), simcoach/tracks/spa.json (10 corners,
  distances=estimates), simcoach/knowledge/ferrari_296_gt3.md. Extend: live-calibrate
  spa distances; more tracks/cars; refine car notes from R-2 + user sources.

## (done) K-1 knowledge base (user 07:13 — "begin building track+car KB")
Brief: scratchpad/overnight/k1-brief.md. Scaffold + LMU roster + exemplar Spa corner
map + exemplar GT3 note. Corner names/dirs = facts (research); lap-distances =
flagged ESTIMATES pending live calibration; car tuning = template + researched
baseline, user refines from their Gemini notebook. DISPATCH AFTER research-energy
COMMITS (avoid two subagents committing to the same index concurrently — that race
bit us at the start; see Incident 1 pattern). Educated guess: exemplar track = Spa
(~7km + GT3 matches the spike session); user confirms priority tracks/cars at lunch.
IF YOU ARE A RECOVERY FIRE: verify `git status` clean + suite green
(.venv/Scripts/python.exe -m pytest -q => 76 passed), reply that the build is
complete and awaiting the user at lunch, and STOP. Do NOT: start new features
(user must shape further work); attempt [LUNCH] items (need rig/user); cancel
recovery cron 5a131f77 (user's call).

## Files Changed (this branch)
- T-A: simcoach/fuel.py, tests/test_fuel.py, tests/conftest.py(+fuel)
- T-B: simcoach/tiers.py, tests/test_tiers.py, simcoach/coach.py, tests/test_coach.py
- T-C: simcoach/store.py, simcoach/model_types.py, tests/test_store.py
- T-D: simcoach/lmu/__init__.py, simcoach/lmu/rest.py, tests/test_lmu_rest.py
- T-E: simcoach/lmu/rf2structs.py, simcoach/lmu/smm.py, tests/test_lmu_smm.py
- fix (final-review C1/C2/M1): simcoach/lmu/smm.py, simcoach/lmu/rest.py, tests/test_lmu_smm.py
- T-F: simcoach/fuel.py, tests/test_fuel.py (+ spec energy section)
- chore: .gitignore (+.superpowers/)

## Decisions / educated guesses (for lunch review)
- Working on a feature branch in the main checkout (not a worktree) for recovery
  simplicity; merge is user's call. Reverse: `git branch -D overnight-followups`.
- Use SUBAGENTS for implementation (user directive 00:01) — keeps controller
  context lean so compaction is rare overnight. I write the failing test +
  brief, dispatch an implementer (sonnet) that implements/tests/commits and
  returns only a short status, then a quick reviewer or self-review of the diff.
  Briefs/reports in scratchpad/overnight/. Recovery still keys off HANDOFF+git.

## Questions for lunch (accumulate; do NOT ping overnight)
- T-B: SILVER tier's exact adjustable set is a GUESS (Bronze + tire pressures,
  ARBs, diff, wing). Bronze (TC/brake-bias/ABS) and Gold (open) are known. Please
  confirm Silver's real allowances, or we just rely on live `isFreeSetting`.
- T-A: race_laps = ceil(duration/lap) + 1 (the +1 for lap-in-progress at time
  expiry). Confirm LMU's timed-race end rule matches (leader finishes current lap
  after clock hits 0). Fuel margin default 5% — adjust to taste.

## [LUNCH] — needs you + the rig (live validation overnight couldn't do)
Do these with LMU running, on track (a WHEEL lap for the sign-convention one):
1. T-D live smoke: with LMU running, `LmuRest().get_player_standing()` /
   `.fuel_fraction()` / `.adjustable_keys()` against localhost:6397 — confirm the
   recorded shapes still match live.
2. T-E live validation (the big one): run the shared-memory reader against the
   live `$rFactor2SMMP_Telemetry$` / `$rFactor2SMMP_Scoring$` buffers. Confirm the
   ctypes struct OFFSETS are right (values sane: throttle/brake 0..1, speed matches
   HUD, gear correct). Structs use `_pack_ = 4` (header is #pragma pack(push,4)).
   KNOWN GAP (te-report.md): the rF2ScoringInfo wrapper struct is an incomplete
   0-byte PLACEHOLDER (no Iron Wolf header was on disk, only LMU's engine header),
   so SCORING vehicle lookups (player mLapDist / mIsPlayer / mLastLapTime) read the
   WRONG offset until completed. FIX FIRST: complete rF2ScoringInfo from the
   pyRfactor2SharedMemory rF2data.py layout (or dump the live buffer and locate
   mVehicles). Telemetry buffer (inputs/speed/tires) is the confident part.
3. Sign convention (do on a WHEEL lap, in a known left+right corner): confirm the
   sign of `mUnfilteredSteering` and `mLocalAccel.x`, and set `RIGHT_IS_POSITIVE`
   in simcoach/corners.py to match. (Controller tonight was fine for feasibility
   but not for magnitude/sign calibration.)
4. Confirm the fuel `mFuel` channel units (liters) and tank capacity source.
4b. ENERGY-as-BoP (T-F): the resource-agnostic planner is built (plan_stint /
   binding_stint handle fuel OR energy MJ). LIVE items: find LMU's energy/state-of-
   charge/deploy/regen fields (rf2structs had none obvious — check the LMU-native
   TelemInfoV01 or REST), and provide the per-stint MJ budget (from BoP tables).
   Then map an `energy`/`soc` channel and feed binding_stint. DEFERRED w/ you:
   the regen-braking coaching model (how hybrid regen blends with friction braking,
   brake-balance shift, lift-and-coast deployment strategy) — needs your domain
   input + live data; it's a coach/knowledge concern, not a deterministic calc.
4c. ENERGY IS UNIVERSAL (user 07:09): GT3s use energy too, replenished at stops with
   fuel. The planner (T-F) + energy channels (T-G) already apply to all cars — good.
   NEW to model WITH YOU at lunch: the ADJUSTABLE FUEL<->ENERGY RATIO. It couples
   fuel and energy (trade one for the other; both topped up at a stop). binding_stint
   currently treats them INDEPENDENTLY (conservative). Building the ratio as an
   optimizable lever needs the exact LMU mechanic (does it split pit time? trade
   budget? affect regen?) — confirm live, then extend the planner. NOT guessed.
5. WIRING (I2, needs live + your calls): none of the 4 new features are reached
   from app.py yet — this branch adds capability, not behavior. Wire LmuSource +
   fuel plan + tier allowed_adjustments into run_repl for a real end-to-end
   capture→coach run (needs ANTHROPIC_API_KEY). When wiring:
   - I1: reconcile the two allowed_adjustments producers — tiers.tier_adjustable()
     returns friendly labels ("brake bias"); rest.adjustable_keys() returns raw LMU
     keys ("VM_..."). Add a key→label map or feed the coach friendly names, else the
     LLM sees key-soup. (Decision needed; not shipped-broken.)
   - M3: drop LmuSource's first partial lap (capture starts mid-lap, not at dist 0).
   - M2: open_mmap has a tiny TOCTOU window (harmless; note only).

## Final-review (overnight) result
merge-after-lunch: was "n" ONLY due to 2 fixable-now T-E bugs (C1 speed m/s vs km/h;
C2 valid/lap_time sentinel poisoning best_lap). BOTH FIXED — commit eaf50d3, 68
passed (build_lap_record() helper: skips lap_time<=0, valid=not mLapInvalidated;
speed now *3.6 km/h; rest.py import to top). Integration otherwise cohesive, no
regression to merged core, gaps honestly marked. After the [LUNCH] validations +
wiring decisions above, this is a normal merge.
- fix-smm flagged for lunch: WHEN mLapInvalidated resets relative to the lap-number
  rollover is unverified (live). Confirm the end-of-lap validity read is correct live.

## Recovery notes
- Recovery cron `5a131f77` armed (every 30m, fires auto-continue.md while idle).
- auto-continue reconciles THIS file against `git status`; git wins. Keep
  "Next Exact Action" + checklist accurate after every step.
- No HANDOFF drift: update Current Status + Next Exact Action + Files Changed
  after each commit.
