# v1 Phase 3 — Tauri shell + Svelte scaffold — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Tauri desktop window that launches the Phase-1 FastAPI sidecar on startup (and kills it on exit), hosting a Svelte SPA whose **session bar** and **lap list** are wired to the real HTTP API. Chat + corner/setup panels are Phase 4; Sessions/Settings screens are Phase 5.

**Architecture:** `frontend/` is a Vite + Svelte 5 SPA with a typed API client hitting `http://127.0.0.1:8765`. `src-tauri/` is a Tauri v2 (Rust) shell that (a) spawns `python -m simcoach.server` as a managed child on startup and terminates it on window close, (b) serves the built SPA. The Python core + server are unchanged except a small additive `GET /status` endpoint and allowing the Tauri webview origin on `WS /coach`.

**Tech Stack:** Node 24 / npm 11, Vite 6, Svelte 5 (runes), vitest + @testing-library/svelte + jsdom; Rust 1.86, Tauri v2 (`tauri`, `tauri-cli`). Backend: existing Python (`.venv`), pytest.

**Spec:** `docs/superpowers/specs/2026-08-29-v1-standalone-ui-design.md` (Build phase 3) + the visual source of truth `docs/design/v1-ui-prototype.html`. Builds on Phase 1 (`docs/superpowers/plans/2026-09-02-v1-phase1-server-config.md`) and Phase 2 (`docs/superpowers/plans/2026-09-02-v1-phase2-streaming-coach.md`).

## Global Constraints

