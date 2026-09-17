# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Status

v1.0 desktop app, built and unit-tested; remaining before release is code
signing and a full live shakedown at the rig. The v0.1 core (live capture →
per-corner analysis → coach) is done and shipped behind a Tauri + Svelte
desktop app driven by a localhost FastAPI sidecar (`simcoach/server`). See
`README.md` for how to run it.

Design specs (read before changing the areas they cover):
- `docs/superpowers/specs/2026-08-27-lmu-ai-coach-design.md` — v0.1 core.
- `docs/superpowers/specs/2026-08-29-v1-standalone-ui-design.md` — desktop app.
- `docs/superpowers/specs/2026-08-31-agentic-tool-call-coach-design.md` — the
  tool-use coach loop.

`docs/vision.md` is the north star.

## Tracking work

- **Open work → GitHub Issues** (the single source of truth). Labels: `area:*`
  (coach/tracks/ui/capture/infra), `rig-verify` (needs a live eyeball at the rig),
  `bug`/`enhancement`/`chore`, `future` (roadmap). `docs/v1-backlog.md` is now just a
  pointer to Issues; shipped work lives in git history + closed issues.
- **Design → specs/plans** in `docs/superpowers/`.
- **Durable context / decisions (the *why*) → project memory** (auto-memory,
  `.remember`). Memory is NOT a task list — never let it drift back into a backlog;
  open tasks belong in Issues.

## What this is

An AI sim-racing coach for Le Mans Ultimate (LMU). It captures laps live from
the game's shared memory, then answers between-laps setup and driving questions
in a running conversation, grounded in the driver's own telemetry and current
setup. The desktop app is the one client on top of that shared core.

## Build & test

Python on Windows; frontend is Svelte + TypeScript under `frontend/`.

```
.venv/Scripts/python.exe -m pip install -e ".[dev]"
.venv/Scripts/python.exe -m pytest              # backend
ruff check simcoach tests scripts               # lint (the exact CI gate command; see #48)
cd frontend && npm test                         # frontend (vitest)
cd frontend && npx svelte-check                 # frontend types
```

CI (`.github/workflows/test.yml`) runs `ruff check simcoach tests scripts`,
`pytest -q`, `vitest run`, and `svelte-check --threshold error` on every push
and PR. `ruff` is pinned (`pyproject.toml` `[project.optional-dependencies]
dev`) with an explicit `[tool.ruff.lint] ignore` for the repo's deliberate
patterns, so a clean `ruff check` is always achievable (#48) -- it does not
silently drift as ruff's own default rule set broadens over releases.

## Architecture invariants (do not violate without updating the spec)

- **Corners are owned data, never LLM-guessed.** Corner direction is *computed*
  from telemetry geometry; names/numbers come from a per-track corner-map file.
  The LLM only ever *receives* corner identity as a fact in the prompt. Any code
  path that asks the model to infer left/right or a corner name is a bug.
- **Two-stage pipeline.** Deterministic feature extraction (pure, tested, no
  LLM) produces a structured per-corner summary; only that summary reaches the
  LLM. Never feed raw ~50 Hz telemetry to the model.
- **UI-agnostic core.** Capture, lap store, feature extractor, knowledge, and
  coach sit behind an internal function API. The desktop app's FastAPI sidecar
  (`simcoach/server`) is thin and reuses that core unchanged. Keep coach/analysis
  logic out of the UI layer.
- **Swappable model.** The LLM sits behind a `Model` interface (Claude API now,
  local model later). No direct SDK calls scattered through the code.
- **Storage split by access pattern.** Cross-session/history data (laps,
  setups, per-corner metrics) lives in SQLite; raw full-rate channels live in
  per-lap files keyed on a **lap-distance axis** (the alignment key a future
  replay/side-by-side overlay needs). Preserve this fidelity even though v0.1
  does not consume it.
- **The coach only states measured values.** An unverified or stale value is
  cut from the prompt, not hedged — this is why baked corner radius/width and
  `track_edge` were pulled from `format_corner_line` rather than caveated.
- **Code extracts, the model narrates.** Deltas vs best, rankings, consistency,
  and balance reads are computed in `analysis.py`/`format.py`; the model is
  never asked to infer early/late or which corner cost time.

## Layout

- `simcoach/` — the core. `source.py`/`lmu/smm.py` (shared-memory capture),
  `store.py` (SQLite + `.npz` laps), `analysis.py`/`corners.py`/`format.py`
  (per-corner feature extraction — entry points that matter: `analyze_lap`,
  `time_loss_buckets`, `stint_consistency`, `stint_tire_trend`, `handling_read`,
  `grip_envelope`, `input_shape`, `_interp_lap_distance`), `coach.py`/
  `coach_tools.py` (tool-use coach), `model/` (swappable `Model`),
  `trackmap.py`/`mapbuild.py`/`ribbon.py`/`raceline.py` (corner maps +
  point-cloud track reconstruction), `track_lookup.py` (live-name → map/model
  resolution + `resolve_context`), `car_lookup.py` (live-name → car model),
  `voice.py` (local speech-to-text + Kokoro model download/transcription),
  `tts_kokoro.py` (offline Kokoro TTS behind the `Speaker` seam with NO GPL code loaded:
  import stubs + misaki without spaCy — pinned to `kokoro-onnx==0.6.1`, `misaki==0.7.4`),
  `speech_numbers.py` (number-to-words for spoken cues; also the `num2words` shim),
  `pronunciations.py` (per-track `pronunciations` table → misaki inline markup;
  harvested once by `scripts/harvest_pronunciations.py` on a dev machine, never shipped).
- `simcoach/server/` — the FastAPI sidecar the desktop app talks to.
- `simcoach/tracks/*.json` — committed shipped corner maps (curated defaults).
- `frontend/` — Svelte + TypeScript desktop UI; `src-tauri/` — the Tauri shell.
  Voice loop: `voice.ts` (state machine), `audio.ts` (browser mic/TTS adapters),
  `voice-shell.ts` (Tauri bridge — global hotkey + wheel/gamepad binds/capture);
  `src-tauri/src/voice_input.rs` (Win32 button poller + global shortcut).
- `packaging/` — PyInstaller sidecar freeze + installer notes (see its README).

## Scope discipline

The deferred list (voice + live in-ear coaching, Assetto Corsa, multi-car/track
per session, local model, vector-DB RAG, side-by-side replay overlay, CrewChief
replacement) is intentional — each bolts onto a seam left open. The web UI is no
longer deferred: it is realized as the FastAPI sidecar + Tauri app. Do not build
ahead of the list; new ideas get triaged into scope or deferred explicitly, not
added silently.
