# Pit Wall View Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the live, read-only Pit Wall race dashboard (track map with car dots, live timing tower with a Race-position/Car-deltas toggle, and a sector-deltas strip), fed by a new WebSocket over LMU scoring shared memory.

**Architecture:** A pure `read_field()` maps LMU scoring into a `FieldSnapshot` (all cars + a session/player block). A thin `/live` WebSocket pushes that snapshot ~4×/s. The frontend `LiveField` store consumes it (auto-reconnecting) and `PitWall.svelte` renders three sibling views from the one snapshot. No LLM, no analysis logic — pure transport + projection, so a future web client reuses the core unchanged.

**Tech Stack:** Python 3 / FastAPI / ctypes (backend); Svelte 5 (runes) / TypeScript / Vitest (frontend); pytest (backend tests).

**Spec:** `docs/superpowers/specs/2026-09-06-pit-wall-view-design.md`

## Global Constraints

- **No LLM in this view.** Pit Wall never calls the `Model`/coach; corner identity and the two-stage pipeline are untouched.
- **UI-agnostic core.** All field logic is a pure function in `simcoach/lmu/live.py`; the WS endpoint is thin transport only. No analysis/coaching logic in the transport or the Svelte components.
- **Read-only shared memory.** Reuse the existing `read_consistent` / `open_first_available` readers; never `OpenProcess`/`ReadProcessMemory` (anti-cheat footprint — see `smm.py` docstring).
- **PENDING LIVE VALIDATION.** LMU struct offsets/units are unverified against a running sim (see `rf2structs.py`). Any new field read carries the same caveat; note it in a comment, do not block on it.
- **Frontend API base:** `import.meta.env.VITE_API_BASE ?? "http://127.0.0.1:8765"`, `ws` scheme derived by `API_BASE.replace(/^http/, "ws")` (mirror `coach.ts`).
- **Deferred, do NOT build:** track-condition flag strip (needs the untranscribed `rF2ScoringInfo._tail`), any coaching/voice affordance on Pit Wall (stays in Coach), live *predictive* in-sector delta, replay overlay.

---

## File Structure

**Backend**
- Create `simcoach/lmu/live.py` — dataclasses (`VehicleState`, `SessionState`, `FieldSnapshot`), pure `read_field(scoring)`, and `read_field_snapshot()` (does the SMM read; always returns a snapshot, `live=False` when unavailable).
- Modify `simcoach/server/app.py` — add the `@app.websocket("/live")` endpoint.
- Create `tests/test_lmu_live.py` — pure `read_field` unit tests.
- Create `tests/test_server_live.py` — `/live` WS endpoint tests (monkeypatch the reader).

**Frontend**
- Create `frontend/src/lib/live.ts` — `FieldSnapshot`/`VehicleState`/`SessionState` types, `CLASS_COLOR`, and the reconnecting `LiveField` store.
- Modify `frontend/src/lib/TrackMap.svelte` — add an optional `cars` prop + a dot overlay layer.
- Create `frontend/src/lib/LiveTower.svelte` — field list + Race-position/Car-deltas toggle.
- Create `frontend/src/lib/SectorDeltas.svelte` — player sector-delta strip.
- Create `frontend/src/lib/PitWall.svelte` — composes the screen; owns the store + status poll.
- Modify `frontend/src/lib/App.svelte` — replace the `pitwall` placeholder with `<PitWall />`.
- Create `frontend/src/lib/live.test.ts`, `LiveTower.test.ts`, `SectorDeltas.test.ts`; extend `TrackMap.test.ts`.

---

## Task 1: Pure field extraction (`read_field`)

**Files:**
- Create: `simcoach/lmu/live.py`
- Test: `tests/test_lmu_live.py`

**Interfaces:**
- Consumes: `rF2Scoring`, `VehicleScoringInfoV01`, `MAX_MAPPED_VEHICLES` from `simcoach.lmu.rf2structs`.
- Produces:
  - `@dataclass VehicleState`: `id:int, place:int, class_position:int, driver:str, car:str, cls:str, lap_dist:float, sector:int, laps:int, in_pits:bool, is_player:bool, gap_leader:float|None, laps_behind_leader:int, gap_next:float|None, laps_behind_next:int`
  - `@dataclass SessionState`: `live:bool, phase:str, current_lap:int|None, max_laps:int|None, elapsed:float, end_time:float, cur_s1:float|None, cur_s2:float|None, best_s1:float|None, best_s2:float|None, last_lap:float|None, best_lap:float|None`
  - `@dataclass FieldSnapshot`: `session:SessionState, cars:list[VehicleState]`
  - `read_field(scoring: rF2Scoring) -> FieldSnapshot`
  - `empty_snapshot() -> FieldSnapshot`

Note on conventions (mirror `smm.compute_sectors`): `mCurSector2`/`mBestSector2` are CUMULATIVE (s1+s2). A sector time `<= 0` means "not set yet" → surface as `None`. `mPlace == 0` is the unset sentinel. Gaps: when `mLapsBehind* > 0` the seconds field is meaningless → set the gap to `None` and carry the lap count.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_lmu_live.py
import pytest
from simcoach.lmu.rf2structs import rF2Scoring, VehicleScoringInfoV01
from simcoach.lmu.live import read_field, empty_snapshot, FieldSnapshot


def _veh(**kw) -> VehicleScoringInfoV01:
    v = VehicleScoringInfoV01()
    for k, val in kw.items():
        if k in ("mDriverName", "mVehicleName", "mVehicleClass"):
            setattr(v, k, val.encode())
        else:
            setattr(v, k, val)
    return v


def _scoring(vehicles, *, session=12, current_et=100.0, end_et=0.0, max_laps=24) -> rF2Scoring:
    s = rF2Scoring()
    s.mScoringInfo.mSession = session
    s.mScoringInfo.mCurrentET = current_et
    s.mScoringInfo.mEndET = end_et
    s.mScoringInfo.mMaxLaps = max_laps
    s.mScoringInfo.mNumVehicles = len(vehicles)
    for i, v in enumerate(vehicles):
        s.mVehicles[i] = v
    return s


