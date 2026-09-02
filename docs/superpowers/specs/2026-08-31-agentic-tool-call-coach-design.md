# Agentic tool-call coach — design (2026-08-31)

Status: approved in chat for an overnight build; this doc is for morning review.
Branch: `feat/agentic-tool-coach`.

## Problem

The coach dumps the full 15-corner table + tires + setup into **every** prompt,
so it can't be smart about what a question needs, and each answer reads like a
fresh report rather than a conversation. The user wants the coach to feel like
Claude Code: session-state aware, pulling lap data **on demand via tool calls**,
sending only what the question needs.

## Approach

Move the coach from "inject everything" to an **agentic tool-use loop**. The
model gets a lightweight system prompt + the conversation, and calls tools to
fetch exactly the data it needs. Tool results are deterministic summaries built
by the existing analysis/store layer — the two-stage invariant holds (no raw
50 Hz telemetry ever reaches the model).

## Components

### 1. `Model` gains a tool loop
`base.py`: add `run(system, messages, tools, dispatch) -> str`.
- `tools`: list of `{"name","description","input_schema"}`.
- `dispatch(name, input_dict) -> str`: executes a tool, returns a text result.
- `ClaudeModel.run`: manual Anthropic agentic loop — `messages.create(tools=…)`;
  while `stop_reason == "tool_use"`, run each `tool_use` block via `dispatch`,
  append the assistant content + a single user message of `tool_result` blocks,
  repeat; return the final text. Cap iterations (e.g. 8) as a safety stop.
- `FakeModel.run`: scriptable — a queue of planned tool calls then a final reply,
  so the loop is unit-testable without the network.
- `complete()` stays for the non-tool fallback (fake provider, legacy path).

### 2. Tool surface — `coach_tools.py`
A `CoachContext` (live record, cmap, store, setup, units, peek) + `dispatch`.
Tools (each returns a compact text summary via shared formatters):
- `get_session_status()` — on-track?/track/car/#laps/has-reference. Orientation.
- `list_recent_laps(limit)` — recent laps (id, time, valid) so the model can pick.
- `get_lap_summary(which)` — full per-corner facts + tires for latest/best/lap_id.
- `get_corner(corner)` — one corner's detail (number or name) + delta vs best.
- `get_tire_report()` — per-wheel means + warm-up.
- `get_setup()` — filtered setup params (+ legal-adjustment note when tiered).

Formatters (`format_corner_line`, `format_lap_facts`, `format_tire_report`) are
factored out of `build_prompt` so the legacy path and the tools share them.

### 3. Coach — agentic system prompt
Persona + continuity + units + corner-naming ("Turn N (Name)") + no-raw-distance,
plus: "the driver's data is available through tools — call them to fetch exactly
what the question needs; don't ask for a full read unless the question wants one."
`Coach.ask` runs `model.run(system, messages, tools, dispatch)` when a dispatcher
is supplied; else falls back to the legacy injected prompt via `complete()`.

### 4. REPL wiring
`run_repl` builds a `CoachContext` bound to the live `latest`/`cmap`/setup/units
(re-resolved by the existing live-session fix) and passes tools + dispatch into
the session each turn. Session-state awareness is coarse (on-track vs not; garage
vs menu needs the UNRELIABLE struct tail — out of scope tonight).

## Invariants preserved
Two-stage (tools return summaries, not raw telemetry); corners owned (identity
from the map, never model-guessed); swappable model (loop behind `Model.run`,
each provider adapts its own format); storage split unchanged.

## Testing
Fake-model scripted tool calls exercise the loop and each tool end-to-end
(no network). A handful of live smoke calls against stored lap 82 (Algarve)
confirm the real model drives the tools. Live *feel* validation needs the user
+ LMU next session — not automatable.

## Out of scope / future (UI)
- **Telemetry graphing** (steering/throttle/brake/tire & brake temp+wear traces
  per lap) — the race-engineer view. This is the desktop UI; the on-demand tool
  layer + per-lap `.npz` channels are the data source it will render. Brake
  wear/temp are not captured yet (add channels when the UI needs them).
- Garage-vs-menu session state (needs the ScoringInfo tail work).
- Streaming the coach reply token-by-token in the terminal.
