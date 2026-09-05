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

## Laps per edge: 2 is the MINIMUM, not the cap (user, 2026-09-05)
The original "two laps" (one left, one right) is the FLOOR, not a limit. Allow N laps per edge
and AVERAGE each side (reuse the existing multi-lap `average_racing_line` per edge on the shared
grid) → smoother, more accurate edges the more laps you drive. So:
- Capture wizard: record ≥1 left-edge lap(s), then ≥1 right-edge lap(s); each buffered lap is
  designated left or right (build takes a LIST of left indices + a LIST of right indices, not a
  single left/right). Minimum = one each.
- `reconstruct_ribbon` signature becomes `(left_laps: list[channels], right_laps: list[channels])`
  → average each side first (per-edge `average_racing_line`), then pair nearest-point → centerline
  / width / bank as before. One lap per side still works (averaging a single lap = that lap).
- `/mapcapture/build` body: `{left: int[], right: int[]}` (buffer index lists) instead of single ints.
NOT YET BUILT — fold into the ribbon build when driven on the rig.

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

## Post-capture solver pass — naive optimal line (user decision)
After the ribbon is reconstructed, run a NAIVE physics/optimal-line solver over the track
corridor (the two edges) to compute the ideal line + apexes + braking zones — independent of
how the user drove. "Naive/quick is fine" (user); not a lap-time optimizer. New pure module
`simcoach/raceline.py`:
- **Corridor**: N stations across the track; each is a segment from left edge to right edge;
  the line picks a lateral position α∈[0,1] per station.
- **Line**: minimum-curvature via **elastic-band relaxation** — start at the centerline,
  iteratively nudge each point to reduce local curvature, clamped within [left,right]. ~tens of
  numpy lines, no QP/scipy solver.
- **Speed profile**: friction-circle point-mass — `v_cap(s)=sqrt(µ·g·r(s))`, then a backward
  pass (braking limit) + forward pass (accel limit). Apex = min-speed / max-curvature point;
  braking zone = where the backward pass pulls speed below the cornering cap before the apex;
  entry/exit window from that.
- **Tunable car knobs** (physical, vary by car — ponytail hardware-calibration rule): one µ +
  lat/long-g limits, GT3 defaults, adjustable. Not baked as universal truth.
- **Telemetry refinement (user decision):** don't stop at physics defaults — calibrate the
  knobs from captured laps (µ / lat-g limit from observed peak lat_g; brake/accel limits from
  peak long_g), and nudge the solved apex/braking toward what the driver actually does (or blend
  the solved line with the driven line). Physics-first, data-refined. Keep it naive; refinement
  is a calibration step, not a re-solve.
- Output = a first-pass corner set (apex/window/direction) the user reviews/edits in the editor.
  Deterministic + pure → unit-testable (a circular-arc corridor → apex at the geometric apex,
  constant corner speed). Falls back to physics defaults when no telemetry laps exist.
- Depends on the ribbon (needs both edges), so it runs after reconstruction.

## Corner direction — inferred from ALL available data at placement (user decision)
When a corner is placed (auto-detected or user-added), infer direction from every signal at the
apex and require agreement: **centerline/racing-line curvature + steering sign + lat_g sign**.
This is more robust than the lone path-curvature cross-product (whose sign constant was
rig-unverified) — and it largely RETIRES that risk: the backend already found steering+lat_g
unanimous and correct across Monza's 10 verified corners (lat_g has a physical cornering sign).
When the signals DISAGREE (low confidence), flag the corner for the user to eyeball rather than
guessing. Still fully telemetry-derived / owned-data — never LLM or hand-set (invariant).

## Banking (e.g. Daytona) — free from elevation + two edges (user decision)
Banking = the outer edge is higher than the inner. We capture `pos_y` on BOTH boundary laps, so
`reconstruct_ribbon` computes **bank angle θ(s) = atan((y_left − y_right) / width(s))** per
station and includes it in the ribbon. The solver's corner-speed cap then uses a banked
friction model — e.g. `v_cap = sqrt(g·r·(µ + tanθ)/(1 − µ·tanθ))` (clamp for stability) —
so banked corners get their higher speed. Telemetry-calibrated lat_g independently reflects
banking too. Flat track (θ≈0) reduces to the plain friction circle. Naive stays naive.

## Display rotation (user decision) — QUEUED behind the ribbon lanes
Telemetry maps come out in world orientation, not the conventional way a track is drawn. Add a
per-track **`rotation`** (degrees, default 0) stored in `tracks/<slug>.json`, applied INSIDE the
world→screen transform (rotate world x/z about the centroid before normalizing to the viewBox).
That way every consumer renders the same orientation — map pane, editor, Pit Wall car dots,
replay dot all apply the SAME rotation (it's part of the shared transform, not a per-view SVG
tweak, so overlays placed by world position stay aligned). Editor gets a rotation control
(90° buttons + fine nudge, or a slider) that saves the value. Back-compat: missing → 0.
SEQUENCING: apply after the mapping-capture/ribbon backend + editor lanes land (they're editing
the same screen-transform + editor files now) — don't pile onto mid-flight agents.

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
