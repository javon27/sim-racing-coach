# v1 Phase 4 — Coach chat + corner/setup panels — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Checkbox steps.

**Goal:** Make the desktop app an actual coach — a streaming chat wired to `WS /coach`, plus the corner-breakdown and setup panels for the selected lap. This is the interactive core; the WS backend (Phase 2) + read endpoints (Phase 1) already exist.

**Spec:** `docs/superpowers/specs/2026-08-29-v1-standalone-ui-design.md` (v1 views 1,3,4) + prototype `docs/design/v1-ui-prototype.html` (the `.chat`, `.thread`, `.composer`, `.panel` Corners/Setup markup + `.md` answer styling — port largely as-is).

## Global Constraints
- Frontend: `frontend/` Svelte 5.56 runes, vitest (`resolve.conditions:['browser']`), `api.ts` client. Backend green bar = 265; frontend = 16.
- Coach/analysis logic stays server-side; corner identity comes from the API. No LLM calls in JS.
- WS endpoint: `ws://127.0.0.1:8765/coach` (or derive from the API base). Client sends `{question, session_id?, lap_id?}`, receives `{type:"tool"|"text"|"done"|"error", ...}` (text events are token deltas; accumulate).
- Reuse the Phase-3 patterns: `withRetry` for HTTP, keep-last-good on transient poll failure, `onDestroy` cleanup.
- TDD; commit per task; commit trailer = the current session's Co-Authored-By + Claude-Session lines.

---

### Task 1: WS coach client (`frontend/src/lib/coach.ts`)
**Interfaces:** a small class/store `CoachStream` — `ask(question, {sessionId?, lapId?})` opens a WebSocket to `${WS_BASE}/coach` (WS_BASE from `VITE_API_BASE` swapped to ws://, default `ws://127.0.0.1:8765`), sends the JSON, and exposes reactive state: `phase` (`idle`/`connecting`/`tool`/`streaming`/`done`/`error`), `toolActivity` (last tool name+input, for the "reading your lap…" indicator), `answer` (accumulated text deltas), `error`. Closes the socket on `done`/`error`. One in-flight ask at a time (ignore/replace).
- [ ] Test (vitest): mock `WebSocket` (a fake that the test drives — emit `tool`, two `text` deltas, `done`); assert `answer` accumulates both deltas, `toolActivity` set on the tool event, `phase` transitions idle→…→done. Test an `error` event sets phase=error. Test send payload shape.
- [ ] Implement; `npm test` + `npm run build` green. Commit `feat(ui): WS coach streaming client`.

### Task 2: Coach chat panel (`ChatPanel.svelte`)
**Consumes:** `CoachStream` (Task 1); `api.getChats()`/`api.getChatMessages(chatId)` for history; the active `lapId`/`sessionId` (props from the Coach screen). **Produces:** the chat hero per the prototype — a thread of you/coach messages, a composer (auto-grow textarea + Send, Enter-to-send), a streaming coach bubble showing the tool indicator then the streamed markdown answer, and a hint line ("Lap 9 selected"). On load, fetch the latest chat's messages and render them; a new question appends + streams. Render the coach answer markdown (a tiny safe renderer or a small lib — headings/lists/bold/code per the prototype `.md`; do NOT pull a heavy dep, keep it minimal).
- [ ] Test: mock `CoachStream` + api; assert sending a question renders a "you" msg then a streaming coach bubble that fills with the accumulated answer; assert history loads.
- [ ] Implement + wire into the Coach screen (hero area). Commit `feat(ui): coach chat panel (streamed)`.

### Task 3: Corner breakdown panel (`CornerPanel.svelte`)
**Consumes:** `api.getLapAnalysis(lapId)`. **Produces:** the prototype's Corners table (dir arrow + T# name, min speed, brake pt, trail, Δ best), for the selected lap; empty/none states. Corner identity straight from the payload.
- [ ] Test: mock analysis payload → rows render with correct names/dirs/deltas; no-lap → empty state.
- [ ] Implement. Commit `feat(ui): corner breakdown panel`.

### Task 4: Setup panel + Corners/Setup tabs (`SetupPanel.svelte`, tabs)
**Consumes:** `api.getSetup()`. **Produces:** the prototype's Setup rows (grouped, ref-highlight optional) + the tab switcher (Corners | Setup) in the rail panel area. `/setup` is `{}` when LMU/rest absent → "no setup" state.
- [ ] Test: mock setup dict → rows render; empty → state. Tab toggle switches panels.
- [ ] Implement + wire the tabs into the rail. Commit `feat(ui): setup panel + Corners/Setup tabs`.

### Task 5: Coach screen wiring (lap selection → chat context + panels)
**Produces:** the LapList `select` event sets the active `lapId`/`sessionId`; that flows to ChatPanel (context for the next question + the hint line) and CornerPanel (which lap to analyze). Assemble the full Coach screen: session bar (top) / laps rail + Corners·Setup tabs (left) / chat hero (right), per the prototype layout.
- [ ] Test: selecting a lap updates the corner panel's analyzed lap + the chat hint. Full Coach screen renders.
- [ ] Implement. Commit `feat(ui): wire lap selection into chat context + corner panel`.

## Non-goals (Phase 5): Sessions screen, Settings screen, Pit Wall.

## Execution notes
Order 1→2→…→5 (2 depends on 1; 5 depends on 2-4). Light review per task (frontend, vitest+build gated); a focused review/red-team on Task 1 (the WS client — reconnect, delta accumulation, socket cleanup). Fresh executor per task.
