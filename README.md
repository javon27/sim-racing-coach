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

Prerequisites:
- **LMU running and you on track** (it reads live shared memory to detect the track
  + car; it exits with a message if no session is found).
- **`ANTHROPIC_API_KEY`** set in your environment. Optionally set
  `SIMCOACH_MODEL_PROVIDER` (default `anthropic`).

```
.venv/Scripts/python.exe -m simcoach
```

It detects your current track and car, loads (or auto-builds) the corner map, then:
laps are captured in the background while you drive, and between laps you type
questions — e.g. `where am I losing time?` — answered against your best stored lap
on that track/car and your current setup. Type `quit` to exit.

## Corner maps

Each track needs a corner map under `simcoach/tracks/<slug>.json`. You don't build
these by hand:

- **Automatic** — if you drive a track with no map, the coach builds one from your
  first lap (numbered corners, real directions/distances from telemetry) and
  continues. `Turn N` names are the default and are honestly useful while you're
  still learning a circuit.
- **From a saved lap** — `python -m simcoach.mapbuild <lap.npz> [--slug SLUG]
  [--length METRES]` writes a validated map and self-checks corner directions.

Real corner **names** (Copse, Ferradura, …) are an optional enrichment from the LMU
wiki / a circuit diagram / your own knowledge — the geometry works without them.
Live track names are matched to map files by `simcoach/track_lookup.py` (handles
suffixes, accents, and aliases like "Circuit de la Sarthe" → `lemans`).

## Configuring your car

LMU exposes the car *class* and the *team entry* name, not the specific model, so
model matching is a small table you maintain in `simcoach/car_lookup.py`:

```python
CAR_ALIASES = {
    "iron dames": "Porsche 911 GT3 R",
    "by tf": "Chevrolet Corvette Z06 GT3.R",
}
```

Add a line when you race a new entry. Unknown entries fall back to `<class> car`
(the coach still runs, just without a model-specific note). The resolved model
name loads its knowledge note from `simcoach/knowledge/<slug>.md`.

## Dev setup

```
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"
.venv/Scripts/python.exe -m pytest
```

## Status

v0.1 is wired end-to-end (live capture → analysis → coach) and unit-tested; the
remaining step is a live shakedown at the rig. Deferred by design: voice, a web UI,
side-by-side replay, multi-provider models beyond Anthropic, and automated
corner-name enrichment.
