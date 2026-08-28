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
Branch overnight-followups, 93 tests passing, tree clean, 40 commits. Also built this
morning: knowledge base (K-1 scaffold/roster/Spa/GT3, R-2 + retrieval: 7/7 sources
mined incl. all videos + Reddit), energy research folded to spec, T-H strategy advisor,
T-I shared-memory compat/health check (multi-candidate MMF + anti-cheat rule). Overnight:
T-A fuel calc, T-B tier-aware coach, T-C session/phase migration, T-D REST client,
T-E shared-memory reader, T-F resource-agnostic (fuel/energy) planner, T-G energy
channels, T-H energy-strategy advisor; +2 Critical integration fixes; energy research
(d289178) folded into spec; knowledge base begun (K-1 f0a2bbb scaffold/roster/Spa/GT3,
R-2 827cb5c setup-guide/classes/sources). Remaining is USER-GATED: live-rig validation,
app wiring, regen-braking coaching, fuel↔energy RATIO coupling, KB gaps — see [LUNCH].

## Next Exact Action  ===  HOLD (queue cleared)  ===
T-J DONE (b5003f2, 100 passed): driver profile personalization. No autonomous step
remains. IF YOU ARE A RECOVERY FIRE: verify git clean + suite green
(.venv/Scripts/python.exe -m pytest -q => 100 passed), reply build complete/awaiting
user, STOP. Do NOT start new features / attempt [LUNCH] items / cancel cron 5a131f77.
Remaining KB gap: no LMP2 source. Lunch: refine driver-profile field schema; the
[LUNCH] live list (MMF name mode, struct offsets, sign convention, energy fields,
strategy/usage endpoint, fuel-save-light exposure) is unchanged.

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

## T-J DONE (b5003f2, 100 passed): driver profile personalization
simcoach/driver.py: DriverProfile (braking_style/balance_preference/risk_tolerance/
notes + learned dict, JSON load/save, to_prompt_block) + observed_style(laps) seam;
coach.build_prompt/ask take optional driver_profile -> appended to SYSTEM prompt as
personalization + a tailoring instruction. FIELD SCHEMA is an educated guess — USER TO
REFINE at lunch (what preference dimensions matter: braking, balance, risk, kerbs,
tire management, qualifying-vs-race goal, etc.). Auto-updating `learned` from live laps
over time is a later increment (observed_style is the seam).

## ABS clarification (user 09:43): KB is CURRENT, not stale
I mis-inferred that the KB's 0-9 ABS content was outdated and nearly flagged it — the
user corrected: the Reddit "New ABS system" thread describes the CURRENT system (0-9
dial organized into oversteer/balanced/understeer groupings). The OLD system was a plain
0-9 "higher=more". KB reflects current correctly; NO staleness flag applied. The ABS
rework is cited in the spec's freshness section as an EXAMPLE of mechanic drift.

## T-K IN FLIGHT (user 09:47): model provider selector
Multi-provider requirement: Model interface already provider-agnostic; v1.0=Anthropic,
support other online providers + local via the same seam. T-K = make_model factory
(brief scratchpad/overnight/tk-brief.md): anthropic+fake now, openai/gemini/local as
clear extension points (NOT implemented — need keys/SDKs). Spec "Model providers"
section added. After T-K commits + verify: back to HOLD. User left for work ~09:48,
back ~lunch for LIVE TESTING.

## KB update MECHANISM (user 09:45) — DESIGN DECISION for lunch
User: "we'll need a mechanism to update the knowledgebase over time — whether that's
here with an agent, or built into the engine." Captured in spec ("Knowledge-base UPDATE
MECHANISM"): A) agent/maintenance-time refresh workflow (recommended backbone —
formalize the ad-hoc research runs into a documented refresh skill+checklist), B) in-
engine (i staleness detection [build early], ii on-demand research hook via coach's
Model+web, iii KB-pack fetch if shipping to others), C) continuous personal learning
(driver profile/laps — already seeded). Decide with user: in-engine vs agent split;
single-user vs distributed. NOT built yet — needs the user's call on approach.

## Recurring maintenance (user 09:41): physics/tire-model updates
Every LMU update can change physics/tire/BoP and invalidate KB advice. Process (in
spec "Knowledge freshness"): tag KB notes with the validated LMU build (gameVersion,
readable from shared memory ~14130/14237); on each update research patch notes for
physics/tire/BoP/aero changes, update affected knowledge, flag re-validation; a
staleness warning (live gameVersion != KB's validated build) is a buildable seam once
live capture is wired. Watch: official LMU notes + CrewChief changelog + community.

## [LUNCH] — needs you + the rig (live validation overnight couldn't do)
Do these with LMU running, on track (a WHEEL lap for the sign-convention one):
1. T-D live smoke: with LMU running, `LmuRest().get_player_standing()` /
   `.fuel_fraction()` / `.adjustable_keys()` against localhost:6397 — confirm the
   recorded shapes still match live.
1b. MMF NAME MODE (from T-I / CrewChief changelog): CrewChief v4.19.4.0 added a
   "legacy shared memory filenames" toggle + renamed its plugin DLLs — so a NEWER
   plugin build may publish the MMFs under DIFFERENT names than the legacy
   $rFactor2SMMP_Telemetry$/$rFactor2SMMP_Scoring$ we have. LIVE: run
   smm.compatibility_check() against the live game; if it can't find the MMF, check
   which filename mode the installed plugin uses and add the current name(s) to the
   candidate lists in smm.py. (The spike confirmed the legacy names worked on the
   user's box yesterday, so likely fine — but verify after any plugin update.)
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
4d. FUEL-SAVE LIGHTS (user 09:26): LMU has an MFD "laps to save for" target that drives
   in-car fuel-save lights (real-life style) — NOT a track HUD marker. Coach role: set
   the target (T-H stint_save_advice output) + strategy, not replace the lights. LIVE Q:
   is the fuel-save light state / MFD target exposed via shared memory or REST? If yes,
   read it and coach the lift directly. (Corrected the spec's earlier "no HUD" note.)
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
