# Per-corner geometry metrics for coach grounding

Date: 2026-09-08
Status: Design (awaiting review)

## Status 2026-09-15

Superseded by the #99 course-correct: baked corner **radius** and **width**
no longer render to the coach (`format_corner_line` stopped rendering them —
they were stale and reached the model as fact). **Elevation** and **banking**
still render (re-baked, rig-verified). The map-geometry bake pipeline this
doc designs is parked, labelled `deprioritized` in Issues.

## Problem

`Generate draft` (`POST /tracks/{slug}/draft-notes`) grounds the model in a
text digest of the corner map, but that digest carries only corner **identity
and along-track position**: label, name, direction, apex distance, span. It
omits the physical shape of the corner — **radius, elevation change, track
width, and banking** — even though that shape is what a driving note is
usually about ("brake earlier than it looks because it drops away"). The model
therefore can't ground elevation/width/radius claims in fact; anything it says
about them today is either copied from pasted guides or invented.

This design feeds those four metrics into the digest, computed deterministically
and shipped with the map so end users benefit without driving.

## Goals / non-goals

- **Goal:** per-corner `radius`, `elevation change`, `width`, `banking`,
  computed deterministically, baked into the shipped map, fed to the draft
  digest — omitting any metric that isn't available (never fabricated).
- **Non-goal:** 3D replay / full elevation profiles; banking-sign rig
  verification; re-driving tracks (only Laguna needs it, and that's a user
  action, not code); any change to live capture (it already records `pos_y`).

## Invariants preserved

- **Two-stage pipeline:** metrics are produced by a pure, tested extractor;
  only the resulting per-corner scalars (then the digest string) reach the LLM.
  Raw ~50 Hz telemetry never does.
- **Owned, computed data:** metrics are geometry facts derived from telemetry /
  the reconstructed surface — never LLM-guessed, same discipline as corner
  direction. A metric that can't be computed is stored `null` and omitted.

## Data availability (verified 2026-09-08)

- **Radius** — Menger curvature on the map's 2D `path` at each corner. Always
  available (every shipped map has a path).
- **Elevation** — `pos_y` is recorded live (`smm.py:244`) and persisted per lap
  (`store` writes all channels) and in point-cloud captures
  (`{data_dir}/captures/{slug}.npz`, keys `lapN_pos_y`). Available for every
  shipped track (Laguna via its 171 driven laps).
- **Width / banking** — from a reconstructed ribbon. 10 of 12 shipped tracks
  already ship a ribbon (`left/right/center/width/bank`) and have a saved
  capture; **only Laguna Seca lacks both** and needs a boundary-weave re-drive
  for width/banking. `test_track` is a stub, ignored.

`reconstruct_surface` computes banking from `pos_y` but does **not** currently
return an elevation profile, and the saved ribbon persists neither `elev` nor a
`dist` axis. Those are the only downstream gaps.

## Components

### 1. `reconstruct_surface` — expose elevation
Add a mean-`pos_y`-per-bin `elev` array (parallel to the existing `dist`) to the
returned dict. Transient use at bake time; persisting the full profile in the
ribbon is out of scope (YAGNI — no consumer yet).

### 2. `corner_geometry()` — pure extractor (in `analysis.py`)
`corner_geometry(path, dist, corners, *, ribbon=None, elev=None) -> {label: metrics}`
where `metrics = {radius_m, width_m, banking_deg, elev_delta_m}` (each optional):
- `radius_m` — min radius over the corner window from Menger curvature on `path`
  (reuse `simcoach.raceline`/`ribbon._menger`).
- `elev_delta_m` — `elev(end) - elev(start)` over the window (+ = uphill),
  sampled from a supplied elevation source (ribbon `elev`, else a clean lap's
  `pos_y`) aligned on `dist`.
- `width_m`, `banking_deg` — ribbon `width`/`bank` sampled at the apex; `null`
  when no ribbon.
Pure and deterministic; no I/O.

### 3. Map schema — optional per-corner fields
Each corner in the map JSON gains optional `radius_m`, `width_m`,
`banking_deg`, `elev_delta_m` (absent/`null` = unavailable). Backward compatible:
`CornerMap.from_data` ignores unknown fields today; readers treat missing as
unavailable.

### 4. Bake at save + one-time backfill
- `_persist_map` computes and writes the metrics whenever it saves a map
  (radius always; width/banking/elev when a ribbon/elevation source is at hand).
- A one-time script backfills the 11 shipped tracks: load path + reconstruct the
  ribbon from each track's saved capture (10 tracks) to get width/bank/elev,
  run the extractor, write the baked fields. **Laguna:** radius + elevation from
  its driven laps; width/banking left `null` until a weave capture exists.

### 5. Draft digest — consume the metrics
`draft_notes` reads the baked per-corner fields into each digest line, e.g.:
`T8 Turn 8: right, apex@2070m, span 2020-2120m, radius ~35m, downhill Δ6m, width 12m, banking 2°`
— each bracketed clause omitted when its field is `null`. Falls back to
computing `radius_m` from the path if a map predates the bake.

## Testing

- `corner_geometry` unit tests on synthetic inputs: a known-radius circular arc
  (radius within tolerance), a constant slope (`elev_delta_m` matches), a
  constant-width/banked strip (width/banking match). Edge: straight → large/inf
  radius; no ribbon → width/banking `null`.
- Backfill smoke test on a track fixture: fields populated and finite.
- Digest test: null metrics omitted; present metrics rendered in order.

## Rollout / re-driving

Backfill all shipped tracks from existing data (no driving). **Laguna Seca**
width/banking need a boundary-weave capture; the driver can record one now.

Wrinkle to handle in implementation: Laguna already has a driver-placed corner
map on a racing-line path. A weave capture must **only** supply cross-section
data (width/banking/elevation) sampled onto that existing path by
`lap_distance` — it must **not** replace the corner map with the reconstructed
ribbon centerline (which would shift apex distances). So the capture flow needs
a "save the raw `.npz` capture without rebuilding geometry" path (or the
backfill reads the buffered/last capture directly). Until then Laguna's
width/banking stay `null`; radius + elevation come from its 171 laps regardless.
Future map saves bake metrics automatically.

## Open items

- Banking **sign** is a fixed perpendicular convention, rig-unverified
  (`ribbon.py` comment). Ship the magnitude; flag sign as unverified in the
  digest wording if needed.
