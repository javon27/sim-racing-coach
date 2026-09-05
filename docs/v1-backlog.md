# v1 Backlog — user notes (2026-09-03, ~23:07 CDT)

Captured from live feedback while driving. To resume when the Pro-plan token
window resets (user switched to API usage mid-session). Source: the UI prototype
has features not yet in the live app, plus coach-quality gaps.

## Lap list / sessions UI
- [x] **Sector timing per lap** — DONE. Data layer `c35239b`, UI `6f07fa9`, LIVE capture `b50dc8f` (compute_sectors with a positivity guard → stores None rather than wrong values if the rF2 convention is off). Convention still wants ONE rig eyeball to confirm live sectors read right (guard prevents bad data until then).
- [x] **Trend chart: spread same-day sessions + scroll/zoom** — DONE `5e3e1df` (x keyed on session timestamp, one slot each; ± buttons + Ctrl/Cmd+wheel; labels thin when zoomed out).
- [x] **Selected lap highlighted** (replaced the wrong "Lap N selected" hint) — DONE `5a47f0a`.
- [x] **Group/delineate laps by phase** + per-phase numbering — DONE `845f268` (session_laps returns phase; LapList groups into phase blocks with sticky headers, numbers 1..N per phase). Chat hint still uses the real lap id.
- [x] **Best/fastest lap shown** without scrolling — DONE `7a05cc1` (best-lap header above the list).
- [x] **Auto-scroll the lap list** as new laps are added — DONE `7a05cc1`.
- [x] The app header should show game status — DONE `df571e6` (On track / Connected / LMU offline chip from /status).
- [x] The main chat should auto-scroll on send + reply — DONE `0fae6ee` (send always snaps to newest; reply follows).

## Setup tab
- [x] **Persist + live-update** — DONE via `3bfb1b0` (lazy-resolve `rest`, per-track map). /setup stays populated once LMU REST is reachable; no restart needed. NOTE: not yet *push*-live on every in-game change — the panel fetches once on mount; a poll/refresh-on-change could be added later if desired.
- [x] **Show only changeable settings** — DONE `d24f772` (filters to `isFreeSetting`, mirrors `tiers.adjustable_from_setup`).
- [x] **Readable setting names** — DONE `d24f772` (curated VM_ map + generic prettify fallback). Curated map is small; extend it as needed.
- [x] **Info tips per setting** — DONE `b98d7f5` (InfoTip ⓘ on 13 settings) + `8b092f9` (Import-history explanation paragraph).

## Coach quality
- [x] **Lap times as mm:ss.ss** — DONE `6aa05d6` (used m:ss.mmm / 3-decimals to match the UI lap list, slightly more precise than literal .ss).
- [~] **Fix context logic** — PROMPT FIX SHIPPED `7941bb9` (AGENTIC_SYSTEM_PROMPT now steers session/race-wide questions to list_recent_laps + get_session_status, not the selected lap). Model-behavior nudge — VERIFY LIVE on the rig (can't unit-test the LLM's routing). Original root cause below. **ROOT-CAUSED 2026-09-04 (code read):** (1) `AGENTIC_SYSTEM_PROMPT` (coach.py) routes ~everything to `get_lap_summary` (one lap) and gives NO guidance to use `list_recent_laps`/`get_session_status` for whole-race/overall questions — the session-scoped `list_recent_laps` tool exists (coach_tools.py:171) but the coach isn't steered to it; (2) `_resolve_lap("latest")` (coach_tools.py:142) returns the SELECTED lap (`self.record`/`self.lap_id`), conflating "the lap you clicked" with "your current lap." FIX = small prompt edit steering overall/race/session questions to list_recent_laps + get_session_status (± clarify `_resolve_lap` latest-vs-selected). **BLOCKED on live-LLM verification** — a prompt-behavior change can't be confirmed by unit tests, so not shipped blind. Do it when the rig/API is available to eyeball a real answer.
- [ ] **Race-narrative awareness**: coach should understand the arc — e.g. started 5th, dropped to 10th after an incident, clawed back to 3rd. Racecraft is the bigger piece. **INVESTIGATED 2026-09-04: NO position/standings/incident data is captured or stored today** — capture records only laps (time/telemetry/phase/session). This is a DATA-PLUMBING PROJECT, not a prompt tweak: (1) spike what's reliably available (`player.mPlace` live in scoring; standings/gaps via `rest.get_player_standing()`; incidents unclear), (2) capture position/place per lap + store it, (3) add a coach tool exposing race arc, (4) prompt the coach to weigh racecraft. Related to memory [[race-context-gaps]]. Needs its own brainstorm.
- [ ] **Get the actual car name** — sessions/coach show a generic or class-level car string, not the real vehicle name (e.g. "Ferrari 296 GT3" not "GT3"). Source the true name from shared memory (rF2 scoring `mVehicleName`/`mVehicleClass` on VehicleScoringInfoV01, or telemetry) and thread it through the session id/label + coach context. Check what `session_id_for`/capture currently uses before changing the session identity — car is part of the stable slug, so changing the string may need a migration or a display-name-vs-id split.
- [ ] **Encouraging tone** — the coach should also be encouraging. NOTE: the agentic prompt ALREADY says "Encouraging and constructive; note what's working; never berate" (coach.py:126). If still not encouraging enough in practice, strengthen that line — but verify against a live transcript first (cheap prompt tweak, but needs a real example of the flat tone to target).
- [x] Chat lost thread mid conversation — FIXED `7941bb9` (WS worker now loads store.chat_messages(chat_id) → passes as `history` to ask_agentic_stream; 2 tests). **ROOT-CAUSED 2026-09-04:** the WS `/coach` worker calls `ask_agentic_stream(ctx, question, car=, input_device=)` with NO `history` arg (app.py:294) — so the coach gets zero prior turns even though the method supports `history=` and the system prompt says "the messages above are what you and the driver already said." Every turn is contextless. FIX (app.py only, ready): when `req.chat_id` is set, load `store.chat_messages(chat_id)` → build `[{role:user,content:question},{role:assistant,content:answer}, ...]` → pass `history=`. Holding the edit until the parallel sector-capture agent finishes editing store.py.

