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