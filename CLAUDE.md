# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Status

Pre-implementation. The only artifact is the approved v0.1 design spec:
`docs/superpowers/specs/2026-08-27-lmu-ai-coach-design.md`. Read it before
proposing or writing code. There is no build, lint, or test setup yet
(planned: Python on Windows). Implementation begins with two spikes defined
in the spec (LMU data-access feasibility + a prior-art research survey).

## What this is

An AI sim-racing coach for Le Mans Ultimate (LMU). One long-running terminal
app captures laps live, then answers between-laps questions about setup and
driving changes in a conversation. v0.1 is LMU-only, one car, one track.

## Architecture invariants (do not violate without updating the spec)

- **Corners are owned data, never LLM-guessed.** Corner direction is *computed*
  from telemetry geometry; names/numbers come from a per-track corner-map file.
  The LLM only ever *receives* corner identity as a fact in the prompt. Any code
  path that asks the model to infer left/right or a corner name is a bug.
- **Two-stage pipeline.** Deterministic feature extraction (pure, tested, no
  LLM) produces a structured per-corner summary; only that summary reaches the
  LLM. Never feed raw ~50 Hz telemetry to the model.
- **UI-agnostic core.** Capture, lap store, feature extractor, knowledge, and
  coach sit behind an internal function API. The terminal is one thin client; a
  future local HTTP server + web UI must reuse the same core unchanged. Keep
  coach/analysis logic out of the terminal loop.
- **Swappable model.** The LLM sits behind a `Model` interface (Claude API now,
  local model later). No direct SDK calls scattered through the code.
- **Storage split by access pattern.** Cross-session/history data (laps,
  setups, per-corner metrics) lives in SQLite; raw full-rate channels live in
  per-lap files keyed on a **lap-distance axis** (the alignment key a future
  replay/side-by-side overlay needs). Preserve this fidelity even though v0.1
  does not consume it.

## Scope discipline

The spec's deferred list (voice, live in-ear coaching, Assetto Corsa,
multi-track/car, local model, vector-DB RAG, HTTP server/web UI, CrewChief
replacement) is intentional. Do not build ahead of it; each item bolts onto a
seam left open. New feature ideas get triaged into v0.1 or deferred explicitly,
not added silently.
