# LMU AI Coach — v0.1 Design

Date: 2026-08-27
Status: approved for spec review

## Goal

A single long-running terminal app that captures laps live from Le Mans
Ultimate (LMU), then answers between-laps questions about what to change —
**setup and driving** — in one conversation. Corners are always identified
correctly, never guessed by the LLM.

## Scope

**v0.1 is:** LMU only, one car, one track. Silent live capture; pull-based
chat ("how was that lap?"). Setup + driving advice in one coach conversation.

**v0.1 is not (deferred, each bolts onto a left-open seam):** voice, in-ear
live coaching, Assetto Corsa, multiple tracks/cars, local model, vector-DB
RAG, HTTP server, web UI, GUI, CrewChief replacement.

## Architecture

One process. A UI-agnostic **core engine** exposed via an internal function
API; the terminal is the first thin client. A future local HTTP server + web
UI wraps the *same* core with no engine rework.

```
core engine (UI-agnostic)
  Capture (bg thread) -> lap store (+ setup snapshot)
  Feature extractor -> Coach -> Model
        ^ internal API (function calls)
  v0.1: terminal client   |   later: local HTTP server -> web view
```

Discipline this buys: coach/analysis logic lives behind function calls both
the terminal and a future server call — never inside the terminal loop.

## Components

1. **Capture service** — background thread polling LMU shared memory (~50 Hz).
   Buffers current lap; on lap completion writes a lap record (all channels +
   track/car/laptime) to the lap store, plus a snapshot of the active setup.
   Silent — no per-lap notification. *Built on interfaces verified by the
   spike (see Risk).*
2. **Corner map** — per-track JSON we own: `lap-distance -> {number, name,
   direction, apex}`. Direction **computed** from telemetry geometry
   (lateral-G / heading sign); names/numbers **hand-curated** from public
   references. Doubles as the "learn corner names" feature.
3. **Feature extractor** — pure, tested functions. In: lap record + corner map
   (+ optional reference lap). Out: per-corner metrics (entry/apex/min/exit
   speed, brake point, brake & throttle traces, trail-brake overlap, tire
   temps/pressures) and deltas. No LLM. The deterministic anti-hallucination
   core.
4. **Knowledge base** — small curated markdown notes on LMU/car-specific
   tuning, injected into the coach prompt. Flat files in v0.1 (Gemini notebook
   mined as a source); grown as gaps appear. No vector DB until notes outgrow
   whole-file loading.
5. **Coach** — takes structured analysis + corner **facts** + relevant
   knowledge notes -> LLM -> conversational answer with follow-ups. Claude API
   behind a swappable `Model` interface (cloud now, local-capable later).

## Data flow

LMU -> Capture -> lap store + setup snapshot. On user question: Feature
extractor builds a structured summary -> Coach loads corner facts + knowledge
notes -> LLM answers -> follow-ups. The LLM only ever **receives** corner
identity as prompt fact; it is structurally prevented from guessing left/right
or names.

## Data model & persistence

Captured data must support **historical, cross-session** analysis (trends,
best-lap selection, setup comparison) — not just the current run. Split by
access pattern:

- **SQLite** (stdlib `sqlite3`) holds everything queried across laps/sessions:
  - `laps(id, ts, sim, track, car, lap_time, valid, setup_id, telemetry_path)`
  - `setups(id, ts, track, car, params_json, source)`
  - `corner_metrics(lap_id, corner_no, entry_spd, apex_spd, min_spd, exit_spd,
    brake_point, throttle_point, trailbrake_overlap, ...)` — one row per corner
    per lap, so per-corner history is a plain query.
- **Per-lap channel files** hold the raw ~50 Hz time-series (referenced by
  `laps.telemetry_path`). Stored as compressed numpy `.npz` — loaded whole by
  the feature extractor, never queried row-by-row, so no columnar/Parquet
  dependency is needed. (Parquet is a drop-in swap later only if external tools
  must read channels directly.) Channels are stored at **full capture rate**
  with both a time and a **lap-distance** axis — distance is the alignment key
  a future replay / side-by-side overlay needs (motorsport overlays align on
  distance, not time). Recording this fidelity now is what keeps that feature
  possible later; nothing in v0.1 consumes it yet.

The feature extractor writes `corner_metrics` as a side effect of analyzing a
lap, so history accumulates automatically. "Best stored lap" (the reference
default) becomes a `SELECT min(lap_time)` on `laps`. Schema is versioned
(a `schema_version` pragma) so it can evolve without orphaning old data.

## Defaults

- **Reference lap** = user's own best stored lap for that track/car. None yet
  -> absolute analysis (min speeds, brake traces), no delta. No alien-lap
  sourcing in v0.1.
- **Stack** = Python on Windows. Terminal chat client.
- **Model** = Claude API behind a `Model` interface; local impl deferred.

## Testing

Feature extractor gets real unit tests against a recorded-lap fixture,
including an assert that computed corner direction matches known geometry.
Capture and coach stay thin.

## First steps: two spikes (before implementation, parallelizable)

1. **Feasibility spike (technical).** Whole design rests on reading LMU live
   telemetry, current setup, and lap boundaries. Those interfaces (rF2
   shared-memory plugin, LMU local REST API, setup file format) are
   **plausible but unverified**. Prove we can read all three on the user's
   machine. If interfaces differ, only the Capture component changes — seams
   isolate it.
2. **Research spike (product / prior art).** Survey what CoachDave Academy
   (setups, reference laps, Delta overlay) and similar tools (e.g. Track
   Titan, RaceStudio/MoTeC, Simulator-Controller) actually offer. Output: a
   findings doc feeding the roadmap/backlog. Read-only; does **not** by itself
   expand v0.1 scope — any feature it surfaces is triaged into v0.1 or
   deferred explicitly.
