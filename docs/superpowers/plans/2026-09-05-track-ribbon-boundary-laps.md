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

## Capture & designation — a dedicated mapping-capture mode (user decision)
Map generation lives INSIDE the Edit-map popup: a **"(Re)generate map layout"** button that
**starts a dedicated mapping capture**. Critically, these laps MUST NOT pollute the user's
records — they are NOT `store.save()`d, do NOT appear in the session/lap list, and never reach
the coach. They route to a transient in-memory buffer used only to build the ribbon.
- Guided flow (answers the earlier "how to designate left/right" question — no session-lap
  picker needed): click "(Re)generate map layout" → record the **left-edge** lap → record the
  **right-edge** lap → reconstruct + preview the ribbon in the editor → Save writes the ribbon
  (+ corners) to `tracks/<slug>.json`. The raw mapping laps are discarded after reconstruction.
- Capture channels already present: `pos_x/pos_y/pos_z` + `lap_distance` (elevation `pos_y`
  added `6e3637a`); live sector boundaries are being added separately.
- ARCHITECTURE: the capture layer needs a "mapping mode" path that yields laps WITHOUT
  persisting to the store (distinct from the normal capture daemon that `store.save`s). The
  server exposes start/stop mapping-capture + a build-ribbon-from-buffered-laps step; the editor
  drives it and shows "recording left/right edge…" status.

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
1. Backend — mapping-capture mode: a capture path that yields laps WITHOUT `store.save` +
   start/stop control; `reconstruct_ribbon(left_channels, right_channels)` pure fn + build/save
   a ribbon into the track file. Tests (synthetic offset edges → straight centerline + constant
   width; different-length laps pair correctly; mapping laps never hit the store).
2. Frontend — "(Re)generate map layout" button in the Edit-map popup: guided left→right capture
   with status, ribbon preview, Save. Ribbon render in the map view.
3. Review + red-team; full suites + build green.
4. Later consumers: Pit Wall car dots; replay viewer (render on this ribbon).

## Open decisions for sign-off
- ~~Lap designation~~ RESOLVED: dedicated in-editor mapping capture (left then right), laps not
  persisted. No session-lap picker.
- Pairing method to start with (recommend: normalized progress, refine to nearest-point).
- Does the ribbon REPLACE the racing-line outline as canonical, or coexist (racing line shown
  as an optional overlay on the ribbon — useful for the coach/replay)? Recommend: ribbon is
  canonical; racing line optional overlay.
