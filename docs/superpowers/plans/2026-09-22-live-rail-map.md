# Live Rail Map + Retire the Pit Wall Implementation Plan (#151)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On the Coach screen, the existing rail `TrackMapPane` goes live while on track (the player's car only, sector boundaries, and a server-computed `Next: T10 Pouhon (L)` callout). A new expand control widens the rail and pushes the chat narrower, and never covers it. The expand control replaces the 🔍 overlay. The Pit Wall screen is retired.

**Architecture:** A pure `next_corner(corners, dist)` in `simcoach/corners.py` picks the corner. The `/live` websocket resolves the live track's `CornerMap` once per track (cached on `app.state.live_cmap` and cleared by `_invalidate_canon`), then sends `next_corner` with the player's lap distance. Once the Pit Wall is gone, the payload is trimmed to `{live, player, next_corner}`. The frontend reuses `TrackMap`/`TrackMapPane`. `TrackMapPane` subscribes to the existing `liveField` and draws the live track's canonical map (from `eventFeed.map`) with one car dot. `App.svelte` owns the rail width (a `rail-wide` class on `.main`, remembered in `localStorage` inside try/catch), and also owns the map height bump that goes with it.

**Tech Stack:** Python 3.13, FastAPI (websocket), pytest; Svelte 5 + TypeScript, vitest + @testing-library/svelte.

**Spec:** `docs/superpowers/specs/2026-09-22-coach-absorbs-pit-wall-design.md` §1 (the "Decided 2026-09-22 (driver, final)" block governs) and §2. §3 (strategy tools) is #152 and is **out of scope** here.

## Global Constraints

- **Work in a worktree on a branch.** The main checkout has the driver's uncommitted files (`simcoach/tracks/weathertech_raceway_laguna_seca.json`, `src-tauri/Cargo.toml`, untracked `simcoach/tracks/fuji_speedway.json` and `simcoach/tracks/silverstone_national_circuit.json`). Never touch, stage, stash or reset them. Set up the worktree like this: `git worktree add ../sim-racing-coach-151 -b feat/151-live-rail-map main`. Then, inside it, create its own venv and install: `python -m venv .venv && .venv/Scripts/python.exe -m pip install -e ".[dev]"`, then `cd frontend && npm ci`. Before every test run, print the resolved `simcoach.__file__` once to prove the venv points at the worktree (see memory "stale worktrees + venv crosstalk").
- **Stage by name only.** Never `git add -A`, `git add .`, `git stash` or `git reset`. `git rm` for deletions.
- **Every commit uses `Refs #151`, never `Fixes #151`.** The live map needs a rig look (see "Rig check" at the end), so the issue closes by hand after the rig pass. Commit trailer, exactly:
  ```
  Refs #151

  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01Pywcy5ZyceRZNDLyjn3F35
  ```
- **Architecture invariant:** the UI never derives corner identity or direction. It only formats the `{label, name, direction}` the server sends (`left` → `L`, `right` → `R`, anything else printed as-is).
- **Corner identity comes from the corner map only.** `next_corner` returns the map's own `Corner` object.
- Backend gate: `.venv/Scripts/python.exe -m pytest -q` green, `ruff check simcoach tests scripts` clean. Frontend gate: `cd frontend && npx vitest run` green, `npx svelte-check --threshold error` 0 errors.
- Keep `simcoach/lmu/live.py` `read_field_snapshot()`, `read_field()`, the dataclasses and `snapshot_to_dict()` unchanged. They are the #152 input. Only the module docstring changes.
- `RaceStrip.svelte` stays (Coach rail). `InputTraces.svelte` stays (used by `InputsPanel`).

## Decisions made while planning (recorded; the driver was not available)

1. **`next_corner` semantics.** While the car is inside a corner's window, the callout names that corner. It moves on when the car leaves the window. Outside every window, it names the first corner whose entry is ahead, wrapping past S/F to the lowest-entry corner. This avoids the callout jumping to the next corner at turn-in. The rule matches the existing `CornerMap.corner_at`.
2. **No `track_len` parameter.** The suggested signature was `next_corner(corners, lap_dist, track_len)`. Wrap-around only needs "no corner entry ahead → the lowest-entry corner", which needs no lap length. So the signature is `next_corner(corners, dist)`. Seam-wrapping windows (`start > end`) are handled by the existing `in_window`.
3. **What decides "live".** The `live` flag on the `/live` frame decides it, not `/events` `status.on_track`. That frame also carries the player position and the next corner, so the three can never disagree within one frame. `read_field` already reports `live=False` in menus and in the garage stall.
4. **Where the live map comes from.** In live mode the pane draws the live track's canonical map from `eventFeed.map`, even if a past session is pinned in the rail. Off track, it shows the selected session's map exactly as today.
5. **How the server finds the track.** `_live_track()` (telemetry `mTrackName`) is the key, so the corner map matches the map frame `/events` sends. It is only called while live. Cost: one extra SMM read per 4 Hz tick while on track.
6. **Sector ticks are drawn by `TrackMap` in every mode**, both session and live. The spec lists them for the live map. Drawing them in the session view too is harmless and needs no mode flag.
7. **Expanding widens the rail and gives the map room.** Widening alone does not enlarge a height-capped map. So expand also (a) raises the map height (`mapH`) to at least half the viewport and restores the previous height on collapse, and (b) renders the map in `fill` mode so it fits its box without scrolling. The existing height resizer keeps working in both states. Only the expanded flag is persisted (`coachRail.expanded`). The wide rail is `min(640px, 50vw)` and applies only above 820 px, because below that the rail is the laps drawer and the map pane is hidden.
8. **Order.** Retiring the Pit Wall lands after the live rail map (spec: "1 lands before 2"). The `/live` payload is trimmed in its own task after the Pit Wall is gone, so every commit stays green. Before that, `next_corner` is added to the payload without removing anything.
9. **"Never overlays the chat" is tested by structure, not pixels.** jsdom does not load `app.css`. The vitest proves that the chat column stays the rail's grid sibling, that no overlay or dialog opens, and that the rail gets `rail-wide`. The actual layout is a rig-verify item.

## File Structure

- `simcoach/corners.py`: add `next_corner(corners, dist) -> Corner | None`.
- `simcoach/server/app.py`: `/live` sends `next_corner`, with a per-track `app.state.live_cmap` cache. `_invalidate_canon` clears that cache. Task 7 trims the payload.
- `simcoach/lmu/live.py`: docstring only (Task 7).
- `frontend/src/lib/TrackMap.svelte`: sector boundary ticks from `map.sectors`; prop comments.
- `frontend/src/lib/TrackMapPane.svelte`: live mode (player dot plus callout); the expand button replaces the 🔍 overlay; new `expanded`/`onexpand` props.
- `frontend/src/lib/App.svelte`: rail-wide state (persisted), map height bump, Pit Wall removed.
- `frontend/src/app.css`: the `.main.rail-wide` column rule.
- `frontend/src/lib/live.ts`: `NextCorner`, then (Task 7) `LiveFrame` replaces the field types.
- Deleted: `frontend/src/lib/PitWall.svelte`, `PitWall.test.ts`, `LiveTower.svelte`, `LiveTower.test.ts`, `SectorDeltas.svelte`, `SectorDeltas.test.ts`.
- Docs: `docs/vision.md`, `CLAUDE.md`. Memory (not in the repo): `pit-wall-voice-vision.md`.
- Tests: `tests/test_next_corner.py` (new), `tests/test_server_live.py`, `frontend/src/lib/TrackMap.test.ts`, `TrackMapPane.test.ts`, `App.test.ts`, `live.test.ts`.

---

### Task 1: Pure `next_corner` in the core

**Files:**
- Modify: `simcoach/corners.py` (add a function right after `class Corner`)
- Test: `tests/test_next_corner.py` (new)

**Interfaces:**
- Consumes: `Corner` (dataclass: `number, name, direction, start_dist, apex_dist, end_dist, label, apexes`) and `in_window(dist, start, end)` from `simcoach/corners.py`.
- Produces: `next_corner(corners: list[Corner], dist: float) -> Corner | None`. It returns the corner whose window contains `dist`. Otherwise it returns the corner with the lowest `start_dist >= dist`. Otherwise (past the last entry) it returns the lowest-`start_dist` corner. It returns `None` for an empty list. The result is always one of the input objects, never a copy.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_next_corner.py
from simcoach.corners import Corner, next_corner