- **Windows-first.** Tauri uses the system WebView2 (present on Win 11). The sidecar is `python -m simcoach.server` via the project `.venv`.
- **Server binds `127.0.0.1:8765` only** (unchanged). The Svelte app is a thin client; NO coach/analysis logic in the frontend. Corner identity comes from the API, never inferred in JS.
- **Two runtime origins for the frontend, both must reach the API** (Task 2 lands the server-side fix for BOTH transports):
  1. `vite dev` in a browser → origin `http://localhost:5173`.
  2. Tauri production webview → origin `http://tauri.localhost` (Windows; hostname `tauri.localhost`) / `tauri://localhost` (hostname `localhost`).
  - **HTTP:** the server has NO CORS middleware, so even though `fetch` sends an allowed `Host: 127.0.0.1:8765`, the browser/WebView2 **blocks the JS from reading the response** (no `Access-Control-Allow-Origin`) from BOTH origins. `api.ts` uses an absolute base, so a Vite proxy would be bypassed and `plugin-http` would break vitest's fetch mock — the right fix is a server `CORSMiddleware` with an explicit origin allowlist. **This is the phase's make-or-break integration point: mocked-fetch vitest + `npm run build` go green without it, but the live window loads no data.**
  - **WS `/coach`:** `_bad_origin` already ACCEPTS the dev origin (`localhost` ∈ `ALLOWED_HOSTS`) and `tauri://localhost` (hostname `localhost`). Only the Windows Tauri origin **`tauri.localhost`** is rejected today and must be added. (Correcting the earlier draft's claim that both were rejected — only `tauri.localhost` is.)
- **Use official scaffolding, don't hand-write boilerplate:** `npm create vite@latest frontend -- --template svelte-ts`, then Tauri v2 init. Pin versions in the lockfiles the scaffolds produce; commit them.
- **Verifiable here vs `[LIVE]`:** `npm run build`, `npm test` (vitest), and `cargo build` (compile-check) run in this environment. The *interactive* run (window opens, sidecar spawns, live data flows) is a `[LIVE]` validation on the user's box — never faked green.
- **Design fidelity:** port the prototype's CSS tokens + component styles (Chakra Petch / IBM Plex, the theme-var system, themed scrollbars) largely as-is. Keep the light/dark/system theming.
- **Backend green bar stays 237** (`.venv/Scripts/python.exe -m pytest -q`); the one backend change (Task 1) adds tests.
- **Frontend commits keep `frontend/` self-contained**; `.gitignore` `frontend/node_modules`, `frontend/dist`, `src-tauri/target`.

---

### Task 1: Backend — `GET /status` endpoint (+ tests)

**Files:**
- Modify: `simcoach/server/app.py`
- Test: `tests/test_server_status.py`

**Interfaces:**
- Produces `GET /status` → `{"track": <str|None>, "car": <str|None>, "on_track": <bool>, "has_map": <bool>, "lmu_connected": <bool>}`. `track`/`car` are read **live per request** via `simcoach.lmu.smm.read_session_track_car()` (returns a `(track, car)` tuple or None; NOT on `LmuRest`), so a mid-session track change is reflected; degrade to None on any error (never 500). `has_map`/`lmu_connected` reflect **boot state** (`app.state.cmap`/`rest`). (This boot-vs-live split is fine for the session bar; a later live-WS phase unifies it.)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_server_status.py
from fastapi.testclient import TestClient
from simcoach.server.app import create_app
from simcoach.store import LapStore
from simcoach.coach import Coach
from simcoach.model.fake import FakeModel


def _tc(tmp_path):
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    app = create_app(store, Coach(FakeModel(), "simcoach/knowledge"))  # cmap/rest None
    return TestClient(app, base_url="http://127.0.0.1")


def test_status_offline_shape(tmp_path):
    r = _tc(tmp_path).get("/status")
    assert r.status_code == 200
    b = r.json()
    assert set(b) == {"track", "car", "on_track", "has_map", "lmu_connected"}
    assert b["has_map"] is False and b["lmu_connected"] is False and b["on_track"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_server_status.py -v`
Expected: FAIL — 404 (no route).

- [ ] **Step 3: Write minimal implementation**

Add to `create_app` (read the real `rest` API — `read_session_track_car` may live on `smm`, not `rest`; wire whichever the server actually holds; degrade to None):

```python
    @app.get("/status")
    def status():
        track = car = None
        try:
            from ..lmu.smm import read_session_track_car
            tc = read_session_track_car()
            if tc:
                track, car = tc
        except Exception:
            track = car = None
        return {
            "track": track, "car": car,
            "on_track": False,               # no live capture in the server yet
            "has_map": app.state.cmap is not None,
            "lmu_connected": app.state.rest is not None,
        }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_server_status.py -v` then FULL suite `-q` → 237+1, no regressions.

- [ ] **Step 5: Commit**

```bash
git add simcoach/server/app.py tests/test_server_status.py
git commit -m "feat(server): GET /status for the session bar (boot-resolved track/car)"
```

---

### Task 2: Backend — accept the frontend origins (HTTP CORS + WS origin)

**This resolves the CRITICAL reachability gap for BOTH transports.** Without the CORS half, the entire frontend builds + unit-passes but loads no live data.

**Files:**
- Modify: `simcoach/server/app.py`
- Test: `tests/test_server_cors.py` (new), `tests/test_server_ws.py` (append)

**Interfaces:**
- A module-level `FRONTEND_ORIGINS = ["http://localhost:5173", "http://tauri.localhost"]` (explicit — never `"*"`, which with credentials is unsafe and needless here).
- **HTTP:** add Starlette `CORSMiddleware` to `create_app` with `allow_origins=FRONTEND_ORIGINS`, `allow_methods=["*"]`, `allow_headers=["*"]`. This is orthogonal to the Host guard (Host stays `127.0.0.1:8765`, still enforced) — it only adds the `Access-Control-Allow-Origin` response header so the browser lets the SPA read the body.
- **WS:** `ALLOWED_ORIGIN_HOSTS = ALLOWED_HOSTS | {"tauri.localhost"}`; `_bad_origin` tests `host not in ALLOWED_ORIGIN_HOSTS`. (The dev origin `localhost` and `tauri://localhost` already pass via `ALLOWED_HOSTS`; only `tauri.localhost` is newly needed.)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_server_cors.py
from fastapi.testclient import TestClient
from simcoach.server.app import create_app
from simcoach.store import LapStore
from simcoach.coach import Coach
from simcoach.model.fake import FakeModel

def _tc(tmp_path):
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    return TestClient(create_app(store, Coach(FakeModel(), "simcoach/knowledge")),
                      base_url="http://127.0.0.1")

def test_cors_allows_dev_and_tauri_origins(tmp_path):
    c = _tc(tmp_path)
    for origin in ("http://localhost:5173", "http://tauri.localhost"):
        r = c.get("/health", headers={"host": "127.0.0.1", "origin": origin})
        assert r.status_code == 200
        assert r.headers.get("access-control-allow-origin") == origin

def test_cors_omits_header_for_unknown_origin(tmp_path):
    r = _tc(tmp_path).get("/health", headers={"host": "127.0.0.1", "origin": "http://evil.com"})
    assert r.status_code == 200  # host-guard passed (127.0.0.1); CORS just withholds the header
    assert r.headers.get("access-control-allow-origin") in (None, "")
```
```python
# tests/test_server_ws.py  (append)
def test_ws_coach_allows_tauri_origin(tmp_path):
    client, _ = _client(tmp_path)   # reuse the module's FakeModel-backed helper
    with client.websocket_connect(
            "/coach", headers={"host": "127.0.0.1", "origin": "http://tauri.localhost"}) as ws:
        ws.send_json({"question": "hi"})
        seen = []
        while True:
            ev = ws.receive_json(); seen.append(ev["type"])
            if ev["type"] in ("done", "error"): break
    assert seen[-1] == "done"
```

- [ ] **Step 2: Run → fail** (no CORS header; `tauri.localhost` WS rejected).

- [ ] **Step 3: Implement:** `from starlette.middleware.cors import CORSMiddleware`; `app.add_middleware(CORSMiddleware, allow_origins=FRONTEND_ORIGINS, allow_methods=["*"], allow_headers=["*"])`. Add `ALLOWED_ORIGIN_HOSTS` and update `_bad_origin`. Keep the existing hostile-origin + host-guard tests green. NOTE: `add_middleware` runs middleware in reverse-add order — CORS must run so its headers survive; verify the existing host-guard 403 for a bad Host still fires (CORS and host-guard are independent).

- [ ] **Step 4: Run** `tests/test_server_cors.py tests/test_server_ws.py -v` then FULL suite `-q` → green.

- [ ] **Step 5: Commit** `feat(server): CORS for dev+Tauri origins + WS tauri.localhost origin`.

---

### Task 3: Frontend scaffold — Vite + Svelte + vitest + typed API client

**Files:**
- Create: `frontend/` (Vite scaffold), `frontend/src/lib/api.ts`, `frontend/src/lib/api.test.ts`
- Modify: `.gitignore` (add `frontend/node_modules`, `frontend/dist`, `src-tauri/target`)

**Interfaces:**
- Produces `api.ts` — a typed client with a configurable base (`import.meta.env.VITE_API_BASE ?? "http://127.0.0.1:8765"`): `getHealth()`, `getStatus()`, `getSessions()`, `getSessionLaps(sid)`, `getLapAnalysis(id)`, `getChats()`, `getSettings()`. Each `fetch` + `res.json()` with a typed return + a thrown error on non-2xx. Types mirror the API payloads (SessionRow, LapRow, etc.).

- [ ] **Step 1: Scaffold + write the failing test**

```bash
cd frontend-parent-is-repo-root
npm create vite@latest frontend -- --template svelte-ts
cd frontend && npm install
npm install -D vitest @testing-library/svelte @testing-library/jest-dom jsdom
```
Add `"test": "vitest run"` to `frontend/package.json` scripts and a `vitest` config with `test.environment: "jsdom"` AND **`resolve.conditions: ["browser"]`** — without the browser condition, Svelte 5 component imports resolve to the SSR build and `render()` in Tasks 4–6 throws. Then write `src/lib/api.test.ts` that mocks `fetch` (vitest `vi.stubGlobal`) and asserts `getSessions()` calls `GET {base}/sessions` and returns the parsed array, and that a 500 response throws.

**Verify-against-docs at execution (versions drift):** confirm `npm create vite@latest ... --template svelte-ts` actually scaffolds **Svelte 5** (not 6+) and install a `@testing-library/svelte` major compatible with the scaffolded Svelte; if the template moved on, adjust the runes syntax in Tasks 4–6 and the testing-library version accordingly. (Same version-recheck applies to Tauri v2 in Task 7.)

- [ ] **Step 2: Run → fail** (`npm test`) — `api.ts` not written.

- [ ] **Step 3: Implement `src/lib/api.ts`** with the typed wrappers + a `request()` helper (`throw new Error(...)` on `!res.ok`).

- [ ] **Step 4: Run** `npm test` (green) and `npm run build` (SPA builds).

- [ ] **Step 5: Commit**

```bash
git add frontend/ .gitignore
git commit -m "feat(ui): Vite+Svelte scaffold + typed API client (vitest)"
```
(Ensure `frontend/node_modules` is git-ignored BEFORE `git add`.)

---

### Task 4: Frontend — app shell (nav rail + layout) from the prototype

**Files:**
- Create: `frontend/src/lib/App.svelte` (or replace the scaffold's `App.svelte`), `frontend/src/app.css` (ported tokens), `frontend/src/lib/theme.ts`
- Test: `frontend/src/lib/App.test.ts`

**Interfaces:**
- Produces the shell: a 60px nav rail (Coach / Sessions / Pit Wall icons + theme toggle + Settings) and a main content grid, matching `docs/design/v1-ui-prototype.html`'s `.shell`/`.nav` markup + CSS tokens (`--bg`,`--surface`,`--accent`,… light/dark/system). `theme.ts` = System→Light→Dark toggle writing `data-theme` on `document.documentElement` (port the prototype's logic).

- [ ] **Step 1: Write the failing test** — `App.test.ts` renders `<App>` and asserts the three nav items + a theme toggle exist (`@testing-library/svelte`).
- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement** the shell components + port the token CSS into `app.css`. Screens are placeholder `<section>`s for now (Coach screen hosts Tasks 5–6; others are stubs).
- [ ] **Step 4: Run** `npm test` + `npm run build` green.
- [ ] **Step 5: Commit** `feat(ui): app shell (nav rail + theming) ported from prototype`.

---

### Task 5: Frontend — session bar wired to `GET /status` (+ `/settings` for tier/units)

**Files:**
- Create: `frontend/src/lib/SessionBar.svelte`, `frontend/src/lib/SessionBar.test.ts`

**Interfaces:**
- Consumes `api.getStatus()` (track/car) + `api.getSettings()` (**`units` only** — `/settings` has no `tier` field; tier is derived from the live setup via `legal_adjustments`, not exposed yet, so it's NOT shown here). Produces a `<SessionBar>` showing track/car (raw) + units; fuel / energy / tier render as **"—" placeholders** (live fuel/energy arrive with `WS /telemetry`, deferred; tier with a future setup endpoint). On a failed fetch, show a "server not reachable" state (not a crash).

- [ ] **Step 1: Write the failing test** — mock `api.getStatus` to return `{track:"Le Mans", car:"Corvette Z06", ...}`; assert the bar renders the track + car; mock a rejection → assert the unreachable state.
- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement** `SessionBar.svelte` (Svelte 5 runes `$state`/`$effect`, `onMount` fetch), styled per the prototype `.bar`.
- [ ] **Step 4: Run** `npm test` + build green.
- [ ] **Step 5: Commit** `feat(ui): session bar wired to /status`.

---

### Task 6: Frontend — lap list rail wired to `/sessions` + `/sessions/{id}/laps`

**Files:**
- Create: `frontend/src/lib/LapList.svelte`, `frontend/src/lib/LapList.test.ts`

**Interfaces:**
- Consumes `api.getSessions()` then `api.getSessionLaps(sid)` for the most-recent session. Produces the laps rail (per the prototype `.rail`/`.laps`): each lap row shows number/time/validity; selecting a lap emits a `select` event carrying `{lap_id}` (Phase 4's chat/corner panel will consume it). Empty store → an "no laps yet" empty state.

- [ ] **Step 1: Write the failing test** — mock `getSessions`→`[{session_id:"s1",track,car,laps:2,best:...}]` and `getSessionLaps`→`[{id:9,lap_time:216.0,valid:1,ts:...}]`; assert both rows render and clicking one dispatches `select` with `{lap_id:9}`.
- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement** `LapList.svelte`, wire it into the Coach screen beside the session bar.
- [ ] **Step 4: Run** `npm test` + build green.
- [ ] **Step 5: Commit** `feat(ui): lap list rail wired to /sessions`.

---

### Task 7: Tauri v2 shell + Python sidecar lifecycle

**Files:**
- Create: `src-tauri/` (Tauri v2 init), `src-tauri/tauri.conf.json`, `src-tauri/src/main.rs` (sidecar spawn/kill)
- Modify: root `.gitignore` (`src-tauri/target`), `frontend` build wiring

**Interfaces:**
- Produces a Tauri app that: builds the Vite frontend as its `frontendDist`; on `setup`, spawns the Python sidecar (`.venv/Scripts/python.exe -m simcoach.server`, cwd = repo root) as a managed `Child`; on `WindowEvent::Destroyed`/exit, kills it. Dev mode uses `beforeDevCommand: npm run dev` + `devUrl: http://localhost:5173`; the sidecar is spawned the same way (so dev and prod both have the API).

**Approach (don't hand-write boilerplate):** run the official init — `cargo install tauri-cli --version '^2'` then `cargo tauri init` (pointing `frontendDist` at `../frontend/dist`, `devUrl` at the vite dev server). Then add ONLY the sidecar spawn/teardown to `main.rs`.

- [ ] **Step 1: Scaffold** Tauri v2 into `src-tauri/` via `cargo tauri init` with the answers above. Commit the generated scaffold first (compile-checkpoint) — `cargo build --manifest-path src-tauri/Cargo.toml` must succeed.
- [ ] **Step 2: Add the sidecar** (raw `std::process` is the right call here — we spawn the system `.venv` python, not a bundled `externalBin`, so the shell plugin's capabilities/permissions are needless):
  - **Resolve paths absolutely** (relative paths drift — under `cargo tauri dev` the cwd is NOT guaranteed to be the repo root): repo root = `Path::new(env!("CARGO_MANIFEST_DIR")).join("..")` (the `src-tauri` dir's parent, resolved at compile time); python = `std::env::var("SIMCOACH_PYTHON")` else `<repo_root>/.venv/Scripts/python.exe`. Spawn with `.current_dir(<repo_root>)`.
  - **Kill-on-parent-death, crash-safe (the important part):** `Child` does NOT kill on drop and `WindowEvent::Destroyed` does NOT fire on a panic/hard-kill, so a naive `.kill()` on Destroyed leaks an orphaned python holding port 8765 → the next launch can't bind. Use a **Windows Job Object** with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` (assign the child to it; the OS kills the child when the parent process object closes, even on crash) — via the `windows`/`win32job` crate. If a Job Object is too heavy, the fallback is **stale-server detection at startup**: probe `GET http://127.0.0.1:8765/health`; if it answers, reuse it (don't spawn a second); if the port is held but unhealthy, surface an error. Additionally handle `RunEvent::ExitRequested`/`Exit` (not only `Destroyed`) to `.kill()` on normal quit. Store the `Child`/job handle in Tauri managed state.
- [ ] **Step 3: Compile-check** `cargo build --manifest-path src-tauri/Cargo.toml` → succeeds (no interactive run here).
- [ ] **Step 4: `[LIVE]` (user's box):** `cargo tauri dev` — (1) window opens; (2) sidecar starts — `curl 127.0.0.1:8765/health` answers while it runs; (3) the session bar + lap list **populate with real data** (this is the true test that CORS + `/status` + the wiring all work end-to-end — the vitest green did NOT prove this); (4) closing the window kills the sidecar — no orphaned python; (5) **crash test:** kill the app via Task Manager, confirm no python is left holding 8765, and that a relaunch still binds. Record results in HANDOFF; do NOT fake it.
- [ ] **Step 5: Commit** `feat(shell): Tauri v2 window + managed Python sidecar (spawn/teardown)`.

---

## Non-goals for Phase 3

Coach chat UI + streaming client + corner/setup panels (Phase 4); Sessions screen + trends + Settings screen (Phase 5); `WS /telemetry` live session-bar values (deferred); Pit Wall; packaging/installer (Phase 6). The frontend↔`WS /coach` streaming *client* is Phase 4 — Task 2 only unblocks its origin server-side.

## Self-Review

**Spec coverage (Phase 3):** window (Task 7) ✓; sidecar launch/teardown (Task 7) ✓; session bar wired to API (Tasks 1, 5) ✓; lap list wired to API (Task 6) ✓; Svelte scaffold (Task 3–4) ✓. Deferred items scoped out explicitly ✓.

**Integration landmines addressed:** Tauri/dev origin vs WS guard (Task 2); missing current-track/car source (Task 1 `GET /status`); dev-vs-prod API reachability (Vite proxy / plugin-http noted — see risk below); sidecar orphan on crash (Task 7 kill-on-destroy).

**Resolved (was the plan reviewer's CRITICAL):** HTTP reachability from both origins is now a real task — **Task 2 lands server `CORSMiddleware`** with an explicit `{localhost:5173, tauri.localhost}` allowlist (not a Vite proxy — the absolute API base bypasses it; not plugin-http — it breaks vitest's fetch mock). `api.ts` keeps its absolute base. **Green-here ≠ works-live:** the frontend's mocked-fetch vitest + build pass without CORS; only Task 7 Step 4 (3) proves real data flows — that's the true integration gate.

## Plan-review fold-in (2026-09-02, critic + red-team, one pass)

- **CRITICAL:** added the HTTP CORS task (Task 2, alongside the WS origin) — the frontend was unreachable from both origins; the plan had deferred it to "decide in review".
- **MAJOR:** Task 7 crash-orphan handling (Windows Job Object `KILL_ON_JOB_CLOSE` / stale-port `/health` probe / handle `Exit` not just `Destroyed`) + absolute path resolution (`CARGO_MANIFEST_DIR/..`, `SIMCOACH_PYTHON`); corrected the false "WS rejects the dev origin" premise (only `tauri.localhost` needs adding); noted the Svelte-5 vitest `resolve.conditions:['browser']` requirement.
- **Minor:** `/status` is a live SMM read (`smm.read_session_track_car`, not `rest`) with boot-state `has_map`/`lmu_connected` — prose corrected; dropped the nonexistent `tier` field from the session bar (units only); affirmed raw `std::process` over the shell plugin.
- **Verify-at-execution (version drift):** confirm the scaffold gives Svelte 5 (not 6) + a compatible testing-library, and Tauri v2 sidecar API — against current docs.

## Execution notes for the orchestrator

- Order: 1→2 (backend, verifiable via pytest) then 3→4→5→6 (frontend, verifiable via vitest/build) then 7 (Tauri, compile-check here + `[LIVE]` on the user's box).
- Review calibration: Task 2 (WS origin — security) + Task 7 (sidecar lifecycle — orphan processes, path handling) are the risky ones → review + a red-team focused on the origin allowance and the sidecar teardown. Tasks 1, 3–6 → one review each (or batch the frontend 3–6).
- Fresh executor per task, plan+spec only. Frontend executors need `npm`; Task 7 needs `cargo` + the Tauri CLI install.
- Much of Phase 3's value (the running window) is `[LIVE]` on the user's machine — set expectations: subagents deliver building, unit-tested, compile-checked code; the interactive shakedown is the user's.