def test_read_field_maps_basic_fields_and_player():
    s = _scoring([
        _veh(mID=1, mPlace=1, mDriverName="A. Lead", mVehicleName="Ferrari 499P",
             mVehicleClass="Hypercar", mLapDist=100.0, mSector=0, mTotalLaps=8,
             mTimeBehindLeader=0.0, mLapsBehindLeader=0),
        _veh(mID=2, mPlace=9, mDriverName="You", mVehicleName="Corvette Z06 GT3.R",
             mVehicleClass="GT3", mLapDist=500.0, mSector=1, mTotalLaps=8, mIsPlayer=True,
             mTimeBehindNext=2.4, mLapsBehindNext=0, mTimeBehindLeader=31.5, mLapsBehindLeader=0,
             mCurSector1=30.1, mBestSector1=29.8, mLastLapTime=92.5, mBestLapTime=91.9),
    ])
    snap = read_field(s)
    assert snap.session.live is True
    assert snap.session.current_lap == 9          # player mTotalLaps + 1
    assert snap.session.max_laps == 24
    assert snap.session.best_s1 == pytest.approx(29.8)
    assert snap.session.last_lap == pytest.approx(92.5)
    you = next(c for c in snap.cars if c.is_player)
    assert you.place == 9
    assert you.car == "Corvette Z06 GT3.R"
    assert you.cls == "GT3"
    assert you.gap_next == pytest.approx(2.4)


def test_class_position_ranks_within_class():
    s = _scoring([
        _veh(mID=1, mPlace=1, mVehicleClass="Hypercar", mVehicleName="H1"),
        _veh(mID=2, mPlace=2, mVehicleClass="GT3", mVehicleName="G1"),
        _veh(mID=3, mPlace=3, mVehicleClass="GT3", mVehicleName="G2", mIsPlayer=True),
    ])
    snap = read_field(s)
    by_id = {c.id: c for c in snap.cars}
    assert by_id[1].class_position == 1
    assert by_id[2].class_position == 1
    assert by_id[3].class_position == 2


def test_laps_down_zeroes_the_seconds_gap():
    s = _scoring([
        _veh(mID=1, mPlace=1, mVehicleClass="GT3", mVehicleName="G1"),
        _veh(mID=2, mPlace=2, mVehicleClass="GT3", mVehicleName="G2", mIsPlayer=True,
             mTimeBehindLeader=45.0, mLapsBehindLeader=1),
    ])
    you = next(c for c in read_field(s).cars if c.is_player)
    assert you.gap_leader is None
    assert you.laps_behind_leader == 1


def test_unset_sector_time_is_none():
    s = _scoring([_veh(mID=2, mPlace=1, mVehicleClass="GT3", mVehicleName="G1",
                       mIsPlayer=True, mCurSector1=0.0, mBestSector1=-1.0)])
    snap = read_field(s)
    assert snap.session.cur_s1 is None
    assert snap.session.best_s1 is None


def test_no_player_is_not_live():
    s = _scoring([_veh(mID=1, mPlace=1, mVehicleClass="GT3", mVehicleName="G1")])
    snap = read_field(s)
    assert snap.session.live is False


def test_clamps_to_num_vehicles():
    s = _scoring([_veh(mID=1, mPlace=1, mVehicleClass="GT3", mVehicleName="G1", mIsPlayer=True)])
    s.mScoringInfo.mNumVehicles = 1
    # a stray struct past the count must be ignored
    s.mVehicles[5] = _veh(mID=99, mPlace=2, mVehicleClass="GT3", mVehicleName="ghost")
    snap = read_field(s)
    assert [c.id for c in snap.cars] == [1]


def test_empty_snapshot_is_not_live():
    snap = empty_snapshot()
    assert isinstance(snap, FieldSnapshot)
    assert snap.session.live is False
    assert snap.cars == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_lmu_live.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'simcoach.lmu.live'`

- [ ] **Step 3: Write the implementation**

```python
# simcoach/lmu/live.py
"""Pure live-field extraction for the Pit Wall view.

read_field() maps one LMU scoring frame (all vehicles) into a FieldSnapshot the
UI renders directly. Pure and MMF-free so it is unit-tested on synthetic
rF2Scoring structs (tests/test_lmu_live.py), same pattern as test_lmu_smm.py.

Sign/unit conventions follow rf2structs.py + smm.compute_sectors and are PENDING
LIVE VALIDATION: mCurSector2/mBestSector2 are CUMULATIVE (s1+s2); a sector/lap
time <= 0 is LMU's "not set" sentinel.
"""
from dataclasses import dataclass, field

from simcoach.lmu.rf2structs import MAX_MAPPED_VEHICLES, VehicleScoringInfoV01, rF2Scoring


@dataclass
class VehicleState:
    id: int
    place: int
    class_position: int
    driver: str
    car: str
    cls: str
    lap_dist: float
    sector: int
    laps: int
    in_pits: bool
    is_player: bool
    gap_leader: float | None
    laps_behind_leader: int
    gap_next: float | None
    laps_behind_next: int


@dataclass
class SessionState:
    live: bool
    phase: str
    current_lap: int | None
    max_laps: int | None
    elapsed: float
    end_time: float
    cur_s1: float | None
    cur_s2: float | None
    best_s1: float | None
    best_s2: float | None
    last_lap: float | None
    best_lap: float | None


@dataclass
class FieldSnapshot:
    session: SessionState
    cars: list[VehicleState] = field(default_factory=list)


def _pos(x: float) -> float | None:
    """LMU sentinel: a sector/lap time <= 0 means 'not set'."""
    return float(x) if x and x > 0 else None


def _decode(raw: bytes) -> str:
    return raw.decode(errors="replace").strip()


def empty_snapshot() -> FieldSnapshot:
    return FieldSnapshot(
        session=SessionState(
            live=False, phase="", current_lap=None, max_laps=None,
            elapsed=0.0, end_time=0.0, cur_s1=None, cur_s2=None,
            best_s1=None, best_s2=None, last_lap=None, best_lap=None,
        ),
        cars=[],
    )


def _phase(m_session: int) -> str:
    # Reuse the coarse mapping smm.session_phase already defines.
    from simcoach.lmu.smm import session_phase
    return session_phase(m_session)


