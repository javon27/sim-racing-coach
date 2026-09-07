# Pit Wall View — Design

Date: 2026-09-06
Status: approved for spec review

## Goal

A glanceable, read-only **live race dashboard** for use during a session:
at a glance, where you are on track relative to the whole field, what your
gaps are, and how the current lap's sectors compare to your best. It is the
third top screen alongside Coach and Sessions (the `pitwall` nav slot already
exists as a placeholder in `App.svelte`).

Pit Wall is **pure situational awareness** — no coaching composer. Between-laps
Q&A and the future voice loop live in the Coach tab; Pit Wall links across to
it rather than duplicating it.

## Scope

**In v1:**

- Live field feed (all cars) over a WebSocket, driven by LMU scoring shared
  memory.
- Track map with live car dots, colored by class, your dot emphasized.
- Live Tower with a toggle: **Race position** ⇄ **Car deltas**.
- Sector-deltas strip: current lap S1/S2/S3 vs your session-best sectors.
- Session header (track/car/session/fuel/energy) reusing `SessionBar`.

**Deferred (each bolts onto a seam left open here, not built now):**

- **Track-condition flags** (green/yellow/sector, blue-flag strip). Needs an
  unvalidated extension to `rF2ScoringInfo` past `mNumVehicles` (see
  Feasibility). The flagstrip is laid out as an empty seam.
- **Voice / coaching affordance on Pit Wall** — coaching stays in the Coach
  tab. Pit Wall only links to it.
- **Live predictive sector delta** on the in-progress sector — v1 fills a
  sector's delta only once that sector closes.
- **Replay / side-by-side overlay** on the map.
- **Offline (AI) vs online** distinction — the feed works identically for
  both; no v1 behavior branches on it.

## Feasibility (what already exists vs. what is new)

Confirmed by reading the live pipeline (`simcoach/lmu/smm.py`,
`simcoach/lmu/rf2structs.py`), the server (`simcoach/server/app.py`), and the
map component (`frontend/src/lib/TrackMap.svelte`).

**Already in place:**

- **Per-car live data.** `VehicleScoringInfoV01` is fully transcribed and
  carries everything the tower and map need: `mPlace`, `mDriverName`,
  `mVehicleName`, `mVehicleClass`, `mLapDist`, `mSector`, `mInPits`,
  `mTotalLaps`, `mTimeBehindLeader`/`mLapsBehindLeader`, and
  `mTimeBehindNext`/`mLapsBehindNext`, plus `mIsPlayer` and `mControl`.
- **Session clock/laps.** `rF2ScoringInfo` maps `mCurrentET`, `mEndET`,
  `mMaxLaps`, `mSession` (all before `mNumVehicles`), so lap counter, phase,
  and time-remaining are available.
- **Track map + distance axis.** `TrackMap` exposes `map.path` (`[x,y]`
  samples) and a parallel `map.dist` (lap-distance) axis, with a
  `nearestDistIndex(map.dist, d)` helper. Placing a car is
  `map.path[nearestDistIndex(map.dist, car.lap_dist)]` — no new geometry.
- **Stored sectors.** The lap store already keeps per-lap `sectors`
  (`[s1,s2,s3]`), so session-best sectors are a query, not new capture.
- **WS pattern.** `/coach` is an established WebSocket endpoint with a
  reconnect flow to mirror.

**New:**

1. **Live field snapshot.** The capture loop reads scoring but extracts only
   the *player* (`_find_player`) and yields only *completed laps*. Nothing
   surfaces the whole field live. A new pure `read_field()` + a `/live` WS
   endpoint are the core new pieces.
2. **Sector-deltas strip** (net-new UI + a small live sector tracker).
3. **Cars overlay** on `TrackMap` (thin layer; placement helper already
   exists).

**Not available (hence deferred):** `rF2ScoringInfo` is transcribed only
through `mNumVehicles`; `mGamePhase`, `mYellowFlagState`, and the per-sector
flag fields sit past that point and are unvalidated against a running sim.
Flags require extending that struct and live-verifying on the rig.

## Architecture

Keeps the **UI-agnostic core** invariant: all field logic is a pure function;
the WS endpoint is a thin transport; the LLM is not involved anywhere in this
view.

```
LMU scoring/telemetry MMF
  -> read_consistent (existing)
  -> read_field(scoring, telem) -> FieldSnapshot        [PURE, new]
  -> /live WebSocket (poll ~4 Hz, push JSON)            [thin, new]
       -> frontend liveField store (reconnecting)
            -> PitWall.svelte
                 SessionBar | SectorDeltas | TrackMap+cars | LiveTower
```

### Backend

**`simcoach/lmu/live.py` (new) — pure field extraction.**

```
VehicleState:
  id, place, class_position, driver, car, cls,
  lap_dist, sector, laps, in_pits, is_player,
  gap_leader, gap_next        # seconds; laps-down encoded as e.g. "+1L"

SessionState:
  phase, current_lap, max_laps, elapsed, end_time, live: bool

FieldSnapshot: { session: SessionState, cars: [VehicleState, ...] }
```

