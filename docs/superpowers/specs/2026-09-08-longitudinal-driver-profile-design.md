# Longitudinal Driver Profile — Design

Date: 2026-09-08
Status: Design approved (brainstorm), pending spec review → implementation plan

## Problem

The coach has no memory of *who it's coaching*. `DriverProfile` (`simcoach/driver.py`)
is a dataclass with a `to_prompt_text()` renderer, plumbed as an optional
`driver_profile=` param through `Coach.ask_agentic`/`build_prompt`/`_augment_system`
and `CoachSession` — but:

1. **It never reaches the shipped coach.** The static fields
   (`braking_style`/`balance_preference`/`risk_tolerance`) are persisted in settings
   (`config.py` `driver` block) and editable in the UI, but `simcoach/server/app.py`
   has zero `driver_profile` references — the WS `/coach` worker calls
   `ask_agentic_stream(...)` without it. Even the static seed is dropped.
2. **There is no evolving part.** `learned_notes` exists on the dataclass but is never
   populated, persisted, or updated. `store.py` has no profile storage.

This is Mode 4 of the vision (`docs/vision.md`): "a coach-maintained, evolving model of
the driver … fed into every conversation, so each mode feels like *the same coach who
knows you*." The vision ties updates to Deep-dive mode (Mode 4 build order), which does
not exist yet — so this spec builds the profile with a lightweight update path now and
leaves full deep-dive-driven distillation for when that mode lands.

## Decisions (from brainstorm)

| # | Decision |
|---|----------|
| 1 | **Update loop = coach tool (proposes) + on-demand post-session distill (proposes), user-reviewed before anything goes active.** No LLM auto-apply. |
| 2 | **One global profile + optionally track/car-tagged notes.** Single-user app; one "coach who knows you", with tags so the coach can weight relevant notes. |
| 3 | **First slice = read path + persistence + lightweight update.** Wire the static seed into the coach now, persist evolving notes in SQLite, add the tool + on-demand distill. Full deep-dive distillation deferred to Mode 4. |
| 4 | **Two-tier profile injection.** Inject only a CONCISE always-on summary each turn (static prefs + a short maintained summary); the coach pulls specific memories on demand via a tool. Keeps the per-turn footprint tiny without starving the coach of context. |
| 5 | **Continuously persist the session's current setup** (upsert on every change, latest-wins → never lost; the last write is the final setup) per (track, car), so the coach can recall "what setup did we run here last time?" via an agentic tool. |

## Invariant & token discipline

