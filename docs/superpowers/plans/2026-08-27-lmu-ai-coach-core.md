# LMU AI Coach — Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A runnable terminal app that, given a captured lap, produces a corner-by-corner analysis and answers between-laps coaching questions — built and tested against a synthetic-lap fixture, with live LMU capture deferred to a follow-up plan.

**Architecture:** UI-agnostic core behind a function API. A deterministic feature extractor turns a `LapRecord` + owned corner map into a structured summary; only that summary reaches a swappable `Model`. Corners are owned data — the LLM receives corner identity as fact, never guesses it. Storage splits queryable history (SQLite) from raw full-rate channels (per-lap `.npz`, keyed on lap-distance).

**Tech Stack:** Python 3.11+, numpy, pytest, ruff, `anthropic` SDK (Claude API), stdlib `sqlite3`. Windows.

**Spec:** `docs/superpowers/specs/2026-08-27-lmu-ai-coach-design.md`

## Global Constraints

- Python 3.11+, Windows. Package root `simcoach/`, tests in `tests/`.
- The LLM is reached ONLY through the `Model` protocol — no `anthropic` import outside `simcoach/model/`.
- Corner direction is computed from telemetry; corner names/numbers come from the corner-map file. No code path asks the model to infer either.
- Raw channels are stored full-rate with a `lap_distance` axis (the replay/overlay alignment key). Never feed raw channels to the model.
- Every non-trivial unit ships with a pytest test. Feature extractor and corner-direction get real assertions against the fixture.

---

### Task 1: Research spike — prior art (no code)

**Files:**
- Create: `docs/superpowers/research/2026-08-27-prior-art.md`

**Interfaces:** Produces a findings doc; no code artifacts.

- [ ] **Step 1: Survey tools.** Research CoachDave Academy (setups, reference laps, Delta overlay), Track Titan, RaceStudio/MoTeC i2, Simulator-Controller. For each: what it does, input data, coaching/analysis style, notable UX ideas.
- [ ] **Step 2: Write findings.** In the doc, one section per tool + a final "Ideas to triage" list mapping each idea to `v0.1 / defer / reject` with a one-line reason.
- [ ] **Step 3: Commit.** `git add docs/superpowers/research && git commit -m "docs: prior-art research spike"`

---

### Task 2: Feasibility spike — LMU data access (throwaway probe)

**Files:**
- Create: `spikes/lmu_access/` (throwaway — labeled, not part of `simcoach/`)
- Create: `docs/superpowers/research/2026-08-27-lmu-feasibility.md`

**Interfaces:** Produces a findings doc that finalizes the capture interface for the follow-up plan. Any code here is throwaway.

- [ ] **Step 1: Telemetry.** Confirm how to read LMU live telemetry (rF2 shared-memory plugin / memory-mapped file). Identify available channels; confirm `lap_distance`, speed, throttle, brake, steering, lateral-G, tire temps/pressures exist and their units + sign conventions.
- [ ] **Step 2: Setup.** Confirm how to read the active car setup (LMU local REST API and/or setup file path + format).
- [ ] **Step 3: Lap boundaries.** Confirm how lap start/finish and validity are detected from the data.
- [ ] **Step 4: Write findings.** Document exact interfaces, field names, units, sign conventions, and a recommended `TelemetrySource` mapping to `LapRecord` (Task 4). Note any gaps vs. spec assumptions.
- [ ] **Step 5: Commit.** `git add docs/superpowers/research && git commit -m "docs: LMU feasibility spike"` (do not commit throwaway probe unless useful as reference).

---

### Task 3: Project scaffold

**Files:**
- Create: `pyproject.toml`, `simcoach/__init__.py`, `tests/__init__.py`, `README.md`

**Interfaces:** Produces the importable `simcoach` package and `pytest`/`ruff` config for all later tasks.

- [ ] **Step 1: Write `pyproject.toml`.**

