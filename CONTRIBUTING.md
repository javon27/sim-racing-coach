# Contributing to Nestor

Thanks for helping. Bug reports, track corner maps, and fixes are all welcome.

## Before you start
- Open work lives in [GitHub Issues](https://github.com/javon27/sim-racing-coach/issues).
  For anything bigger than a small fix, comment on or open an issue first, so we agree
  on scope before you write code.
- Read `CLAUDE.md` (architecture invariants and layout) and, for the area you touch,
  the matching design spec in `docs/superpowers/specs/`.

## Setup and checks
```
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"
cd frontend && npm ci
```
CI runs these on every push and PR, and all of them must pass:
```
ruff check simcoach tests scripts
.venv/Scripts/python.exe -m pytest -q
cd frontend && npx vitest run
cd frontend && npx svelte-check --threshold error
```
The tests stub the game, so you don't need LMU installed to run them.

## Architecture rules (short version)
- **Corners are owned data.** Corner names, numbers and directions come from the
  per-track map and telemetry geometry. The model is never asked to infer them.
- **Code extracts, the model narrates.** Deltas, rankings and balance reads are
  computed in `analysis.py`/`format.py`. Only that computed summary reaches the model,
  never raw telemetry.
- **The coach states only measured values.** Cut an unverified value; don't hedge it.
- **Keep the core UI-agnostic.** Coaching logic stays out of `frontend/` and the
  `simcoach/server` routes.
- **The model sits behind `simcoach/model/`.** No direct SDK calls elsewhere.

## Labels
- `rig-verify`: the change needs checking in a live LMU session. Unit tests passing is
  not enough. Say in the PR what you checked in the game.
- `area:*` (coach/tracks/ui/capture/infra), `bug`/`enhancement`/`chore`, `future`.

## Pull requests
- Keep each PR to one issue, with tests for any changed behaviour.
- Use `Fixes #N` only when the PR fully closes the issue; otherwise write `Refs #N`.
- Track notes must be in your own words. If they are adapted from the LMU wiki, set
  `notes.source` to say so (that content is CC BY-SA 4.0).
- By contributing, you agree your contribution is licensed under the MIT license.