- `read_field(scoring, telem) -> FieldSnapshot` maps every mapped
  `VehicleScoringInfoV01` (up to `MAX_MAPPED_VEHICLES`, clamped by
  `mNumVehicles`) to a `VehicleState`.
- `class_position` is **derived** — not a struct field — by ranking cars
  within each `mVehicleClass` by `mPlace`.
- `current_lap` = player `mTotalLaps + 1` (current lap in progress);
  `max_laps` = `mMaxLaps` (0/short sentinel ⇒ omit, timed race).
- Gaps come straight from `mTimeBehind*`; when `mLapsBehind* > 0`, encode as a
  lap-count label rather than a bogus seconds value.
- `live=False` with empty `cars` when no player is found (menus/garage) — the
  same "is LMU actually racing" guard the existing readers use.
- Pure and dependency-free ⇒ unit-tested directly on synthetic `rF2Scoring`
  structs, mirroring `tests/test_lmu_smm.py`.

**`/live` WebSocket in `app.py` (new)** — mirrors `/coach`:

- On connect, loop at ~4 Hz: `read_consistent(scoring)` + telem,
  `read_field(...)`, push JSON; on read failure or no player, push
  `{live:false}` (don't drop the socket — LMU may still be loading).
- Independent of the lap-capture loop; read-only MMF access only (same
  anti-cheat footprint the module already documents).
- Poll rate is a module constant (start ~4 Hz; the map/tower don't need 50 Hz).

### Frontend

**`liveField` store (new)** — opens `/live`, exposes the latest
`FieldSnapshot`, reconnects on drop reusing the coach WS reconnect logic.

**`PitWall.svelte` (new)** — replaces the placeholder; renders from the store:

- **Header:** reuse `SessionBar`.
- **Left (track view):** LIVE badge + lap counter/clock; `SectorDeltas` strip;
  `TrackMap` with a cars overlay; empty flagstrip seam.
- **Right (`LiveTower`):** the field list + the Race-position/Car-deltas
  toggle.
- Layout reuses the prototype `.pit` grid (`1fr 380px`), collapsing to a
  single column on narrow widths.
- When `live=false`, show a quiet "waiting for LMU / not on track" state, not
  an error.

**`LiveTower.svelte` (new)** — one snapshot, two framings; toggle state
persisted in `localStorage`:

- **Race position** (default): full field ordered by `place`; gap column =
  `gap_leader` (class-relative variant available); class chip per row; your
  row highlighted; optional per-car sector mini-bars.
- **Car deltas**: anchored on *you*; shows the cars immediately ahead and
  behind with ± gap chained from `gap_next` around your position — the
  on-track battle view.

**`SectorDeltas.svelte` (new)** — current lap's S1/S2/S3 vs your session-best
sectors, colored good/bad, each cell filled when that sector closes (player
`mCurrentSector` forward transition). Total row = running lap vs best lap.
Reference = session-best `sectors` from the store for the active
(track, car, mode). In-progress predictive delta is the deferred seam.

**Cars overlay on `TrackMap`** — a thin layer over the existing SVG: for each
car, `map.path[nearestDistIndex(map.dist, car.lap_dist)]`, dot colored by
class, your dot larger/outlined. `car.lap_dist` and `map.dist` are both
lap-distance in meters, so no rescaling.

## Data flow: the toggle, precisely

Both tower views are projections of a single `FieldSnapshot` — no extra
fetch:

- **Race position:** sort `cars` by `place`; gap text from `gap_leader`.
- **Car deltas:** find the player's index in the `place`-sorted list; take a
  window around it; gap text from `gap_next` (signed relative to the player).

This keeps the client stateless between snapshots and makes the toggle a pure
render choice.

## Error handling

- **LMU not running / no player:** `{live:false}`; UI shows the waiting state;
  socket stays open and keeps polling.
- **Torn/insane reads:** reuse `read_consistent` + the existing sanity checks;
  a bad frame is skipped, not pushed.
- **WS drop:** client auto-reconnects (coach pattern); stale snapshot is
  visibly marked until the next push.
- **Missing/short track map:** map area shows a placeholder; the tower and
  sector strip still work (they don't depend on the map).
- **Missing session-best sectors:** sector strip shows dashes, not zeros.

## Testing

- **Pure `read_field`** (primary): synthetic `rF2Scoring` fixtures covering
  class-position ranking, gap-vs-laps-down encoding, player anchoring, the
  empty/no-player grid, and clamping to `mNumVehicles`.
- **`/live` endpoint smoke:** connects, pushes a snapshot, degrades to
  `{live:false}` when the reader raises.
- **Frontend:** store reconnect behavior; tower toggle projection (same
  snapshot → both orderings); sector-delta coloring and fill-on-close;
  car-dot placement against a known `map.dist`.

## Invariants respected

- **No LLM in this view.** Pit Wall never calls the model; corner identity and
  the two-stage pipeline are untouched.
- **UI-agnostic core.** Field logic is a pure function; the WS is thin
  transport a future web client reuses unchanged.
- **Owned data.** Corner names/labels on the map still come from the corner
  map, never inferred.
- **Swappable model / storage split.** Neither is affected.
