# simcoach

An AI sim-racing coach for **Le Mans Ultimate**. It captures your laps live from
the game's shared memory, then answers setup and driving questions between laps in
a running conversation — "where am I losing time?", "how do I fix corner-exit
oversteer?" — grounded in your own telemetry and current car setup.

## How it works

A deterministic pipeline does the measuring; the LLM only ever coaches on facts:

1. **Capture** — live lap telemetry (~50 Hz) is read from LMU's shared memory and
   stored (SQLite for history + queryable per-corner metrics, `.npz` for the raw
   per-lap channel stream keyed on a de-staircased ~1 m lap-distance axis, with
   exact elapsed time alongside it).
2. **Analyze** — per-corner facts and deltas vs. your best lap, plus stint-level
   reads — ranked time loss (where a lap gained or lost time, by corner and
   straight), consistency, car balance (understeer/oversteer, lockup, wheelspin),
   tire-deg trend, traction-circle/grip envelope use, and input shape (brake
   release, modulation) — are all computed from the telemetry and a per-track
   **corner map**. No AI in this step.
3. **Coach** — the model narrates: it calls tools to fetch exactly the computed
   facts a question needs (plus your current setup and car/track knowledge
   notes) and returns prioritized, specific advice. It never infers a delta,
   ranking, or corner identity itself.

**Corner identity is owned data, never guessed.** A corner's number, name, and
direction come from the corner map + telemetry geometry — the model receives them
as fact and is told never to infer them. See the design doc:
`docs/superpowers/specs/2026-08-27-lmu-ai-coach-design.md`.

## Running the coach

### Desktop app (v1)

A Tauri shell with a Svelte UI and a localhost FastAPI sidecar — coach chat, a
sessions browser, and a settings screen. Dev run (needs Rust + Node):

```
cargo tauri dev        # from repo root — spawns the Python sidecar + Vite dev server
```

First run: open **Settings → AI**, paste your Anthropic API key and pick a model.
The key is stored in the OS keyring (via `keyring` — Windows Credential Manager),
never in a file, and **no environment variables are required**. App data — config,
laps, telemetry, logs — lives under `%APPDATA%\SimCoach` (override with
`SIMCOACH_DATA_DIR`).

**Import history** (Settings → LMU history) backfills past sessions by parsing LMU
result logs (`UserData/Log/Results/*.xml`) — no live capture needed. Set your LMU
profile name(s) there so it can pick out your own laps.

The sidecar alone, for frontend dev: `python -m simcoach.server` (→ `127.0.0.1:8765`).

### Voice

Optional hands-free chat — enable it in **Settings → Voice**. First enable downloads
a local speech-recognition model (~250 MB, one-time). Hold the push-to-talk key (or
a bound wheel/gamepad button — captured in the same Settings section) to talk, or
tap it to start hands-free conversation mode; answers are read back aloud. No extra
API key or network service beyond the model download — transcription runs locally.

## Corner maps

Each track needs a corner map under `simcoach/tracks/<slug>.json`. It's what the
coach consumes for a corner's identity: number/label, name, direction, its
along-track window (start/end/apex distance), and any authored notes. Radius,
width, and banking bake are not a feature the coach uses — the map-building
pipeline is parked (`deprioritized`).

You don't build maps by hand: an in-app track-map editor reconstructs the track
surface from a point cloud of free-driving passes (weave across the width; no
left/right to designate), detects corners by curvature, and seeds sectors from
the game's real boundaries. You can then rename corners, add labels/sections,
rotate, and rebuild from the saved capture without re-driving. Your edits merge
over the shipped geometry as a user annotation overlay — the reconstructed
layout stays authoritative and only changes on a new app release. From a saved
lap you can also build one via CLI: `python -m simcoach.mapbuild <lap.npz>
[--slug SLUG] [--length METRES]`.

Real corner **names** (Copse, Ferradura, …) are an optional enrichment from the LMU
wiki / a circuit diagram / your own knowledge — the geometry works without them.
Live track names are matched to map files by `simcoach/track_lookup.py` (handles
suffixes, accents, and aliases like "Circuit de la Sarthe" → `lemans`).

## Car names

LMU's live shared memory exposes the car *class* and the *team entry* name (e.g.
`Team WRT 2026 #32:WEC`), not the specific model. LMU's own result logs, however,
name both together for every entry — `simcoach/car_lookup.py` scrapes
`UserData/Log/Results/*.xml` into a `{team entry → car model}` table, so the app
shows `BMW M4 LMGT3` instead of the ambiguous team string. No table to maintain:
any entry you've driven or raced against is already in your result logs. Unknown
entries fall back to `<class> car`, then the raw entry name.

The resolved model name loads its knowledge note from
`simcoach/knowledge/<slug>.md` (slugged from the model, e.g.
`porsche_911_gt3_r_lmgt3.md`).

## Dev setup

```
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"
.venv/Scripts/python.exe -m pytest
```

## Marketing site

A standalone landing page for Nestor lives in [`site/`](site/) — a single static
page (Caddy + Dockerfile) hosted on Railway (service `nestor`, live at
`nestor-production-83cb.up.railway.app`). It **auto-deploys from GitHub**: a push to
`main` that changes anything under `site/` triggers a build; unrelated commits are
skipped (watch path `/site/**`). Its screenshots are real captures of this app. See
[`site/README.md`](site/README.md) for local preview, how to refresh the screenshots
and brand assets, and the deploy + CNAME details.

## Status

As of 2026-09-15: v1.0 desktop app (Tauri + Svelte + FastAPI sidecar), built and
unit-tested (Python + frontend). The #99 course-correct is done — the coach only
states measured values, and Phase 1 of the engineer-grade analysis is complete
(ranked time loss, consistency, handling, tire trend, grip envelope, input
shape all live as tools). Installer packaging has landed — CI freezes the
sidecar and builds MSI/NSIS bundles on a `vX.Y.Z` tag (see
`packaging/README.md`). Remaining: code signing (installers are unsigned, so
SmartScreen warns), a full live shakedown at the rig, this docs pass, Phase 0
setup-channel capture, and the voice loop.
