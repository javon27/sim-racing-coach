# Track Map Editor + Canonical Shipped Maps — plan (2026-09-04)

## Why
The telemetry-generated track map shipped (Map tab). User feedback + new constraints:
1. Visual fixes (thicker line, non-selectable labels, S/F + direction arrow) — **DONE `63dff74`**.
2. Show **sectors** on the map too.
3. **Couple** the track outline with the corner map via an **editor** — a popout over the
   Map tab where the driver drags corners to their real position, adds/removes/renames them.
4. The generated + edited maps are **shipped with the packaged app** and **reused by the
   live Pit Wall view** (car dots on the track).

## Decisions (locked with the user)
- **Snap-to-line editing.** Dragging a corner moves it *along the driven racing line*; under
  the hood that edits `apex_dist`. Corners always sit on the line. No per-corner free x/y.
- **Popout modal** over the Map tab (an "Edit map" button), not a separate OS window / screen.
- **Direction is inferred, never hand-set** (CLAUDE.md invariant). Inferred from **path
  curvature** at the apex (cross-product sign of consecutive segments) — this is telemetry
  *position* geometry, so it honors the invariant AND makes the shipped file self-contained
  (no dependency on a stored lap's steering channel). Calibrate the sign against Monza's
  already-verified directions.
- **Maps are shipped owned data** → the track JSON must be renderable offline and by the Pit
  Wall, so it carries a **canonical world-coordinate outline**, not just corner distances.

## Data model — extend `simcoach/tracks/<slug>.json` (back-compatible)
Today: `{ "corners": [{number, name, direction, start_dist, apex_dist, end_dist}] }`.
`CornerMap.load` reads only `data["corners"]` and requires `direction` — so ADD fields, keep
`direction` as a stored-but-machine-derived value (never user-entered). New optional keys:
```jsonc
{
  "corners": [ ... unchanged shape; direction always re-derived on save ... ],
  "path": [[x, z], ...],      // canonical outline in WORLD coords, downsampled (~300 pts)
  "path_dist": [d, ...],      // lap_distance parallel to path (maps dist <-> world point)
  "sectors": [d1, d2],        // lap_distance of the S1/S2 boundaries (S3 = to S/F)
  "length": <float>           // lap length (m) if known; else max(path_dist)
}
```
- Existing files without the new keys still load and render per-lap exactly as today.
- The world-coord `path` is what the Pit Wall normalizes live car positions against (same
  `pos_x/pos_z` space as scoring), so map + car dots share one frame.

## Backend
1. `build_track_map(channels, corner_map, width)` — also emit `dist[]` (lap_distance parallel
   to the returned screen `path`) and per-corner `apex_dist/start_dist/end_dist`. Prefer a
   **shipped `path`/`path_dist`** from the corner map when present; else derive from the lap
   (today's behavior). Keep returning `{}` when neither is available.
2. `direction_from_path(path_world, path_dist, apex_dist)` — new pure helper: local turn sign
   from the cross product of the segments around the apex point. One calibration constant
   (screen/world handedness), pinned by a test asserting Monza's known directions.
3. `PUT /laps/{lap_id}/map` — save. Body: `{corners:[{number,name,apex_dist, start_dist?,
   end_dist?}], sectors?:[d1,d2]}`. Server:
   - resolve slug via `track_lookup.track_slug(rec.track)` → `simcoach/tracks/<slug>.json`
     (write even if it doesn't exist yet — first-time map creation).
   - derive a default window around each apex if `start/end` omitted; validate
     `start < apex < end` and ordering (reuse `CornerMap.load`'s invariants).
   - **re-derive `direction`** for every corner from geometry (never trust the client).
   - persist the canonical `path`/`path_dist` (from THIS lap) + `sectors` into the file.
   - return the refreshed map (same shape as GET).
   - guard: reject writes outside `simcoach/tracks/`; atomic write (tmp + replace).
4. Tests + a red-team pass (this endpoint WRITES owned, shipped data — path traversal on
   slug, degenerate windows, direction sign, concurrent write, non-finite coords).

## Frontend — `TrackMapEditor.svelte` (popout modal from the Map tab)
- Large map (reuse the TrackMap rendering: thick line, S/F, direction arrow, sectors).
- Draggable corner markers → snap to nearest `path` point → new `apex_dist` (from `dist[]`).
- Add corner (click the line), remove, rename, renumber. Direction shown read-only (auto).
- Two draggable sector-split markers along the line (define `sectors`), S/F fixed at dist 0.
- Save → `PUT /laps/{id}/map` → refresh. Cancel discards.
- Tests (drag→apex_dist mapping, add/remove, save payload shape, direction is read-only).

## Sectors — resolved without new capture
The editor DEFINES sector splits (two draggable markers) → stored as distances in the track
file. This sidesteps the live-capture blocker (we capture sector *times*, not boundary
*distances*, and no per-sample time channel exists). Live auto-detect / cross-check against
captured sector times is an optional later enhancement.

## Pit Wall (future consumer — designed for, not built here)
The canonical world `path` + `path_dist` + `sectors` + corner apexes give the Pit Wall a
stable track frame; live car `pos_x/pos_z` from scoring normalize into the same box to place
class-colored dots. No extra map work needed then — it consumes this file.

## Build order (SDD, fresh-context subagents, review+red-team per phase)
1. Backend: schema + `build_track_map` dist/apex fields + `direction_from_path` + `PUT` save. 
2. Frontend: `TrackMapEditor` modal + Map-tab "Edit map" button.
3. Review + red-team both lanes; full py + fe suites + build green.
4. Later: sector live cross-check; Pit Wall consumer.

## Invariants preserved
- Corners owned data; direction always machine-derived (now from path curvature) — no LLM,
  no hand-set direction. Names/numbers/apex position are the human-verified parts (this IS
  the driver-verification step, same as Imola/Spa were).
- Two-stage pipeline, UI-agnostic core, storage split — all untouched.