```toml
[project]
name = "simcoach"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["numpy>=1.26", "anthropic>=0.40"]

[project.optional-dependencies]
dev = ["pytest>=8", "ruff>=0.6"]

[tool.pytest.ini_options]
testpaths = ["tests"]

[tool.ruff]
line-length = 100
```

- [ ] **Step 2: Create empty `simcoach/__init__.py` and `tests/__init__.py`.**
- [ ] **Step 3: Verify install.** Run: `python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"`. Expected: succeeds, `pytest` runs (0 tests).
- [ ] **Step 4: Commit.** `git add -A && git commit -m "chore: project scaffold"`

---

### Task 4: LapRecord + synthetic fixture

**Files:**
- Create: `simcoach/model_types.py`, `tests/conftest.py`, `tests/test_model_types.py`

**Interfaces:**
- Produces: `LapRecord` dataclass — `sim: str, track: str, car: str, lap_time: float, valid: bool, timestamp: float, channels: dict[str, np.ndarray]`. Required channel keys: `lap_distance, time, speed, throttle, brake, steering, lat_g` plus `tire_temp_fl/fr/rl/rr`, `tire_press_fl/fr/rl/rr`. All channels are 1-D float arrays of equal length, ordered by increasing `lap_distance` over [0, track_length].
- Produces: pytest fixture `synthetic_lap` → a `LapRecord` with a known layout: two corners (one left, one right) with defined brake/throttle/steering traces, used by all downstream tests.

- [ ] **Step 1: Write failing test.**

```python
def test_laprecord_channels_equal_length(synthetic_lap):
    n = len(synthetic_lap.channels["lap_distance"])
    assert n > 0
    assert all(len(v) == n for v in synthetic_lap.channels.values())
    d = synthetic_lap.channels["lap_distance"]
    assert (d[1:] >= d[:-1]).all()  # monotonic non-decreasing
```

- [ ] **Step 2: Run — expect FAIL** (`synthetic_lap` fixture / `LapRecord` missing). Run: `pytest tests/test_model_types.py -v`
- [ ] **Step 3: Implement `LapRecord`.**

```python
from dataclasses import dataclass
import numpy as np

@dataclass
class LapRecord:
    sim: str
    track: str
    car: str
    lap_time: float
    valid: bool
    timestamp: float
    channels: dict[str, np.ndarray]
```

- [ ] **Step 4: Implement fixture in `conftest.py`.** Build a ~1000-sample lap over 3000 m: flat straight, then a braking + left corner (steering < 0), reacceleration, then a right corner (steering > 0). Set `lat_g` sign consistent with steering, tire temps rising through corners.

```python
import numpy as np, pytest
from simcoach.model_types import LapRecord

@pytest.fixture
def synthetic_lap():
    n = 1000
    dist = np.linspace(0, 3000, n)
    speed = np.full(n, 60.0)
    brake = np.zeros(n); throttle = np.ones(n); steer = np.zeros(n)
    left = (dist > 800) & (dist < 1200)     # left corner
    right = (dist > 2000) & (dist < 2400)   # right corner
    for mask, sign in ((left, -1.0), (right, 1.0)):
        brake[mask] = 0.8; throttle[mask] = 0.1
        steer[mask] = sign * 0.5; speed[mask] = 30.0
    lat_g = steer * 2.0
    ch = {"lap_distance": dist, "time": dist / 40.0, "speed": speed,
          "throttle": throttle, "brake": brake, "steering": steer, "lat_g": lat_g}
    for t in ("fl", "fr", "rl", "rr"):
        ch[f"tire_temp_{t}"] = 80 + 5 * (brake > 0)
        ch[f"tire_press_{t}"] = np.full(n, 27.5)
    return LapRecord("lmu", "test_track", "test_car", 92.5, True, 0.0, ch)
```

- [ ] **Step 5: Run — expect PASS.** Run: `pytest tests/test_model_types.py -v`
- [ ] **Step 6: Commit.** `git add -A && git commit -m "feat: LapRecord + synthetic lap fixture"`

