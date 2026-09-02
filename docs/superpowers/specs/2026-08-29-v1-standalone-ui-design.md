# v1.0 Standalone UI — Design

**Status:** decisions locked 2026-09-02 (revised from the 2026-08-29 draft). Supersedes
the deferred "HTTP server + web view" line item as the v1.0 frontend. The MCP-server
approach is parked as a viable fallback, not this cut. **Next step: implementation plan
(writing-plans), then build.**

**Goal:** a standalone desktop app with a purpose-built UX for between-laps coaching —
replacing the terminal REPL (a throwaway dev harness) as the real interface. Reuses the
existing UI-agnostic Python core unchanged.

**Reference prototype:** `docs/design/v1-ui-prototype.html` — a complete vanilla
HTML/JS/SVG mockup ("Pit Wall") with the full design system (Chakra Petch / IBM Plex
fonts, light/dark/system theming, tokens). It is the visual source of truth; its markup,
CSS, and SVG port to Svelte components largely as-is.

## Locked decisions (2026-09-02)

1. **v1 scope = Coach + Sessions.** The Pit Wall (live map + timing tower) is deferred to
   a later phase and gets a **scoring-feasibility spike first** (live all-car standings,
   positions, sector/blue flags — the scoring shared-memory tail is flagged unreliable).
2. **Frontend = Svelte + Vite** (lighter than React, closest to the prototype's plain-HTML
   feel; still a real component framework).
3. **Chat is continuous, decoupled from race session.** One ongoing conversation can span
   multiple races; race sessions are *context* the coach references, not chat containers.
4. **Coach is agentic** — streaming must surface tool activity + streamed answer text (see
   below); this postdates the original draft and replaces its single-reply `WS /coach`.

## Architecture

- **Shell:** Tauri (Rust) native window hosting the web frontend.
- **Frontend:** Svelte + Vite SPA.
- **Backend:** the Python core exposed via a local **FastAPI + websocket** server that
  Tauri launches as a sidecar on startup and shuts down on exit. Binds to localhost only.
- **Transport:** HTTP for reads (sessions, laps, analysis, setup, chats); websocket for
  the **streamed agentic coach** and live session-bar status.
- **Core reuse:** `smm`/`LmuSource` (capture), `store` (laps), `analysis`, `coach`,
  `coach_tools`, `mapbuild`, `track_lookup`/`car_lookup`, `tiers`, `fuel` — unchanged. The
  server is a thin adapter.

## Data model — continuous chat (decoupled from race session)

The one real storage change. Today `coach_chat` keys on the race `session_id`, conflating
chat with race. v1 separates them:

- **`chat`** — a conversation with its own id; continuous, spans races.
- **Message context stamp** — each message records the active `session_id` and selected
  `lap_id` *at ask-time*, so answers stay reproducible and the UI can show "asked about
  Lap 9, Le Mans," and Sessions rows can deep-link to the messages tied to them.
- **Race sessions unchanged** — laps still group by `session_id`; a race session is
  context, not a container.
- **Migration** — add `chat_id` to `coach_chat` (schema bump via PRAGMA `user_version`);
  keep `session_id`/`lap_id` as the per-message context; wrap existing rows into one
  legacy chat so no history is lost.

## Core interaction model

Capture runs in the background; the driver asks questions between laps and gets prioritized
coaching. Selecting a lap (rail) or a session (Sessions screen) sets the chat's **active
context**; new questions reference it.

## v1.0 views

1. **Coach chat** (hero) — between-laps Q&A, streamed. Pre-lap questions work (general
   guidance); lap-specific once a lap is banked. Input-device + tier context flow through
   as in the core.
2. **Session bar** — resolved track/car (raw entry shown), tier / legal adjustments, live
   fuel + virtual-energy.
3. **Lap list → corner breakdown** — captured laps with times/validity + sector deltas;
   select one to see per-corner metrics (min speed, brake point, trail-brake) and deltas
   vs. best, and to set it as the chat's active context.
4. **Setup panel** — the coaching-relevant filtered set, params the latest answer
   referenced highlighted.
5. **Sessions screen** — cross-session browse + best-lap trends + game/track/car filters.
   A row **deep-links into the continuous chat**, anchored (scrolled + highlighted) to the
   messages asked against that session, with a "show full chat" affordance; a session with
   no chat yet lands you in the chat with it as active context. Also sets the lap rail to
   that session.
6. *(Deferred)* **Pit Wall (live)** — track map + timing tower; needs the feasibility spike.
7. *(Deferred)* **Replay / overlay** — side-by-side from the `.npz` lap-distance data.

## Agentic streaming

`Coach.ask_agentic` runs a tool-use loop (`model.run(...)`); the `Model` interface has
`complete` + `run` but **no streaming**. v1 adds a **streaming agentic path** that yields
events instead of returning a string:

- `tool` events as the coach pulls data → the prototype's "reading your lap 9…" indicator
  becomes real tool activity.
- `text` deltas for the answer → tokens stream in (the "feels modern" UX that motivated
  v1.0).
- `done` marker.

The server relays these over `WS /coach`. **Lazy fallback to derisk:** emit tool-activity
events but deliver the answer as one block first; add token-streaming after.

## Server API (thin adapter over the core)

- `GET /sessions` → race sessions (track/car/date/type/laps/best), for the Sessions browse
- `GET /sessions/{id}/laps` → laps in a session `{id, lap_time, valid, ts}`
- `GET /laps/{id}/analysis` → per-corner metrics + deltas
- `GET /setup` → filtered current setup
- `GET /chats`, `GET /chats/{id}` → chat list + one chat's messages (with context stamps)
- `WS /coach` → client sends `{question, session_id?, lap_id?}`; server streams
  `tool` / `text` / `done` events
- `WS /telemetry` → live status ticks (fuel/energy/on-track) for the session bar

Corner identity in every payload is authoritative (map + telemetry); the frontend never
infers it. The coach-guardrail prompt stays server-side.

## Build phases

1. **FastAPI over the core + chat/session data-model migration** — endpoints above
   (non-streaming first) + `chat_id` migration; unit-tested against the core with a fake
   model + fixture store. No frontend yet.
2. **Streaming agentic `WS /coach`** — add the streaming path to the `Model` interface +
   Anthropic streaming with tool use; server relays `tool`/`text`/`done`.
3. **Tauri shell + Svelte scaffold** — window, sidecar launch/teardown, session bar + lap
   list wired to the API.
4. **Coach chat + corner breakdown + setup panel** — the interactive core.
5. **Sessions screen + trends** — browse, filters, best-lap chart, session→chat deep-link.
6. **Polish** — layout, theming, empty/loading/error states, packaging a standalone build.
7. *(later)* Pit Wall feasibility spike → Pit Wall screen; then Replay overlay.

## Non-goals for v1.0

Pit Wall (deferred, spike-gated), Replay overlay, voice, cloud sync, multi-user, the MCP
server, and CrewChief-style live in-ear coaching.