## Track maps
- [ ] **Rebuild the Algarve corner map**. Corner count is wrong. **INVESTIGATED 2026-09-04:** current map `algarve_international_circuit.json` has **14 corners; real Portimão has 15** (an under-count / collapsed complex). Tried auto-rebuild via `mapbuild.build_map()` on 3 real captured laps (telemetry/38-40.npz) → **10 / 12 / 14 corners, inconsistent, never 15** — confirms [[track-map-audit]] (auto-build collapses chicanes + is lap-to-lap noisy). CLAUDE.md invariant forbids hand-guessing corners (owned data, computed from telemetry). So this needs **driver verification against a circuit diagram** (as Imola/Spa were). **CANDIDATE (2026-09-04):** ran mapbuild over all 30 captured Algarve laps — count distribution {1:1, 9:1, 10:1, 11:2, 12:10, 13:5, 14:7, 15:3}, mode 12, too noisy to auto-pick. BUT 2 laps independently produced **15 corners with directions `RRRLLLRRLRLLRRR`** — if the driver confirms that count+direction pattern matches Portimão, regenerate the map from one of those 15-corner laps (Turn-N names, geometry from telemetry — honors the invariant). USER: confirm RRRLLLRRLRLLRRR vs the real circuit, then say go. 30 laps in repo-root simcoach.sqlite; 16 in %APPDATA%.
- [x] Brake column distance-from-apex — DONE `56e932d` (CornerPanel shows apex_dist − brake_point = m before apex, matching the coach; was raw S/F distance).

## Code review (2026-09-04, this session's 10 commits `3bfb1b0..56e932d`)
Reviewer: 0 Critical/High, 2 Medium, 4 Low. Both Mediums FIXED `29835b9`:
- [x] `lmu_connected` monotonic (chip never showed "LMU offline" after LMU closed) → `_ensure_rest()` now probes liveness each call + clears on failure; regression test added.
- [x] LapList phase-group `#each` keyed by phase string (crash if a phase recurs non-contiguously) → keyed by group index.

Remaining LOW items (deferred, cheap when picked up):
- [ ] `on_track` stays true after first captured lap (pre-existing — `app.state.latest` never cleared; belongs with the capture/latest lifecycle, not the chip).
- [x] `format_laptime`/`formatTime` minute-boundary → DONE `4a1a2dc` (round to ms before splitting; both format.py + LapList; test added).
- [ ] CornerPanel brake shows "—" for a corner straddling S/F (apex just after line, brake just before) — theoretical, only if a mapped corner spans S/F.
- (Accepted ceiling) `/status` builds+probes LmuRest each poll while disconnected — localhost-cheap, self-heals.

