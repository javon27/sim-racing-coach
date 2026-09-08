# Car Knowledge for the Agentic Coach — Design

Date: 2026-09-08
Status: Design approved (brainstorm), pending spec review → implementation plan

## Problem

The agentic (shipped) coach cannot reach the car knowledge that already exists. The
per-car guides in `simcoach/knowledge/*.md` (handling characteristics, driver aids,
sources, with `[general-sim]`/`[LMU]`/`[needs-refinement]` provenance tags) are loaded
ONLY by the dead legacy `Coach.build_prompt` path (`coach.py:209-215`, by slugging the
car name to `knowledge/<slug>.md`). The tool-loop (`ask_agentic`/`ask_agentic_stream`)
never loads them — so the coach answers car/setup questions from model memory, the same
class of gap the track-narratives feature fixed for tracks.

Two sub-gaps:
1. **Per-car knowledge** isn't reachable by the coach.
2. **No LMU-specific setup CONCEPTS** — how *this game's* physics respond to setup
   changes — as owned data. The user wants the coach grounded in LMU setup behavior,
   not just general motorsport knowledge.

This mirrors track-narratives (`docs/superpowers/specs/2026-09-08-track-narratives-design.md`),
adapted for cars: cars have **no per-corner structure** and there is **no existing
per-car file** to attach to, so a new car-notes store is created.

## Decisions (from brainstorm)

| # | Decision |
|---|----------|
| 1 | **Structured car-notes JSON** — new `simcoach/cars/<slug>.json`, shipped-authoritative + optional user overlay (same split as maps). |
| 2 | **Per-car notes + a shared LMU setup-concepts doc** — general setup principles the coach can cite for ANY car, including cars with no authored notes yet. |
| 3 | **Data + coach tool only — NO in-app UI.** Car knowledge is curated and SHIPPED for the LMU cars; no user-facing car editor, no in-app draft. |
| 4 | **Migrate the existing car `.md`** into the new store; retire the legacy car-note load; hand-author going forward (no in-app draft this round). |

## Invariant & scope notes

- **Owned data:** car knowledge is shipped-authoritative (`simcoach/cars/`) with an
  optional user overlay (`<data_dir>/cars/`). Curated/verified, never live-LLM-guessed
  at coach time (the coach quotes it). Provenance tags (`[LMU]`/`[general-sim]`/
  `[needs-refinement]`) are preserved so the coach can hedge appropriately.
- **Token discipline:** `get_car_notes` is an on-demand PULL tool (like
  `get_track_notes`), never injected every turn. The shared setup-concepts doc is
  **curated concise** — NOT the raw 20KB `lmu-setup-guide.md`; that guide is the seed
  material to distill from, not the shipped payload.
- **No UI:** no api.ts, no screen, no endpoints, no draft pass this round. A user
  overlay is supported by the loader (parity with maps, near-zero cost) but nothing
  in-app writes to it — the seam is left open for a future power-user override.

## 1. Data model & storage

**Per-car notes** — `simcoach/cars/<slug>.json`, where `<slug>` is the slug of the
resolved car model name (the SAME slug the legacy loader used to find
`knowledge/<slug>.md`, so the mapping is unchanged — `resolve_car()` already yields that
name). Shape:

```json
{
  "name": "Ferrari 296 GT3",
  "notes": {
    "overview": "Mid-engine, well-balanced baseline; mild entry understeer, power-on exit oversteer.",
    "handling": "[general-sim] Front ride height is a key lever... [LMU] high-speed rear snap if balance shifted forward...",
    "setup_concepts": "",
    "driver_aids": "[LMU] LMGT3 is the only LMU class with native ABS... TC has TC / power-cut / slip-angle facets...",
    "sources": ["Coach Dave Academy LMU 296 guide (URL)", "OverTake LMGT3 ABS guide (URL)"]
  }
}
```

- `overview`/`handling`/`setup_concepts`/`driver_aids` are freeform text (provenance
  tags kept inline); `sources` is a list. `setup_concepts` starts empty on migration
  (the existing `.md` has no dedicated setup-concepts section) and is hand-authored per
  car over time; the shared doc (below) covers the general case meanwhile.
- **Loader** mirrors `track_lookup.load_map_data`: a `car_notes.load_car_notes(car_name,
  cars_dir, user_dir=None)` that slugs the resolved name, reads the shipped
  `cars/<slug>.json`, merges an optional user overlay's `notes`, returns `{}` when
  absent. Slug-built paths only (no traversal), same guards as `track_lookup`.

