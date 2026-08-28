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
- [ ] T-A Fuel calculator — `simcoach/fuel.py` + `fuel` channel on LapRecord +
      fixture. Deterministic: fuel_per_lap (lap-over-lap) × race_laps
      (race_duration ÷ representative_lap_time) + margin → total needed, stops
      required, fuel-to-add. Tests in `tests/test_fuel.py`.
- [ ] T-B Tier model + tier-aware coach — `simcoach/tiers.py` (Bronze/Silver/Gold
      allowed-adjustment sets; also an `isFreeSetting`-driven "allowed keys"
      path per the spike). Wire a `tier`/allowed-set into `Coach.build_prompt`
      so setup advice is constrained; driving advice unconstrained. Tests extend
      `tests/test_coach.py`.
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
Branch created, HANDOFF written. No feature code yet. Next: T-A.

## Next Exact Action
Start T-A: write `tests/test_fuel.py` (failing) for fuel_per_lap + race fuel
calc, add `fuel` channel to the synthetic_lap fixture, implement
`simcoach/fuel.py`, run `tests/test_fuel.py`, commit, tick T-A.

## Files Changed (this branch)
(none yet)

## Decisions / educated guesses (for lunch review)
- Working on a feature branch in the main checkout (not a worktree) for recovery
  simplicity; merge is user's call. Reverse: `git branch -D overnight-followups`.
- Direct TDD implementation (not SDD subagent orchestration) for unattended
  robustness — fewer stall points. Quality guarded by thorough tests + a
  self-review pass per module.

## Questions for lunch (accumulate; do NOT ping overnight)
- (none yet)

## Recovery notes
- Recovery cron `5a131f77` armed (every 30m, fires auto-continue.md while idle).
- auto-continue reconciles THIS file against `git status`; git wins. Keep
  "Next Exact Action" + checklist accurate after every step.
- No HANDOFF drift: update Current Status + Next Exact Action + Files Changed
  after each commit.