## Security red-team (2026-09-04, server-side of `3bfb1b0..HEAD`)
Reviewer: 0 Critical/High/Medium, 2 Low. Verified CLEAN: SQL/DDL injection (params + hardcoded DDL + slug-sanitized track names), secret leakage (keyring-separated, PUT/settings whitelist, no traceback to client), cmaps unbounded-growth (bounded by on-disk track files), v6→v7 migration on partial/concurrent db, WS Origin/Host guards, CSRF on mutating JSON routes (preflight-protected).
- [x] `/status` per-poll REST probe amplification (introduced by 29835b9) — FIXED `e2672db` (rate-cap probe to once per REST_PROBE_TTL=2s; rate-cap + disconnect tests).
- [ ] (LOW, deferred) bodyless `POST /ingest` is CSRF-reachable cross-origin — negligible impact (idempotent local re-scan, response unreadable cross-origin); fixing needs a JSON-body/confirm gate + a coordinated frontend change for near-zero value. Only worth it as defense-in-depth if revisiting /ingest.

## Race-narrative — FIRST SLICE SHIPPED 2026-09-04 (`c9ccfc2` backend + `9d0f291` frontend)
Design decided by user: new race_sessions table; Coach+UI scope; racecraft-aware/context-led; live mPlace DEFERRED. Delivered:
- store schema v8 `race_sessions` table; `save_result_session` persists position/class_position/finish_status/incidents/penalties (were parsed then dropped); `race_session(sid)` getter.
- `GET /sessions/{sid}/race` endpoint (→ dict, or `{}` when absent).
- coach `get_race_result` tool + RACECRAFT paragraph in AGENTIC_SYSTEM_PROMPT (lead with the race story when a result exists; pace-coach otherwise).
- UI: `RaceStrip.svelte` in the Coach screen (P{finish} / class / finish status / incident+penalty counts; hidden when no race data). 303 py / 85 fe green.
STILL TODO on race-narrative: (a) DONE `f83881f` — live per-lap `mPlace` capture (schema v9 laps.position, guarded `player_position`, `session_position_arc`, arc line in `get_race_result`: "started P5, as low as P10, finished P3"); wants ONE rig glance to confirm mPlace reads sensibly (guard blocks bad data meanwhile). (b) prompt behavior needs a live-LLM eyeball; (c) `penalties[]` object shape → richer UI detail if wanted. (d) OPTIONAL: surface the live arc in the RaceStrip UI too (currently coach-only).

