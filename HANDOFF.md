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
- [ ] T-C Session/phase model — additive: `phase` (practice|qualifying|race) +
      `session_id` columns on `laps` (schema user_version 1→2, migrate in place),
      `LapStore.save(..., phase=?, session_id=?)`, query helpers. Preserve all
      existing behavior. Tests extend `tests/test_store.py`.
- [ ] T-D LMU REST client — `simcoach/lmu/rest.py`: read setup
      (getPlayerGarageData: 172 keys, isFreeSetting), standings/timing + fuel
      (watch/standings: lapDistance, fuelFraction, sector, lapStartET, etc.).
      Unit-test against RECORDED JSON fixtures captured in the spike (see
      Decisions for shapes). `[LUNCH]` live smoke against localhost:6397.
- [ ] T-E LMU shared-memory reader — `simcoach/lmu/smm.py` + `LmuSource`
      (implements TelemetrySource): vendor rF2 ctypes structs (rF2Telemetry /
      rF2Scoring; MMF `$rFactor2SMMP_Telemetry$` / `$rFactor2SMMP_Scoring$`),
      map fields → LapRecord channels. begin==end version check for torn-free
      reads. Unit-test parsing against synthetic buffers. `[LUNCH]` live smoke +
      confirm RIGHT_IS_POSITIVE sign convention (needs a WHEEL lap).

## Current Status
T-A (fuel, 1450560) + T-B (tiers, 260de08) done, 47 passed. Next: T-C
(session/phase schema migration). Brief at scratchpad/overnight/tc-brief.md.
T-C is a schema migration — REVIEW it after (dispatch a reviewer subagent).

## Next Exact Action
Dispatch/execute T-C per scratchpad/overnight/tc-brief.md: add phase+session_id
to LapRecord, migrate store schema v1->v2 (fresh-create with cols + ALTER for
existing v1), persist/restore metadata, add session_lap_ids + best_lap phase
filter, tests incl. a migration test. Run tests/test_store.py + full suite,
commit "feat: session/phase lap metadata with in-place schema migration (v2)",
then REVIEW the diff (schema migration), tick T-C.

## Files Changed (this branch)
- T-A: simcoach/fuel.py, tests/test_fuel.py, tests/conftest.py(+fuel)
- T-B: simcoach/tiers.py, tests/test_tiers.py, simcoach/coach.py, tests/test_coach.py

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

## Recovery notes
- Recovery cron `5a131f77` armed (every 30m, fires auto-continue.md while idle).
- auto-continue reconciles THIS file against `git status`; git wins. Keep
  "Next Exact Action" + checklist accurate after every step.
- No HANDOFF drift: update Current Status + Next Exact Action + Files Changed
  after each commit.
