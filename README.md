# simcoach

An AI sim-racing coach for **Le Mans Ultimate**. It captures your laps live from
the game's shared memory, then answers setup and driving questions between laps in
a running conversation — "where am I losing time?", "how do I fix corner-exit
oversteer?" — grounded in your own telemetry and current car setup.

## How it works

A deterministic pipeline does the measuring; the LLM only ever coaches on facts:

1. **Capture** — live lap telemetry (~50 Hz) is read from LMU's shared memory and
   stored (SQLite for history + queryable per-corner metrics, `.npz` for the raw
   per-lap channel stream keyed on lap-distance).
2. **Analyze** — a per-corner summary (min speed, brake point, trail-brake overlap,
   deltas vs. your best lap) is computed from the telemetry and a per-track
   **corner map**.
3. **Coach** — that summary, your current setup, and car/track knowledge notes go
   to the model, which returns prioritized, specific advice.

**Corner identity is owned data, never guessed.** A corner's number, name, and
direction come from the corner map + telemetry geometry — the model receives them
as fact and is told never to infer them. See the design doc:
`docs/superpowers/specs/2026-08-27-lmu-ai-coach-design.md`.

## Running the coach

Two interfaces share the same core (capture → analysis → coach).

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

### Terminal

The original CLI coach loop. Needs **LMU running with you on track** (it reads the
live shared memory to detect track + car; exits if no session is found):

```
.venv/Scripts/python.exe -m simcoach
```

It loads (or auto-builds) the corner map, captures laps in the background while you
drive, and between laps answers questions — e.g. `where am I losing time?` — against
your best stored lap on that track/car and your current setup. Type `quit` to exit.
The API key comes from the keyring (set it once in the app) or an `ANTHROPIC_API_KEY`
override.

The sidecar alone, for frontend dev: `python -m simcoach.server` (→ `127.0.0.1:8765`).

## Corner maps

Each track needs a corner map under `simcoach/tracks/<slug>.json`. You don't build
these by hand:

- **In the desktop app** — a track-map editor reconstructs the track surface from a
  point cloud of free-driving passes (weave across the width; no left/right to
  designate), detects corners by curvature, and seeds sectors from the game's real
  boundaries. You can then rename corners, add labels/sections, rotate, and rebuild
  from the saved capture without re-driving. Your edits merge over the shipped
  geometry as a user annotation overlay — the reconstructed layout stays
  authoritative and only changes on a new app release.
- **Automatic (terminal)** — if you drive a track with no map, the CLI coach builds
  one from your first lap (numbered corners, real directions/distances from
  telemetry) and continues. `Turn N` names are the default and are honestly useful
  while you're still learning a circuit.
- **From a saved lap (CLI)** — `python -m simcoach.mapbuild <lap.npz> [--slug SLUG]
  [--length METRES]` writes a validated map and self-checks corner directions.

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
page (Caddy + Dockerfile) meant to be hosted on Railway under a `jaysontrades.com`
subdomain. Its screenshots are real captures of this app. See
[`site/README.md`](site/README.md) for local preview, how to refresh the
screenshots and brand assets, and the Railway deploy + CNAME steps.

## Status

v1.0 is the standalone desktop app (Tauri + Svelte + FastAPI sidecar) built over the
v0.1 core: live capture → per-corner analysis → streaming coach chat, plus a sessions
browser, an in-app track-map editor (point-cloud reconstruction + curvature-based
corner detection), a settings screen (keyring-backed key, driver profile, data
reset), and LMU result-log history import. Unit-tested (Python + frontend). Installer
packaging has landed — CI freezes the sidecar and builds MSI/NSIS bundles on a
`vX.Y.Z` tag (see `packaging/README.md`). Remaining: code signing (installers are
unsigned, so SmartScreen warns) and a full live shakedown at the rig. Deferred by
design: voice, side-by-side replay, multi-provider models beyond Anthropic, and
automated corner-name enrichment.
