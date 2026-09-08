# Track Narratives + Track Management View — Design

Date: 2026-09-08
Status: Design approved (brainstorm), pending spec review → implementation plan

## Problem

Corner maps give the coach **geometry** (numbers, names, direction, apexes) but not
**knowledge** (character, cues, hazards). Two concrete failures:

1. The agentic (shipped) coach was caught inventing corner character from model
   memory (e.g. Algarve "Turn 1 Ayrton Senna…"), violating the owned-data invariant.
2. **A rich authored corpus already exists and the live coach never reads it.** The
   per-track LMU-wiki guides in `simcoach/knowledge/*.md` (overview + corner-by-corner
   + common mistakes + source attribution) are loaded only by the *legacy*
   `Coach.build_prompt`/`ask` path (`coach.py:195-207`). The shipped agentic loop
   (`ask_agentic` / `ask_agentic_stream`) exposes tools only and never loads that
   corpus.

The fix: make authored per-track narrative **owned data** (shipped-authoritative +
user overlay, the same storage split maps already use), reachable by the agentic coach
through a tool, editable in a dedicated **Track management view**, and draftable in-app
from track geometry + external guides (author-time, human-verified — never
live-LLM-guessed at coach time).

## Invariant compliance

- Track knowledge is **owned data**: shipped-authoritative curated defaults in
  `simcoach/tracks/`, a user overlay in app-data. Same split as corner maps.
- The LLM may **draft** a narrative at *author time* from geometry + external guides;
  the driver edits and verifies before Save. This mirrors driver-verified map rebuilds.
  The coach **never** generates track character at question time — it only quotes
  authored notes.
- Corner identity (name/number/direction) remains computed/owned; notes attach to
  corners by their existing label, never redefine them.

## Decisions (from brainstorm)

| # | Decision |
|---|----------|
| 1 | **Full Track management view** — a dedicated nav screen with coverage status that absorbs the map editor. |
| 2 | **Schema = track overview + per-corner notes** keyed to the corner label. |
| 3 | **Notes live in `<slug>.json`** under a new `notes` key, merged via the existing overlay machinery. |
| 4 | **In-app "Generate draft"** — author-time Model call, user edits/verifies, Save persists. |
| 5 | **Migrate `knowledge/*.md`** into the new schema as the authoritative seed; retire the freeform track `.md` path. |
| 6 | **External guides = manual paste** into the draft pass. No automated fetch/scrape in this spec. |
| 7 | **Token-mindful** notes: on-demand tool + bounded fetches + budgeted authoring. |

## 1. Data model & storage

Add a `notes` object to the track JSON, and add `"notes"` to
`track_lookup._ANNOTATION_FIELDS` so `load_map_data`'s per-field merge
(shipped-authoritative + user overlay) carries it with no new loader and no sibling
file.

```json
"notes": {
  "overview": "4.653 km, 15 turns. Confidence/rhythm track; blind crests mean braking references matter more than sight.",
  "corners": {
    "1": "Heavy downhill braking, compression at the apex — brake earlier than it looks. Late apex; avoid the sausage kerb.",
    "10-11": "Double-apex right: straight-line brake to the first apex, trail to rotate for a late second apex."
  },
  "source": "LMU wiki (lemansultimate.wiki.gg), CC-BY-SA; driver-verified 2026-09-08"
}
```

- Per-corner notes are keyed by the **corner label** (the same string
  `cmap.corners[].label` uses, e.g. `"1"`, `"10-11"`, `"8A"`), so they join the map
  directly.
- `overview` is a short track-level blurb; `source` is free-text attribution.
- User edits write the **full merged `notes`** back to the app-data overlay
  (`<data_dir>/tracks/<slug>.json`), identical to how corner edits work today
  (editor loads merged view, saves the whole field). `_write_track_json` and the
  dev-curation vs app-data-overlay routing (`_curating()`, `_map_dirs()`) are reused
  unchanged.

**Token budget (authoring-enforced, soft):** overview ≤ ~80 words; each corner note
≤ ~40 words. Enforced by the draft prompt and surfaced as an editor hint (e.g. a live
word count). Not a hard validation error — a knob, not a gate.

## 2. Coach integration

### `get_track_notes` tool (`coach_tools.py`)

```
get_track_notes(corner: str | None = None)
```

- No `corner`: returns the overview + all per-corner notes.
- With `corner`: returns the overview + just that corner's note (bounded token cost).
- Reads the merged map (`self.cmap` already carries the merged `notes`). No telemetry
  needed — works pre-lap.
- Deterministic text summary, like the other tools. Missing notes → a clean
  "No authored notes for this track yet."

Also append the authored note for a corner to `get_corner`'s output, so
single-corner questions never pull the whole set.

### Wire knowledge into the agentic path

The agentic coach currently reads no `knowledge/*.md`. `get_track_notes` is the
bridge: authored narrative reaches the coach on demand instead of every turn (the
legacy path injected the whole `.md` into every message — the token cost this design
avoids).

### Prompt

