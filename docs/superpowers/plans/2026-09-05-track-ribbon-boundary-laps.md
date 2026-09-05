# Track ribbon from boundary laps — plan (2026-09-05)

## Why
The generated map is currently the driver's *racing line* (averaged), not the track. Concern:
it's "based on my driving." PNG auto-import is ruled out — LMU map imagery is packed inside
`assetsMAPs.mas` (proprietary; no loose PNGs; unpacking is asset-grey). User's solution: drive
**two slow boundary laps per track** (left edge, right edge) → reconstruct the **true track
geometry** (edges + centerline + width) from our OWN telemetry. No game assets, not a racing line.

## Consumers (why it's foundational, not just a prettier map)
1. **Coach reference map** — the right-pane canonical map, corners labeled on the real track.
2. **Pit Wall** — live class-colored car dots placed on the real track ribbon.
3. **Replay viewer (à la Coach Dave Delta)** — play back / compare laps on the track. This is
   the spec's deferred **replay-overlay** seam; full-rate telemetry keyed on the lap-distance
   axis (an architecture invariant) is exactly the alignment key it needs.

## Shipped, user-facing tool (not just a dev tool)
This whole flow — drive boundary laps → designate → reconstruct ribbon → place/verify corners
→ save — ships **in the app** so end-users can generate layouts for tracks we DON'T ship (and
Assetto Corsa tracks once AC support lands). Same tool we use to build the shipped set. Ties to
the deferred "import user-built maps as canonical" item: a user's generated layout lives in
their app-data; a promote/export path makes it canonical. So the UI must be end-user usable
(clear designation, preview, save), not a dev script.

## Capture & designation
- No new capture: position (`pos_x/pos_z` + `lap_distance`) is already recorded per lap.
- The user drives ONE slow lap hugging the **left edge** and ONE hugging the **right edge**.
- Designate them in the editor (DECISION — recommend): a picker "set as left edge / right
  edge" over the session's laps. Store the chosen lap ids (or freeze the reconstructed ribbon
  into the track file so the source laps aren't needed at runtime).

## Reconstruction (backend, pure fn + tested)
Key wrinkle: the two laps have DIFFERENT lengths (inside is shorter), so DO NOT pair by raw
`lap_distance`.
- Both laps share the S/F line (lap_distance 0), so anchor both there, then **pair by
  normalized track progress** t∈[0,1] (resample each edge to N samples over its own length).
  First pass = progress-pairing; refine to **nearest-point** (project each centerline sample
  perpendicular to the other edge) if progress-pairing skews on long straights vs tight corners.
- `centerline(t) = midpoint(L(t), R(t))`; `width(t) = |L(t) − R(t)|`.
- Output ribbon (world coords, downsampled, + a normalized screen box):
  `{ left: [[x,z]…], right: [[x,z]…], center: [[x,z]…], width: […], dist: […] }`.
- Corners are placed on the CENTERLINE by `apex_dist` (reuse existing corner logic).
- Store in `tracks/<slug>.json` as the canonical ribbon (compact); the racing-line `path`
  becomes optional/legacy. Back-compat: keep loading files that only have `corners`/`path`.

## Render (frontend)
- Fill the ribbon between `left` and `right` = the track surface (our own asset). Draw the
  centerline, S/F, sectors, corner dots. viewBox padded (as now).
- Same world→screen normalization used for overlays: Pit Wall car dots, replay moving dot(s).

## Editor
- Designate left/right boundary laps → build + preview the ribbon → place/verify corners on
  the centerline (reuse the existing corner editor: auto-detect + drag + rename + sectors).

## Invariants preserved
- Telemetry-derived, owned data; corners computed (not LLM/hand-guessed); two-stage pipeline;
  UI-agnostic core; storage split (ribbon summary in the track JSON, full-rate laps on disk).

## Build order (SDD, fresh-context subagents, review+red-team per phase)
1. Backend: `reconstruct_ribbon(left_channels, right_channels)` pure fn + storage + an endpoint
   to build/save a ribbon from two designated laps. Tests (synthetic offset edges → straight
   centerline + constant width; different-length laps pair correctly).
2. Frontend: lap designation picker + ribbon render in the map view/editor.
3. Review + red-team; full suites + build green.
4. Later consumers: Pit Wall car dots; replay viewer (separate features that render on this ribbon).

## Open decisions for sign-off
- Lap designation UI (recommend: editor picker "set as left/right edge").
- Pairing method to start with (recommend: normalized progress, refine to nearest-point).
- Does the ribbon REPLACE the racing-line outline as canonical, or coexist (racing line shown
  as an optional overlay on the ribbon — useful for the coach/replay)? Recommend: ribbon is
  canonical; racing line optional overlay.