def _c(n, start, apex, end, direction, name):
    return Corner(number=n, name=name, direction=direction, start_dist=start,
                  apex_dist=apex, end_dist=end, label=str(n))


T1 = _c(1, 100.0, 150.0, 200.0, "right", "La Source")
T2 = _c(2, 900.0, 950.0, 1000.0, "left", "Raidillon")
T3 = _c(3, 3900.0, 3980.0, 50.0, "right", "Bus Stop")  # window wraps the S/F line


def test_mid_lap_returns_the_first_corner_ahead():
    assert next_corner([T1, T2], 500.0) is T2


def test_inside_a_corner_window_returns_that_corner():
    assert next_corner([T1, T2], 150.0) is T1
    assert next_corner([T1, T2], 200.0) is T1  # window is inclusive at the exit


def test_after_the_last_corner_wraps_to_the_first():
    assert next_corner([T1, T2], 3000.0) is T1


def test_between_the_last_corner_and_the_line_with_a_seam_corner():
    assert next_corner([T1, T2, T3], 2000.0) is T3
    assert next_corner([T1, T2, T3], 3950.0) is T3  # inside it, before the line
    assert next_corner([T1, T2, T3], 20.0) is T3    # inside it, past the line
    assert next_corner([T1, T2, T3], 60.0) is T1    # out of it: T1 is next


def test_input_order_does_not_matter():
    assert next_corner([T2, T1], 500.0) is T2
    assert next_corner([T2, T1], 3000.0) is T1


def test_negative_lap_distance_before_the_first_crossing():
    # rF2 reports a small negative lap distance on the grid before the line.
    assert next_corner([T1, T2], -30.0) is T1


def test_no_corners_is_none():
    assert next_corner([], 500.0) is None


def test_label_name_and_direction_come_from_the_map():
    c = next_corner([T1, T2], 500.0)
    assert (c.label, c.name, c.direction) == ("2", "Raidillon", "left")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_next_corner.py -q`
Expected: FAIL with `ImportError: cannot import name 'next_corner' from 'simcoach.corners'`.

- [ ] **Step 3: Implement**

In `simcoach/corners.py`, insert directly after the `class Corner` dataclass (before `def validated_sectors`):

```python
def next_corner(corners: list[Corner], dist: float) -> Corner | None:
    """The corner the car is in, else the next corner entry ahead of `dist`, wrapping
    past S/F to the first. None when there are no corners.

    Corner identity is owned data (#151): the Coach rail map's "Next: T10 Pouhon (L)"
    comes from here, server-side, never from the UI. A seam-wrapping window
    (start > end) is handled by in_window; wrap-around needs no lap length because
    "no entry ahead" simply means the lowest entry is next.
    """
    for c in corners:
        if in_window(dist, c.start_dist, c.end_dist):
            return c
    ahead = [c for c in corners if c.start_dist >= dist]
    return min(ahead or corners, key=lambda c: c.start_dist, default=None)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_next_corner.py tests/test_corners.py -q`
Expected: PASS (all).

Run: `ruff check simcoach tests scripts`
Expected: `All checks passed!`

- [ ] **Step 5: Commit**

```bash
git add simcoach/corners.py tests/test_next_corner.py
git commit -m "feat(corners): #151 - next_corner picks the corner the car is in or the next entry ahead

Refs #151

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Pywcy5ZyceRZNDLyjn3F35"
```

---

### Task 2: `/live` sends the player's next corner (server-side, map cached per track)

**Files:**
- Modify: `simcoach/server/app.py`: the `corners` import (line ~47), `create_app` state init (near `app.state.canon = {}`, line ~454), `_invalidate_canon` (line ~904), and the `@app.websocket("/live")` handler (line ~2551).
- Test: `tests/test_server_live.py`

**Interfaces:**
- Consumes: `next_corner` (Task 1); the existing closures `_live_track() -> str | None` and `_cmap_for_map(track) -> CornerMap | None`; `read_field_snapshot()` / `snapshot_to_dict()` from `simcoach.lmu.live`.
- Produces: each `/live` JSON frame gains a top-level `"next_corner": {"label": str, "name": str, "direction": str} | None`. It is `None` when not live, when there is no player car, or when the track has no corner map. Also produces `app.state.live_cmap: tuple[str | None, CornerMap | None] | None`, a one-entry cache keyed on the live track name and cleared by `_invalidate_canon`.

- [ ] **Step 1: Write the failing tests**

Edit `tests/test_server_live.py`. Replace the import block and `_client` with the following. Keep `_snap` and the three existing tests unchanged below it.

```python
# tests/test_server_live.py
import json

import pytest
from fastapi.testclient import TestClient

import simcoach.server.app as app_mod
from simcoach.coach import Coach
from simcoach.corners import CornerMap
from simcoach.lmu.live import FieldSnapshot, SessionState, VehicleState
from simcoach.model.fake import FakeModel
from simcoach.server.app import create_app
from simcoach.store import LapStore


@pytest.fixture(autouse=True)
def _pin_live_track(monkeypatch):
    # /live resolves the corner map from _live_track(), which reads REAL shared
    # memory -- pin it so a running LMU on the dev machine can't leak in.
    monkeypatch.setattr("simcoach.lmu.smm.read_session_track_car",
                        lambda: ("Nowhere Raceway", "GT3"))


def _client(tmp_path, **kw):
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    coach = Coach(FakeModel())
    tracks = tmp_path / "tracks"
    tracks.mkdir(exist_ok=True)
    kw.setdefault("tracks_dir", str(tracks))  # hermetic: never the shipped maps
    return TestClient(create_app(store, coach, **kw), base_url="http://127.0.0.1")


def _cmap():
    return CornerMap.from_data({"corners": [
        {"number": 1, "name": "La Source", "direction": "right",
         "start_dist": 100, "apex_dist": 150, "end_dist": 200},
        {"number": 2, "name": "Raidillon", "direction": "left",
         "start_dist": 900, "apex_dist": 950, "end_dist": 1000},
    ]})
```

Append these tests at the end of the file:

```python
def test_live_sends_the_players_next_corner_from_the_track_map(tmp_path, monkeypatch):
    monkeypatch.setattr("simcoach.lmu.live.read_field_snapshot", lambda: _snap(True))
    with _client(tmp_path, cmap=_cmap()).websocket_connect(
            "/live", headers={"host": "127.0.0.1"}) as ws:
        data = ws.receive_json()
    # Player at lap_dist 500: past T1, before T2 -> T2, direction from the map.
    assert data["next_corner"] == {"label": "2", "name": "Raidillon", "direction": "left"}


def test_live_next_corner_is_none_without_a_corner_map(tmp_path, monkeypatch):
    monkeypatch.setattr("simcoach.lmu.live.read_field_snapshot", lambda: _snap(True))
    with _client(tmp_path).websocket_connect("/live", headers={"host": "127.0.0.1"}) as ws:
        data = ws.receive_json()
    assert data["next_corner"] is None


def test_live_not_on_track_sends_no_next_corner(tmp_path, monkeypatch):
    monkeypatch.setattr("simcoach.lmu.live.read_field_snapshot", lambda: _snap(False))
    with _client(tmp_path, cmap=_cmap()).websocket_connect(
            "/live", headers={"host": "127.0.0.1"}) as ws:
        data = ws.receive_json()
    assert data["next_corner"] is None


