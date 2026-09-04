# v1 Phase 5 — Sessions + Settings screens — Implementation Plan

> **For agentic workers:** superpowers:subagent-driven-development. Checkbox steps.

**Goal:** Fill the two remaining nav screens — **Sessions** (browse/filter/trends + deep-link into the chat) and **Settings** (API key, model, units, driver profile, data reset, LMU dir + player-name aliases, and the "Import history" button). Backends all exist (Phase 1 + the ingest workstream).

**Spec:** `docs/superpowers/specs/2026-08-29-v1-standalone-ui-design.md` (view 5 + the "Settings & configuration" section + the lmu-steward references note: toggles auto-save, secret/path fields need explicit Save). Prototype `docs/design/v1-ui-prototype.html` (`#s-sessions` markup + JS: filters, `svgChart`, session list; and the Settings groupings).

## Global Constraints
- Frontend `frontend/` Svelte 5.56, vitest, `api.ts`. Backend endpoints (already built): `GET/PUT /settings`, `POST /settings/api-key`, `POST /settings/test-key`, `DELETE /data`, `POST /ingest`, `GET /sessions`, `GET /status`. Backend green bar 268; frontend grows from ~41.
- Settings UX (from spec/lmu-steward): toggles/selects **auto-save** (PUT on change); the **API key** and **data/LMU paths** need an explicit action (Save / Test / Import). `GET /settings` never returns the key — show `api_key_set` state only.
- No secrets in logs/UI beyond `api_key_set: bool`. Corner/coach logic stays server-side.
- TDD, commit per task, current session commit trailer.

---

### Task 0: Extend `api.ts` (do FIRST — screens depend on it; avoids concurrent api.ts edits)
Add to `frontend/src/lib/api.ts` (reuse the `request()` helper; check the real endpoint shapes in `simcoach/server/app.py`):
- `putSettings(cfg: Record<string, unknown>): Promise<SettingsResponse>` → `PUT /settings`
- `setApiKey(key: string): Promise<{api_key_set: boolean}>` → `POST /settings/api-key`
- `testApiKey(): Promise<{ok: boolean, error: string|null}>` → `POST /settings/test-key`
- `clearData(): Promise<{cleared: boolean}>` → `DELETE /data` with body `{confirm: true}`
- `ingestHistory(): Promise<{scanned:number,imported:number,skipped:number,unmatched:number,errors:number}>` → `POST /ingest`
- Extend `SettingsResponse` type to include the config fields (units, input_device, model, corner_naming, driver, api_key_set, data_dir, app_version, lmu_build, lmu_dir, lmu_player_names).
- [ ] Test each in `api.test.ts` (mock fetch → correct method/path/body). `npm test` + build. Commit `feat(ui): api client for settings/ingest/data endpoints`.

---

### Task 1 (Sessions): screen — filters + session list + stat cards
`frontend/src/lib/SessionsScreen.svelte` + test. Consumes `api.getSessions()`. Port the prototype `#s-sessions`: Game/Track/Car filter dropdowns (car grouped by class, "All" everywhere), a filtered session list (`.srow`: date, track·config, car, type, laps, best), and the stat cards (personal best, improvement, sessions, consistency). Empty → "No sessions — drive or import history." Mount into the Sessions nav screen slot in `App.svelte`.
- [ ] Test: mock sessions → rows + filters render; filtering narrows the list; empty state. Commit `feat(ui): sessions screen — filters + list + stats`.

### Task 2 (Sessions): best-lap trend chart
Add the SVG best-lap-by-session chart (port the prototype `svgChart` multi-series) into SessionsScreen, driven by the game+track filter (placeholder until a game+track are chosen, per the prototype). Own component `TrendChart.svelte` + test.
- [ ] Test: series data → an `<svg>` with the right point count renders; <1 point / no selection → placeholder. Commit `feat(ui): sessions best-lap trend chart`.

### Task 3 (Sessions): row → deep-link into the coach chat
A session row click navigates to the Coach screen and sets the active session context (the Coach screen's `selectedSessionId`/lap rail reflect that session; the chat is anchored to it). Wire via a shared app-level `activeSession` state in `App.svelte` (Sessions sets it + switches screen; Coach reads it).
- [ ] Test: clicking a session row switches to Coach and sets the active session. Commit `feat(ui): sessions row deep-links into coach chat`.

### Task 4 (Settings): config form (auto-save)
`frontend/src/lib/SettingsScreen.svelte` + test. Consumes `api.getSettings()` + `api.putSettings()`. Groups: **Display** (units metric/imperial, theme system/light/dark [reuse `theme.ts`], corner naming names/numbers), **Driving** (input device wheel/controller), **AI** (model opus/sonnet/haiku selector). **Driver profile** (braking_style/balance_preference/risk_tolerance text/selects). **About** (app_version, lmu_build, "API key ✓/not set"). Toggles/selects AUTO-SAVE via `putSettings` on change (debounce/optimistic). Mount into the Settings nav slot.
- [ ] Test: renders current settings; changing units calls putSettings with the new value; renders api_key_set state. Commit `feat(ui): settings screen — display/driving/AI/profile (auto-save)`.

### Task 5 (Settings): API key + data + LMU import
Add to SettingsScreen: **AI/API key** — a masked input + "Save key" (`setApiKey`) + "Test key" (`testApiKey`, shows ok/error). **Data** — show `data_dir`, a "Reset / clear data" button (confirm dialog → `clearData()`). **LMU history** — an `lmu_dir` path field + `lmu_player_names` alias list (edit → `putSettings`) + an "Import history" button (`ingestHistory()` → show the summary "imported N / unmatched M"). The key/paths/import are EXPLICIT actions (not auto-save).
- [ ] Test: entering a key + Save calls setApiKey; Test-key shows the result; Reset confirms then calls clearData; Import calls ingestHistory + shows the summary. Commit `feat(ui): settings — API key, data reset, LMU import`.

## Non-goals: Pit Wall (spike-gated); packaging (Phase 6).

## Execution notes
Task 0 FIRST (api.ts, sequential). Then Sessions (1→2→3) and Settings (4→5) are two independent lanes — can run in parallel (disjoint component files; App.svelte edits: Sessions mount [T1], Settings mount [T4], deep-link state [T3] — sequence the App.svelte-touching tasks or have each mount its own screen slot to minimize overlap). Light review per task; a focused review on Task 5 (secret handling in the key field — never log/echo the key). Fresh executor per task.