## Race-narrative SPIKE (2026-09-04) — feasibility + options (now partly built above)
**Data availability (the blocker's real answer):**
- **Result-log imports (post-race): the narrative data is ALREADY PARSED but DROPPED.** `results.py` `ResultSession` carries `position`, `class_position`, `finish_status`, `incidents: list[str]`, `penalties: list[dict]` (parsed at results.py:178-184), but `store.save_result_session` persists only laps and throws the rest away (same pattern as the pre-fix sectors). So a finished race's arc (start/finish place, incidents, penalties) is recoverable offline from the 30+ real result XMLs with NO rig — fully testable.
- **Live sessions:** per-lap position = `player.mPlace` (scoring struct, an int) — capturable like sectors (low-risk, but wants a rig eyeball). Live gaps/standings via `rest.get_player_standing()`. Per-lap position PROGRESSION (5th→10th→3rd) needs live mPlace capture; the result XML gives the finish + incidents, not necessarily per-lap places.

**Why not built yet (design decisions that are the USER's, per brainstorming gate):**
1. Schema: a new `race_sessions` table (position/incidents/penalties keyed by session_id) vs columns on an existing table? 
2. Coach tool shape: one `get_race_result` tool (place, incidents) + prompt guidance to weigh racecraft over pace? 
3. How much to weight racecraft vs lap-time in the coach's priorities, and tone (encouraging through a comeback).
4. UX: surface the arc in the UI (Sessions/Coach) or coach-only?

**Recommended first slice (once designed):** persist the already-parsed position/incidents/penalties in save_result_session (mirrors the sector fix — cheap, offline-testable) → add a `get_race_result` coach tool → prompt weighting. Live mPlace capture is a later, rig-verified slice. NOT started — needs the user's call on 1-4 (a short brainstorm).

## Investigation notes
- **Sector timing** (2026-09-04): NOT stored anywhere today. `LapRecord` (model_types.py) has no sector field; live capture (smm.py) never records sectors; `save_result_session` PARSES s1/s2/s3 (results.py) but drops them on insert; `laps` table has no sector columns. Build = a vertical slice: (1) add s1/s2/s3 columns via a `laps` schema migration, (2) capture live sectors in smm (`mLastSector1`/`mLastSector2` on vehicle scoring; s3 = lap_time − s1 − s2), (3) persist in `store.save` AND `save_result_session` (ingest already parses them), (4) return them from `session_laps` / `/sessions/{sid}/laps`, (5) render in LapList. Bounded but multi-layer — good single-subagent task.

## In-flight work interrupted by the spend limit (state, not user notes)
- [x] **Sidecar follows live track/car** — **DONE, committed `3bfb1b0` (2026-09-04, 284 passed).** Salvaged the stash, root-caused the 2 red tests (wrong monkeypatch target: patched `simcoach.__main__.read_session_track_car` but /status reads `simcoach.lmu.smm`'s), fixed, cleaned net-new lint. `create_app` now takes `rest_factory` + `map_resolver`; `_ensure_rest()` lazy-connects, `_map_for(track)` resolves per-track. Covers the Setup-persist backlog item (persist half). The Setup **filter + readable names** half is still TODO (see Setup tab section; partial `SetupPanel.svelte` still uncommitted).
- [x] **Window remember size/position + off-screen recovery** — DONE `40c18b1` (tauri-plugin-window-state + is_on_screen guard recenters on primary; cargo build + 4 tests green).

## Miscellaneous
- [x] ~~"Could not load settings."~~ — **DIAGNOSED 2026-09-04 00:51: not a data/config problem.** Caused by the broken uncommitted sidecar WIP (above) running in the live sidecar. On committed code `972013e`, GET /settings returns 200 fine. Fixed by stashing the WIP + rebuilding the sidecar. No further action unless it recurs on clean code.
- [x] ~~Sessions tab doesn't list any sessions.~~ — **Same root cause.** On committed code, GET /sessions returns the real session list (verified). Also resolved by stashing the broken WIP.
## In-app labeled track maps (2026-09-04) — SHIPPED
Telemetry-generated outline + auto-placed corner labels (the RECOMMENDED approach; owned data, no game assets). Delivered:
- capture records `pos_x`/`pos_z` (`smm.py`, via `mPos`) → flows to the lap `.npz` (no schema change) — `b50dc8f`-style guardless append; the builder filters non-finite.
- `simcoach/trackmap.py` `build_track_map(channels, corner_map, width=1000)` — pure world→screen (aspect-preserved, vertical-flipped), path downsampled ≤300 pts, corners placed at the sample nearest each `apex_dist`; `{}` when no usable position data.
- `GET /laps/{id}/map` (`2a43826`) → `{width,height,path,corners}` or `{}`; resolves the cmap per-track via `_map_for`.
- UI: `TrackMap.svelte` + a **Map** tab in `CornerSetupTabs` (`e617b8f`); `getLapMap` in api.ts. 320 py / 100 fe green.
- LIMITATION: maps only render for laps captured AFTER this change (existing laps have no position data — same as sectors/mPlace).
- [ ] TWEAK against LMU PNGs on the rig — the outline is the driver's racing line, so shape may want correcting vs the real circuit. USER: drive a lap, open a corner → Map tab, eyeball vs the game map; adjust if needed. (User: "we'll probably have to tweak them… we can use the lmu PNGs to correct.")

## Track map EDITOR + canonical shipped maps (2026-09-04) — SHIPPED
Plan: `docs/superpowers/plans/2026-09-04-track-map-editor.md`. Built by two fresh-context agents, reviewed + red-teamed, fixes folded. 338 py / 109 fe green.
- Map visual fixes `63dff74` (thicker line, non-selectable labels, S/F marker + direction arrow).
- Backend `1f5b60d` + hardening `9974ade`: `average_racing_line` (multi-lap mean on the lap_distance grid, nan-interp so short laps don't bias the tail), `direction_from_path` (path-curvature cross-product, closed-loop modulo indexing), extended `build_track_map` (dist[]+apex/start/end), `GET /laps/{id}/map?avg=0|1`, `PUT /laps/{id}/map` save (server re-derives direction — invariant held; atomic tmp write, traversal-guarded, name/count caps, allow_nan=False, start/end clamped, cmap-cache invalidated). tracks JSON gained `path`/`path_dist`/`sectors`/`length` (back-compat; all 16 shipped files still load).
- Frontend `fe26ca4` + fixes `f0677d5`: `TrackMapEditor.svelte` popout (drag corners/sectors snap to line, add/remove/rename/renumber, read-only direction, save→refresh; CTM pointer transform honors letterboxing), "Edit map" button on the Map tab, `getLapMap(id,avg)`/`saveLapMap`.
- Red-team: 0 Crit/High/Med. CSRF mitigated (PUT preflight + origin allowlist + Host guard). Traversal + server-derived-direction guards verified & tested.

Follow-ups after initial ship:
- [x] **Canonical per-track map** `876e8a7` — `GET /laps/{id}/map`: lap_id only selects the track; outline resolves frozen `path` in the file → else average the track's own pos laps → else `{}`; cached per (track,car), PUT invalidates. `/status` has_map reflects canonical availability. Fixes "map only shows on the position-captured lap" (any lap of the track now returns the canonical map).
- [x] **Map moved to a collapsible right pane** `b46c2bf` — canonical map is per-track, so it left the lap tabs: `TrackMapPane.svelte` on the right of the Coach view (collapsible, persists state); Map tab removed from `CornerSetupTabs` (Corners/Setup stay lap-scoped). Padding `dc3ec73`; per-track empty state `57b7caf`.

**DATA IS THE USER's** (2026-09-04): user builds the track/corner maps themselves in the editor. Do NOT hand-edit/copy corner-map JSON. (Reverted `52e268d` — I wrongly copied the full-GP map onto "Monza Curva Grande Circuit", which is a DISTINCT layout: T1=Roggia, no Prima Variante. See memory [[monza-layouts-distinct]]. Direction-match ≠ correct map.)

REMAINING on the editor (deferred, not blockers):
- [ ] **Import user-built maps as canonical/shipped** (user: "once i've built them, we can import them as canonical into the app") — the editor's PUT writes `tracks/<slug>.json` off the install dir; needs a clean path to PROMOTE maps the user builds (in their runtime/app-data) into the packaged canonical set (repo `simcoach/tracks/`). Design: export/import of a track-map file, or a "promote to canonical" action. Not started.
- [ ] **Direction real-data verify** (rig-gated) — `_LEFT_TURN_IS_CROSS_POSITIVE` pinned by synthetic test + indirect lat_g/steering check; `test_direction_monza_real` SKIPPED (no stored lap has `pos_x/pos_z` yet — capture started `2a43826`). RISK: shipped directions could be globally inverted until confirmed on a real pos lap.
- [ ] **Manual portrait-track drag check** — CTM pointer transform isn't unit-testable in jsdom; one manual drag on a portrait track (world zrange>xrange) confirms letterbox snapping.

## Edit-map "Save" button unstyled (2026-09-05, LOW)
The Save button on the edit-map screen has no styling (looks like a default/bare button). Give
it the app's primary-button style (match the other primary actions). Cosmetic. TrackMapEditor.svelte.

## Ribbon-preview rotation not persisted on first Save (2026-09-05, LOW)
Review finding: in the fresh-ribbon flow the preview rotates by the dialed `angle`, but
`mapcapture_save` (app.py ~665) writes `rotation: _track_rotation(track)` (the pre-existing
value), not the dialed `angle` — so a rotation set during a first-time ribbon preview is shown
but lost on Save (reappears only after a later `POST /laps/{id}/rotation`). Fix: thread the
dialed rotation into `saveRibbon`/`mapcapture_save`. Cosmetic; ribbon-first-save only.
(Also INFO, no action: analysis.py multi-apex midpoint boundary is inclusive both sides
[min-only, no effect]; a hand-edited file with UNSORTED apexes falls back gracefully — save/solver always sort.)

## Startup: blank window flash on the wrong monitor (2026-09-04, LOW)
On launch a blank white window flashes on the PRIMARY monitor before the saved window
position/state loads on the secondary monitor. Cause: Tauri creates the window VISIBLE at the
default position, then tauri-plugin-window-state moves it to the saved spot → visible flash.
Fix: set the window `visible: false` in `src-tauri/tauri.conf.json`, then `show()` after the
window-state position is restored AND the frontend has painted (an app-ready event), OR show a
splash screen during load. Rust/Tauri shell change. Low priority.

## Future feature: track corner trainer (2026-09-04)
As the user drives, call out the corner NAME in real time — overlay and/or voice — from our
corner map (`corner_at(lap_distance) → name`, already exists). A learning aid for unfamiliar
tracks. VR-friendly via voice (ties to the voice/Pit-Wall vision — see memory
[[pit-wall-voice-vision]]). Cheap first cut: on-track position → current/next corner name.
- **Extends into live on-track coaching** (user): beyond names, live per-corner cues as you
  approach (brake reference, gear, "trail less here") drawn from the per-corner metrics +
  driver profile. This IS the live-coaching mode of the 4-mode vision (voice-first, agentic,
  CrewChief-replacement). The corner map + ribbon + solver-derived apex/brake zones are the
  data foundation it consumes. Future; not scoped yet.

## Track map pane: collapsed edge should vanish, leaving only the caret tab (2026-09-05, LOW)
When the map pane is collapsed it still keeps a 20px border-left strip against the content. The
user wants NO edge when collapsed — just the floating caret button tab to reopen the pane.
Fix in `frontend/src/lib/TrackMapPane.svelte`: drop the `.map-pane.collapsed` width/border so the
strip contributes no layout edge; render the reopen caret as an overlay tab (absolute/floating)
rather than a bordered column. Low priority, cosmetic.

## Corner detection under-counts on generated maps (2026-09-05)
On a generated Algarve ribbon the solver produced ~4 corners vs the real 15 (user
added the rest by hand). raceline.solve_corners / the curvature segmentation is
under-segmenting — likely too-coarse smoothing or too-high a curvature threshold,
merging adjacent corners. Improve corner segmentation (adaptive threshold, min-gap,
per-apex splitting) so first-pass detection is closer to the true count. Manual add
works as a fallback today. Medium priority — quality, not blocking.

## Ribbon review — deferred findings (2026-09-05, from 60c72c2 review)
- MEDIUM: edge classification takes the single most-extreme lap per side
  (ribbon.py argmin/argmax of per-lap mean lateral offset), so a lopsided weave
  lap that spends more time on one side than the dedicated edge lap can be picked
  as the boundary. The <20%-spread fallback only covers "all laps similar", not
  this case. Fix idea: use the lateral ENVELOPE (per-station min/max across all
  laps) for the edge rather than one lap's path, or let the user designate edge
  laps. (Algarve came out well, so not currently biting.)
- LOW: rebuilt corners lose direction corroboration — the saved raw cloud is
  position-only (pos_x/y/z + lap_distance), so on /rebuild there's no steering/
  lat_g to vote → confidence forced "high" even for corners the live build
  flagged low/mismatched. Curvature still authoritative; only the confidence
  label regresses. Persist steering/lat_g in the cloud to restore it.
- NIT: reconstruct_surface bins bound laps by d/global-max-len, so a shorter
  bound lap's top bins go empty + wrap-interpolated; a stray overshoot/out-lap in
  the buffer would thin the edges (no clean-lap filter on the mapping buffer).

## Ribbon consumers: replay overlay + 3D track viewer (2026-09-05)
The reconstructed ribbon is the shared foundation for two future viewers. Both
can REUSE the persisted raw captures (`<data_dir>/captures/<slug>.npz`, pos_x/y/z
+ lap_distance per lap) — no re-driving — via the existing /mapcapture/rebuild path.

- REPLAY overlay (à la Coach Dave Delta): the ribbon is the track canvas; each
  lap's actual driven line comes from the per-lap RACE telemetry
  (`telemetry/<lap_id>.npz`, full-rate, keyed on lap_distance), NOT the mapping
  captures. GAP to close: expose the world->screen transform params
  (xmin/zmin/scale/rotation/centroid) or a world->screen mapping endpoint, so a
  lap's world (x,z) samples project onto the same ribbon transform. build_track_map
  currently bakes+returns screen coords but not the raw params. lap_distance
  alignment + shared transform (incl. rotation) already satisfy the rest.
- 3D track viewer: the ribbon has plan-view geometry (center/edges/width) + per-
  station BANK (cross-slope), but NOT the longitudinal elevation profile, so it'd
  render flat-but-banked (no hills). GAP: add a per-station center elevation
  (fit pos_y vs lap_distance) to the reconstruct_surface output. pos_y is already
  captured and flows through reconstruction — it's an output+fit addition, and can
  be recomputed from saved captures on Rebuild (no re-drive). Then each station has
  x, z, y_center, width, bank -> enough to extrude a 3D banked ribbon mesh.
  Note: only tracks with a saved <slug>.npz get 3D from captures; tracks mapped
  before raw-capture-persist would need one re-drive to record the cloud.