def test_live_resolves_the_track_file_once_not_every_tick(tmp_path, monkeypatch):
    tracks = tmp_path / "tracks"
    tracks.mkdir()
    (tracks / "nowhere_raceway.json").write_text(json.dumps({"corners": [
        {"number": 1, "name": "Hairpin", "direction": "right",
         "start_dist": 400, "apex_dist": 450, "end_dist": 520},
    ]}), encoding="utf-8")
    monkeypatch.setattr("simcoach.lmu.live.read_field_snapshot", lambda: _snap(True))
    client = _client(tmp_path, tracks_dir=str(tracks))
    calls = []
    real = app_mod.load_map_data
    monkeypatch.setattr(app_mod, "load_map_data",
                        lambda *a, **k: calls.append(a) or real(*a, **k))
    with client.websocket_connect("/live", headers={"host": "127.0.0.1"}) as ws:
        frames = [ws.receive_json() for _ in range(3)]
    # lap_dist 500 is inside Hairpin's window -> Hairpin on every tick...
    assert [f["next_corner"]["name"] for f in frames] == ["Hairpin"] * 3
    # ...but the track file was read once, not at 4 Hz.
    assert len(calls) == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_server_live.py -q`
Expected: the four new tests FAIL with `KeyError: 'next_corner'`. The three existing tests PASS.

- [ ] **Step 3: Implement**

(a) In `simcoach/server/app.py`, change the corners import:

```python
from ..corners import CornerMap, in_window, next_corner, validated_sections, validated_sectors
```

(b) In `create_app`, directly after `app.state.canon = {}             # per-(track,car) canonical map payload cache`, add:

```python
    app.state.live_cmap = None       # (live track, CornerMap|None) for /live's next corner
```

(c) In `_invalidate_canon`, append as its last line (inside the function):

```python
        app.state.live_cmap = None  # /live re-resolves the corner map on its next tick
```

(d) Replace the whole `live_ws` handler body after `await ws.accept()` with the code below. The `_LIVE_PERIOD` line and the host-guard preamble stay as they are.

```python
        from ..lmu.live import read_field_snapshot, snapshot_to_dict

        def _next_corner_view(dist: float) -> dict | None:
            # #151: the rail map's "Next: T10 Pouhon (L)". Corner identity is owned
            # data, so it is resolved HERE from the live track's corner map, never in
            # the UI. Resolved once per track (this runs at 4 Hz); _invalidate_canon
            # drops the cache when a map is saved.
            track = _live_track()
            cached = app.state.live_cmap
            if cached is None or cached[0] != track:
                cached = app.state.live_cmap = (track, _cmap_for_map(track) if track else None)
            c = next_corner(cached[1].corners, dist) if cached[1] else None
            return {"label": c.label, "name": c.name, "direction": c.direction} if c else None

        def _live_tick() -> dict:
            # Blocking mmap + track-file reads: run off the event loop.
            snap = read_field_snapshot()
            payload = snapshot_to_dict(snap)
            # Live scoring only carries the team/entry name; map it to the car
            # MODEL the same way /status does (raw name stays the resolve key).
            for c in payload["cars"]:
                c["car"] = resolve_car(c["car"], c["cls"])
            me = next((c for c in snap.cars if c.is_player), None)
            live = snap.session.live and me is not None
            payload["next_corner"] = _next_corner_view(me.lap_dist) if live else None
            return payload

        loop = asyncio.get_running_loop()
        try:
            while True:
                await ws.send_json(await loop.run_in_executor(None, _live_tick))
                await asyncio.sleep(_LIVE_PERIOD)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            if ws.client_state == WebSocketState.CONNECTED:
                try: await ws.close()
                except Exception: pass
```

Note: the monkeypatch of `simcoach.lmu.live.read_field_snapshot` still works because the function-level `from ..lmu.live import ...` runs per connection, after the patch.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_server_live.py tests/test_next_corner.py -q`
Expected: PASS (7 in test_server_live.py, 8 in test_next_corner.py).

Run: `.venv/Scripts/python.exe -m pytest -q` then `ruff check simcoach tests scripts`
Expected: full suite green; `All checks passed!`

- [ ] **Step 5: Commit**

```bash
git add simcoach/server/app.py tests/test_server_live.py
git commit -m "feat(server): #151 - /live sends the player's next corner from the live track's map