def _vehicle_state(v: VehicleScoringInfoV01, class_position: int) -> VehicleState:
    return VehicleState(
        id=int(v.mID),
        place=int(v.mPlace),
        class_position=class_position,
        driver=_decode(v.mDriverName),
        car=_decode(v.mVehicleName),
        cls=_decode(v.mVehicleClass),
        lap_dist=float(v.mLapDist),
        sector=int(v.mSector),
        laps=int(v.mTotalLaps),
        in_pits=bool(v.mInPits),
        is_player=bool(v.mIsPlayer),
        gap_leader=None if v.mLapsBehindLeader else float(v.mTimeBehindLeader),
        laps_behind_leader=int(v.mLapsBehindLeader),
        gap_next=None if v.mLapsBehindNext else float(v.mTimeBehindNext),
        laps_behind_next=int(v.mLapsBehindNext),
    )


def read_field(scoring: rF2Scoring) -> FieldSnapshot:
    info = scoring.mScoringInfo
    n = max(0, min(int(info.mNumVehicles), MAX_MAPPED_VEHICLES))
    raw = [scoring.mVehicles[i] for i in range(n)]

    # class_position: rank within each class by place (1-based).
    by_class: dict[str, list] = {}
    for v in raw:
        by_class.setdefault(_decode(v.mVehicleClass), []).append(v)
    class_pos: dict[int, int] = {}
    for members in by_class.values():
        for rank, v in enumerate(sorted(members, key=lambda x: int(x.mPlace)), start=1):
            class_pos[int(v.mID)] = rank

    cars = [_vehicle_state(v, class_pos[int(v.mID)]) for v in raw]
    player = next((v for v in raw if v.mIsPlayer), None)

    if player is None:
        snap = empty_snapshot()
        snap.cars = cars
        return snap

    return FieldSnapshot(
        session=SessionState(
            live=True,
            phase=_phase(int(info.mSession)),
            current_lap=int(player.mTotalLaps) + 1,
            max_laps=int(info.mMaxLaps) or None,
            elapsed=float(info.mCurrentET),
            end_time=float(info.mEndET),
            cur_s1=_pos(player.mCurSector1),
            cur_s2=_pos(player.mCurSector2),
            best_s1=_pos(player.mBestSector1),
            best_s2=_pos(player.mBestSector2),
            last_lap=_pos(player.mLastLapTime),
            best_lap=_pos(player.mBestLapTime),
        ),
        cars=cars,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_lmu_live.py -v`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add simcoach/lmu/live.py tests/test_lmu_live.py
git commit -m "feat(live): pure read_field over LMU scoring for the pit wall"
```

---

## Task 2: `/live` WebSocket endpoint

**Files:**
- Modify: `simcoach/lmu/live.py` (add `read_field_snapshot`)
- Modify: `simcoach/server/app.py` (add the endpoint; near the `/coach` handler ~line 892)
- Test: `tests/test_server_live.py`

**Interfaces:**
- Consumes: `read_field`, `empty_snapshot`, `FieldSnapshot` from Task 1; `read_consistent`, `SCORING_CANDIDATES` from `simcoach.lmu.smm`; `rF2Scoring` from `rf2structs`.
- Produces:
  - `read_field_snapshot() -> FieldSnapshot` (module-level in `live.py`; monkeypatched in tests). Never raises — returns `empty_snapshot()` on any failure.
  - `snapshot_to_dict(snap: FieldSnapshot) -> dict` (JSON-safe; `dataclasses.asdict`).
  - WS `/live` that sends one snapshot dict immediately on connect, then every `_LIVE_PERIOD` seconds.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_server_live.py
from fastapi.testclient import TestClient

from simcoach.server.app import create_app
from simcoach.store import LapStore
from simcoach.coach import Coach
from simcoach.model.fake import FakeModel
from simcoach.lmu.live import FieldSnapshot, SessionState, VehicleState


def _client(tmp_path):
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    coach = Coach(FakeModel(), "simcoach/knowledge")
    return TestClient(create_app(store, coach), base_url="http://127.0.0.1")


def _snap(live):
    session = SessionState(live=live, phase="race", current_lap=9, max_laps=24,
                           elapsed=100.0, end_time=0.0, cur_s1=None, cur_s2=None,
                           best_s1=None, best_s2=None, last_lap=None, best_lap=None)
    cars = [VehicleState(id=2, place=9, class_position=3, driver="You", car="Corvette",
                         cls="GT3", lap_dist=500.0, sector=1, laps=8, in_pits=False,
                         is_player=True, gap_leader=31.5, laps_behind_leader=0,
                         gap_next=2.4, laps_behind_next=0)] if live else []
    return FieldSnapshot(session=session, cars=cars)


def test_live_pushes_a_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr("simcoach.lmu.live.read_field_snapshot", lambda: _snap(True))
    with _client(tmp_path).websocket_connect("/live") as ws:
        data = ws.receive_json()
    assert data["session"]["live"] is True
    assert data["session"]["current_lap"] == 9
    assert data["cars"][0]["is_player"] is True
    assert data["cars"][0]["gap_next"] == 2.4


def test_live_reports_not_live_when_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr("simcoach.lmu.live.read_field_snapshot", lambda: _snap(False))
    with _client(tmp_path).websocket_connect("/live") as ws:
        data = ws.receive_json()
    assert data["session"]["live"] is False
    assert data["cars"] == []


def test_live_rejects_spoofed_host(tmp_path):
    import pytest
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with _client(tmp_path).websocket_connect("/live", headers={"host": "evil.com"}) as ws:
            ws.receive_json()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_server_live.py -v`
Expected: FAIL — no `/live` route (WebSocketDisconnect on connect for the first two tests).

- [ ] **Step 3a: Add `read_field_snapshot` + `snapshot_to_dict` to `live.py`**

```python
# append to simcoach/lmu/live.py
import ctypes as _C
from dataclasses import asdict


def snapshot_to_dict(snap: FieldSnapshot) -> dict:
    return asdict(snap)


def read_field_snapshot() -> FieldSnapshot:
    """Read one live scoring frame and map it. Never raises: returns
    empty_snapshot() (live=False) when LMU isn't running / the read fails, so the
    /live socket can keep polling instead of dropping."""
    from simcoach.lmu.smm import SCORING_CANDIDATES, open_first_available, read_consistent
    try:
        name, handle = open_first_available(SCORING_CANDIDATES, _C.sizeof(rF2Scoring))
        handle.close()
        scoring = read_consistent(name, rF2Scoring)
    except Exception:
        return empty_snapshot()
    return read_field(scoring)
```

- [ ] **Step 3b: Add the `/live` endpoint in `app.py`** (place it just after the `/coach` handler, before `@app.get("/settings")`)

```python
    _LIVE_PERIOD = 0.25  # ~4 Hz; the map/tower don't need capture-rate updates

    @app.websocket("/live")
    async def live_ws(ws: WebSocket):
        # HTTP Host-guard middleware does NOT run for websockets — check here
        # (same preamble as /coach).
        host = (ws.headers.get("host") or "").split(":")[0].lower()
        if host not in ALLOWED_HOSTS or _bad_origin(ws.headers.get("origin")):
            await ws.close(code=1008)
            return
        await ws.accept()

        from ..lmu.live import read_field_snapshot, snapshot_to_dict
        loop = asyncio.get_running_loop()
        try:
            while True:
                # Blocking mmap read off the event loop.
                snap = await loop.run_in_executor(None, read_field_snapshot)
                await ws.send_json(snapshot_to_dict(snap))
                await asyncio.sleep(_LIVE_PERIOD)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            if ws.client_state == WebSocketState.CONNECTED:
                try: await ws.close()
                except Exception: pass
```

Note: the test monkeypatches `simcoach.lmu.live.read_field_snapshot`; the endpoint imports it *inside* the handler (as written) so the patched attribute is picked up at call time. Keep the import inside the handler.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_server_live.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Run the full backend suite (no regressions)**

Run: `python -m pytest tests/test_lmu_live.py tests/test_server_live.py tests/test_lmu_smm.py tests/test_server_health.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add simcoach/lmu/live.py simcoach/server/app.py tests/test_server_live.py
git commit -m "feat(live): /live websocket pushing field snapshots ~4Hz"
```

---

## Task 3: Frontend `LiveField` store

**Files:**
- Create: `frontend/src/lib/live.ts`
- Test: `frontend/src/lib/live.test.ts`

**Interfaces:**
- Produces:
  - `interface VehicleState`, `interface SessionState`, `interface FieldSnapshot` (mirror Task 1 JSON, snake_case keys).
  - `const CLASS_COLOR: Record<string, string>`.
  - `class LiveField` with `snapshot: FieldSnapshot | null`, `connected: boolean`, `subscribe(run)` (Svelte store contract), `start(): void`, `stop(): void`. Reconnects on close with a fixed backoff.

- [ ] **Step 1: Write the failing test**

```ts
// frontend/src/lib/live.test.ts
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { LiveField, type FieldSnapshot } from "./live";

class FakeWS {
  static last: FakeWS | null = null;
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  close = vi.fn(() => { this.onclose?.(); });
  constructor(public url: string) { FakeWS.last = this; }
}

const snap: FieldSnapshot = {
  session: { live: true, phase: "race", current_lap: 9, max_laps: 24, elapsed: 100,
    end_time: 0, cur_s1: null, cur_s2: null, best_s1: null, best_s2: null,
    last_lap: null, best_lap: null },
  cars: [],
};

beforeEach(() => { vi.stubGlobal("WebSocket", FakeWS as unknown as typeof WebSocket); });
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe("LiveField", () => {
  it("exposes the latest snapshot to subscribers", () => {
    const lf = new LiveField();
    let seen: FieldSnapshot | null = null;
    lf.subscribe((v) => { seen = v.snapshot; });
    lf.start();
    FakeWS.last!.onopen?.();
    FakeWS.last!.onmessage?.({ data: JSON.stringify(snap) });
    expect(seen!.session.current_lap).toBe(9);
    expect(lf.connected).toBe(true);
  });

  it("reconnects after the socket closes", () => {
    vi.useFakeTimers();
    const lf = new LiveField();
    lf.subscribe(() => {});
    lf.start();
    const first = FakeWS.last!;
    first.onclose?.();
    expect(lf.connected).toBe(false);
    vi.advanceTimersByTime(2000);
    expect(FakeWS.last).not.toBe(first); // a new socket was opened
    vi.useRealTimers();
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd frontend && npx vitest run src/lib/live.test.ts`
Expected: FAIL — cannot resolve `./live`.

- [ ] **Step 3: Write the implementation**

```ts
// frontend/src/lib/live.ts
// WS client for the /live pit-wall feed (simcoach/server/app.py @app.websocket("/live")).
// UI-agnostic core rule: this file only streams + shapes the snapshot, no analysis.

const API_BASE = import.meta.env.VITE_API_BASE ?? "http://127.0.0.1:8765";
const WS_BASE = API_BASE.replace(/^http/, "ws");
const RECONNECT_MS = 1500;

// Class colors matching the game's representation (mirrors the v1 prototype).
export const CLASS_COLOR: Record<string, string> = {
  Hypercar: "#EF4444", LMP2: "#3B82F6", LMP3: "#A855F7", GT3: "#22C55E", GTE: "#F59E0B",
};

export interface VehicleState {
  id: number; place: number; class_position: number;
  driver: string; car: string; cls: string;
  lap_dist: number; sector: number; laps: number;
  in_pits: boolean; is_player: boolean;
  gap_leader: number | null; laps_behind_leader: number;
  gap_next: number | null; laps_behind_next: number;
}

export interface SessionState {
  live: boolean; phase: string;
  current_lap: number | null; max_laps: number | null;
  elapsed: number; end_time: number;
  cur_s1: number | null; cur_s2: number | null;
  best_s1: number | null; best_s2: number | null;
  last_lap: number | null; best_lap: number | null;
}

export interface FieldSnapshot { session: SessionState; cars: VehicleState[]; }

export class LiveField {
  snapshot: FieldSnapshot | null = null;
  connected = false;

  #ws: WebSocket | null = null;
  #stopped = false;
  #timer: ReturnType<typeof setTimeout> | null = null;
  #listeners = new Set<(value: this) => void>();

  subscribe(run: (value: this) => void): () => void {
    this.#listeners.add(run);
    run(this);
    return () => this.#listeners.delete(run);
  }

  start(): void {
    this.#stopped = false;
    this.#open();
  }

  stop(): void {
    this.#stopped = true;
    if (this.#timer) { clearTimeout(this.#timer); this.#timer = null; }
    this.#close();
    this.connected = false;
    this.#notify();
  }

  #open(): void {
    const ws = new WebSocket(`${WS_BASE}/live`);
    this.#ws = ws;
    ws.onopen = () => { this.connected = true; this.#notify(); };
    ws.onmessage = (e: MessageEvent) => {
      try { this.snapshot = JSON.parse(e.data as string) as FieldSnapshot; }
      catch { return; }
      this.#notify();
    };
    ws.onerror = () => ws.close();
    ws.onclose = () => {
      this.connected = false;
      this.#notify();
      if (!this.#stopped && !this.#timer) {
        this.#timer = setTimeout(() => { this.#timer = null; this.#open(); }, RECONNECT_MS);
      }
    };
  }

  #close(): void {
    const ws = this.#ws;
    this.#ws = null;
    if (!ws) return;
    ws.onopen = ws.onmessage = ws.onerror = ws.onclose = null;
    ws.close();
  }

  #notify(): void { for (const l of this.#listeners) l(this); }
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd frontend && npx vitest run src/lib/live.test.ts`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/live.ts frontend/src/lib/live.test.ts
git commit -m "feat(live): reconnecting LiveField store for the /live feed"
```

---

## Task 4: Car-dots overlay on `TrackMap`

**Files:**
- Modify: `frontend/src/lib/TrackMap.svelte`
- Test: `frontend/src/lib/TrackMap.test.ts` (add a case)

**Interfaces:**
- Consumes: `CLASS_COLOR` from `./live`; the existing `nearestDistIndex(map.dist, d)` and `map.path`/`map.dist`.
- Produces: `TrackMap` gains an optional prop `cars?: { lapDist: number; cls: string; isPlayer: boolean }[]` (default `[]`). When `map` is loaded, renders one dot per car at `map.path[nearestDistIndex(map.dist, lapDist)]`, colored by `CLASS_COLOR[cls]`, the player's dot larger + outlined.

- [ ] **Step 1: Write the failing test**

Read the top of `frontend/src/lib/TrackMap.test.ts` first to reuse its map-mock + render helper. Then add:

```ts
// add to frontend/src/lib/TrackMap.test.ts
it("renders a dot per live car, positioned by lap distance", async () => {
  // (reuse this file's existing getLapMap mock returning a map with
  //  path=[[0,0],[10,0],[20,0]] and dist=[0,50,100])
  const { container } = render(TrackMap, {
    props: {
      lapId: 1,
      cars: [
        { lapDist: 0, cls: "GT3", isPlayer: false },
        { lapDist: 100, cls: "Hypercar", isPlayer: true },
      ],
    },
  });
  await waitFor(() => expect(container.querySelectorAll(".car-dot").length).toBe(2));
  const player = container.querySelector(".car-dot.player");
  expect(player).not.toBeNull();
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd frontend && npx vitest run src/lib/TrackMap.test.ts`
Expected: FAIL — no `.car-dot` elements.

- [ ] **Step 3: Implement the overlay**

In the `<script>` block, extend the props and add a derived list:

```ts
import { CLASS_COLOR } from "./live";

let { lapId, cars = [] }: {
  lapId: number | null;
  cars?: { lapDist: number; cls: string; isPlayer: boolean }[];
} = $props();

const carDots = $derived(
  map
    ? cars.map((c) => {
        const [x, y] = map!.path[nearestDistIndex(map!.dist, c.lapDist)];
        return { x, y, color: CLASS_COLOR[c.cls] ?? "var(--faint)", isPlayer: c.isPlayer };
      })
    : [],
);
```

In the SVG (inside the `{#if map}` / `<svg …>` block, after the existing corner/section markup so dots draw on top), add:

```svelte
{#each carDots as d (d.x + "," + d.y + d.isPlayer)}
  <circle
    class="car-dot"
    class:player={d.isPlayer}
    cx={d.x}
    cy={d.y}
    r={d.isPlayer ? u * 0.03 : u * 0.022}
    fill={d.color}
    stroke={d.isPlayer ? "var(--text)" : "var(--surface)"}
    stroke-width={u * (d.isPlayer ? 0.012 : 0.008)}
  />
{/each}
```

(`u` is the existing unit scalar used elsewhere in this component for stroke widths; reuse it. If it is not in scope in the SVG block, compute dot radii from `map.width` the same way the file already derives `u`.)

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd frontend && npx vitest run src/lib/TrackMap.test.ts`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/TrackMap.svelte frontend/src/lib/TrackMap.test.ts
git commit -m "feat(trackmap): optional live car-dots overlay"
```

---

## Task 5: `LiveTower` component (field list + toggle)

**Files:**
- Create: `frontend/src/lib/LiveTower.svelte`
- Test: `frontend/src/lib/LiveTower.test.ts`

**Interfaces:**
- Consumes: `FieldSnapshot`, `VehicleState`, `CLASS_COLOR` from `./live`.
- Produces: `LiveTower` with prop `snapshot: FieldSnapshot | null`. Internal `mode: "position" | "deltas"` persisted to `localStorage["pitwall.tower"]`.
  - **position** mode: all cars sorted by `place`; gap text = `gap_leader` formatted (`+1L` when `laps_behind_leader > 0`, blank for the leader).
  - **deltas** mode: window of cars around the player (up to 3 ahead + player + 3 behind by `place` order); gap text from `gap_next` (`laps_behind_next` → `+nL`).
  - Player row marked with class `you`.

- [ ] **Step 1: Write the failing test**

```ts
// frontend/src/lib/LiveTower.test.ts
import { describe, it, expect, beforeEach } from "vitest";
import { render } from "@testing-library/svelte";
import LiveTower from "./LiveTower.svelte";
import type { FieldSnapshot, VehicleState } from "./live";

function car(p: number, over: Partial<VehicleState> = {}): VehicleState {
  return { id: p, place: p, class_position: p, driver: `D${p}`, car: "Car", cls: "GT3",
    lap_dist: 0, sector: 0, laps: 8, in_pits: false, is_player: false,
    gap_leader: p === 1 ? 0 : p, laps_behind_leader: 0,
    gap_next: p === 1 ? 0 : 1.2, laps_behind_next: 0, ...over };
}

const snap: FieldSnapshot = {
  session: { live: true, phase: "race", current_lap: 9, max_laps: 24, elapsed: 100,
    end_time: 0, cur_s1: null, cur_s2: null, best_s1: null, best_s2: null,
    last_lap: null, best_lap: null },
  cars: [car(1), car(2), car(3, { is_player: true }), car(4), car(5)],
};

beforeEach(() => localStorage.clear());

describe("LiveTower", () => {
  it("lists the full field ordered by place in position mode", () => {
    const { container } = render(LiveTower, { props: { snapshot: snap } });
    const rows = container.querySelectorAll(".trow");
    expect(rows.length).toBe(5);
    expect(container.querySelector(".trow.you")).not.toBeNull();
  });

  it("windows around the player in deltas mode", async () => {
    localStorage.setItem("pitwall.tower", "deltas");
    const { container } = render(LiveTower, { props: { snapshot: snap } });
    // player is P3, so with 3-each window the whole 5-car field still shows,
    // but the anchored player row must be present and marked.
    expect(container.querySelector(".trow.you")).not.toBeNull();
  });

  it("renders nothing useful when snapshot is null", () => {
    const { container } = render(LiveTower, { props: { snapshot: null } });
    expect(container.querySelectorAll(".trow").length).toBe(0);
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd frontend && npx vitest run src/lib/LiveTower.test.ts`
Expected: FAIL — cannot resolve `./LiveTower.svelte`.

- [ ] **Step 3: Implement the component**

```svelte
<!-- frontend/src/lib/LiveTower.svelte -->
<script lang="ts">
  import { CLASS_COLOR, type FieldSnapshot, type VehicleState } from "./live";

  let { snapshot }: { snapshot: FieldSnapshot | null } = $props();

  type Mode = "position" | "deltas";
  const KEY = "pitwall.tower";
  let mode: Mode = $state(
    ((): Mode => {
      try { return (localStorage.getItem(KEY) as Mode) || "position"; } catch { return "position"; }
    })(),
  );
  function setMode(m: Mode) {
    mode = m;
    try { localStorage.setItem(KEY, m); } catch { /* private mode */ }
  }

  const byPlace = $derived(
    snapshot ? [...snapshot.cars].sort((a, b) => a.place - b.place) : [],
  );

  const rows = $derived.by(() => {
    if (mode === "position") return byPlace;
    const i = byPlace.findIndex((c) => c.is_player);
    if (i < 0) return byPlace;
    return byPlace.slice(Math.max(0, i - 3), i + 4);
  });

  function gapText(c: VehicleState): string {
    if (mode === "deltas") {
      if (c.laps_behind_next > 0) return `+${c.laps_behind_next}L`;
      return c.gap_next != null && c.gap_next > 0 ? `+${c.gap_next.toFixed(1)}` : "—";
    }
    if (c.place === 1) return "Leader";
    if (c.laps_behind_leader > 0) return `+${c.laps_behind_leader}L`;
    return c.gap_leader != null ? `+${c.gap_leader.toFixed(1)}` : "—";
  }
</script>

<div class="tower">
  <div class="tower-h">
    <span>Live timing</span>
    <div class="toggle">
      <button class:on={mode === "position"} onclick={() => setMode("position")}>Position</button>
      <button class:on={mode === "deltas"} onclick={() => setMode("deltas")}>Deltas</button>
    </div>
  </div>
  {#each rows as c (c.id)}
    <div class="trow" class:you={c.is_player} style="--cls:{CLASS_COLOR[c.cls] ?? 'var(--faint)'}">
      <div class="pos">{c.place}</div>
      <div class="drv">
        {c.is_player ? "You" : c.driver}
        <span class="chip" style="color:{CLASS_COLOR[c.cls] ?? 'var(--faint)'};border-color:{CLASS_COLOR[c.cls] ?? 'var(--faint)'}">{c.cls}</span>
        <small>{c.car}</small>
      </div>
      <div class="gap">{gapText(c)}</div>
    </div>
  {/each}
</div>

<style>
  .tower { border-left: 1px solid var(--line); background: var(--surface); overflow-y: auto; min-height: 0; }
  .tower-h { display: flex; justify-content: space-between; align-items: center; gap: 8px;
    padding: 12px 14px 10px; font-family: var(--display, inherit); font-size: 12px; font-weight: 700;
    text-transform: uppercase; letter-spacing: .1em; color: var(--faint); }
  .toggle { display: flex; gap: 4px; }
  .toggle button { font-size: 10px; text-transform: uppercase; letter-spacing: .06em;
    padding: 3px 8px; border-radius: 6px; border: 1px solid var(--line);
    background: transparent; color: var(--muted); cursor: pointer; }
  .toggle button.on { color: var(--accent); border-color: var(--accent); background: var(--accent-soft); }
  .trow { display: grid; grid-template-columns: 24px 1fr auto; gap: 10px; align-items: center;
    padding: 9px 14px 9px 11px; border-top: 1px solid var(--line); border-left: 3px solid var(--cls, transparent); }
  .trow.you { background: var(--accent-soft); }
  .pos { font-weight: 700; text-align: center; font-variant-numeric: tabular-nums; }
  .drv { font-weight: 600; font-size: 13px; }
  .drv small { display: block; color: var(--muted); font-size: 11px; font-weight: 500; }
  .chip { font-size: 8.5px; font-weight: 700; text-transform: uppercase; border: 1px solid;
    border-radius: 4px; padding: 1px 4px; margin-left: 6px; }
  .gap { font-size: 12px; font-weight: 600; font-variant-numeric: tabular-nums; }
</style>
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd frontend && npx vitest run src/lib/LiveTower.test.ts`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/LiveTower.svelte frontend/src/lib/LiveTower.test.ts
git commit -m "feat(pitwall): LiveTower with position/deltas toggle"
```

---

## Task 6: `SectorDeltas` strip

**Files:**
- Create: `frontend/src/lib/SectorDeltas.svelte`
- Test: `frontend/src/lib/SectorDeltas.test.ts`

**Interfaces:**
- Consumes: `SessionState` from `./live`.
- Produces: `SectorDeltas` with prop `session: SessionState | null`. Shows S1, S2, S3, and Total cells vs the player's session-best:
  - **S1** (current lap, live): `cur_s1 - best_s1`, shown once `cur_s1` and `best_s1` are set.
  - **S2** (current lap, live): `(cur_s2 - cur_s1) - (best_s2 - best_s1)`, shown once `cur_s2` is set.
  - **S3** (last completed lap): `(last_lap - last_s2eq)`… S3 for the *current* lap only exists at the line, so v1 shows the **last lap's** S3 = `last_lap - cur_s2` is NOT valid across laps → v1 shows S3 as a dash and a deferred marker. (Live current-lap S3 is the deferred predictive seam.)
  - **Total** (last completed lap): `last_lap - best_lap`.
  - Each cell: negative delta → `good` class, positive → `bad`, missing data → `—`.

- [ ] **Step 1: Write the failing test**

```ts
// frontend/src/lib/SectorDeltas.test.ts
import { describe, it, expect } from "vitest";
import { render } from "@testing-library/svelte";
import SectorDeltas from "./SectorDeltas.svelte";
import type { SessionState } from "./live";

function session(over: Partial<SessionState> = {}): SessionState {
  return { live: true, phase: "race", current_lap: 9, max_laps: 24, elapsed: 100,
    end_time: 0, cur_s1: null, cur_s2: null, best_s1: null, best_s2: null,
    last_lap: null, best_lap: null, ...over };
}

describe("SectorDeltas", () => {
  it("shows a green S1 when the current sector beats best", () => {
    const { getByTestId } = render(SectorDeltas, {
      props: { session: session({ cur_s1: 29.5, best_s1: 29.8 }) },
    });
    const s1 = getByTestId("sec-s1");
    expect(s1.textContent).toContain("-0.3");
    expect(s1.className).toContain("good");
  });

  it("shows a red total when the last lap is slower than best", () => {
    const { getByTestId } = render(SectorDeltas, {
      props: { session: session({ last_lap: 92.5, best_lap: 91.9 }) },
    });
    const tot = getByTestId("sec-total");
    expect(tot.textContent).toContain("+0.6");
    expect(tot.className).toContain("bad");
  });

  it("dashes cells with no data", () => {
    const { getByTestId } = render(SectorDeltas, { props: { session: session() } });
    expect(getByTestId("sec-s1").textContent).toContain("—");
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd frontend && npx vitest run src/lib/SectorDeltas.test.ts`
Expected: FAIL — cannot resolve `./SectorDeltas.svelte`.

- [ ] **Step 3: Implement the component**

```svelte
<!-- frontend/src/lib/SectorDeltas.svelte -->
<script lang="ts">
  import type { SessionState } from "./live";

  let { session }: { session: SessionState | null } = $props();

  // Returns [text, kind] where kind ∈ "good"|"bad"|"none".
  function delta(cur: number | null, best: number | null): [string, string] {
    if (cur == null || best == null) return ["—", "none"];
    const d = cur - best;
    const s = `${d >= 0 ? "+" : "-"}${Math.abs(d).toFixed(3)}`;
    return [s, d <= 0 ? "good" : "bad"];
  }

  const s1 = $derived(delta(session?.cur_s1 ?? null, session?.best_s1 ?? null));
  const s2 = $derived(
    delta(
      session?.cur_s2 != null && session?.cur_s1 != null ? session.cur_s2 - session.cur_s1 : null,
      session?.best_s2 != null && session?.best_s1 != null ? session.best_s2 - session.best_s1 : null,
    ),
  );
  // S3 live requires lap completion (the sector closes at the line) — deferred seam.
  const s3: [string, string] = ["—", "none"];
  const total = $derived(delta(session?.last_lap ?? null, session?.best_lap ?? null));
</script>

<div class="sectors">
  <div class="sector"><span class="s">S1</span><span class="d {s1[1]}" data-testid="sec-s1">{s1[0]}</span></div>
  <div class="sector"><span class="s">S2</span><span class="d {s2[1]}" data-testid="sec-s2">{s2[0]}</span></div>
  <div class="sector"><span class="s">S3</span><span class="d {s3[1]}" data-testid="sec-s3">{s3[0]}</span></div>
  <div class="sector total"><span class="s">Lap</span><span class="d {total[1]}" data-testid="sec-total">{total[0]}</span></div>
</div>

<style>
  .sectors { display: flex; gap: 1px; background: var(--line); border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
  .sector { flex: 1; background: var(--surface); padding: 8px 14px; display: flex; align-items: baseline; gap: 10px; }
  .sector.total { background: var(--surface-2, var(--surface)); }
  .s { font-size: 10px; text-transform: uppercase; letter-spacing: .12em; color: var(--faint); }
  .d { margin-left: auto; font-weight: 600; font-variant-numeric: tabular-nums; }
  .d.good { color: var(--good); }
  .d.bad { color: var(--bad); }
  .d.none { color: var(--faint); }
</style>
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd frontend && npx vitest run src/lib/SectorDeltas.test.ts`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/SectorDeltas.svelte frontend/src/lib/SectorDeltas.test.ts
git commit -m "feat(pitwall): player sector-delta strip"
```

---

## Task 7: `PitWall` screen + wire into `App`

**Files:**
- Create: `frontend/src/lib/PitWall.svelte`
- Modify: `frontend/src/lib/App.svelte` (replace the `pitwall` placeholder)
- Test: `frontend/src/lib/PitWall.test.ts`

**Interfaces:**
- Consumes: `LiveField`, `FieldSnapshot`, `CLASS_COLOR` from `./live`; `SessionBar`, `TrackMap`, `LiveTower`, `SectorDeltas`; `getStatus` from `./api`.
- Produces: `PitWall` (no props). On mount: start a `LiveField`, poll `getStatus()` (every 2s) for the live `lap_id` to feed `TrackMap`. On destroy: stop both. Renders `SessionBar` (header), then a two-column body (`1fr 380px`): left = LIVE/lap header + `SectorDeltas` + `TrackMap` (with `cars` derived from the snapshot); right = `LiveTower`. When `snapshot?.session.live` is false, show a quiet "Waiting for LMU — get on track" state in the left column instead of the map.

- [ ] **Step 1: Write the failing test**

```ts
// frontend/src/lib/PitWall.test.ts
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, waitFor } from "@testing-library/svelte";

// Stub the network + WS deps before importing the component.
vi.mock("./api", () => ({
  getStatus: vi.fn(async () => ({ track: "Le Mans", car: "Corvette", car_model: null,
    on_track: true, activity: "track", has_map: true, lmu_connected: true,
    session_id: "s", lap_id: 42 })),
  getSettings: vi.fn(async () => ({ units: "metric" })),
  withRetry: (fn: () => unknown) => fn(),
  getLapMap: vi.fn(async () => ({})),
}));

import PitWall from "./PitWall.svelte";
import { LiveField } from "./live";

beforeEach(() => {
  vi.spyOn(LiveField.prototype, "start").mockImplementation(function (this: LiveField) {});
  vi.spyOn(LiveField.prototype, "stop").mockImplementation(function (this: LiveField) {});
});
afterEach(() => vi.restoreAllMocks());

describe("PitWall", () => {
  it("shows the waiting state when not live", async () => {
    const { getByText } = render(PitWall);
    await waitFor(() => expect(getByText(/waiting for lmu/i)).toBeTruthy());
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd frontend && npx vitest run src/lib/PitWall.test.ts`
Expected: FAIL — cannot resolve `./PitWall.svelte`.

- [ ] **Step 3: Implement the component**

```svelte
<!-- frontend/src/lib/PitWall.svelte -->
<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import SessionBar from "./SessionBar.svelte";
  import TrackMap from "./TrackMap.svelte";
  import LiveTower from "./LiveTower.svelte";
  import SectorDeltas from "./SectorDeltas.svelte";
  import { LiveField } from "./live";
  import { getStatus } from "./api";

  const live = new LiveField();
  let snapshot = $state(live.snapshot);
  let lapId: number | null = $state(null);
  let statusTimer: ReturnType<typeof setInterval> | undefined;

  const unsub = live.subscribe((v) => { snapshot = v.snapshot; });

  async function pollStatus() {
    try { lapId = (await getStatus()).lap_id ?? null; } catch { /* keep last */ }
  }

  onMount(() => {
    live.start();
    pollStatus();
    statusTimer = setInterval(pollStatus, 2000);
  });
  onDestroy(() => {
    unsub();
    live.stop();
    if (statusTimer) clearInterval(statusTimer);
  });

  const cars = $derived(
    (snapshot?.cars ?? []).map((c) => ({ lapDist: c.lap_dist, cls: c.cls, isPlayer: c.is_player })),
  );
  const isLive = $derived(snapshot?.session.live ?? false);
  const lapLabel = $derived.by(() => {
    const s = snapshot?.session;
    if (!s?.current_lap) return "";
    return s.max_laps ? `Lap ${s.current_lap} / ${s.max_laps}` : `Lap ${s.current_lap}`;
  });
</script>

<div class="pitwall">
  <SessionBar />
  <div class="pit">
    <div class="trackview">
      <div class="tv-h">
        <span class="live-dot" class:on={isLive}>● LIVE</span>
        <span class="lapc">{lapLabel}</span>
      </div>
      {#if isLive}
        <SectorDeltas session={snapshot?.session ?? null} />
        <div class="map"><TrackMap {lapId} {cars} /></div>
      {:else}
        <div class="waiting">Waiting for LMU — get on track to see the field.</div>
      {/if}
    </div>
    <LiveTower snapshot={snapshot ?? null} />
  </div>
</div>

<style>
  .pitwall { height: 100%; display: grid; grid-template-rows: auto 1fr; min-height: 0; }
  .pit { display: grid; grid-template-columns: 1fr 380px; min-height: 0; }
  .trackview { display: flex; flex-direction: column; gap: 10px; padding: 16px 20px; min-height: 0; }
  .tv-h { display: flex; align-items: center; gap: 12px; }
  .live-dot { font-size: 10px; letter-spacing: .1em; font-weight: 700; color: var(--faint); }
  .live-dot.on { color: var(--bad); }
  .lapc { margin-left: auto; font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums; }
  .map { flex: 1; display: grid; place-items: center; min-height: 0; }
  .waiting { flex: 1; display: grid; place-items: center; color: var(--faint); font-size: 13px; text-align: center; }
  @media (max-width: 820px) { .pit { grid-template-columns: 1fr; } }
</style>
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd frontend && npx vitest run src/lib/PitWall.test.ts`
Expected: PASS

- [ ] **Step 5: Wire into `App.svelte`**

Add the import near the other screen imports (`App.svelte` ~line 10):

```svelte
  import PitWall from "./PitWall.svelte";
```

Replace the placeholder line (`App.svelte:215`):

```svelte
    <section class="screen" class:on={screen === "pitwall"}>
      {#if screen === "pitwall"}<PitWall />{/if}
    </section>
```

(The `{#if screen === "pitwall"}` guard means the `LiveField` socket only opens while the tab is active — `onMount`/`onDestroy` start/stop the feed as the user switches screens.)

- [ ] **Step 6: Run the frontend suite (no regressions)**

Run: `cd frontend && npx vitest run`
Expected: PASS (new specs + existing App/TrackMap specs green)

- [ ] **Step 7: Typecheck + build**

Run: `cd frontend && npm run check && npm run build`
Expected: no type errors; build succeeds.

- [ ] **Step 8: Commit**

```bash
git add frontend/src/lib/PitWall.svelte frontend/src/lib/PitWall.test.ts frontend/src/lib/App.svelte
git commit -m "feat(pitwall): compose the live pit wall screen and wire into App"
```

---

## Self-Review

**1. Spec coverage:**
- Live field feed over WebSocket → Tasks 1–2 (`read_field`, `/live`). ✓
- Track map with class-colored car dots, player emphasized → Task 4. ✓
- Live Tower with Race-position ⇄ Car-deltas toggle → Task 5. ✓
- Sector-deltas strip vs session best → Task 6. ✓
- Session header (reuse `SessionBar`) → Task 7. ✓
- WS transport chosen in spec → Task 2 (matches `/coach` pattern). ✓
- `{live:false}` waiting state → Tasks 1/2 (`empty_snapshot`) + Task 7 (waiting UI). ✓
- Deferred (flags, voice, predictive S3, replay) → not built; S3-live called out as a seam in Task 6. ✓
- Invariants (no LLM, UI-agnostic core, read-only MMF) → Global Constraints + pure `read_field`. ✓

**Refinement vs spec (intentional):** the spec framed the sector strip as "current lap, filled as each sector closes." The mapped fields make S1 and S2 current-lap-live (`mCurSector1/2`), but S3 only closes at the line, so v1 ships S1/S2 live + Total from the last lap and leaves live S3 as the already-deferred predictive seam. No new deferral — a precise reading of the same boundary.

**2. Placeholder scan:** No TBD/TODO/"handle edge cases"/"similar to Task N". Every code step is complete. ✓

**3. Type consistency:** `FieldSnapshot`/`SessionState`/`VehicleState` field names match across Python (Task 1), the WS JSON (Task 2 = `dataclasses.asdict`, snake_case), and the TS interfaces (Task 3), and are consumed unchanged in Tasks 4–7. `read_field_snapshot` is imported inside the `/live` handler so the test monkeypatch resolves. `CLASS_COLOR` defined once in `live.ts`, imported by Tasks 4–5. ✓
