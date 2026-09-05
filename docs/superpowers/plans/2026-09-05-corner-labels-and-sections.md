# Corner labels + named sections — plan (2026-09-05)

## Why (Laguna Seca exposed it)
1. **Corner labels can't express official numbering.** Our corner `number` is a positional
   integer (auto-renumbered by apex order). Real tracks use sub-letters — Laguna's Corkscrew is
   **T8 / T8A**, then Rainey is **T9**. Positional ints would number the two Corkscrew apexes 8
   and 9, shoving Rainey to 10 — off from reality. (Andretti Hairpin as a single T2 is fine per
   the user — no change.)
2. **No named sections.** Some straights are named, and corner *groups* have names (the
   Corkscrew = T8+T8A). We can only name individual corners today.

## Design
### Corner label (the one real fork)
Add an editable `label` string to each corner, DISTINCT from the internal positional `number`:
- `number` (int) stays the positional order key (auto-renumber on add/drag — unchanged, keeps
  ordering stable).
- `label` (string) is what the MAP shows (e.g. "2", "8", "8A"). Defaults to `str(number)` and
  tracks it until the user overrides (then it's sticky — inserting/reordering other corners
  won't clobber a hand-set "8A").
- `name` (string) stays the descriptive name ("Andretti Hairpin", "Rainey Curve").
So a corner is `{number, label, name, direction, start/apex/end_dist}`. Map label = `label`;
tooltip/list = `name`. Back-compat: missing `label` → `str(number)`.

### Named sections (straights + corner-group complexes)
New `sections` array in `tracks/<slug>.json`:
`{name, start_dist, end_dist, kind: "straight" | "complex" | "section"}`.
- A section is a named DISTANCE SPAN (e.g. a straight, or the Corkscrew spanning T8→T8A).
- Editor: add a section, name it, place its start/end by dragging along the line (like sector
  splits); pick kind. Sections can overlap corners (the Corkscrew complex contains T8/T8A).
- Render: draw the section's span on the map (a highlighted arc/band along the line) with its
  name; keep it visually distinct from corner dots and sector splits.
- Coach: sections + labels become context the coach can reference ("through the Corkscrew",
  "down the back straight") — a later consumer; the data lands now.

## Backend
- `Corner` gains `label` (str, optional; `CornerMap.load` defaults it to `str(number)` — keep
  back-compat with all 16 shipped files). Persisted in save (PUT map + mapcapture save).
- Track JSON gains `sections`; served in map/ribbon payloads; persisted on save; validated
  (start<end, within [0,length]).
- No change to direction/analysis (labels/sections are naming/annotation, not geometry).

## Frontend (editor)
- Corner list: `label` becomes an editable field (small text input, defaults to the number);
  the map draws `label` instead of `T{number}`.
- New "Sections" affordance: add/name/place/remove named sections (straights + complexes),
  drag start/end handles, choose kind; render their names on the map.

## Multi-apex corners (user: coach must understand a double-apex)
Andretti Hairpin stays a single T2 label, but it's a DOUBLE-APEX — the coach needs to know so
it advises correctly (sacrifice the first apex, rotate for the second, etc.). "Double-apex" is
a TELEMETRY fact (two speed minima within the corner window), not an LLM guess → invariant-safe.
- **Data:** a corner gains `apexes: number[]` (lap_distances), defaulting to `[apex_dist]` for
  single-apex; a double-apex carries two. `apex_dist` stays the primary (first/dominant apex)
  for back-compat.
- **Detection:** the post-capture solver / analysis finds N local speed minima inside the
  corner window → seeds `apexes`. The editor lets the user confirm/add/remove an apex within a
  corner (human verification).
- **Analysis (`analysis.py`):** for a multi-apex corner, compute per-apex metrics (min speed at
  each apex, the between-apex behavior).
- **Coach:** the corner summary/tool exposes apex count + per-apex speeds; prompt guidance to
  give double-apex-appropriate advice. Coach still only RECEIVES the geometry as fact.
- Single-apex corners are unchanged (`apexes=[apex_dist]`).

## Future consideration: track NOTES, not rendered features — NOT now
User flagged (not to build yet): marking things like sausage kerbs. Refined direction: model
these as **track notes (text the coach reads), not geometry drawn on the layout.** Lighter, and
it reuses an existing seam — the coach already loads per-track knowledge notes
(`simcoach/knowledge/<track>.md`). Options when built: (a) free-form per-track notes (extend
the existing markdown notes), and/or (b) a short `note` string ANCHORED to a corner (in the
corner map) or a distance, surfaced with that corner's summary ("sausage kerb on T6 exit —
don't run wide"). No map markers, no `features` geometry. Coach receives notes as facts
(invariant-safe — human-authored track knowledge, not LLM-inferred). Deferred — design when needed.

## The split: bounded zones = geometry, point advice = notes (user)
Things with REAL BOUNDARIES → geometry (the `sections`/zones model — add `kind:"drs"` for DRS
zones alongside straight/complex; also programmatically useful, e.g. where DRS deployment is
legal). POINT advice → track notes (sausage kerbs, bumps). So a DRS zone is a bounded `section`;
a sausage kerb is a note. (LMU/WEC cars don't run DRS, but this matters for Assetto Corsa +
future sims — the zones model already covers it via `kind`.)

## Invariants preserved
- Direction still computed from telemetry; labels/names/sections are user annotation on top of
  owned geometry. Two-stage pipeline, storage split untouched.

## Sequencing
Docs-only now. BUILD after the in-flight rotation lanes land (they touch the same
editor/app.py/screen-transform files) — don't pile onto mid-flight agents. Then backend
(label + sections model/persist/serve) + frontend (label field + sections editor) lanes,
review + red-team.

## Open decision for sign-off
- Corner label model: **editable `label` (allows "8A"), distinct from positional `number`,
  map shows label** (recommended) — vs making `number` itself a free string (loses the stable
  positional ordering I added earlier). Recommend the separate-`label` approach.
- Sections scope for v1: name + span + kind, editor-placed, rendered on the map (coach
  consumption later). Confirm that's enough, or if straights/complexes need distinct treatment.