---

### Task 5: Persistence (SQLite + npz)

**Files:**
- Create: `simcoach/store.py`, `tests/test_store.py`

**Interfaces:**
- Consumes: `LapRecord` (Task 4).
- Produces: `LapStore(db_path: str, data_dir: str)` with:
  - `save(record: LapRecord, metrics: list[CornerMetrics] | None = None) -> int` (returns `lap_id`; writes channels to `{data_dir}/{lap_id}.npz`, a row to `laps`, and any metrics rows).
  - `load(lap_id: int) -> LapRecord`
  - `best_lap(track: str, car: str) -> LapRecord | None` (fastest valid lap).
  - Tables: `laps(id, ts, sim, track, car, lap_time, valid, telemetry_path)`, `setups(id, ts, track, car, params_json, source)`, `corner_metrics(lap_id, corner_no, entry_spd, apex_spd, min_spd, exit_spd, brake_point, throttle_point, trailbrake_overlap)`. `PRAGMA user_version` holds schema version 1.

- [ ] **Step 1: Write failing test.**

```python
def test_save_load_and_best(tmp_path, synthetic_lap):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path/"c.sqlite"), str(tmp_path/"data"))
    lid = s.save(synthetic_lap)
    back = s.load(lid)
    assert back.lap_time == synthetic_lap.lap_time
    assert np.allclose(back.channels["speed"], synthetic_lap.channels["speed"])
    slow = LapRecord(**{**synthetic_lap.__dict__, "lap_time": 99.0})
    s.save(slow)
    assert s.best_lap("test_track", "test_car").lap_time == 92.5
```

- [ ] **Step 2: Run — expect FAIL.** Run: `pytest tests/test_store.py -v`
- [ ] **Step 3: Implement `LapStore`.** Create tables on init (idempotent, set `user_version=1`). `save`: insert lap row, get `lastrowid`, `np.savez_compressed` channels to `{data_dir}/{id}.npz`, update `telemetry_path`, insert metrics rows if given. `load`: read row + `np.load`. `best_lap`: `SELECT id FROM laps WHERE track=? AND car=? AND valid=1 ORDER BY lap_time ASC LIMIT 1`.
- [ ] **Step 4: Run — expect PASS.** Run: `pytest tests/test_store.py -v`
- [ ] **Step 5: Commit.** `git add -A && git commit -m "feat: SQLite+npz lap store"`

---

### Task 6: Corner map + computed direction

**Files:**
- Create: `simcoach/corners.py`, `simcoach/tracks/test_track.json`, `tests/test_corners.py`

**Interfaces:**
- Consumes: `LapRecord` (Task 4).
- Produces:
  - `Corner` dataclass — `number: int, name: str, direction: str, start_dist: float, apex_dist: float, end_dist: float`.
  - `CornerMap.load(path: str) -> CornerMap`; `.corner_at(dist: float) -> Corner | None`; `.corners: list[Corner]`.
  - `compute_direction(record: LapRecord, start: float, end: float) -> str` — returns `"left"` or `"right"` from the sign of mean `steering` over `[start,end]`. Sign convention constant `RIGHT_IS_POSITIVE = True` (a documented calibration knob; the feasibility spike sets its true value for LMU).
  - `verify_map_directions(record, cmap) -> list[tuple[int,str,str]]` — returns `(corner_no, declared, computed)` for any corner whose file `direction` disagrees with `compute_direction`. Empty list = consistent.

- [ ] **Step 1: Write failing test (the anti-hallucination assertion).**

```python
def test_computed_direction_matches_geometry(synthetic_lap):
    from simcoach.corners import compute_direction
    assert compute_direction(synthetic_lap, 800, 1200) == "left"
    assert compute_direction(synthetic_lap, 2000, 2400) == "right"

def test_map_directions_consistent(synthetic_lap):
    from simcoach.corners import CornerMap, verify_map_directions
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    assert verify_map_directions(synthetic_lap, cmap) == []
```

