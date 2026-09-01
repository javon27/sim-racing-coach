# CURRENT STATE (2026-09-01, overnight) — READ THIS FIRST

**Branch:** `feat/agentic-tool-coach` (NOT merged — user feel-tests + merges).
`main` has the earlier "usable coaching" + live-session-context bugfix.
**Tests:** `.venv/Scripts/python.exe -m pytest -q` → 166 passing, tree clean.

**What this branch adds (all committed, TDD):**
1. Agentic tool-call coach — the model fetches data on demand via tools instead
   of every prompt dumping the whole lap. Live-validated (real API, Algarve laps).
2. Tool-call logging (coach_chat v4) — which tools each answer used is persisted.
3. Session/phase stamping at capture (Gap 1) — laps grouped per stint; status +
   reference are session-scoped, not all-time.
4. Extended telemetry capture — long_g, gear, rpm, abs/tc, and per-wheel
   rotation/brake-temp/brake-press/grip/load/pressure/wear (33 channels).
5. New coach signals: per-corner min_grip; get_wheel_report (brakes/press/wear);
   get_energy_report (fuel + virtual energy). Tools now: session_status,
   list_recent_laps, lap_summary, corner, tire_report, wheel_report,
   energy_report, setup.

**NEEDS LIVE LMU (blocked offline — do NOT fake):**
- Feel-test the agentic coach + merge the branch (user's call).
- Validate PENDING items on a real session: mSession→phase mapping, the extended
  channel signs/units/scales, long_g sign.
- Track conditions (Gap 2): no REST weather endpoint exists in rest.py; the
  ScoringInfo weather tail needs the live buffer to break out. Real wheel-lock
  detection needs mRotation validated (only grip is surfaced now; model infers).
- Traffic (Gap 4): gated on the same struct-tail work.

**Queued (offline-doable next):** surface long_g as per-corner braking g; drop the
legacy build_prompt/ask path once agentic is trusted; telemetry-graph tool that
returns a chart image to the vision model (user's idea).

**If you are a recovery fire:** verify `git status` clean + suite green, confirm
the above is current, and STOP — the user is asleep and will feel-test/merge.
Do not start new features or merge the branch unprompted.

---

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

## Current Status  ===  RECOVERY GOAL COMPLETE — MERGED TO MAIN  ===
Overnight-followups branch is MERGED to main (e7915fb) + live-validation hardening
(scoring struct 02dabc1, ClaudeModel 6f8bc34, REST strategy_usage bc24120) + CoachSession
session history / L1 memory (2532a81). Tree clean on main. The overnight recovery goal
(build follow-ups → user review+merge at lunch) is DONE. User is now in an ATTENDED
interactive session, actively racing and directing new work — this is no longer the
unattended overnight scenario the recovery cron was armed for.

## TONIGHT (2026-08-28→29) — overnight multi-track capture + wiki seed  [UPDATED 00:05]
- Night capture DONE + STOPPED (game closed ~23:xx; task bmkp9g226 stopped per user
  directive at 00:05). Banked laps in scratchpad_live/night/<slug>_lap_<n>.npz across 9
  track/layouts: algarve_international_circuit(1), autodromo_jos_carlos_pace/Interlagos(1),
  autodromo_enzo_e_dino_ferrari/Imola(1), autodromo_nazionale_monza(1),
  monza_curva_grande_circuit(1), bahrain_international_circuit(1), bahrain_outer_circuit(2),
  bahrain_paddock_circuit(3), circuit_de_barcelona(1). Mostly 1 lap each — enough to seed a
  rough calibration; more laps later = better median. These slugs = the map filenames to build.
- Wiki-scrape subagent #1 FAILED (midnight 429). #2 (00:05) mis-concluded "wiki nearly
  empty / only Bahrain" — WRONG: it used the game's track names + a hallucination-prone
  WebFetch summarizer. TRUTH (verified via API + raw wikitext 00:12): wiki has 13 Track
  Guide pages incl. Portimao(=Algarve), Interlagos, Imola, Monza, Bahrain, Spa, Fuji,
  Sebring, Qatar, COTA, Paul Ricard, Circuit de la Sarthe(=Le Mans), Silverstone. NO
  Barcelona. GOTCHA: wiki uses its own names (Algarve="Portimao"). Fetch via action=raw.
  Subagent #3 RE-DISPATCHED 00:12 with the exact 13 titles + wiki→telemetry-slug mapping +
  raw-wikitext method. Await its notification; do NOT duplicate. (memory lmu-wiki-track-source.md updated.)
- WIKI IS STALE/INCOMPLETE (user 23:46): lags the game (LMGT3 page missing Corvette Z06
  GT3.R). Roster = the GAME (garage/telemetry), not the wiki. Saved to memory
  lmu-wiki-track-source.md. Expect new cars/tracks absent from the wiki.

### WIKI SEED RESULT (subagent #3, verified 00:25) — all load via CornerMap.load
NEW seed maps (distances=ESTIMATES, partial corner coverage where wiki omitted direction):
- simcoach/tracks/algarve_international_circuit.json — 10/15 corners (Portimao)
- simcoach/tracks/autodromo_jos_carlos_pace.json — 13/15 (Interlagos)
- simcoach/tracks/autodromo_nazionale_monza.json — 8/11 (Monza WEC chicane layout)
NEW knowledge notes (12): algarve, interlagos, monza, imola(autodromo_enzo_e_dino_ferrari),
fuji_speedway, sebring_international_raceway, lusail(Qatar), circuit_of_the_americas,
circuit_paul_ricard, spa, circuit_de_la_sarthe(Le Mans), silverstone. (+ bahrain from
earlier pass, + existing ferrari_296_gt3). Porsche 911 GT3 R note DONE (00:35,
simcoach/knowledge/porsche_911_gt3_r.md) — wiki page is a stub (specs only), so note is
specs [LMU-wiki] + general rear-engine baseline [general-sim]/[needs-refinement], flagged
for telemetry refinement. Reinforces: wiki car pages are stubs, game/telemetry is truth.
NO map for: Imola (wiki prose lacks directions for ~2/3 corners), Bahrain (same), Barcelona
(no wiki guide) — build these from telemetry. All new files UNTRACKED, not committed
(await user review in the morning).

### CALIBRATION DONE (2026-08-29 ~10:50) — from telemetry alone, no user-at-wheel needed
Proved the user's point: with wiki seed (names/order) + telemetry (geometry), maps calibrate
from data. Method = steering-peak corner detection (scratchpad_live/calibrate_track.py) +
verify_map_directions gate. Results (all directions VERIFIED against the captured lap):
- autodromo_jos_carlos_pace.json (Interlagos) — 10 corners, NAMED (I know the track);
  telemetry CORRECTED a wiki error (Curva do Sol = left, not right).
- autodromo_nazionale_monza.json — 7 corners, NAMED landmarks (Rettifilo/Curva Grande/
  Roggia x2/Lesmo/Ascari/Parabolica).
- algarve_international_circuit.json — 14 corners, geometry+directions VERIFIED but NAMES are
  "Turn N" placeholders (I don't know Portimao's corner names; wiki names were rough). The
  ONLY gap is naming an unfamiliar track — needs driver knowledge or more laps, NOT the wheel.
111 tests pass. All 3 UNTRACKED/uncommitted.
STILL NO MAP (no seed): Imola, Bahrain (3 layouts), Barcelona — build numbered-geometry maps
from their night laps the same way (scratchpad_live/night/*.npz); names from wiki knowledge
notes where possible. monza_curva_grande_circuit layout also uncaptured-into-map.

### NO-SEED MAPS DONE (2026-08-29 ~11:00) — all direction-verified, 111 tests pass
Built numbered-geometry maps (scratchpad_live/build_noseed_maps.py) from telemetry:
autodromo_enzo_e_dino_ferrari(Imola,11), bahrain_international_circuit(9),
bahrain_outer_circuit(6), bahrain_paddock_circuit(6), circuit_de_barcelona(12),
monza_curva_grande_circuit(6, NAMED landmarks). "Turn N" = detection-ordered placeholders
(NOT official turn numbers where detection<official count); Imola/Barcelona famous names
pending driver confirm. Map library now ~13 track/layouts. All new tracks UNTRACKED.

### PICK-UP POINT (2026-08-29 ~18:51, committed; user hit weekly token limit)
Everything committed to main. 137 tests pass. Resume here when tokens reset:
- v0.1 is code-complete + live-validated (detection/resolution/setup all confirmed vs running LMU);
  only the interactive coach shakedown (drive->ask->confirm, spends API) remains for v0.1 sign-off.
- v1.0 UI: DESIGN PHASE. Interactive prototype at docs/design/v1-ui-prototype.html (also artifact
  https://claude.ai/code/artifact/b2b69ed8-cc3c-4fc7-9c37-e5446f56af76). Stack decided: Tauri +
  web frontend (framework-agnostic; perf-first, likely Svelte) + Python core behind local FastAPI/
  websocket. Spec: docs/superpowers/specs/2026-08-29-v1-standalone-ui-design.md (needs updating to
  match the prototype's final IA).
- PROTOTYPE IA (locked with user): nav rail (Coach / Sessions / Pit Wall + global theme toggle +
  Settings). COACH = session bar, resizable laps rail with collapsible per-lap sector deltas
  (Show all/Hide all) + sector color bars, Corners/Setup tabs, markdown chat hero w/ streaming +
  controller-aware + "…coaching…" state. SESSIONS = Game/Track/Car dropdowns (All everywhere; Car
  grouped by class, class selectable), best-lap trend chart (multi-line when >1 car, placeholders
  until game+track chosen), stat cards, session list linking to that session's chat. PIT WALL
  (design-only, game-agnostic from standings/telemetry; LMU's own pit wall an optional embed) =
  track map w/ sector arcs + corner names + S/F marker + direction arrow + class-colored car dots
  (Hyper=red LMP2=blue LMP3=purple GT3=green GTE=orange), timing tower, race flags (yellow sectors,
  class-aware blue). Themed scrollbars throughout.
- NEXT when resumed: finalize the v1.0 spec against the prototype, then build phase 1 (FastAPI
  server over the core, testable without a frontend). Then streaming Model.stream_complete, then
  Tauri shell. Open follow-ups still tracked below (fast-corner map merge, carClass from scoring,
  Le Mans layout variants, contact detection, fuel/energy advisor wiring).

### v1.0 DIRECTION: STANDALONE UI (2026-08-29 ~15:40)
User chose a full standalone desktop UI (NOT web-only, NOT MCP — MCP parked as fallback).
Stack: Tauri shell + React web frontend + Python core behind a local FastAPI/websocket
server (streaming coach replies). Spec: docs/superpowers/specs/2026-08-29-v1-standalone-ui-
design.md. First-cut views: coach chat (streamed), session bar, lap list->corner breakdown,
setup panel; replay overlay deferred. 6 build phases (phase 1 = FastAPI server over the core).
OPEN DECISIONS awaiting user: React vs Svelte (defaulted React); live-telemetry view in v1.0
(defaulted deferred). Also done this session: tier auto-detect wired (tiers.legal_adjustments
-> coach allowed_adjustments in run_repl; 137 tests). Live-intelligence remaining: fuel/energy
strategy advisor (fuel.py cores exist, unwired), contact detection (impact fields in struct).
NEXT: user reviews UI spec + confirms open decisions, then build phase 1.

### LIVE SHAKEDOWN FEEDBACK + FIXES (2026-08-29 ~13:00) — 134 tests pass
User ran the coach live (Corvette @ Le Mans no-chicane). Content was GOOD; issues fixed:
- TONE too harsh + WALL of text -> coach SYSTEM_PROMPT: constructive tone (note what's working),
  terminal format (headline + <=3 short points, "radio call not report").
- CONTROLLER: user on a controller; coach hammered near-zero trail-brake (a controller artifact).
  Added input_device (build_prompt/ask/run_repl; env SIMCOACH_INPUT_DEVICE=controller) -> coach
  told not to read pad artifacts (near-zero trailbrake/stepped steering) as technique errors.
- TERMINAL UX: run_repl now prints you ▸ / coach ▸ with rule separators + "…coaching…" indicator.
- SETUP BLOAT (170 params) -> coach._setup_lines() keyword-filter + drop N/A -> ~81.
- WRONG LAYOUT (the big correctness bug): coach caught that C5/C6/C11 chicanes don't exist on the
  no-chicane config. FIXED: built simcoach/tracks/circuit_de_la_sarthe_mulsanne.json (17 corners,
  13556m, from the stored lap via mapbuild) + track_lookup LAYOUT_RULES so 'Mulsanne' -> that map,
  plain 'Circuit de la Sarthe' -> lemans (variant slugs excluded from generic scorer).
NEXT (user, after): STREAMING the reply (Model-interface change: streaming complete()) so it types
out instead of appearing at once. Token budget flagged ~84% at 13:01, ~2h to reset.

### LIVE VALIDATION of the wiring (2026-08-29 ~12:35, game UP) — 132 tests pass
Ran the detection/resolution chain against live LMU (TF #34 Corvette @ "Circuit de la Sarthe
Mulsanne"). WORKS end-to-end: read_session_track_car OK; resolve_track_map -> lemans (sarthe
alias); resolve_car -> "Chevrolet Corvette Z06 GT3.R" (alias CONFIRMED correct); get_setup()
-> 170 params. FIXES/FINDINGS:
- SETUP BLOAT: 170 params too many for the prompt -> coach._setup_lines() filters to
  coaching-relevant keywords + drops N/A/Non-adjustable -> 81 params. test updated.
- carClass came back None live (garage data) -> class fallback weak for unknown entries;
  alias table carried it. Consider reading class from scoring (mVehicleClass) instead of REST.
- LAYOUT CAVEAT (user's concern, live example): "Circuit de la Sarthe Mulsanne" resolved to
  generic lemans.json — Mulsanne is a Le Mans VARIANT, layout may not match. Track resolver +
  maps don't yet distinguish Le Mans sub-layouts. Telemetry fingerprint would catch it.
- Pre-lap Q&A + crash-resilience also added this session (see below). Also: added track-note
  loading to coach, wait-for-session at startup.
STILL PENDING: full interactive shakedown (drive lap -> ask -> confirm advice; uses API).

### SETUP-IN-PROMPT + CAR MATCHING DONE (2026-08-29 ~12:20) — 130 tests pass
- Coach now SENDS CURRENT SETUP: coach.build_prompt/ask take setup=None; formats VM_* params
  (value + [locked this tier] for non-isFreeSetting) as "Current car setup" section. app.run_repl
  gets rest= param, fetches rest.get_setup() per question (silent-degrade on failure). Enables
  specific setup tweaks ("wing 6->8").
- CAR MATCHING: simcoach/car_lookup.py resolve_car(vehicle, class) via user-maintained
  CAR_ALIASES (entry substring -> model; starters: iron dames->Porsche 911 GT3 R, by tf/racing
  team turkey->Corvette Z06 GT3.R); falls back to "<class> car". LIMIT: game exposes only class
  + team entry, NOT model, so new entries need a CAR_ALIASES line. coach.build_prompt now loads
  car note by SLUG of car name ("Porsche 911 GT3 R"->porsche_911_gt3_r.md).
- __main__: resolves car, passes rest to run_repl; run_repl best_lap uses record.car (raw stored),
  coach uses resolved model. tests: test_car_lookup, test_coach_setup.

### MAP-BUILDER SCRIPT DONE (2026-08-29 ~12:10) — 125 tests pass
simcoach/mapbuild.py: LLM-free corner-map builder. `python -m simcoach.mapbuild <npz>
[--slug X] [--length M]` detects corners (steering-peak, fast+slow), directions+distances
from telemetry, writes numbered-geometry map, self-verifies. detect_corners()/build_map()
importable. tests/test_mapbuild.py (synthetic lap). Replaces the hardcoded scratch builder.
Geometry fully automated; real NAMES still need wiki/diagram/driver enrichment.
NEXT (offered): auto-build a map in __main__ when resolve_track_map()==None (build from
first captured lap) so the coach never blocks on a missing track. Also: track-map-IMAGE
seed source (user idea) for complete named corner lists — must match exact LMU layout,
validate vs telemetry fingerprint (see memory lmu-wiki-track-source.md).

### v0.1 WIRING DONE (2026-08-29 ~11:15) — 123 tests pass
__main__.py now LIVE-wired: read_session_track_car() peeks track+car from shared memory ->
resolve_track_map() (new simcoach/track_lookup.py: token-overlap + alias table, handles
'Silverstone...ELMS'->silverstone, accented Interlagos, 'Circuit de la Sarthe'->lemans) ->
loads corner map -> Coach(make_model(),knowledge) -> LmuSource -> run_repl. Graceful
"no game" message verified (game closed). New: simcoach/track_lookup.py + tests/test_track_lookup.py
(12 cases). smm.read_session_track_car() added.
REMAINING to call v0.1 usable:
1. LIVE SHAKEDOWN (needs rig + ANTHROPIC_API_KEY): `python -m simcoach` while on track ->
   drive laps -> ask "where am I losing time?" -> confirm advice flows. THE last validation.
2. Car-model resolution: mVehicleName is a TEAM/entry name ("Iron Dames 2025 #85:ELMS",
   "Racing Team Turkey by TF 2026 #34:WEC"), NOT the car model — so the porsche_911_gt3_r.md
   note won't auto-load (coach degrades gracefully). Resolve via REST getPlayerGarageData or a
   team->model map. v0.1.1.
3. MAP COMPLETENESS (user 11:07): telemetry-only rebuild DROPPED fast corners the wiki seed had
   (Interlagos 13->10, Monza 8->7). FIX: merge — keep wiki corner NAMES+ORDER (all corners incl.
   fast), but derive ALL DIRECTIONS from telemetry. KEY FINDING (11:35): the wiki knowledge
   notes' directions are UNRELIABLE — Interlagos note calls Curva do Sol a "right" but it's a
   LEFT (telemetry + reality); 2nd wiki-direction error after Silverstone's Farm. So the merge
   must NOT import wiki directions — names/order only, directions from telemetry (compute
   steering sign at each corner's position, incl. fast corners that didn't peak-detect). Single
   lap gives noisy direction reads for fast/flat corners -> ideally 2-3 laps/track first. Held
   for a deliberate pass (not an idle-tick rush) to avoid mislabeling corners (the invariant).
   Note: seed maps were OVERWRITTEN by the telemetry rebuild; reconstruct full corner lists from
   simcoach/knowledge/<track>.md (they carry the wiki corner-by-corner list).

### THE GAP TO A USABLE PRODUCT (assessed 2026-08-29, verified against app.py/__main__.py)
Core is BUILT + validated in pieces: run_repl (app.py) already ties capture→store→analyze→
coach.ask in a background-capture + foreground-Q&A loop. Coach gave real advice end-to-end.
GAP = __main__.py is still a TEST harness (TRACK/CAR hardcoded to test_track/test_car,
uses ReplaySource + ClaudeModel() directly). To be usable v0.1, wire __main__ for LIVE:
  1. Swap ReplaySource -> LmuSource (live capture).
  2. Auto-detect current track+car from telemetry (mTrackName/mVehicleName) and load the
     matching corner map + knowledge — INCLUDING slug normalization (e.g. game's
     'aut_dromo_jos_carlos_pace' vs map 'autodromo_jos_carlos_pace'; accents).
  3. Use make_model factory (ANTHROPIC_API_KEY is in the user's env) instead of ClaudeModel().
  4. Load the track knowledge note into the coach prompt (currently only {car}.md).
Then ONE live shakedown (drive laps -> ask -> confirm advice). ~a focused session. Everything
else (voice, web UI, energy-ratio, contact detection, tier auto-detect) is deferred post-v0.1.

### (obsolete) NEXT: no-seed tracks — DONE above
For each of the 9 slugs: once its seed JSON exists (from the subagent) OR from scratch,
calibrate apex distances via the median-speed-minima method (scratchpad_live/calibrate_
silverstone.py pattern, pointed at scratchpad_live/night/<slug>_lap_*.npz) and verify
directions with simcoach.corners.verify_map_directions against a clean lap. Bahrain has 3
layouts (paddock has most laps=3). Porsche 911 GT3 R car note still pending (subagent task).

### RECOVERY WAKE PROTOCOL (user directive 22:13) — game-running check [capture already stopped]
Capture task bmkp9g226 is STOPPED (game closed). If a NEW capture is started later, re-apply:
each wake, check MMF/game-up; if game closed for a sustained stretch, TaskStop the capture.

## Next Exact Action  ===  HOLD — attended session, awaiting user decision  ===
SILVERSTONE MAP CALIBRATED + VERIFIED (simcoach/tracks/silverstone.json, untracked).
Captured 15 laps (Porsche 911 GT3 R, Iron Dames #85, "Silverstone Grand Prix Circuit -
ELMS"). KEY: this config's S/F is between Woodcote and Copse, so lap STARTS at Copse
(T1) — user confirmed. Rebuilt map in Copse-first driving order (15 named corners);
7 apexes MEASURED from 12-lap median speed-minima (Copse/Becketts/Stowe/Club/Abbey/
The Loop/Luffield), other 8 interpolated. verify_map_directions vs stint_lap_2 =>
ALL 15 directions match telemetry (fixed esses windows + flipped Farm to LEFT — later
cross-validated by the LMU wiki, which also lists Farm as left & same corner order).
111 tests pass. Built scratchpad_live/capture_race.py (full-race capture: keeps launch/
formation/in-out/all laps, unlike capture_wait.py which drops invalid laps + opening
partial). SOURCING RESEARCH done: LMU wiki (lemansultimate.wiki.gg) = primary for corner
names+setup+tips; telemetry = distances+directions (ground truth); CrewChief (GitLab
gitlab.com/mr_belowski/CrewChiefV4, MIT license, rF2+LMU, trackLandmarks.json compatible
but auto-gen names/no direction) = fallback. Wiki content is CC-BY-SA => summarize+attribute.
AWAITING USER: offered to build simcoach/knowledge/silverstone.md (from wiki, summarized)
+ fetch wiki Porsche 911 GT3 R guide for the missing car note. Also still open from
earlier: full-race capture wiring, contact/impact fields (mLastImpactET/Magnitude).
IF YOU ARE A RECOVERY FIRE: verify git; HOLD (user is present, directing). Do not commit
silverstone.json unprompted. Recovery cron 5a131f77 cancellation is the USER'S call.

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

## T-K DONE (56a1204, 105 passed): model provider selector
make_model factory (simcoach/model/factory.py): anthropic+fake now; openai/gemini/local
raise clear NotImplementedError extension points; provider from SIMCOACH_MODEL_PROVIDER
env or default anthropic; anthropic import stays isolated (lazy). FOLLOW-UP: wire
app/__main__ to use make_model instead of constructing ClaudeModel directly (small,
not done). User left for work ~09:48, back ~lunch for LIVE TESTING.

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
2. ✅ DONE 2026-08-28 19:xx — T-E live validation: telemetry offsets CORRECT as-is;
   scoring rF2ScoringInfo wrapper was 440 bytes short -> FIXED (mVehicles offset 560,
   commit 02dabc1), 28/28 mID match, player + lap_distance read correctly at Fuji.
   Suite still 105. STILL PENDING: steering sign convention (RIGHT_IS_POSITIVE) —
   needs live left/right input; and live-driving channel stream (was stationary).
2old. T-E live validation (the big one): run the shared-memory reader against the
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

## LIVE VALIDATION COMPLETE (2026-08-28 20:13, at Le Mans, GT3 #34)
Full capture pipeline validated end-to-end on a live lap (Mulsanne straight + chicane):
- Telemetry struct offsets: CORRECT. Scoring wrapper: FIXED (commit 02dabc1).
- All channels correct live: speed 66-277km/h, throttle/brake 0-1, steering +/-, gear
  1-6, lap_distance advancing (alignment axis), lat_g +/-1.3, fuel burning (liters),
  virtual_energy 0.97, tire temps 38-66C, soc/battery 0 (GT3 no hybrid - correct).
- Sample rate 48.6 Hz (~50 target). Sign convention RIGHT_IS_POSITIVE=True confirmed.
- REST fully validated + strategy_usage (per-lap fuel/ve/tyres) added (bc24120).
STILL TODO: full lap-boundary capture (needs a completed lap; logic unit-tested);
coach end-to-end (needs ANTHROPIC_API_KEY); Le Mans corner map; tier auto-detect build.
