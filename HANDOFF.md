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

## Current Status
ALL 5 MODULES DONE (T-A..T-E), 64 tests passing on branch overnight-followups.
Overnight build complete. Running a final whole-branch review as capstone, then
resting state for user's lunch review + the [LUNCH] live-validation items.
(T-E first attempt died on account session-limit ~00:22 (reset 3:50am); retried
clean.) Brief at scratchpad/overnight/te-brief.md. T-E is front-loaded code
transcribed from the on-disk header; struct offsets + sign convention + live
loop are [LUNCH]-validated (LMU not running overnight). REVIEW T-E after.

## Next Exact Action
Dispatch/execute T-E per scratchpad/overnight/te-brief.md: simcoach/lmu/rf2structs.py
(ctypes from InternalsPlugin.hpp), simcoach/lmu/smm.py (reader + LmuSource over
$rFactor2SMMP_*$ buffers, begin==end reads), tests/test_lmu_smm.py (self-consistent
synthetic-buffer parsing tests). Run + full suite + commit + REVIEW, tick T-E.
After T-E: overnight build complete — write final summary + [LUNCH] checklist in
HANDOFF; leave branch for user to review/merge. Consider a final full-suite review.

## Files Changed (this branch)
- T-A: simcoach/fuel.py, tests/test_fuel.py, tests/conftest.py(+fuel)
- T-B: simcoach/tiers.py, tests/test_tiers.py, simcoach/coach.py, tests/test_coach.py
- T-C: simcoach/store.py, simcoach/model_types.py, tests/test_store.py
- T-D: simcoach/lmu/__init__.py, simcoach/lmu/rest.py, tests/test_lmu_rest.py
- T-E: simcoach/lmu/rf2structs.py, simcoach/lmu/smm.py, tests/test_lmu_smm.py
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
Then: wire LmuSource into the terminal app for a real end-to-end capture→coach run
(needs ANTHROPIC_API_KEY for the coach).

## Recovery notes
- Recovery cron `5a131f77` armed (every 30m, fires auto-continue.md while idle).
- auto-continue reconciles THIS file against `git status`; git wins. Keep
  "Next Exact Action" + checklist accurate after every step.
- No HANDOFF drift: update Current Status + Next Exact Action + Files Changed
  after each commit.