- [ ] **Step 2: Run — expect FAIL.** Run: `pytest tests/test_corners.py -v`
- [ ] **Step 3: Write `test_track.json`.**

```json
{"track": "test_track", "corners": [
  {"number": 1, "name": "Left Hairpin", "direction": "left",  "start_dist": 800,  "apex_dist": 1000, "end_dist": 1200},
  {"number": 2, "name": "Right Sweeper", "direction": "right", "start_dist": 2000, "apex_dist": 2200, "end_dist": 2400}
]}
```

- [ ] **Step 4: Implement `corners.py`.** `compute_direction`: slice `steering` where `start<=lap_distance<=end`, take mean; positive→(`"right"` if `RIGHT_IS_POSITIVE` else `"left"`), negative→the other. `CornerMap.load`: parse JSON into `Corner` list. `corner_at`: first corner whose `[start,end]` contains dist. `verify_map_directions`: compare each corner's declared vs computed.
- [ ] **Step 5: Run — expect PASS.** Run: `pytest tests/test_corners.py -v`
- [ ] **Step 6: Commit.** `git add -A && git commit -m "feat: corner map + computed direction"`

---

### Task 7: Feature extractor

**Files:**
- Create: `simcoach/analysis.py`, `tests/test_analysis.py`

**Interfaces:**
- Consumes: `LapRecord` (Task 4), `CornerMap`/`Corner` (Task 6).
- Produces:
  - `CornerMetrics` dataclass — `corner_no: int, name: str, direction: str, entry_spd: float, apex_spd: float, min_spd: float, exit_spd: float, brake_point: float, throttle_point: float, trailbrake_overlap: float` (+ optional `min_spd_delta: float | None`).
  - `LapAnalysis` dataclass — `lap_time: float, corners: list[CornerMetrics], has_reference: bool`.
  - `analyze_lap(record: LapRecord, cmap: CornerMap, reference: LapRecord | None = None) -> LapAnalysis`.
  - Helper `_metrics_for_corner(record, corner) -> CornerMetrics`.
