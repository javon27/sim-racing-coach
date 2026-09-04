# v1.0 Standalone UI — Design

**Status:** decisions locked 2026-09-02; amended 2026-09-02 with installer/no-env-vars +
config-and-settings scope (decisions 5–6, Settings section). Supersedes
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
5. **Ships as a shareable installer.** The app must run on a fresh Windows machine (LMU is
   Windows-only, so every user is on Windows) with **no reliance on system environment
   variables** — env vars are honored only as developer overrides. All user configuration,
   including the Anthropic API key, is entered in-app and persisted by the app itself.
6. **API access is bring-your-own-key** for v1: each user enters their own Anthropic key.
   The `local`/offline model provider seam (`make_model`) stays open as the future keyless
   path; the Settings design accommodates it without building it now.

## Settings & configuration (installer-ready)

The core already has `simcoach/config.py` (`load_config()`, a flat JSON dict, today just
`units`) explicitly built for "the future desktop UI's settings page." v1 grows it into the
single configuration layer both the terminal harness and the desktop app read/write.

- **Config precedence:** `env override > config file > default`. Env vars keep working for
  developers but nothing *requires* them.
- **Paths move to an OS app-data dir** (Windows `%APPDATA%\SimCoach\`), env-overridable via
  `SIMCOACH_DATA_DIR`. The config JSON, the SQLite DB, and the telemetry `.npz` files all
  live there so the installed app works without CWD assumptions. (Today `config.py`,
  `store` paths, and `__main__` all assume CWD-relative paths.)
- **Secrets never touch the JSON.** The Anthropic API key is stored via the `keyring`
  library in **Windows Credential Manager** (per-user DPAPI encryption, no OS UI — entry
  stays in the app's own field). `make_model` is refactored to read the key from this layer
  instead of `os.environ`.
- **`config.py` gains `save_config()`** and new fields (below). Unknown/malformed files
  still fall back to defaults rather than crashing (existing behavior, preserved).

### Settings screen

Reached from the nav rail's gear icon (present in the prototype, no screen behind it yet).
Groups:

1. **AI / Coach** — Anthropic API key (masked field → keyring, with a "test key" check that
   makes one cheap validation call); Model selector (Opus / Sonnet / Haiku → `make_model`
   `model=`). No provider dropdown yet — anthropic-only until the local model lands.
2. **Driving** — Input device (Wheel / Controller → coach `input_device`).
3. **Display** — Units (Metric / Imperial → `config.units`); Theme (System / Light / Dark);
   Corner naming (Names / Numbers → new config flag, plumbed into corner formatting).
4. **Driver profile** — braking style / balance preference / risk tolerance
   (`driver.DriverProfile`), fed into coach personalization. (Schema is still a rough
   educated guess; editing it here is where it gets refined.)
5. **Data** — storage location (view + "open folder"); **Reset / clear data** — a
   confirm-gated destructive control that wipes stored laps, chats, and telemetry.
6. **About** — app version, validated LMU build, "API key set ✓" indicator.

Manual tier override is explicitly **out** — tier stays auto-derived from the live setup's
adjustable flags.

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
- `GET /settings` → current config (units, input_device, model, corner_naming, driver
  profile, data dir, app/LMU versions, `api_key_set: bool`) — the key itself is never returned
- `PUT /settings` → persist config changes (JSON via `save_config`; key routed to keyring)
- `POST /settings/api-key` → store the key in keyring; `POST /settings/test-key` → one cheap
  validation call, returns ok/error
- `DELETE /data` → confirm-gated wipe of laps, chats, telemetry (the Settings reset control)

Corner identity in every payload is authoritative (map + telemetry); the frontend never
infers it. The coach-guardrail prompt stays server-side.

## Build phases

1. **FastAPI over the core + config layer + chat/session data-model migration** — the
   read/settings endpoints above (non-streaming first); the installer-ready config layer
   (app-data paths, `save_config`, keyring secrets, `make_model` reading from config); and
   the `chat_id` migration. Unit-tested against the core with a fake model + fixture store.
   No frontend yet.
2. **Streaming agentic `WS /coach`** — add the streaming path to the `Model` interface +
   Anthropic streaming with tool use; server relays `tool`/`text`/`done`.
3. **Tauri shell + Svelte scaffold** — window, sidecar launch/teardown, session bar + lap
   list wired to the API.
4. **Coach chat + corner breakdown + setup panel** — the interactive core.
5. **Sessions screen + trends + Settings screen** — browse, filters, best-lap chart,
   session→chat deep-link; and the Settings screen wired to the config endpoints.
6. **Polish + packaging** — layout, theming, empty/loading/error states, and the standalone
   Windows installer (Tauri bundle + Python sidecar; first-run with no key set).
7. *(later)* Pit Wall feasibility spike → Pit Wall screen; then Replay overlay.

## References & external inspiration

**lmu-steward** (github.com/misirlu13/lmu-steward) — a same-domain LMU desktop companion (Electron/TS)
for race *stewarding/incident analysis*, not coaching. We take UX + data-source + distribution
inspiration only; architecture stays Tauri + Svelte + Python (lighter than their Electron). Actionable:
- **Settings UX (Phase 5):** toggles auto-save; secret/path fields (API key, data dir) require an
  explicit "Save". Adopt.
- **Sessions UX (Phase 5):** expandable session/weekend cards → rows, tabbed views, toggle filters with
  badge counts, inline `⋮` row actions. Fold into the Sessions screen.
- **Packaging (Phase 6):** Windows installer + export/import `.zip` preserving pairings; CSV/MD/JSON
  export of session/chat data. Reference for the installer + reset/export controls.
- **IN SCOPE (user-approved 2026-09-02) — result-log ingest workstream (own spec→plan→build):** parse
  LMU's **`.xml` result logs** to **backfill session/lap history without live capture**, feeding the
  same store/`/sessions`/`/laps` the UI reads. Schema VERIFIED against real files:
  - **Location:** `<LMU>/UserData/Log/Results/*.xml` (Steam sample: `S:\SteamLibrary\...\Le Mans Ultimate\UserData\Log\Results`); ~1358 files, 400–800KB each, back to 2025. Filename `YYYY_MM_DD_HH_MM_SS-NN{P|Q|R}n.xml`.
  - **Format:** `rFactorXML → RaceResults`: session meta (`TrackVenue`, `TrackCourse` = exact layout [feeds track_lookup], `TrackEvent`, `TrackLength`, `GameVersion`, `DateTime`/`TimeString`, `VehiclesAllowed`) → `<Race>`/qualify/practice session → `<Stream>` (`ChatMessage`, `Penalty`[Driver/Penalty/Time/Reason/et], `Incident`[contact text+magnitude], `TrackLimits`[warnings/points]) → per-`<Driver>` (`Name`, `VehName`, `CarClass`, `CarNumber`, `GridPos`, `Position`, `ClassPosition`, `FinishStatus`, `FinishTime`, `BestLapTime`, `Pitstops`, `isPlayer`, `DNFReason`, nested `<Lap>`+`<Sector>`).
  - **Player identification (VERIFIED — `isPlayer` is USELESS):** in MP result files `<isPlayer>1</isPlayer>`
    is set for EVERY driver (43/43 in the sample), so it cannot identify the user. Instead: read the XML's
    `<PlayerFile>` (e.g. "Settings") → load `UserData/player/{PlayerFile}.JSON` → its `"Player Name"` field
    (sample: "Jayson G") → match against `<Driver><Name>`. The result file names its own active profile, so
    this is robust across multiple profiles. Add a Settings override; cache the resolved name.
    CAVEAT FROM REAL DATA: the profile name ("Jayson G") differs from older driver names ("Jayson Gallardo")
    — display names change over time, BOTH appear across the corpus — so match an **alias SET** (profile name ∪
    Settings `lmu_player_names`), not one name. Per-lap **sectors are `<Lap>` attributes `s1/s2/s3`** (NOT `<Sector>`
    children = stream best-sector announcements). Validated: 66 real files parse, 0 exceptions (practice/qualy/race/endurance).
  - **Extract (player-scoped):** for the matched player `<Driver>` → a session (track/car/date/type) + its laps
    (time+sectors) + best + finishing position + the player's incidents/penalties/track-limits. Backfills SUMMARY
    rows; **no telemetry** (corner coaching stays on `.npz`). Dedup on file (path+mtime/hash); one-time backfill + incremental for new files.
  - **Path detection (user requirement):** auto-detect the LMU `UserData` dir across **Steam** (`libraryfolders.vdf`/registry — library can be any drive), **Epic**, and standalone; plus a **Settings override**. Ties into the config layer + Settings screen.
  - **`.Vcr` replays = OPAQUE HANDLE only** — indexed/linked to open in LMU, NOT decoded (proprietary binary; telemetry stays on `.npz`).
  - Slots after / parallel to the UI phases; does not block Phase 3.
- Their confidence-scored *capture↔replay* linking validates our decoupled **chat↔race-session** seam.

## Non-goals for v1.0

Pit Wall (deferred, spike-gated), Replay overlay, voice, cloud sync, multi-user, the MCP
server, and CrewChief-style live in-ear coaching.