Extend `AGENTIC_SYSTEM_PROMPT`: prefer authored notes for track character/cues; call
`get_track_notes` before describing how a track or corner drives; never fabricate
track character (a sibling to the existing corner-identity ban). Feeds the pre-lap
"track walkthrough" cleanly.

### Migration

One-time migration of `simcoach/knowledge/<slug>.md` → `notes` in each shipped
`simcoach/tracks/<slug>.json`:

- The corpus follows a stable `**T<label> <Name>:** <text>` pattern under a
  `## Corner-by-corner` heading → parses into `notes.corners[label]`.
- The intro paragraph + `## General` + `## Common mistakes` fold into
  `notes.overview` (trimmed toward the budget).
- `Source:` line → `notes.source`.
- Retire the freeform **track** `.md` load path in `coach.py` once migrated.
- **Car** knowledge `.md` (`porsche_911_gt3_r_lmgt3.md`, etc.) is unaffected — out of
  scope; the legacy car-note load stays.

Migration is a scripted one-time pass (committed output), not a runtime parser. Where
a `.md` doesn't fit the pattern cleanly, the migration flags it for hand-finishing
rather than guessing.

## 3. Draft generation (author-time, in-app)

New endpoint: `POST /tracks/{slug}/draft-notes`.

- Assembles a **geometry digest** from the merged map + ribbon: corner list
  (label / name / direction / apex_dist / corner length), per-corner **width** and
  **bank** from the ribbon reconstruction, and curvature/radius.
- Accepts optional **external guide text** pasted by the user in the request body.
- Calls the swappable `Model` with a prompt that asks for a concise, budget-respecting
  narrative (overview + per-corner) grounded in the digest and guides.
- **Returns the draft only — never writes.** The user reviews/edits in the UI, then
  the existing notes-save path persists it as owned data.

This is the only place the LLM touches narrative, and it is author-time and verified,
preserving the invariant.

**Elevation dependency:** feeding true elevation change into the digest needs the
`pos_y`-per-station addition to `reconstruct_surface` (flagged as a gap in
`v1-backlog.md` under "Ribbon consumers"). Draft v1 uses width + bank + corner
geometry + guides; elevation is added to the digest when that lands. Not a blocker.

## 4. Track management UI

New `tracks` screen in `App.svelte`'s nav (sibling to Coach / Sessions / Pit Wall /
Settings — a new `Screen` value + nav item + section).

- **Track list** with coverage badges per track: has map? has notes? shipped vs
  user-edited?
- **Track detail:**
  - View/edit `overview` + per-corner notes (word-count hint per the token budget).
  - **Generate draft** button (calls `POST /tracks/{slug}/draft-notes`), a paste box
    for external guide text, editable draft, **Save**.
  - The **hosted `TrackMapEditor.svelte`** — map + notes in one place.
- The map pane's current "Edit map" popout is superseded by this screen.

**Risk control:** `TrackMapEditor.svelte` is working UI today. **Host it intact** in
the new screen — do not rewrite it. Smallest safe diff.

## 5. Testing

- **Migration:** parser round-trips a known `.md` (Algarve) into keyed `notes`
  (overview + per-corner labels present; source captured).
- **`get_track_notes`:** overlay notes win over shipped; `corner=` returns only that
  corner; missing notes → clean empty message.
- **Storage:** a `notes` overlay merges without clobbering geometry
  (`path`/`dist`/`corners`/`sectors`).
- **`get_corner`:** appends the authored note for the requested corner.
- **Draft endpoint:** digest assembly is deterministic (mock `Model`); the endpoint
  never writes to disk — returns the draft only.
- **Frontend:** Track list renders coverage badges; notes editor loads/saves; hosted
  map editor still mounts.

## 6. Scope boundaries

**In scope:** notes data model + overlay merge, `get_track_notes` + `get_corner` note
append + agentic prompt, migration of the track `.md` corpus, in-app draft endpoint
(paste-guides), the Track management view hosting the existing map editor.

**Deferred / out of scope:**

- Automated external-guide fetching/scraping (manual paste only this round).
- Car-knowledge `.md` migration.
- `pos_y` elevation profile in the digest (add when the ribbon gap is closed).
- **Token-usage estimator on the chat screen** — captured as a future feature; not
  part of this spec.

## Files touched (anticipated)

- `simcoach/track_lookup.py` — add `"notes"` to `_ANNOTATION_FIELDS`.
- `simcoach/coach_tools.py` — `get_track_notes` tool + spec; note append in
  `get_corner`.
- `simcoach/coach.py` — agentic prompt guidance; retire the legacy track-`.md` load
  once migrated.
- `simcoach/server/app.py` — `POST /tracks/{slug}/draft-notes`; a tracks/coverage
  listing endpoint for the UI; notes save reuses existing map-write plumbing.
- Migration script (one-time) → updates `simcoach/tracks/*.json` with `notes`.
- `frontend/src/lib/` — new `TracksScreen.svelte` (+ children), nav wiring in
  `App.svelte`, `api.ts` methods; host `TrackMapEditor.svelte`.
- Tests across `tests/` and `frontend/src/lib/`.