- Definitions (compute over each corner's `[start,end]` window on `lap_distance`): `entry_spd` = speed at start; `apex_spd` = speed at `apex_dist`; `min_spd` = min speed; `exit_spd` = speed at end; `brake_point` = first `lap_distance` in window where `brake > 0.1` (else `end_dist`); `throttle_point` = first `lap_distance` where `throttle > 0.8` after the min-speed point (else `end_dist`); `trailbrake_overlap` = fraction of window samples where `brake > 0.1` AND `abs(steering) > 0.1`. With a reference, `min_spd_delta = min_spd - reference_min_spd` for the matching corner number.

- [ ] **Step 1: Write failing test.**

```python
def test_analyze_lap_metrics(synthetic_lap):
    from simcoach.analysis import analyze_lap
    from simcoach.corners import CornerMap
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    a = analyze_lap(synthetic_lap, cmap)
    assert a.has_reference is False
    c1 = a.corners[0]
    assert c1.corner_no == 1 and c1.direction == "left"
    assert abs(c1.min_spd - 30.0) < 1e-6
    assert 800 <= c1.brake_point <= 1200
    assert c1.trailbrake_overlap > 0.0

def test_analyze_lap_delta(synthetic_lap):
    from simcoach.analysis import analyze_lap
    from simcoach.corners import CornerMap
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    a = analyze_lap(synthetic_lap, cmap, reference=synthetic_lap)
    assert a.has_reference is True
    assert abs(a.corners[0].min_spd_delta) < 1e-6
```

- [ ] **Step 2: Run — expect FAIL.** Run: `pytest tests/test_analysis.py -v`
- [ ] **Step 3: Implement `analysis.py`** per the definitions above. Use boolean masks on `lap_distance`; guard empty windows by returning `end_dist`/`nan`-safe values.
- [ ] **Step 4: Run — expect PASS.** Run: `pytest tests/test_analysis.py -v`
- [ ] **Step 5: Commit.** `git add -A && git commit -m "feat: deterministic feature extractor"`

---

### Task 8: Model interface + Claude impl

**Files:**
- Create: `simcoach/model/__init__.py`, `simcoach/model/base.py`, `simcoach/model/claude.py`, `simcoach/model/fake.py`, `tests/test_model.py`

**Interfaces:**
- Produces:
  - `Model` protocol (`base.py`) — `def complete(self, system: str, messages: list[dict]) -> str`. `messages` are `{"role": "user"|"assistant", "content": str}`.
  - `ClaudeModel(model: str = "claude-opus-4-8[1m]", api_key: str | None = None)` implementing `Model` via the `anthropic` SDK. THIS IS THE ONLY FILE THAT IMPORTS `anthropic`.
  - `FakeModel(reply: str = "ok")` implementing `Model`, recording the last `system` and `messages` for assertions.

- [ ] **Step 1: Write failing test (uses the fake — no network).**

```python
def test_fake_model_records_call():
    from simcoach.model.fake import FakeModel
    m = FakeModel(reply="brake later")
    out = m.complete("sys", [{"role": "user", "content": "hi"}])
    assert out == "brake later"
    assert m.last_system == "sys"
```

- [ ] **Step 2: Run — expect FAIL.** Run: `pytest tests/test_model.py -v`
- [ ] **Step 3: Implement `base.py`** (`typing.Protocol`), **`fake.py`**, and **`claude.py`** (`anthropic.Anthropic().messages.create(...)`, return `resp.content[0].text`). Keep `claude.py` untested by unit tests (network); it is exercised manually.
- [ ] **Step 4: Run — expect PASS.** Run: `pytest tests/test_model.py -v`
- [ ] **Step 5: Commit.** `git add -A && git commit -m "feat: Model interface + Claude/Fake impls"`

---

### Task 9: Coach

**Files:**
- Create: `simcoach/coach.py`, `simcoach/knowledge/test_car.md`, `tests/test_coach.py`

**Interfaces:**
- Consumes: `LapAnalysis` (Task 7), `CornerMap` (Task 6), `Model` (Task 8).
- Produces:
  - `Coach(model: Model, knowledge_dir: str)`.
  - `build_prompt(analysis: LapAnalysis, cmap: CornerMap, car: str, question: str) -> tuple[str, str]` → `(system, user_content)`. The user content embeds corner FACTS (number, name, direction, apex) and per-corner metrics as text, plus loaded knowledge notes for `car`, plus the question. The system prompt states corners are authoritative facts and must not be re-guessed.
  - `ask(analysis, cmap, car, question, history: list[dict] | None = None) -> str` — builds the prompt, calls `model.complete`, returns the reply.

- [ ] **Step 1: Write failing test (fake model asserts corner facts reach the prompt, never guessed).**

```python
def test_coach_injects_corner_facts(synthetic_lap):
    from simcoach.model.fake import FakeModel
    from simcoach.coach import Coach
    from simcoach.corners import CornerMap
    from simcoach.analysis import analyze_lap
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    a = analyze_lap(synthetic_lap, cmap)
    m = FakeModel(reply="trail-brake deeper")
    c = Coach(m, "simcoach/knowledge")
    out = c.ask(a, cmap, "test_car", "where am I losing time?")
    assert out == "trail-brake deeper"
    sent = m.last_messages[-1]["content"]
    assert "Left Hairpin" in sent and "left" in sent   # fact injected
    assert "test_car" in m.last_system or "corner" in m.last_system.lower()
```

- [ ] **Step 2: Run — expect FAIL.** Run: `pytest tests/test_coach.py -v`
- [ ] **Step 3: Write `knowledge/test_car.md`** (a few lines of dummy tuning notes) and implement `coach.py`. `build_prompt` renders each corner as `"C{n} {name} ({direction}, apex {apex}m): min {min_spd} km/h, brake@{brake_point}m, trailbrake {overlap:.0%}"`; loads `{knowledge_dir}/{car}.md` if present. System prompt: "The corner list is authoritative fact from telemetry. Never infer or change a corner's number, name, or direction."
- [ ] **Step 4: Run — expect PASS.** Run: `pytest tests/test_coach.py -v`
- [ ] **Step 5: Commit.** `git add -A && git commit -m "feat: coach prompt assembly"`

---

### Task 10: Terminal app + replay source

**Files:**
- Create: `simcoach/source.py`, `simcoach/app.py`, `simcoach/__main__.py`, `tests/test_source.py`

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `TelemetrySource` protocol (`source.py`) — `def laps(self) -> Iterator[LapRecord]`. This is the seam the deferred LMU live reader implements later.
  - `ReplaySource(store: LapStore, track: str, car: str)` yielding stored laps — lets the app run end-to-end with no LMU.
  - `run_repl(source, store, cmap, coach, car)` (`app.py`) — background thread pulls `source.laps()` into `store`; foreground loop reads questions, analyzes the latest lap (reference = `store.best_lap`), prints `coach.ask(...)`. Silent capture (no per-lap prompt), per spec.
  - `python -m simcoach` entry point wiring a `ClaudeModel`, store, and (for now) a `ReplaySource` over the fixture-seeded store.

- [ ] **Step 1: Write failing test (replay source only — REPL is manual).**

```python
def test_replay_source_yields_saved(tmp_path, synthetic_lap):
    from simcoach.store import LapStore
    from simcoach.source import ReplaySource
    s = LapStore(str(tmp_path/"c.sqlite"), str(tmp_path/"d"))
    s.save(synthetic_lap)
    got = list(ReplaySource(s, "test_track", "test_car").laps())
    assert len(got) == 1 and got[0].track == "test_track"
```

- [ ] **Step 2: Run — expect FAIL.** Run: `pytest tests/test_source.py -v`
- [ ] **Step 3: Implement** `source.py` (protocol + `ReplaySource` querying `laps` rows and loading each), `app.py` (`run_repl` with a `threading.Thread` for capture, `input()` loop for chat), and `__main__.py` wiring. Keep `run_repl` thin; no logic beyond orchestration.
- [ ] **Step 4: Run — expect PASS.** Run: `pytest tests/test_source.py -v`
- [ ] **Step 5: Manual smoke.** Seed the store with the fixture, run `python -m simcoach`, ask "where am I losing time?", confirm a coached answer referencing the correct corners. (Requires `ANTHROPIC_API_KEY`.)
- [ ] **Step 6: Commit.** `git add -A && git commit -m "feat: terminal app + replay source"`

---

## Deferred to follow-up plan (post feasibility spike)

- `LmuSource` implementing `TelemetrySource`: read LMU shared memory on the background thread, segment laps, map fields → `LapRecord` channels using the units/sign conventions confirmed in Task 2 (including the real value of `RIGHT_IS_POSITIVE`).
- Active-setup capture into the `setups` table.
- Real corner maps + knowledge notes for the actual first car/track.

## Self-Review

- **Spec coverage:** capture (deferred + Task 10 seam), corner-owned-data (Tasks 6, 9), two-stage pipeline (Task 7 → 9), knowledge base (Task 9), swappable model (Task 8), SQLite+npz history with lap-distance axis (Tasks 4, 5), reference-lap default (Tasks 5 `best_lap`, 7 delta), terminal client / UI-agnostic core (Task 10), both spikes (Tasks 1, 2). Covered.
- **Placeholder scan:** none — all steps carry concrete code or exact commands.
- **Type consistency:** `LapRecord`, `Corner`, `CornerMetrics`, `LapAnalysis`, `Model.complete`, `LapStore.save/load/best_lap`, `CornerMap.load/corner_at`, `compute_direction`, `analyze_lap`, `Coach.ask/build_prompt`, `TelemetrySource.laps` used consistently across tasks.
