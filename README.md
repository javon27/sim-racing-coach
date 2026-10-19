# Nestor

An AI sim-racing coach for **Le Mans Ultimate**. It captures your laps live from
the game's shared memory, then answers setup and driving questions between laps in
a running conversation — "where am I losing time?", "how do I fix corner-exit
oversteer?" — grounded in your own telemetry and current car setup.

Windows only (LMU is Windows-only). Free and open source under the MIT license.

## Getting started

1. Install from the latest [GitHub Release](https://github.com/javon27/sim-racing-coach/releases)
   (MSI or NSIS installer), or build from source (below).
2. Open **Settings → AI**, paste your own [Anthropic API key](https://console.anthropic.com/)
   and pick a model.
3. Start LMU and drive. Laps are captured automatically; ask the coach between laps.

**Bring your own key.** Nestor has no server and no account: the coach calls the
Anthropic API directly with your key, which is stored in the Windows Credential
Manager, never in a file. You pay Anthropic for what you use. As a rough guide, the
author's first 249 questions on Claude Sonnet averaged about **$0.04 per question**.
The chat screen shows a running token and cost meter.

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

## Building from source

Needs Python 3.11+, Node 20 and Rust (stable).

```
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"   # voice loop: see packaging/README.md (GPL-free install)
cd frontend && npm ci && cd ..
cargo tauri dev                                      # run the app
```

Installers are built by CI on a `vX.Y.Z` tag; see `packaging/README.md`.
Contributing: see [`CONTRIBUTING.md`](CONTRIBUTING.md).

## License

MIT, see [`LICENSE`](LICENSE). Some bundled content carries its own license (LMU
wiki-derived track notes are CC BY-SA 4.0; CMUdict is BSD). See
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

Nestor is an independent fan project. It is not affiliated with, endorsed by, or
sponsored by Studio 397, Motorsport Games, or the Automobile Club de l'Ouest. Le
Mans Ultimate and related names are trademarks of their respective owners.
