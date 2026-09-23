# Coach absorbs the Pit Wall — design

Date: 2026-09-22. Status: approved in chat, awaiting written-spec review.

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

1. Live map panel on the Coach screen.
2. Retire the Pit Wall screen.
3. Two strategy tools for the coach.

Out of scope: proactive/unprompted spoken callouts (decided: **PTT only**), spotter functions,
instantaneous gap or tactical calls, multi-driver/team strategy.

## 1. Live map panel (Coach screen)

- New collapsible `LiveMapPanel` on the Coach screen beside the inputs panel. **Open by default when
  the badge is On track**; the driver's collapse choice is remembered per viewer (localStorage,
  wrapped in try/catch, render correctly without it). Hidden when LMU is offline or the track has no
  corner map.
- Reuses the existing `TrackMap`/`TrackMapPane` rendering and the live position feed the Pit Wall
  uses today — no new map renderer.
- Shows: track outline, corner labels, **sector boundaries** (from the map's `sectors`; absent →
  none drawn), **the player's car only**.
- **Next-corner callout** under the map, e.g. `Next: T10 Pouhon (L)`. Computed **server-side** from
  the player's lap distance against the corner map and sent in the live payload — the UI never
  derives corner identity (architecture invariant: corners are owned data). Direction comes from
  the map, never inferred.
- Tests: pure unit test for next-corner selection (mid-lap, between last corner and the line,
  wrap-around at start/finish, no map → none); vitest for panel visibility and default-open by
  status.

## 2. Retire the Pit Wall

- Remove the `pitwall` screen and nav tab from `App.svelte`, and delete `PitWall.svelte`,
  `LiveTower.svelte`, `RaceStrip.svelte`, `SectorDeltas.svelte` and their tests (nothing else imports
  them).
- Keep `TrackMap`/`TrackMapPane` (used by §1) and the `/live` feed, trimmed to what the panel needs
  (player position + next corner).
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
  `get_strategy`. PTT keeps the existing 2–3-sentence spoken length.

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

### Guards (both tools)

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
as-of-lap stamp, and one spoken answer each for fuel, pit window, tire life and gap trend.

## Invariants honoured

- Code extracts, model narrates — all projections computed in code.
- Only measured values stated; the wear floor is labelled as a threshold, not a fact.
- Corners are owned data — next-corner computed server-side from the map.
- UI-agnostic core — strategy math in the core, the sidecar route and tools stay thin.
