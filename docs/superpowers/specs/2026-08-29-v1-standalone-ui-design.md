# v1.0 Standalone UI — Design

**Status:** draft for review (2026-08-29). Supersedes the deferred "HTTP server + web
view" line item as the v1.0 frontend. The MCP-server approach (expose the core as MCP
tools for any LLM frontend) is parked as a viable fallback, not this cut.

**Goal:** a standalone desktop app with a solid, purpose-built UX for between-laps
coaching — replacing the terminal REPL (a throwaway dev harness) as the real
interface. Reuses the existing UI-agnostic Python core unchanged.

## Architecture

- **Shell:** Tauri (Rust) native window hosting a web frontend.
- **Frontend:** web SPA — **React** (default; see Open Decisions) — for the UX ceiling
  and component ecosystem.
- **Backend:** the Python core exposed via a local **FastAPI + websocket** server that
  Tauri launches as a sidecar on startup and shuts down on exit. Binds to localhost only.
- **Transport:** HTTP for reads (session, laps, analysis, setup); websocket for
  **streamed coach replies** and live session status. Streaming lives here — this is
  what the terminal couldn't do and why UX polish was deferred to v1.0.
- **Core reuse:** `smm`/`LmuSource` (capture), `store` (laps), `analysis`, `coach`,
  `mapbuild`, `track_lookup`/`car_lookup`, `tiers`, `fuel` — all unchanged. The server
  is a thin adapter; the `Coach` LLM call moves behind `WS /coach` with streaming.

## Core interaction model

The proven loop: capture runs in the background; the driver asks questions between laps
and gets prioritized coaching. The UI makes that loop visible and adds context panels.

## v1.0 first-cut views

1. **Coach chat** — the between-laps Q&A, streamed token-by-token. Pre-lap questions
   work (general guidance); lap-specific once a lap is banked. Input-device + tier
   context flow through as they do in the core.
2. **Session bar** — resolved track/car (with the raw entry shown), tier / legal
   adjustments, live fuel + virtual-energy status.
3. **Lap list → corner breakdown** — captured laps with times/validity; select one to
   see per-corner metrics (min speed, brake point, trail-brake) and deltas vs. best.
4. **Setup panel** — current setup (the coaching-relevant filtered set), with the
   params the latest answer referenced highlighted.
5. *(Deferred to the next phase)* **Replay / overlay** — side-by-side lap comparison
   from the `.npz` lap-distance data; the data is already captured for it.

## Server API (thin adapter over the core)

- `GET /session` → `{track, track_map, car, raw_entry, tier, legal_adjustments, fuel, energy}`
- `GET /laps` → list `{id, lap_time, valid, ts}`
- `GET /laps/{id}/analysis` → per-corner metrics + deltas
- `GET /setup` → filtered current setup
- `WS /coach` → client sends `{question}`; server streams the reply, then a done marker
- `WS /telemetry` → live status ticks (fuel/energy/on-track) for the session bar

Corner identity in every payload is authoritative (from map + telemetry); the frontend
never infers it. The coach-guardrail prompt stays server-side in `coach`.

## Build phases

1. **FastAPI server over the core** — endpoints above (non-streaming first), unit-tested
   against the core with a fake model + fixture store. No frontend yet.
2. **Streaming `WS /coach`** — add a streaming path to the `Model` interface
   (`stream_complete`) + Anthropic streaming; server relays tokens.
3. **Tauri shell + frontend scaffold** — window, sidecar launch/teardown, session bar +
   lap list wired to the API.
4. **Coach chat + corner breakdown + setup panel** — the interactive core.
5. **Polish** — layout, theming, empty/loading/error states, packaging a standalone build.
6. *(next)* Replay overlay.

## Open Decisions (confirm before/early in the build)

- **Frontend framework:** React (default) vs Svelte (lighter, less boilerplate). Both
  fit; React chosen for ecosystem/components unless you prefer Svelte.
- **Live telemetry view in v1.0?** Deferred by default (first cut = coaching loop). The
  `WS /telemetry` status feed still powers the session bar; a full live dashboard is
  post-v1.0 unless you want it in.
- **Model streaming scope:** phase 2 adds `stream_complete`; the non-streaming terminal
  path stays as a fallback.

## Non-goals for v1.0

Voice, cloud sync, multi-user, the MCP server, and CrewChief-style live in-ear coaching
remain out of this cut.