- **Owned-data invariant is respected, not violated.** Unlike corner identity/telemetry
  (objective, owned, never LLM-guessed), the driver profile is inherently *subjective*
  (tendencies, what they're working on). LLM-authored learnings are opinions/observations,
  which is acceptable — BUT they never become active silently: every LLM-proposed note is
  **pending** until the driver approves it (mirrors the track-notes draft-then-verify).
- **Token discipline — two tiers (this is the core design choice):**
  - **Always-on:** only a **concise driver summary** rides every coach turn's system prompt
    — the static prefs plus a short maintained `summary` (a sentence or few; hard-capped,
    e.g. ≤ ~60 words). This is tiny and constant, so it never bloats the context window.
  - **On-demand:** the fuller set of memories is a **pull tool** (`get_driver_memories`,
    §3c) the coach calls when a question warrants specifics (by topic/track/car). Memories
    can therefore be richer and more numerous than a per-turn cap would allow — they cost
    tokens only when actually needed, exactly like `get_track_notes`.
  - Pending memories are **never** surfaced (neither in the summary nor the tool) until the
    driver approves them. The maintained `summary` itself is reviewed like any memory
    (proposed by the distill, approved before it goes live).

## 1. Data model & storage

**Static preferences** keep their storage — the `driver` block in settings
(`braking_style`/`balance_preference`/`risk_tolerance`), saved via `putSettings`. No change
to persistence; only their UI moves (the standalone Settings section is folded into the
unified Memories surface — see §4).

**Evolving learnings** are persisted in SQLite (`store.py`) — the cross-session home,
alongside laps/setups/race_sessions. Add a `driver_learnings` table via a schema
migration (bump the current version):

```
driver_learnings(
  id            INTEGER PRIMARY KEY,
  text          TEXT NOT NULL,          -- the learning, one concise observation
  status        TEXT NOT NULL,          -- 'pending' | 'active' | 'retired'
  track         TEXT,                   -- optional tag (NULL = general)
  car           TEXT,                   -- optional tag (NULL = general)
  source        TEXT,                   -- 'coach_tool' | 'distill' | 'manual'
  session_id    TEXT,                   -- the session that produced it (nullable)
  created_ts    REAL NOT NULL,
  updated_ts    REAL NOT NULL
)
```

Single global profile: no per-car/track *rows of profiles* — one logical profile, and
the `track`/`car` columns merely TAG individual notes so the coach can prefer relevant
ones. `status` gates visibility: `active` notes feed the prompt, `pending` await review,
`retired` are kept for history but never injected.

Store API (new):
- `add_learning(text, *, status, track=None, car=None, source, session_id=None) -> id`
- `list_learnings(status=None) -> list[dict]`
- `set_learning_status(id, status) -> None`
- `update_learning(id, text) -> None`
- `active_learnings(track=None, car=None, limit=…) -> list[dict]` — active notes, general
  ones plus those matching the current track/car tag, newest first (used by the on-demand
  `get_driver_memories` tool, not the per-turn prompt).

**Maintained summary** (the always-on tier). A single short string — the coach's one-glance
picture of the driver — stored in SQLite as a single reviewed value (e.g. a
`driver_summary` meta row, with a pending→active review like memories). The distill proposes
an updated summary; the driver approves it. Store API: `get_driver_summary() -> {text, status}`,
`set_driver_summary(text, status)`. Only the active summary is injected each turn.

**Session setups.** Persist each session's setup so the coach can recall the last one used
for a track/car. Add a `session_setups` table (or reuse the `race_sessions`/session row with
a `setup_json` column) via the migration:

```
session_setups(
  session_id  TEXT PRIMARY KEY,   -- the (track, car, online|offline) session
  track       TEXT NOT NULL,
  car         TEXT NOT NULL,
  setup_json  TEXT NOT NULL,      -- the adjustable setup snapshot (same shape get_setup reads)
  updated_ts  REAL NOT NULL
)
```

- **Capture:** continuously persist the current setup and re-write it whenever it changes,
  so it's never lost — the stored row always holds the session's latest (hence final) setup.
  The setup is read from LMU REST while in the garage; persist on every observation
  (`session_setups` upsert, latest-write-wins) — on the Setup panel's fetch/refresh and via a
  light poll while connected so in-game changes are captured without a manual refresh (the
  backlog already flags the Setup panel as fetch-once-on-mount; this adds the change-tracking
  refresh). No end-of-session trigger needed — because every change is written, the last write
  IS the final setup. Only real adjustable params are stored (same filtering `get_setup` uses).
- Store API: `save_session_setup(session_id, track, car, setup) -> None`,
  `last_setup(track, car) -> dict | None` (most recent prior session's setup for that combo).

## 2. Read path — inject the profile into the coach

Assemble a `DriverProfile` at request time carrying only the **concise always-on tier**, and
pass it into the agentic coach:

- In the server's WS `/coach` worker (`app.py`), build
  `DriverProfile(braking_style=…, balance_preference=…, risk_tolerance=…,
  learned_notes=[summary])` where the static fields come from settings and the single
  entry is the active `get_driver_summary()` text (NOT the full memory list). Pass it as
  `driver_profile=` to `ask_agentic_stream(...)`. `_augment_system` already appends
  `to_prompt_text()`.
- Do the same in the REPL (`app.py run_repl` → `CoachSession(driver_profile=…)`), so both
  clients share the behavior.
- `DriverProfile.to_prompt_text()` already renders the fields + notes; reuse it. The
  always-on block stays bounded because it carries only prefs + the capped summary — the
  detailed memories are reached through the tool in §3c, not here.

This closes gap #1 (the coach finally knows the driver) while keeping every turn cheap.

## 3. Update loop (lightweight, user-reviewed)

Two ways a learning is PROPOSED (both write `status='pending'`, never active):

**a) Coach tool `record_driver_learning`** (`coach_tools.py`) — the between-laps coach can
call it mid-conversation when it notices a durable tendency ("you consistently brake early
into slow corners"). Args: `text` (the observation), optional `track`/`car` tag (defaults to
the current session's). It calls `store.add_learning(..., status='pending', source='coach_tool',
session_id=…)` and returns a short confirmation. Prompt guidance: propose a learning only
when it's a *durable, cross-lap* pattern worth remembering — not per-lap noise — and tell the
driver it's saved for review. (Pending, so it does not affect the current or future prompts
until approved.)

**b) On-demand post-session distill** — `POST /profile/distill` (author-time, like
draft-notes; **proposes only, never activates**). Given a `session_id`, it assembles a
deterministic digest (per-corner deltas vs best, recurring weak corners, the session's chat
history, race arc) and asks the `Model` to propose a few concise candidate learnings +
suggest retiring/merging stale active notes. Returns the proposals; nothing is written to
`active` without the driver's approval. This is the seam Deep-dive mode (Mode 4) will later
drive automatically; for now it's a button.

**Review** — the driver approves/edits/rejects pending proposals; approval flips
`status='active'` (and may retire a superseded note). Manual add/edit is also allowed
(`source='manual'`). Only `active` notes ever reach the prompt.

Endpoints: `GET /profile` (static prefs + active + pending notes), `PUT /profile` (static
prefs), `POST /profile/learnings` (manual add), `PUT /profile/learnings/{id}` (edit/status),
`DELETE`/retire, `POST /profile/distill` (propose).

## 3b. Agentic cross-session history tool (companion capability)

The profile is the coach's *distilled* memory; this is its *queryable raw history*.
Today the agentic tools are session-scoped (`list_recent_laps` is scoped to the current
session; `get_lap_summary("best")` is session-or-all-time). There's no way for the coach
to answer "what's my all-time fastest here?", "how has my pace at this track progressed?",
or "how did past races here go?" — even though `store.py` already computes most of it
(`best_lap(track, car)`, `recent_laps`, session listings, and the trend-chart's
best-lap-per-session series).

Add agentic tool(s) exposing existing store queries as deterministic text summaries
(two-stage invariant holds — computed facts only, never raw telemetry):
- `get_personal_best` — all-time fastest lap for the current (or a specified) track/car,
  with when it was set; contrast with the current session's best.
- `get_progress` — best lap per session over time at this track/car (the trend series),
  summarized as a short progression ("5 sessions here: 1:34.9 → 1:33.1, best 2 weeks ago").
- `list_past_sessions` — recent past sessions for this track/car: date, session type,
  finishing result if any, best lap.
- `get_last_setup` — the most recent prior session's setup for this (or a specified)
  track/car (`store.last_setup(track, car)`), so "what setup did we run here last time?"
  works at the start of a new session. Distinct from `get_setup`, which returns the CURRENT
  live setup; this returns a stored past one. Returns a clean "no stored setup for this
  combo yet" when absent.

These reuse `store.py` (add thin read helpers only where a query isn't already exposed),
are registered in `coach_tools.TOOLS` + `dispatch`, and get one prompt line steering the
coach to them for historical/progress/"all-time"/"last setup" questions. Scoped to the
current track/car by default; accept optional track/car args. This capability is
independent of the profile's learning loop — it's read-only — but ships here because it's
the same "coach who knows your history" theme.

## 3c. Agentic tool — pull fuller driver memories on demand

The always-on summary (§2) is deliberately thin. When a question warrants specifics, the
coach pulls detail with `get_driver_memories` (`coach_tools.py`):
- Args: optional `track`/`car` (default to the current session) and/or a free-text `topic`
  to filter by. Returns the matching **active** memories (from `store.active_learnings`,
  general + tag-matched), as a deterministic text list — never pending ones.
- Prompt guidance: consult it when the driver asks about their tendencies/habits/what
  they're working on, or when tailoring advice would benefit from history beyond the summary.
- This is the on-demand half of the two-tier design (§Decisions #4): rich memory without a
  per-turn token cost, mirroring `get_track_notes`.

## 4. UI — "Memories" manager in Settings

Lives in the **Settings screen**, framed as a **Driver Memories** manager — the familiar
pattern from LLM chat apps (ChatGPT's "Manage memories"): a scrollable list of the coach's
memories about the driver, each individually editable and deletable, with an at-a-glance count.

**Fold in and remove the existing standalone section.** SettingsScreen.svelte today has a
separate `<h3>Driver profile</h3>` block (~lines 190–206) with the three static-pref text
inputs (`settings.driver.braking_style`/`balance_preference`/`risk_tolerance`, saved via
`putSettings`). That block is **removed** and its three fields become the top of this unified
surface — one Driver Profile/Memories place, not two. The `settings.driver` persistence path
(config `driver` block + `putSettings`) is reused unchanged; only the UI location changes.

- **Static preferences** — the braking/balance/risk fields, relocated here from the old
  standalone section (same inputs, same save path).
- **Driver summary** — the concise always-on summary (§2): shown at the top, editable, and
  approvable when the distill has proposed a new version (the one piece that rides every
  prompt, so it's the driver's most direct control over the coach's baseline impression).
- **Memories (active learnings)** — the list: each row shows the memory text, its track/car
  tag and source (coach / distilled / manual), with inline **edit** and **delete (retire)**;
  a **"+ Add memory"** for manual entries. The active-note count is shown against the cap so
  the driver sees the profile stays lean.
- **Pending review** — memories the coach or a distill *proposed* but that aren't active yet:
  **approve / edit / reject** each. Approve flips it to active; nothing a machine proposed
  affects coaching until the driver accepts it here.
- A **"Review session learnings"** action (calls `/profile/distill` for a chosen session)
  surfaces new proposals into the pending list.

The whole surface is the driver's control over what the coach remembers — matching the
mental model of managing memories in other assistants.

## 5. Testing

- Store: migration creates `driver_learnings`; add/list/status/update round-trip;
  `active_learnings` returns only active, general + matching-tag, respects the cap/order.
- Read path: server builds a `DriverProfile` from settings + the active **summary** (NOT the
  full memory list) and passes it to `ask_agentic_stream`; assert the active summary is
  present in the assembled prompt and that individual memories / a pending summary are NOT.
- Summary storage: `get/set_driver_summary` round-trip; pending summary not injected until active.
- Session setups: `save_session_setup`/`last_setup` round-trip over multi-session data
  (latest prior session's setup for a combo; None when absent).
- `get_driver_memories` tool: returns active memories filtered by track/car/topic, never
  pending; registered in TOOLS + dispatch.
- `get_last_setup` tool: returns the stored prior setup for the combo; clean message when none.
- Coach tool: `record_driver_learning` writes a `pending` row (never active) and is
  registered in TOOLS + dispatch.
- Distill endpoint: deterministic digest assembly (mock Model), returns proposals, writes
  nothing to `active` (assert no active rows created).
- Token cap: `active_learnings` never returns more than the cap; `to_prompt_text` stays bounded.
- History tools: `get_personal_best`/`get_progress`/`list_past_sessions` return correct
  computed summaries over seeded multi-session data (all-time vs session best; progression
  order; past-session listing), registered in TOOLS + dispatch; scoped to current track/car
  with optional overrides.
- Frontend: Memories manager — pending review approve→active flow; active list edit/delete;
  manual add; "Review session learnings" populates pending.

## 6. Scope boundaries

**In scope:** `driver_learnings` table + maintained `driver_summary` + `session_setups` +
store API; two-tier injection (concise summary always-on + `get_driver_memories` pull tool);
`record_driver_learning` coach tool; on-demand `/profile/distill` proposal endpoint;
per-session final-setup capture + `get_last_setup` recall; the "Memories" manager UI in
Settings (folding in and removing the old driver-profile section, plus summary review); the
agentic cross-session history tools (`get_personal_best`/`get_progress`/`list_past_sessions`).

**Deferred / out of scope:**
- **Deep-dive Analyst mode (Mode 4)** — the distill endpoint is the seam it will later
  drive automatically; building the mode itself is separate.
- Automatic (no-review) learning application.
- Per-car/per-track separate *profiles* (only note-level tags here).
- Voice / Pit Wall persona (other vision items).
- Cross-session *trend* analytics beyond distilled notes.

## Files touched (anticipated)

- `simcoach/store.py` — `driver_learnings` + `driver_summary` + `session_setups` tables +
  migration + their store APIs.
- `simcoach/driver.py` — (likely unchanged; confirm `to_prompt_text` bounds for the summary tier).
- `simcoach/server/app.py` — build+inject the concise `DriverProfile` (prefs + summary) in the
  `/coach` worker; per-session setup capture (upsert the current setup → `session_setups` on
  every observed change, incl. a light poll while connected); the
  `/profile*` endpoints.
- `simcoach/app.py` (`run_repl`) — inject the profile in the REPL client too.
- `simcoach/coach_tools.py` — `record_driver_learning`, `get_driver_memories`, `get_last_setup`,
  and the history tools (`get_personal_best`/`get_progress`/`list_past_sessions`) + specs + dispatch.
- `simcoach/coach.py` — prompt guidance (when to propose a learning; when to pull memories /
  history / last setup).
- `frontend/src/lib/SettingsScreen.svelte` — remove the standalone `<h3>Driver profile</h3>`
  block (~190–206) and build the unified Driver Memories manager (static prefs relocated +
  active/pending memory lists) in its place; `api.ts` — profile/memory + history methods.
- Tests across `tests/` and `frontend/src/lib/`.
