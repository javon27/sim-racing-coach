# Coach absorbs the Pit Wall — design

Date: 2026-09-22. Status: approved by the driver 2026-09-22.

## Why

We are not competing with CrewChief on in-ear spotting. In practice the Pit Wall earned its place
for one thing only: **seeing where the car is relative to the labelled corners**. Everything else it
showed (track-map extras, relative, position tower, sector deltas) duplicates the in-game HUD, and
the driver inputs graph has already moved to the Coach screen. Switching screens mid-race to see
the map is the pain.

So: the corner map moves onto the Coach screen, the Pit Wall is retired, and race strategy becomes
something the coach answers over PTT.

## Scope

Three pieces, shipped in this order (1 lands before 2 so the one used feature is never missing):

1. The Coach screen's rail map goes live (and expands without covering the chat).
2. Retire the Pit Wall screen.
3. Three race tools for the coach: race state, strategy, and battle (opponent comparison).

Out of scope: proactive/unprompted spoken callouts (decided: **PTT only**), spotter functions,
instantaneous gap or tactical calls, multi-driver/team strategy.

## 1. Live rail map (Coach screen)

> **Decided 2026-09-22 (driver, final):** reuse the EXISTING rail map (`TrackMapPane` in the Coach
> screen's lap rail) — no new panel, no second map. It stays where it is and goes live while on
> track. It must be **expandable without ever covering the chat text**: expanding WIDENS the rail
> (pushing the chat column narrower, still fully readable); collapsing restores it. This replaces
> today's 🔍 overlay view on the Coach screen.

- **While on track the rail map is live** (player car dot, next-corner callout); otherwise it shows
  the selected session's map exactly as today. The expanded/normal choice is remembered per viewer
  (localStorage, wrapped in try/catch, render correctly without it). The rail's existing height
  resizer keeps working.
- Reuses the existing `TrackMap`/`TrackMapPane` rendering and the live position feed the Pit Wall
  uses today — no new map renderer.
- Shows: track outline, corner labels, **sector boundaries** (from the map's `sectors`; absent →
  none drawn), **the player's car only**.
- **Next-corner callout** under the map, e.g. `Next: T10 Pouhon (L)`. Computed **server-side** from
  the player's lap distance against the corner map and sent in the live payload — the UI never
  derives corner identity (architecture invariant: corners are owned data). Direction comes from
  the map, never inferred.
- Tests: pure unit test for next-corner selection (mid-lap, between last corner and the line,
  wrap-around at start/finish, no map → none); vitest for live vs. session mode by status, and that
  expanding widens the rail and never overlays the chat.

## 2. Retire the Pit Wall

- Remove the `pitwall` screen and nav tab from `App.svelte`, and delete `PitWall.svelte`,
  `LiveTower.svelte`, `SectorDeltas.svelte` and their tests (nothing else imports them).
  **Correction (2026-09-22, post-approval):** `RaceStrip.svelte` is NOT Pit Wall-only — it is the
  post-race result strip in the Coach screen's lap rail, and it stays.
- Keep `TrackMap`/`TrackMapPane` (used by §1) and the `/live` feed, trimmed to what the rail map
  needs (player position + next corner).
- Keep `simcoach/lmu/live.py` `read_field_snapshot()` — it becomes the input to `get_race_state`.
- Docs: `docs/vision.md` — replace the Pit Wall / CrewChief-replacement direction with "the coach
  answers race questions over PTT; no in-ear spotter"; `CLAUDE.md` scope list — "CrewChief
  replacement" becomes explicitly *not pursuing*; update the pit-wall/voice project memory to match.
- Tests: svelte-check + vitest green; App test asserts the tab is gone and Coach still mounts.

## 3. Strategy tools

### Governing constraint: latency

A PTT answer arrives seconds after the question (speech-to-text → model + tool loop → TTS).
Anything true only "right now" is stale on arrival. Therefore:

- **Every figure must stay valid for laps, not moments**: lap numbers ("pit window laps 18–22"),
  per-lap targets ("save 0.15 L/lap"), multi-lap trends ("closing 0.3 s/lap, reaching P5 around lap
  24"). No instantaneous gaps, no immediate tactical calls.
- **Computed at lap boundaries, not per question.** On each lap commit the server recomputes one
  race-state + strategy snapshot and caches it, stamped `as_of_lap = N`. The tools only read the
  cache (no work while the driver waits), and the coach states the stamp ("as of lap 12 …").
  Completed laps only — never a half-lap.
- **One tool call per strategy question.** The system prompt gets one routing line: race-position /
  gap / remaining questions → `get_race_state`; fuel / energy / pit / tire-life questions →
  `get_strategy`; "where is he faster than me" / passing questions → `get_battle`. PTT keeps the existing 2–3-sentence spoken length.

### `get_race_state`

- Overall position and **class** position.
- Remaining: laps (`max_laps`) or time (`end_time − elapsed`) for timed races, plus estimated laps
  left at the driver's recent clean-lap pace.
- **In-class** car ahead and behind: who, and the **gap trend** — slope over the last N laps
  (s/lap) and, when closing, the projected lap they'd be reached. Needs ≥ 2 laps of history.
- History source: at each lap commit, capture stores a per-lap sample on the lap row (overall
  position, class position, class gap ahead, class gap behind) via a schema migration. Live values
  come from `read_field_snapshot()` (`gap_next`, `laps_behind_next`, `max_laps`, `end_time`).

### `get_strategy`

- **Fuel/energy to the end**: `fuel.plan_race` / `binding_stint` on the measured per-lap burn (same
  source as `get_energy_report`) and the laps remaining from race state → makes it or not, margin in
  laps, per-lap save target when short.
- **Pit window**: earliest/latest stop lap, bounded by whichever binds first — fuel/energy or tires.
- **Tire life**: laps until the worst tire reaches a wear floor, from `stint_tire_trend`'s per-lap
  wear rate. The floor is a **named constant** and the coach states it ("to 30% remaining") — it is
  a judgement, never presented as a measurement.

### `get_battle` — where an opponent is faster or slower than me

Answers "the car I've been fighting all lap — where is he quicker?", about the **last completed
lap**, so it is still true when heard.

- **Every car in the player's class is recorded, every lap.** Who the opponent is can change several
  times in one lap, so we never pre-pick "ahead/behind". During the lap, capture records, for each
  same-class car, the time it crosses each corner's entry and exit (by lap distance, from the
  scoring feed) and its speed nearest the apex. Other classes are not recorded.
- At lap commit, code turns that into **per-corner and per-section time deltas** between the player
  and each same-class car that was within a battle range of the player during that lap.
- **Who "the opponent" is**, resolved in code, not by the model: by default the same-class car(s) the
  player was nearest to for most of the last lap (it may name two when the fight changed hands); the
  driver can also name a position or a driver ("P4", "the Ferrari", a driver name) and the tool
  matches it against the class field.
- Output: where the opponent gains and loses (corners/sections ranked by delta), and the player's
  strongest zones against that car (e.g. "you carry 8 km/h more into T10") — the passing-chance
  read, narrated by the model from code-ranked facts.
- **Honesty**: scoring updates at a few Hz, so a single corner's delta has an error of roughly
  0.05–0.1 s (measured at the rig before shipping the threshold). Only corners/sections whose delta
  exceeds that noise floor are reported; nothing is stated at false precision.
- Storage: one row per (lap, same-class car) with the per-corner crossing times and apex speeds —
  cross-session data, so SQLite, keyed to the player's lap.

### Guards (all three tools)

- Every rate/projection needs ≥ 2 clean laps in the current stint; otherwise "not enough laps yet".
- Out-laps, in-laps and partial laps are excluded from every rate.
- Offline / not in a race → "no live race", never a guess.

### Tests

Pure tests for the projection math (gap slope + catch lap, pit window from the binding constraint,
timed vs lap-count remaining, wear-floor projection, the 2-lap guard); tool-level tests with a fake
snapshot and cache; migration test for the per-lap sample columns; a test that the cache only
changes on lap commit.

### Rig check (rig-verify)

A real timed race and a lap-count race: `end_time` semantics, class gaps in multiclass, the
as-of-lap stamp, and one spoken answer each for fuel, pit window, tire life and gap trend. For
`get_battle`: measure the scoring feed's real update rate and the per-corner timing error against
the player's own full-rate lap before fixing the noise floor; one spoken battle answer after a
lap-long fight where the opponent changed.

## Invariants honoured

- Code extracts, model narrates — all projections computed in code.
- Only measured values stated; the wear floor is labelled as a threshold, not a fact.
- Corners are owned data — next-corner computed server-side from the map.
- UI-agnostic core — strategy math in the core, the sidecar route and tools stay thin.