Refs #151

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Pywcy5ZyceRZNDLyjn3F35"
```

---

### Task 3: `TrackMap` draws sector boundary ticks

**Files:**
- Modify: `frontend/src/lib/TrackMap.svelte`
- Test: `frontend/src/lib/TrackMap.test.ts`

**Interfaces:**
- Consumes: `TrackMap.sectors: number[]` (lap distances of the S1/S2 boundaries; `[]` when unset) from `api.ts`, and `nearestDistIndex(dist, target)` (already imported in `TrackMap.svelte`).
- Produces: one `<rect class="sector-tick">` per entry of `map.sectors`, centred on the path point nearest that lap distance. None when `sectors` is empty or absent.

- [ ] **Step 1: Write the failing test**

In `frontend/src/lib/TrackMap.test.ts`, change the api import line to also import the type:

```ts
import { getLapMap, type TrackMap as TrackMapData } from "./api";
```

and add inside `describe("TrackMap", ...)`:

```ts
  it("#151: draws a tick at each sector boundary from the map's sectors, none when absent", async () => {
    const withSectors = { ...mapFixture, sectors: [50] } as unknown as TrackMapData;
    const { container } = render(TrackMap, { props: { mapData: withSectors } });
    await waitFor(() => expect(container.querySelectorAll("rect.sector-tick").length).toBe(1));
    const tick = container.querySelector("rect.sector-tick") as SVGRectElement;
    // Sector at lap distance 50 sits on path point [10, 20].
    const cx = Number(tick.getAttribute("x")) + Number(tick.getAttribute("width")) / 2;
    const cy = Number(tick.getAttribute("y")) + Number(tick.getAttribute("height")) / 2;
    expect(cx).toBeCloseTo(10);
    expect(cy).toBeCloseTo(20);
    cleanup();

    const none = { ...mapFixture, sectors: [] } as unknown as TrackMapData;
    const { container: c2 } = render(TrackMap, { props: { mapData: none } });
    await waitFor(() => expect(c2.querySelector("polyline")).not.toBeNull());
    expect(c2.querySelectorAll("rect.sector-tick").length).toBe(0);
  });
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd frontend && npx vitest run src/lib/TrackMap.test.ts`
Expected: FAIL. The new test times out in `waitFor` with `expected 0 to be 1`.

- [ ] **Step 3: Implement**

In `frontend/src/lib/TrackMap.svelte`, insert this block immediately before `{#each map.corners as c, i (c.number)}` in the SVG:

```svelte
      <!-- #151: sector boundaries from the map's own `sectors` (none drawn when absent). -->
      {#each map.sectors ?? [] as s, i (i)}
        {@const p = map.path[nearestDistIndex(map.dist, s)]}
        <rect class="sector-tick" x={p[0] - su * 0.014} y={p[1] - su * 0.014}
              width={su * 0.028} height={su * 0.028} fill="var(--text)">
          <title>Sector {i + 1} boundary</title>
        </rect>
      {/each}
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd frontend && npx vitest run src/lib/TrackMap.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/TrackMap.svelte frontend/src/lib/TrackMap.test.ts
git commit -m "feat(ui): #151 - track map draws sector boundary ticks from the map's sectors

Refs #151

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Pywcy5ZyceRZNDLyjn3F35"
```

---

### Task 4: `TrackMapPane` live mode (player car + next-corner callout)

**Files:**
- Modify: `frontend/src/lib/live.ts` (add `NextCorner`; `FieldSnapshot.next_corner`)
- Modify: `frontend/src/lib/TrackMapPane.svelte`
- Modify: `frontend/src/lib/live.test.ts` (fixture gains `next_corner: null`)
- Test: `frontend/src/lib/TrackMapPane.test.ts`

**Interfaces:**
- Consumes: `liveField` (`snapshot: FieldSnapshot | null`, `subscribe(run: (v: LiveField) => void) => () => boolean`), the `/live` frame's `session.live`, `cars[].is_player/lap_dist/cls/id`, and `next_corner` (Task 2); `eventFeed.status` / `eventFeed.map`; the `TrackMap` props `mapData`, `cars: {id, lapDist, cls, isPlayer, pos?}[]`.
- Produces: `export interface NextCorner { label: string; name: string; direction: string }` in `live.ts`, and `FieldSnapshot.next_corner: NextCorner | null`. While `session.live` is true, the pane renders `<TrackMap mapData={liveMap} cars={[player only]} />` and a `<p class="next-corner">Next: T{label} {name} ({L|R})</p>` below the map body. Otherwise the pane behaves as today.

- [ ] **Step 1: Write the failing tests**

In `frontend/src/lib/TrackMapPane.test.ts`, add after the `vi.mock("./api", ...)` block:

```ts
// #151: a controllable stand-in for the shared /live feed (App owns the real one).
const live = vi.hoisted(() => {
  let snapshot: unknown = null;
  const listeners = new Set<(v: { snapshot: unknown }) => void>();
  return {
    get snapshot() {
      return snapshot;
    },
    subscribe(run: (v: { snapshot: unknown }) => void) {
      listeners.add(run);
      run({ snapshot });
      return () => listeners.delete(run);
    },
    emit(s: unknown) {
      snapshot = s;
      for (const l of listeners) l({ snapshot: s });
    },
  };
});
vi.mock("./live", async () => {
  const actual = await vi.importActual<typeof import("./live")>("./live");
  return { ...actual, liveField: live };
});

type Next = { label: string; name: string; direction: string } | null;
// One /live frame with the player AND an opponent: only the player may be drawn.
const onTrackFrame = (lapDist: number, next: Next) => ({
  session: { live: true },
  cars: [
    { id: 2, is_player: true, lap_dist: lapDist, cls: "GT3" },
    { id: 3, is_player: false, lap_dist: 10, cls: "GT3" },
  ],
  next_corner: next,
});
const offTrackFrame = () => ({ session: { live: false }, cars: [], next_corner: null });
const liveStatus = { ...noStatus, track: "Live Track", on_track: true, activity: "track", has_map: true };
```

(`noStatus` is declared above the describe block, so place `liveStatus` after it.) In the existing `beforeEach`, add as its first line:

```ts
    live.emit(null);
```

Add these tests inside `describe("TrackMapPane", ...)`:

```ts
  it("#151: on track the rail map is live — the live map, ONLY the player's car, and the next corner", async () => {
    eventFeed.push("status", liveStatus);
    eventFeed.push("map", { ...mapFixture, track_name: "Live Track" });
    live.emit(onTrackFrame(100, { label: "1", name: "First", direction: "right" }));
    // A past session is pinned in the rail: live still wins while on track.
    const { container } = render(TrackMapPane, { props: { sessionId: "s1" } });

    expect(await screen.findByText("Next: T1 First (R)")).toBeTruthy();
    expect(await screen.findByText("Live Track")).toBeTruthy();
    const dots = container.querySelectorAll("circle.car-dot");
    expect(dots.length).toBe(1); // the opponent in the frame is NOT drawn
    expect(dots[0].classList.contains("player")).toBe(true);
    expect(dots[0].getAttribute("cx")).toBe("10"); // lap_dist 100 -> path point [10, 20]
    expect(getLapMap).not.toHaveBeenCalled(); // the pinned session's map is not what's drawn
  });

  it("#151: off track it shows the selected session's map as before — no car, no callout", async () => {
    live.emit(offTrackFrame());
    const { container } = render(TrackMapPane, { props: { sessionId: "s1" } });
    expect(await screen.findByText("T1")).toBeTruthy();
    expect(getLapMap).toHaveBeenCalledWith(9);
    expect(container.querySelector("circle.car-dot")).toBeNull();
    expect(screen.queryByText(/^Next:/)).toBeNull();
  });

  it("#151: leaving the track drops the car and the callout", async () => {
    eventFeed.push("status", liveStatus);
    eventFeed.push("map", { ...mapFixture, track_name: "Live Track" });
    live.emit(onTrackFrame(100, { label: "1", name: "First", direction: "right" }));
    const { container } = render(TrackMapPane, { props: { sessionId: undefined } });
    expect(await screen.findByText("Next: T1 First (R)")).toBeTruthy();

    live.emit(offTrackFrame());
    await tick();
    expect(screen.queryByText(/^Next:/)).toBeNull();
    expect(container.querySelector("circle.car-dot")).toBeNull();
    expect(screen.getByText("T1")).toBeTruthy(); // no session pinned: still the live track's map
  });

  it("#151: no callout when the server resolves no corner (e.g. no corner map)", async () => {
    eventFeed.push("status", liveStatus);
    eventFeed.push("map", { ...mapFixture, track_name: "Live Track" });
    live.emit(onTrackFrame(100, null));
    const { container } = render(TrackMapPane, { props: { sessionId: undefined } });
    await waitFor(() => expect(container.querySelector("circle.car-dot")).not.toBeNull());
    expect(screen.queryByText(/^Next:/)).toBeNull();
  });
```

Add `waitFor` to the `@testing-library/svelte` import at the top of the file:

```ts
import { render, screen, cleanup, fireEvent, waitFor } from "@testing-library/svelte";
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd frontend && npx vitest run src/lib/TrackMapPane.test.ts`
Expected: the first, third and fourth new tests FAIL, because there is no `Next: …` text and no `circle.car-dot`. The second new test PASSES, since it pins today's behaviour. The existing tests PASS.

- [ ] **Step 3: Implement**

(a) `frontend/src/lib/live.ts`: add above `export interface FieldSnapshot`:

```ts
/** #151: the corner the server resolved from the live track's corner map. The UI
    only formats it — corner identity is owned data, never derived here. */
export interface NextCorner { label: string; name: string; direction: string; }
```

and change the `FieldSnapshot` line to:

```ts
export interface FieldSnapshot { session: SessionState; cars: VehicleState[]; next_corner: NextCorner | null; }
```

(b) `frontend/src/lib/live.test.ts`: in the `snap` fixture, after `cars: [],` add `next_corner: null,`.

(c) `frontend/src/lib/TrackMapPane.svelte`. In the `<script>`:
- Add `import { liveField } from "./live";` after the `eventFeed` import.
- Replace these three declarations:

```ts
  const liveTrack = $derived(sessionId ? null : (feedStatus?.track ?? null));
  const liveMap = $derived.by<TrackMapData | null>(() => {
    if (sessionId) return null;
    const m = feedMap;
    return m && "path" in m && m.path.length > 0 ? m : null;
  });

  const displayTrack = $derived(sessionId ? trackName : liveTrack);
```

with:

```ts
  // #151: on track the rail map is LIVE. The shared /live feed is started once in
  // App; we only subscribe. Its `live` flag gates the mode because the same frame
  // carries the player position and the server-resolved next corner.
  let live = $state(liveField.snapshot);
  $effect(() => liveField.subscribe((v) => { live = v.snapshot; }));
  const onTrack = $derived(live?.session.live === true);
  const player = $derived(onTrack ? (live?.cars.find((c) => c.is_player) ?? null) : null);
  // ONLY the player's car: the field is the game HUD's job, not the rail's.
  const liveCars = $derived(
    player ? [{ id: player.id, lapDist: player.lap_dist, cls: player.cls, isPlayer: true }] : [],
  );
  const nextCorner = $derived(onTrack ? (live?.next_corner ?? null) : null);
  const DIR: Record<string, string> = { left: "L", right: "R" };

  const liveTrack = $derived(feedStatus?.track ?? null);
  const liveMap = $derived.by<TrackMapData | null>(() => {
    const m = feedMap;
    return m && "path" in m && m.path.length > 0 ? m : null;
  });

  const displayTrack = $derived(onTrack || !sessionId ? liveTrack : trackName);
```

In the markup, replace the whole `<div class="map-body">…</div>` with:

```svelte
  <div class="map-body">
    {#if onTrack}
      <!-- #151: live — the live track's canonical map with only the player's car. -->
      <TrackMap mapData={liveMap} cars={liveCars} />
    {:else if lapId !== null}
      <TrackMap {lapId} />
    {:else if !sessionId && liveTrack}
      <!-- Live session, no completed lap yet: the track is detected, so draw its
           canonical map directly (mapData bypasses the lapId fetch) instead of
           waiting on a lap. TrackMap's own "none" phase (mapData === null) covers
           "track detected, no map built yet" with its own build-me hint. -->
      <TrackMap mapData={liveMap} />
    {:else}
      <p class="empty">No map yet — build one in Track view (Tracks → Edit).</p>
    {/if}
  </div>
  {#if nextCorner}
    <p class="next-corner">Next: T{nextCorner.label}{nextCorner.name ? ` ${nextCorner.name}` : ""} ({DIR[nextCorner.direction] ?? nextCorner.direction})</p>
  {/if}
```

In `<style>`, add:

```css
  .next-corner {
    margin: 0;
    padding: 6px 12px 10px;
    font-family: var(--mono, monospace);
    font-size: 13px;
    font-weight: 700;
    color: var(--accent);
  }
```

- [ ] **Step 4: Run to verify they pass**

Run: `cd frontend && npx vitest run src/lib/TrackMapPane.test.ts src/lib/live.test.ts src/lib/App.test.ts`
Expected: PASS.

Run: `cd frontend && npx svelte-check --threshold error`
Expected: `0 errors`.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/live.ts frontend/src/lib/live.test.ts frontend/src/lib/TrackMapPane.svelte frontend/src/lib/TrackMapPane.test.ts
git commit -m "feat(ui): #151 - rail map goes live on track: player car + server next-corner callout

Refs #151

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Pywcy5ZyceRZNDLyjn3F35"
```

---

### Task 5: Expand control widens the rail (replaces the 🔍 overlay)

**Files:**
- Modify: `frontend/src/lib/TrackMapPane.svelte` (the expand button replaces the magnifier and `TrackMapEditor`; new props)
- Modify: `frontend/src/lib/TrackMap.svelte` (prop comments only)
- Modify: `frontend/src/lib/App.svelte` (rail-wide state, persistence, map height bump)
- Modify: `frontend/src/app.css` (`.main.rail-wide`)
- Test: `frontend/src/lib/TrackMapPane.test.ts`, `frontend/src/lib/App.test.ts`

**Interfaces:**
- Consumes: `mapH` (App's existing `$state`, which drives `--map-h` on `aside.rail`); `TrackMap`'s `fill` prop.
- Produces: new `TrackMapPane` props `expanded?: boolean` (default `false`) and `onexpand?: () => void`. A button with `aria-label` `"Expand track map"` / `"Collapse track map"` and `aria-pressed={expanded}`. `App` gets `.main.rail-wide` while expanded, persisted at `localStorage["coachRail.expanded"]` as `"true"`/`"false"` (try/catch both ways). Expanding raises `mapH` to `max(mapH, round(innerHeight * 0.5))`, and collapsing restores the pre-expand `mapH`.

- [ ] **Step 1: Write the failing tests**

(a) `frontend/src/lib/TrackMapPane.test.ts`: delete the test `"opens the map strictly read-only from the magnifier"` and add:

```ts
  it("#151: the expand button asks the owner to widen the rail — no overlay dialog", async () => {
    const onexpand = vi.fn();
    render(TrackMapPane, { props: { sessionId: "s1", expanded: false, onexpand } });
    await screen.findByText("T1");
    expect(screen.queryByLabelText("Enlarge track map")).toBeNull(); // the 🔍 overlay is gone
    await fireEvent.click(screen.getByLabelText("Expand track map"));
    expect(onexpand).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("#151: expanded, the button reads Collapse and the map fits its box", async () => {
    const { container } = render(TrackMapPane, { props: { sessionId: "s1", expanded: true, onexpand: () => {} } });
    await screen.findByText("T1");
    expect(screen.getByLabelText("Collapse track map").getAttribute("aria-pressed")).toBe("true");
    expect(container.querySelector(".panel.fill")).not.toBeNull();
  });
```

(b) `frontend/src/lib/App.test.ts`: in `describe("Coach screen assembly")`'s `beforeEach`, add as the first line:

```ts
    localStorage.removeItem("coachRail.expanded");
```

and add inside that describe:

```ts
  it("#151: expanding the rail map widens the rail, never overlays the chat, and is remembered", async () => {
    const { container } = render(App);
    await screen.findByRole("navigation"); // shell mounts past the splash
    const main = container.querySelector(".main") as HTMLElement;
    const rail = container.querySelector("aside.rail") as HTMLElement;
    expect(main.classList.contains("rail-wide")).toBe(false);
    expect(rail.style.getPropertyValue("--map-h")).toBe("200px");

    await fireEvent.click(await screen.findByLabelText("Expand track map"));
    expect(main.classList.contains("rail-wide")).toBe(true);
    expect(rail.style.getPropertyValue("--map-h")).toBe(`${Math.round(window.innerHeight * 0.5)}px`);
    // The chat column is still the rail's grid sibling (pushed narrower by CSS), and
    // nothing is layered over it: no dialog, no overlay.
    const chatCol = container.querySelector(".coach-main") as HTMLElement;
    expect(chatCol.parentElement).toBe(main);
    expect(within(chatCol).getByPlaceholderText(/ask your engineer/i)).toBeTruthy();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(localStorage.getItem("coachRail.expanded")).toBe("true");

    await fireEvent.click(screen.getByLabelText("Collapse track map"));
    expect(main.classList.contains("rail-wide")).toBe(false);
    expect(rail.style.getPropertyValue("--map-h")).toBe("200px");
    expect(localStorage.getItem("coachRail.expanded")).toBe("false");
  });

  it("#151: the expanded rail is restored for this viewer", async () => {
    localStorage.setItem("coachRail.expanded", "true");
    const { container } = render(App);
    await screen.findByRole("navigation");
    expect((container.querySelector(".main") as HTMLElement).classList.contains("rail-wide")).toBe(true);
    expect(await screen.findByLabelText("Collapse track map")).toBeTruthy();
  });

  it("#151: with storage blocked the rail still renders and expands (just not remembered)", async () => {
    const get = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    const set = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    try {
      const { container } = render(App);
      await screen.findByRole("navigation");
      const main = container.querySelector(".main") as HTMLElement;
      expect(main.classList.contains("rail-wide")).toBe(false);
      await fireEvent.click(await screen.findByLabelText("Expand track map"));
      expect(main.classList.contains("rail-wide")).toBe(true);
    } finally {
      get.mockRestore();
      set.mockRestore();
    }
  });
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd frontend && npx vitest run src/lib/TrackMapPane.test.ts src/lib/App.test.ts`
Expected: the new tests FAIL with `Unable to find a label with the text of: Expand track map` (or `Collapse track map`).

- [ ] **Step 3: Implement**

(a) `frontend/src/lib/TrackMapPane.svelte`:
- Delete `import TrackMapEditor from "./TrackMapEditor.svelte";` and the line `let viewing = $state(false); …`.
- Replace the props line with:

```ts
  let { sessionId, expanded = false, onexpand }: {
    sessionId: string | undefined;
    // #151: App owns the rail width; this pane only asks for it to change.
    expanded?: boolean;
    onexpand?: () => void;
  } = $props();
```

- Replace the whole `<div class="acts">…</div>` with:

```svelte
    <div class="acts">
      <!-- #151: expand WIDENS the rail (pushing the chat narrower) — replaces the
           🔍 overlay, which covered the chat. -->
      <button type="button" class="icon" aria-pressed={expanded}
              aria-label={expanded ? "Collapse track map" : "Expand track map"}
              title={expanded ? "Collapse map" : "Expand map"}
              onclick={() => onexpand?.()}>
        {#if expanded}
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 14h6v6M20 10h-6V4M14 10l7-7M3 21l7-7"/></svg>
        {:else}
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7"/></svg>
        {/if}
      </button>
    </div>
```

- Add `fill={expanded}` to all three `<TrackMap …/>` tags in `.map-body`: `<TrackMap mapData={liveMap} cars={liveCars} fill={expanded} />`, `<TrackMap {lapId} fill={expanded} />` and `<TrackMap mapData={liveMap} fill={expanded} />`.
- Delete the `<TrackMapEditor … />` element at the bottom of the section.

(b) `frontend/src/lib/TrackMap.svelte`: update the two prop comments. There is no code change.

```ts
    // The live rail map passes the current-track map directly (no lap to route
    // through); when set (incl. null), it drives the pane and the lapId fetch is bypassed.
    mapData?: TrackMap | null;
    // fill: the SVG fits the container's full box (the expanded Coach rail, #151)
    // instead of sizing to its aspect ratio (the normal rail, where it flows in a column).
    fill?: boolean;
```

(c) `frontend/src/lib/App.svelte`: replace the line `let mapH = $state(200); // px; drives --map-h on the rail` with:

```ts
  // #151: the rail map's expand control WIDENS the rail (.main.rail-wide in app.css),
  // pushing the chat column narrower — never over it. Remembered per viewer; a
  // blocked storage just means it isn't remembered. Expanding also gives the map
  // height (widening alone can't grow a height-capped map); collapsing restores it.
  const RAIL_WIDE_KEY = "coachRail.expanded";
  function loadRailWide(): boolean {
    try {
      return localStorage.getItem(RAIL_WIDE_KEY) === "true";
    } catch {
      return false;
    }
  }
  function wideMapH(h: number): number {
    return Math.max(h, Math.round((typeof window !== "undefined" ? window.innerHeight : 800) * 0.5));
  }
  const startWide = loadRailWide();
  let railWide = $state(startWide);
  let mapH = $state(startWide ? wideMapH(200) : 200); // px; drives --map-h on the rail
  let mapHNormal = 200;
  function toggleRailWide() {
    railWide = !railWide;
    if (railWide) {
      mapHNormal = mapH;
      mapH = wideMapH(mapH);
    } else {
      mapH = mapHNormal;
    }
    try {
      localStorage.setItem(RAIL_WIDE_KEY, String(railWide));
    } catch {
      /* private mode / storage disabled — the choice just won't persist */
    }
  }
```

Change `<div class="main" class:rail-open={railOpen}>` to:

```svelte
        <div class="main" class:rail-open={railOpen} class:rail-wide={railWide}>
```

Change `<TrackMapPane sessionId={selectedSessionId} />` to:

```svelte
            <TrackMapPane sessionId={selectedSessionId} expanded={railWide} onexpand={toggleRailWide} />
```

(d) `frontend/src/app.css`: directly after the `.main { … }` rule (line ~170–175), add:

```css
/* #151: the expanded rail map WIDENS the rail column; the chat (1fr) gets narrower
   but is never covered. Wide screens only — at <=820px the rail is the laps drawer
   and the map pane is hidden, and this 2-class selector would otherwise beat the
   narrow layout's single-column .main. */
@media (min-width: 821px) {
  .main.rail-wide {
    grid-template-columns: min(640px, 50vw) 1fr auto;
  }
}
```

- [ ] **Step 4: Run to verify they pass**

Run: `cd frontend && npx vitest run src/lib/TrackMapPane.test.ts src/lib/App.test.ts src/lib/TrackMap.test.ts`
Expected: PASS.

Run: `cd frontend && npx svelte-check --threshold error`
Expected: `0 errors`.

Run: `grep -n "TrackMapEditor\|viewing" frontend/src/lib/TrackMapPane.svelte`
Expected: no output.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/TrackMapPane.svelte frontend/src/lib/TrackMapPane.test.ts frontend/src/lib/TrackMap.svelte frontend/src/lib/App.svelte frontend/src/lib/App.test.ts frontend/src/app.css
git commit -m "feat(ui): #151 - expanding the rail map widens the rail instead of overlaying the chat

Refs #151

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Pywcy5ZyceRZNDLyjn3F35"
```

---

### Task 6: Retire the Pit Wall screen

**Files:**
- Modify: `frontend/src/lib/App.svelte`
- Modify: `frontend/src/lib/App.test.ts` (new mount sentinel; nav test)
- Delete: `frontend/src/lib/PitWall.svelte`, `frontend/src/lib/PitWall.test.ts`, `frontend/src/lib/LiveTower.svelte`, `frontend/src/lib/LiveTower.test.ts`, `frontend/src/lib/SectorDeltas.svelte`, `frontend/src/lib/SectorDeltas.test.ts`

**Interfaces:**
- Consumes: nothing new.
- Produces: `type Screen = "coach" | "sessions" | "tracks" | "settings"`. The shell's mount sentinel in tests becomes `screen.findByRole("navigation")` (the only `<nav>` in the app).

- [ ] **Step 1: Update the tests first (they must fail against the current App)**

In `frontend/src/lib/App.test.ts`:
- Replace the first test (`"renders the three nav items and a theme toggle"`) with:

```ts
  it("renders the nav items and a theme toggle — and no Pit Wall (#151)", async () => {
    const { container } = render(App);
    await screen.findByRole("navigation"); // shell mounts past the splash
    const nav = within(container.querySelector(".nav") as HTMLElement);

    expect(nav.getByText("Coach")).toBeTruthy();
    expect(nav.getByText("Sessions")).toBeTruthy();
    expect(nav.getByText("Tracks")).toBeTruthy();
    expect(nav.getByText("Settings")).toBeTruthy();
    expect(nav.getByText(/^Theme ·/)).toBeTruthy();
    expect(nav.queryByText("Pit Wall")).toBeNull();
    // Coach is the landing screen and still mounts.
    expect(nav.getByText("Coach").closest(".nav-item")?.classList.contains("on")).toBe(true);
  });
```

- Replace every remaining sentinel line. The replacement contains no backslashes, so `sed` is safe here:

```bash
sed -i 's/await screen.findByText("Pit Wall");/await screen.findByRole("navigation");/' frontend/src/lib/App.test.ts
grep -n "Pit Wall" frontend/src/lib/App.test.ts
```

Expected grep output: only the `queryByText("Pit Wall")` line above.

- [ ] **Step 2: Run to verify the nav test fails**

Run: `cd frontend && npx vitest run src/lib/App.test.ts`
Expected: FAIL in `"renders the nav items … no Pit Wall"`, because `nav.queryByText("Pit Wall")` is still present. Every other test PASSES with the new sentinel.

- [ ] **Step 3: Implement**

In `frontend/src/lib/App.svelte`:
- Delete `import PitWall from "./PitWall.svelte";`.
- `type Screen = "coach" | "sessions" | "pitwall" | "tracks" | "settings";` becomes `type Screen = "coach" | "sessions" | "tracks" | "settings";`.
- Delete the whole Pit Wall `<button …>` in `<nav>` (the one with `class:on={screen === "pitwall"}` and `<span class="lbl">Pit Wall</span>`).
- Replace the SessionBar comment and line:

```svelte
    <!-- One shared header for Coach + Pit Wall: a single instance (kept mounted
         across a coach<->pitwall switch) so the two views can never show different
         status/track/car for a frame. Sessions/Settings have their own headers. -->
    {#if screen === "coach" || screen === "pitwall"}<SessionBar />{/if}
```

with:

```svelte
    <!-- The Coach screen's header. Sessions/Tracks/Settings have their own. -->
    {#if screen === "coach"}<SessionBar />{/if}
```

- Delete the section:

```svelte
    <section class="screen" class:on={screen === "pitwall"}>
      {#if screen === "pitwall"}<PitWall />{/if}
    </section>
```

- Replace the `onMount` comment above `liveField.start()` with:

```ts
  // Start the shared feeds once, for the whole app lifetime: the /live feed (the
  // Coach rail map's live car + next corner, #151) and the /events feed that
  // replaced the screens' HTTP pollers (#106).
```

Then delete the retired components:

```bash
git rm frontend/src/lib/PitWall.svelte frontend/src/lib/PitWall.test.ts frontend/src/lib/LiveTower.svelte frontend/src/lib/LiveTower.test.ts frontend/src/lib/SectorDeltas.svelte frontend/src/lib/SectorDeltas.test.ts
```

- [ ] **Step 4: Run to verify**

Run: `grep -rn "PitWall\|LiveTower\|SectorDeltas\|pitwall" frontend/src`
Expected: no output.

Run: `cd frontend && npx vitest run` then `npx svelte-check --threshold error`
Expected: all green; `0 errors`.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/App.svelte frontend/src/lib/App.test.ts
git commit -m "feat(ui): #151 - retire the Pit Wall screen (its map now lives in the Coach rail)

Refs #151

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Pywcy5ZyceRZNDLyjn3F35"
```

(`git rm` already staged the deletions; they are part of this commit.)

---

### Task 7: Trim `/live` to what the rail map needs

**Files:**
- Modify: `simcoach/server/app.py` (`live_ws` `_live_tick`)
- Modify: `simcoach/lmu/live.py` (module docstring only)
- Modify: `frontend/src/lib/live.ts` (`LiveFrame` replaces `FieldSnapshot`/`SessionState`/`VehicleState`)
- Modify: `frontend/src/lib/TrackMapPane.svelte` (reads `LiveFrame`)
- Test: `tests/test_server_live.py`, `frontend/src/lib/live.test.ts`, `frontend/src/lib/TrackMapPane.test.ts`

**Interfaces:**
- Consumes: `_next_corner_view(dist)` (Task 2).
- Produces: `/live` frame = `{"live": bool, "player": {"lap_dist": float, "cls": str} | None, "next_corner": {"label", "name", "direction"} | None}`. In TS: `export interface LiveFrame { live: boolean; player: { lap_dist: number; cls: string } | null; next_corner: NextCorner | null }`, and `LiveField.snapshot: LiveFrame | null`. `read_field_snapshot()` and the Python dataclasses are unchanged (the #152 input).

- [ ] **Step 1: Update the tests first**

`tests/test_server_live.py`: replace `test_live_pushes_a_snapshot` and `test_live_reports_not_live_when_unavailable` with:

```python
def test_live_sends_only_what_the_rail_map_needs(tmp_path, monkeypatch):
    monkeypatch.setattr("simcoach.lmu.live.read_field_snapshot", lambda: _snap(True))
    # starlette's TestClient.websocket_connect hardcodes Host: testserver
    # regardless of base_url (unlike its HTTP methods) -- override explicitly
    # so this "good" request clears the Host guard (same fix as test_server_ws.py).
    with _client(tmp_path).websocket_connect("/live", headers={"host": "127.0.0.1"}) as ws:
        data = ws.receive_json()
    assert data == {"live": True, "player": {"lap_dist": 500.0, "cls": "GT3"},
                    "next_corner": None}


def test_live_reports_not_live_when_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr("simcoach.lmu.live.read_field_snapshot", lambda: _snap(False))
    with _client(tmp_path).websocket_connect("/live", headers={"host": "127.0.0.1"}) as ws:
        data = ws.receive_json()
    assert data == {"live": False, "player": None, "next_corner": None}
```

`frontend/src/lib/live.test.ts`: change the import and fixture to:

```ts
import { LiveField, type LiveFrame } from "./live";
```

```ts
const snap: LiveFrame = { live: true, player: { lap_dist: 500, cls: "GT3" }, next_corner: null };
```

and in the first test replace `let seen: FieldSnapshot | null = null;` with `let seen: LiveFrame | null = null;` and `expect(seen!.session.current_lap).toBe(9);` with `expect(seen!.player!.lap_dist).toBe(500);`.

`frontend/src/lib/TrackMapPane.test.ts`: replace `onTrackFrame` and `offTrackFrame` with the trimmed shape. The opponent is gone from the wire, so the only-the-player test now pins the dot count against the frame shape.

```ts
const onTrackFrame = (lapDist: number, next: Next) => ({
  live: true,
  player: { lap_dist: lapDist, cls: "GT3" },
  next_corner: next,
});
const offTrackFrame = () => ({ live: false, player: null, next_corner: null });
```

and in the first #151 test change the comment `// the opponent in the frame is NOT drawn` to `// one dot: the player's`.

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_server_live.py -q`
Expected: the two replaced tests FAIL (the payload still has `session`/`cars`).

Run: `cd frontend && npx vitest run src/lib/TrackMapPane.test.ts`
Expected: the on-track #151 tests FAIL (the pane still reads `session.live`).

- [ ] **Step 3: Implement**

(a) `simcoach/server/app.py`. In `live_ws`, change `from ..lmu.live import read_field_snapshot, snapshot_to_dict` to `from ..lmu.live import read_field_snapshot` and replace `_live_tick` with:

```python
        def _live_tick() -> dict:
            # Blocking mmap + track-file reads: run off the event loop. Trimmed to what
            # the Coach rail map draws (#151): the player's position and next corner.
            # The full field stays available to the core via read_field_snapshot()
            # (the race-state tools, #152) -- it just isn't streamed to the UI.
            snap = read_field_snapshot()
            me = next((c for c in snap.cars if c.is_player), None)
            if not snap.session.live or me is None:
                return {"live": False, "player": None, "next_corner": None}
            return {"live": True, "player": {"lap_dist": me.lap_dist, "cls": me.cls},
                    "next_corner": _next_corner_view(me.lap_dist)}
```

(`resolve_car` stays imported; `/status` still uses it.)

(b) `simcoach/lmu/live.py`: first docstring line `"""Pure live-field extraction for the Pit Wall view.` becomes:

```python
"""Pure live-field extraction: the Coach rail map's player position (#151) and the
input to the race-state tools (#152).
```

(c) `frontend/src/lib/live.ts`. Replace the three interfaces `VehicleState`, `SessionState` and `FieldSnapshot` with the following. Keep `CLASS_COLOR` (TrackMap colours the player's dot by class) and keep `NextCorner`.

```ts
/** One /live frame (#151): only what the Coach rail map draws. */
export interface LiveFrame {
  live: boolean;
  player: { lap_dist: number; cls: string } | null;
  next_corner: NextCorner | null;
}
```

In `class LiveField`, change `snapshot: FieldSnapshot | null = null;` to `snapshot: LiveFrame | null = null;` and `as FieldSnapshot` to `as LiveFrame`. Change the header comment's first line to `// WS client for the /live feed (simcoach/server/app.py @app.websocket("/live")).` Change the `liveField` comment to:

```ts
// Shared, app-lifetime live feed: started once at boot (App.svelte) and kept
// streaming regardless of the active view. The Coach rail map (TrackMapPane)
// subscribes; components must NOT start/stop it.
```

(d) `frontend/src/lib/TrackMapPane.svelte`: replace the `onTrack`, `player` and `liveCars` declarations with:

```ts
  const onTrack = $derived(live?.live === true);
  const player = $derived(onTrack ? (live?.player ?? null) : null);
  // ONLY the player's car: the field is the game HUD's job, not the rail's.
  const liveCars = $derived(
    player ? [{ id: 0, lapDist: player.lap_dist, cls: player.cls, isPlayer: true }] : [],
  );
```

- [ ] **Step 4: Run to verify**

Run: `.venv/Scripts/python.exe -m pytest -q` then `ruff check simcoach tests scripts`
Expected: green; `All checks passed!`

Run: `cd frontend && npx vitest run` then `npx svelte-check --threshold error`
Expected: green; `0 errors`.

Run: `grep -rn "FieldSnapshot\|SessionState\|VehicleState" frontend/src`
Expected: no output.

- [ ] **Step 5: Commit**

```bash
git add simcoach/server/app.py simcoach/lmu/live.py tests/test_server_live.py frontend/src/lib/live.ts frontend/src/lib/live.test.ts frontend/src/lib/TrackMapPane.svelte frontend/src/lib/TrackMapPane.test.ts
git commit -m "refactor(live): #151 - trim /live to the rail map's needs (player position + next corner)

Refs #151

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Pywcy5ZyceRZNDLyjn3F35"
```

---

### Task 8: Docs: no CrewChief replacement, race questions over PTT

**Files:**
- Modify: `docs/vision.md`, `CLAUDE.md`
- Modify (outside the repo, not committed): the Claude project memory file `pit-wall-voice-vision.md` and its `MEMORY.md` index line

**Interfaces:** none (docs).

- [ ] **Step 1: `docs/vision.md`: four edits**

(1) Replace mode 2:

```markdown
2. **Pit Wall** — live, in-race. A race-engineer persona for pit timing, fuel/
   energy-to-finish, gaps and deltas, relative pace. Agentic: it has the tools
   to answer whatever comes up, not a fixed command set. Aims to replace
   CrewChief. *Vision.*
```

with:

```markdown
2. **Race questions over PTT** — live, in-race. The coach answers race
   questions (position and gap trend, fuel/energy to the end, pit window, tire
   life, where a rival is faster) over push-to-talk, from figures computed at
   lap boundaries. Agentic: it has the tools to answer whatever comes up, not a
   fixed command set. **Not pursuing:** an in-ear spotter or a CrewChief
   replacement. The separate Pit Wall screen was retired 2026-09-22 (#151); its
   one used feature, the live corner map, is now the Coach screen's rail map.
   *Tools: #152.*
```

(2) Replace `During: hands-free race-engineer questions (Pit Wall).` with `During: race questions over push-to-talk (no in-ear spotter).`

(3) Replace build-order item 4:

```markdown
4. **Pit Wall persona + live-race tools** — standings/gaps, fuel/energy, pit
   window, relative pace (most data already reachable via `simcoach/lmu/rest.py`
   plus the per-lap position capture already in place).
```

with:

```markdown
4. **Race tools for the coach (PTT)** — race state and gap trend, fuel/energy
   and pit window, tire life, battle comparison (#152; spec
   `docs/superpowers/specs/2026-09-22-coach-absorbs-pit-wall-design.md` §3).
   The Pit Wall screen is retired (#151).
```

(4) Replace `Later/optional: proactive *push* spotter alerts ("box this lap, fuel's short"),` and `distinct from the agentic *pull* model above. Fast-follow after voice: make` with:

```markdown
Not pursuing: proactive *push* spotter alerts ("box this lap, fuel's short") —
race answers are PTT only (decided 2026-09-22). Fast-follow after voice: make
```

(the next line, `building a track map less of a curation loop (#100).`, stays).

- [ ] **Step 2: `CLAUDE.md`: scope line**

Replace:

```markdown
The deferred list (voice + live in-ear coaching, Assetto Corsa, multi-car/track
per session, local model, vector-DB RAG, side-by-side replay overlay, CrewChief
replacement) is intentional — each bolts onto a seam left open. The web UI is no
```

with:

```markdown
The deferred list (voice + live in-ear coaching, Assetto Corsa, multi-car/track
per session, local model, vector-DB RAG, side-by-side replay overlay) is
intentional — each bolts onto a seam left open. **Not pursuing:** a CrewChief
replacement / in-ear spotter — race questions are answered by the coach over PTT,
and the Pit Wall screen was retired (#151). The web UI is no
```

- [ ] **Step 3: Verify**

Run: `grep -n -i "crewchief\|pit wall" docs/vision.md CLAUDE.md`
Expected: only the new "not pursuing" / "retired" lines.

- [ ] **Step 4: Commit**

```bash
git add docs/vision.md CLAUDE.md
git commit -m "docs: #151 - Pit Wall retired; no CrewChief replacement, race questions over PTT

Refs #151

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Pywcy5ZyceRZNDLyjn3F35"
```

- [ ] **Step 5: Project memory (not committed)**

At the top of the body of `pit-wall-voice-vision.md`, just below the front-matter, insert:

```markdown
**SUPERSEDED 2026-09-22 (spec `2026-09-22-coach-absorbs-pit-wall-design.md`, #151/#152):** no CrewChief replacement and no in-ear spotter; no separate Pit Wall persona or screen (retired, #151). The coach answers race questions over **PTT only** via `get_race_state` / `get_strategy` / `get_battle` (#152). The live corner map lives in the Coach rail. The voice-first/VR point below still holds.
```

In `MEMORY.md`, change the `[Pit-wall + voice vision]` line's description to: `SUPERSEDED 2026-09-22: no CrewChief replacement/spotter, Pit Wall screen retired (#151); race questions = coach over PTT (#152); voice-first/VR still holds`.

---

## Rig check (why every commit is `Refs #151`, not `Fixes`)

Label the issue `rig-verify` if it isn't already. Close #151 by hand after a live look:

1. On track, the rail map shows your car only. The dot tracks your position, and the callout reads e.g. `Next: T10 Pouhon (L)`. It holds through a corner and advances at its exit. It wraps from the last corner to T1 across the line. It is correct on a track whose corner window wraps S/F, if one is to hand.
2. Sector ticks are where the game's S1/S2 splits are.
3. Back in the garage or menus, the rail shows the selected session's map again, with no dot and no callout.
4. Expand: the rail widens and the map grows. The chat stays fully readable next to it, never covered. The height resizer still works in both states. The choice survives an app restart. At a narrow window (≤820 px) nothing breaks.
5. The live track name the server uses (`_live_track`, telemetry) resolves the same corner map the rail draws. If the callout names corners from a different layout than the one drawn, that is the bug to look for.
6. No Pit Wall tab, and the Coach screen mounts normally.

## Follow-ups (not in this plan; file as issues if wanted)

- `getInputTraces` (`api.ts`) and `GET /track/inputs` lose their only UI caller (PitWall). `getCurrentMap` / `GET /track/map` were already UI-orphaned. Delete them or keep them for #152.
- The marketing site (`site/index.html`, `assets/pit-wall*.jpg`) still sells the Pit Wall. That copy is now wrong and needs the driver's call.
- The `CLAUDE.md` deferred list still says "voice + live in-ear coaching". Voice shipped, and proactive spoken callouts are now "not pursuing" (spec: PTT only). The spec only asked for the CrewChief line, so this was left for the driver.

---

## Self-Review

**Spec coverage**

| Spec bullet | Task |
|---|---|
| §1 reuse the existing rail `TrackMapPane`, no new panel/map | 4, 5 |
| §1 live while on track: player car dot | 4 (trimmed wire shape: 7) |
| §1 otherwise the selected session's map exactly as today | 4 (test "off track … as before") |
| §1 expand WIDENS the rail, pushes the chat narrower, never overlays | 5 (CSS + App test; layout is rig item 4) |
| §1 replaces the 🔍 overlay | 5 (magnifier + `TrackMapEditor` removed from the pane; test asserts it is gone) |
| §1 expanded choice remembered per viewer, try/catch, renders without it | 5 (three App tests incl. blocked storage) |
| §1 the rail's height resizer keeps working | 5 (`mapH` stays App-owned; resizer untouched; expand bump restores on collapse) |
| §1 reuses `TrackMap`/`TrackMapPane` + the live feed the Pit Wall used | 4 (`liveField` + `eventFeed.map`) |
| §1 track outline, corner labels | unchanged `TrackMap` |
| §1 sector boundaries from `sectors`; absent → none | 3 |
| §1 player's car only | 4 (frame carries an opponent that must not be drawn), 7 (wire carries only the player) |
| §1 next-corner callout computed server-side; direction from the map | 1, 2, 4 |
| §1 tests: next-corner unit (mid-lap, last corner → line, wrap, no map) | 1 (+ seam-wrapping window, unsorted, negative dist), 2 (no map → None) |
| §1 tests: vitest live vs session mode; expand widens and never overlays | 4, 5 |
| §2 remove the `pitwall` screen + nav tab | 6 |
| §2 delete PitWall/LiveTower/SectorDeltas + tests | 6 |
| §2 RaceStrip stays | untouched (Global Constraints) |
| §2 keep TrackMap/TrackMapPane; `/live` trimmed to player position + next corner | 7 |
| §2 keep `read_field_snapshot()` | 7 (unchanged; docstring only) |
| §2 docs: vision.md, CLAUDE.md scope line, pit-wall/voice memory | 8 |
| §2 tests: svelte-check + vitest green; App test asserts the tab is gone and Coach mounts | 6 |

**Placeholder scan:** no "TBD"/"TODO"/"similar to Task N"/"add error handling". Every code step has literal code. Every run step has an exact command and an expected result.

**Type consistency:**
- `next_corner(corners: list[Corner], dist: float) -> Corner | None` is defined in Task 1 and used in Task 2's `_next_corner_view`.
- The wire key is `next_corner` with `{label, name, direction}` in Tasks 2, 4 and 7, matching `NextCorner` (Task 4, `live.ts`).
- `app.state.live_cmap` is set in Task 2 (init, handler, `_invalidate_canon`).
- The TrackMapPane props are `sessionId`, `expanded?: boolean` and `onexpand?: () => void`, identical in Task 5's pane code, pane tests and App usage.
- The `TrackMap` `cars` item `{id, lapDist, cls, isPlayer}` matches the existing prop type in Tasks 4 and 7.
- The storage key `coachRail.expanded` is the same in App code and all three App tests.
- The `LiveFrame` fields `live`/`player.lap_dist`/`player.cls`/`next_corner` are the same in the Task 7 server payload, `live.ts`, the pane and the fixtures.