**Shared LMU setup-concepts** — one shipped doc,
`simcoach/cars/_lmu_setup_concepts.json` (leading `_` so it never collides with a car
slug), holding a **curated-concise** distillation of LMU setup behavior (what each
adjustment does in LMU: ride height, ARB, aero balance, brake bias, ABS/TC facets,
etc.). Distilled from `docs/superpowers/knowledge/lmu-setup-guide.md` (20KB seed) into a
short owned doc (target ~1 page). Applies to any car; returned alongside per-car notes.

## 2. Coach integration

### `get_car_notes` tool (`coach_tools.py`)

```
get_car_notes()
```

- Returns the current car's authored notes (overview / handling / setup_concepts /
  driver_aids / sources) **plus** the shared LMU setup-concepts, as one deterministic
  text summary. No arg needed (scopes to `ctx.car`); works pre-lap.
- If the car has no authored notes yet: return the shared setup-concepts alone, prefixed
  with "No car-specific notes for {car} yet — general LMU setup principles:".
- Deterministic text, like the other tools. On-demand pull — not injected per turn.
- Registered in `TOOLS` + `dispatch`. `CoachContext` gains the merged car notes (loaded
  once per turn like `cmap`), and the shared concepts loaded from the shipped file.

### Prompt

Extend `AGENTIC_SYSTEM_PROMPT`: for car handling, setup direction, or driver-aid
questions, call `get_car_notes` and ground advice in the authored notes + LMU setup
concepts; prefer these over general motorsport memory; respect provenance
(`[needs-refinement]` items are tentative — hedge). Never fabricate car character.

### Migration

One-time script `scripts/migrate_car_notes.py`: `simcoach/knowledge/<car>.md` →
`simcoach/cars/<slug>.json` `notes`:
- Intro paragraph → `overview`; `## Handling characteristics` → `handling`;
  `## Driver aids*` → `driver_aids`; `## Sources` → `sources` (list); `setup_concepts`
  left empty. Preserve provenance tags verbatim. Migrate the real cars
  (`ferrari_296_lmgt3`, `porsche_911_gt3_r_lmgt3`) + `test_car` (for tests).
- Delete the migrated car `.md`; **retire the legacy car-note load** in
  `Coach.build_prompt` (`coach.py:209-215`). (The track `.md` load was already retired;
  this removes the last `knowledge/*.md` load — confirm `knowledge_dir` has no other use,
  clean up if fully dead.)
- Scripted one-time pass (committed output); flag any `.md` that doesn't fit for hand-finishing.

## 3. Testing

- Loader: `load_car_notes` merges shipped + user overlay; `{}` when absent; slug matches
  the resolved car name; no traversal.
- Migration: parser round-trips a known car `.md` (Ferrari) into keyed `notes`
  (overview/handling/driver_aids/sources present, provenance tags preserved).
- `get_car_notes`: with authored notes → returns them + shared concepts; with NO notes →
  shared concepts alone + the "no car-specific notes yet" prefix; registered in
  TOOLS + dispatch.
- Shared concepts doc loads and is non-empty; is concise (sanity length check).
- Retirement: legacy car-note branch removed; any test asserting it updated to
  `get_car_notes`.

## 4. Scope boundaries

**In scope:** `cars/<slug>.json` store + `load_car_notes` loader; the shared
`_lmu_setup_concepts.json` (curated from the 20KB guide); `get_car_notes` coach tool +
prompt; migration of the car `.md` corpus; retiring the legacy car-note load.

**Out of scope (deferred/decided):**
- Any UI — no Cars screen, no editor, no api.ts, no endpoints.
- In-app authoring / LLM draft pass for cars.
- Automated scraping of external setup sources (curation is manual/offline; glean
  CONCEPTS, never copy paid setups — re: lmucarsetups.com).
- Community reference **times** (that's a track-notes field already) and live setup
  values (`get_setup`) — unchanged.

## 5. Files touched (anticipated)

- Create `simcoach/car_notes.py` — `load_car_notes` (mirrors `track_lookup.load_map_data`).
- Create `simcoach/cars/*.json` — migrated per-car notes + `_lmu_setup_concepts.json`
  (migration + curation output, committed).
- Create `scripts/migrate_car_notes.py` + `tests/test_migrate_car_notes.py`.
- Modify `simcoach/coach_tools.py` — `get_car_notes` tool + spec + dispatch; load merged
  car notes + shared concepts into `CoachContext`.
- Modify `simcoach/coach.py` — prompt guidance; retire the legacy car-note load.
- Modify the coach construction path (`__main__.py` / `server/__main__.py` /
  `server/app.py`) — pass the cars dir + user overlay dir into `CoachContext` (mirror how
  the map resolver/dirs are threaded).
- Delete migrated `simcoach/knowledge/<car>.md`.
- Tests across `tests/`.
