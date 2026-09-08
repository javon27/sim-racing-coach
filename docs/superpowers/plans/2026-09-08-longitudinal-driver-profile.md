# Longitudinal Driver Profile Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the agentic coach a persistent, evolving, user-reviewed model of the driver (injected as a concise summary + pullable memories), plus continuously-persisted session setups and agentic cross-session history recall.

**Architecture:** A new SQLite schema version (11) adds `driver_learnings` (reviewed "memories"), a `driver_summary` (the concise always-on tier), and `session_setups` (upserted current setup per session). The server builds a `DriverProfile` (static prefs + active summary) and injects it into `ask_agentic_stream`; richer memory/history/last-setup are reached through new agentic pull tools. Learnings are only ever *proposed* (coach tool / on-demand distill) and go active only after the driver approves them in a "Memories" manager that replaces the old Settings driver-profile section.

**Tech Stack:** Python 3 / FastAPI / pytest (backend); Svelte 5 + TypeScript + vitest (frontend). Model behind the `Model` protocol (`complete(system, messages) -> str`). SQLite via `simcoach/store.py`.

**Spec:** `docs/superpowers/specs/2026-09-08-longitudinal-driver-profile-design.md`

## Global Constraints

- **Two-tier injection:** only a concise always-on summary (static prefs + a maintained `driver_summary`, hard-capped ~60 words) rides every coach turn. Detailed memories are pull-only via `get_driver_memories`. Never inject the full memory list per turn.
- **User-reviewed, never auto-applied:** every LLM-proposed memory and every proposed summary is written `status='pending'` and is NEVER surfaced to the coach (not in the summary, not via the tool) until the driver approves it → `status='active'`. Only `active` reaches the coach.
- **Owned-data invariant intact:** the profile is subjective (opinions/observations), so LLM authorship is acceptable — but only through the pending→active review gate. Telemetry/corner facts remain computed; history/setup tools return computed/stored facts, never raw telemetry.
- **One global profile:** a single logical driver model; `track`/`car` are per-memory TAGS, not separate profiles.
- **Setup capture:** continuously upsert the current setup into `session_setups` on every observed change (latest-wins); the last write is the "final" setup. No end-of-session trigger.
- **Migrations are cumulative + idempotent:** bump `SCHEMA_VERSION` to 11 and add a `if current_version < 11:` block using `executescript`/`_add_column`, tolerant of partial application (mirror the existing v2–v10 steps).
- **Env:** tests run via `./.venv/Scripts/python.exe -m pytest` from the repo root (do NOT `pip install -e .` — it fails on this repo's flat layout). Frontend: `cd frontend && npm test`. Lint: `./.venv/Scripts/python.exe -m ruff check <files>` (line-length 100); only avoid NEW findings — the repo carries pre-existing lint debt.

---

# Phase 1 — Storage + read-path (coach finally knows the driver)

### Task 1: Schema v11 + driver_learnings & driver_summary storage

**Files:**
- Modify: `simcoach/store.py` (`SCHEMA_VERSION`, `_SCHEMA`, the migration block ~151-223, new methods)
- Test: `tests/test_store.py`

**Interfaces:**
- Produces: on `LapStore`:
  - `add_learning(text: str, *, status: str, track: str | None = None, car: str | None = None, source: str, session_id: str | None = None) -> int`
  - `list_learnings(status: str | None = None) -> list[dict]` (each: id, text, status, track, car, source, session_id, created_ts, updated_ts)
  - `set_learning_status(learning_id: int, status: str) -> None`
  - `update_learning(learning_id: int, text: str) -> None`
  - `active_learnings(track: str | None = None, car: str | None = None, limit: int = 50) -> list[dict]` — status='active', general (track/car NULL) + those matching the given track/car, newest first, capped at `limit`
  - `get_driver_summary() -> dict` → `{"text": str, "status": str}` (defaults `{"text": "", "status": "active"}` when unset)
  - `set_driver_summary(text: str, status: str) -> None`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_store.py` (follow the file's existing `LapStore(tmp_path...)` construction):

```python
def test_driver_learnings_crud_and_active_scope(tmp_path):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    gid = s.add_learning("brakes early into slow corners", status="active", source="manual")
    tid = s.add_learning("struggles at Monza T1", status="active", track="Monza", car="GT3", source="coach_tool")
    pid = s.add_learning("maybe overdrives entry", status="pending", source="distill")
    # active_learnings: general + matching tag, excludes pending and non-matching tag
    active_monza = s.active_learnings(track="Monza", car="GT3")
    texts = [l["text"] for l in active_monza]
    assert "brakes early into slow corners" in texts          # general
    assert "struggles at Monza T1" in texts                    # tag match
    assert "maybe overdrives entry" not in texts               # pending excluded
    active_spa = s.active_learnings(track="Spa", car="GT3")
    assert "struggles at Monza T1" not in [l["text"] for l in active_spa]  # non-matching tag excluded
    # status change + update
    s.set_learning_status(pid, "active")
    assert any(l["id"] == pid for l in s.active_learnings())
    s.update_learning(gid, "brakes early, especially downhill")
    assert [l for l in s.list_learnings() if l["id"] == gid][0]["text"] == "brakes early, especially downhill"
    # cap
    assert len(s.active_learnings(limit=1)) == 1

def test_driver_summary_roundtrip_and_default(tmp_path):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    assert s.get_driver_summary() == {"text": "", "status": "active"}
    s.set_driver_summary("Smooth trail-braker, works on late apexes.", "active")
    assert s.get_driver_summary()["text"].startswith("Smooth trail-braker")

def test_schema_migrates_from_v10(tmp_path):
    import sqlite3
    from simcoach.store import LapStore, SCHEMA_VERSION
    db = str(tmp_path / "db.sqlite")
    # simulate a v10 db: create the connection, set user_version=10, close
    s = LapStore(db, str(tmp_path / "telem"))
    s.conn.execute("PRAGMA user_version = 10"); s.conn.commit(); s.conn.close()
    s2 = LapStore(db, str(tmp_path / "telem"))   # should migrate to 11 without error
    assert s2.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    s2.add_learning("x", status="active", source="manual")  # new table usable
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_store.py -k "driver_learnings or driver_summary or migrates_from_v10" -v`
Expected: FAIL — methods/tables don't exist.

- [ ] **Step 3: Add the schema + migration**

In `simcoach/store.py`: bump `SCHEMA_VERSION = 11`. Add the new tables to `_SCHEMA` (so fresh installs get them) AND to the migration block for existing DBs. Define the DDL once as a constant to keep both paths identical (mirroring `_COACH_CHAT_SCHEMA`):

```python
_PROFILE_SCHEMA = """
CREATE TABLE IF NOT EXISTS driver_learnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    status TEXT NOT NULL,
    track TEXT,
    car TEXT,
    source TEXT,
    session_id TEXT,
    created_ts REAL NOT NULL,
    updated_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS driver_summary (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    text TEXT NOT NULL,
    status TEXT NOT NULL,
    updated_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS session_setups (
    session_id TEXT PRIMARY KEY,
    track TEXT NOT NULL,
    car TEXT NOT NULL,
    setup_json TEXT NOT NULL,
    updated_ts REAL NOT NULL
);
"""
```

Append `_PROFILE_SCHEMA` to `_SCHEMA` (like `+ _COACH_CHAT_SCHEMA`). In the migration block, add after the `< 10` step:

```python
            if current_version < 11:
                # v10 -> v11: driver profile memories + maintained summary + per-session
                # setup snapshots. Idempotent creates.
                self.conn.executescript(_PROFILE_SCHEMA)
```

- [ ] **Step 4: Implement the store methods**

Add methods on `LapStore` (use `self._lock` like the other methods, `time.time()` for timestamps — confirm the module's time import). `driver_summary` is a single row id=1 via `INSERT ... ON CONFLICT(id) DO UPDATE`. `active_learnings` filters `status='active' AND (track IS NULL OR track=?) AND (car IS NULL OR car=?)` ordered by `updated_ts DESC` limited by `limit`. Return dict rows (match the file's row->dict helper style).

- [ ] **Step 5: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_store.py -k "driver_learnings or driver_summary or migrates_from_v10" -v`
Expected: PASS

- [ ] **Step 6: Full store suite + ruff**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_store.py -q` then `./.venv/Scripts/python.exe -m ruff check simcoach/store.py`
Expected: PASS; no new lint.

- [ ] **Step 7: Commit**

```bash
git add simcoach/store.py tests/test_store.py
git commit -m "feat(store): schema v11 - driver_learnings, driver_summary, session_setups"
```

---

### Task 2: session_setups store API + continuous capture

**Files:**
- Modify: `simcoach/store.py` (methods), `simcoach/server/app.py` (capture wiring)
- Test: `tests/test_store.py`, `tests/test_server_live.py` (or the appropriate server test file)

**Interfaces:**
- Consumes: the `session_setups` table (created in Task 1).
- Produces:
  - `LapStore.save_session_setup(session_id: str, track: str, car: str, setup: dict) -> None` — upsert (INSERT OR REPLACE / ON CONFLICT), `setup_json = json.dumps(setup)`, `updated_ts = now`.
  - `LapStore.last_setup(track: str, car: str) -> dict | None` — the most recent `session_setups` row for that track/car (max `updated_ts`), returning `{"track","car","setup","session_id","updated_ts"}` with `setup` JSON-decoded, or None.

- [ ] **Step 1: Write the failing store tests**

Add to `tests/test_store.py`:

```python
def test_session_setup_upsert_and_last(tmp_path):
    from simcoach.store import LapStore
    s = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    assert s.last_setup("Monza", "GT3") is None
    s.save_session_setup("sess-1", "Monza", "GT3", {"wing": 5})
    s.save_session_setup("sess-1", "Monza", "GT3", {"wing": 7})   # same session -> upsert, no dup
    s.save_session_setup("sess-2", "Monza", "GT3", {"wing": 8})   # newer session
    rows = s.conn.execute("SELECT COUNT(*) FROM session_setups").fetchone()[0]
    assert rows == 2                                              # upsert kept sess-1 as one row
    last = s.last_setup("Monza", "GT3")
    assert last["setup"] == {"wing": 8} and last["session_id"] == "sess-2"
    assert s.last_setup("Spa", "GT3") is None
```

- [ ] **Step 2: Run to verify fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_store.py -k session_setup -v`
Expected: FAIL — methods missing.

- [ ] **Step 3: Implement the store methods** (upsert on `session_id`; `last_setup` orders by `updated_ts DESC LIMIT 1`).

- [ ] **Step 4: Run to verify pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_store.py -k session_setup -v`
Expected: PASS

- [ ] **Step 5: Wire continuous capture in the server**

In `simcoach/server/app.py`, wherever the current setup is fetched for the live session (the Setup panel / `get_setup` path — grep for where the adjustable setup dict is produced and the current `session_id`/track/car are known), call `store.save_session_setup(session_id, track, car, setup)` whenever a setup is observed, so the store tracks the current setup (latest-wins). Reuse the existing setup-filtering (real adjustable params only). Keep it best-effort (never break a request if capture fails — wrap in try/except, log). If setup fetching is currently only on-demand (fetch-once), add capture at that fetch site now; a periodic refresh is an enhancement noted in the spec but not required for this task — at minimum, persist on every setup fetch.

- [ ] **Step 6: Test the capture wiring**

Add a server test (mirror the app-construction harness in `tests/test_server_live.py` / `tests/test_server_reads.py`, sandboxing paths) that drives the setup-fetch path with a fake REST setup and asserts a `session_setups` row is written for the live session and `store.last_setup(track, car)` returns it. If the capture site is hard to reach in a unit test, at minimum assert `save_session_setup`/`last_setup` are correct (Steps 1-4) and add a focused test of the helper the endpoint calls.

- [ ] **Step 7: Full suite + ruff**

Run: `./.venv/Scripts/python.exe -m pytest -q` then ruff on changed files. `git status --short simcoach/tracks/` must stay empty.
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add simcoach/store.py simcoach/server/app.py tests/
git commit -m "feat(setups): persist current session setup continuously + last_setup query"
```

---

### Task 3: Read path — inject the concise profile into the coach

**Files:**
- Modify: `simcoach/server/app.py` (the WS `/coach` worker), `simcoach/app.py` (`run_repl` → `CoachSession`)
- Test: `tests/test_server_live.py` (or a coach-focused server test), `tests/test_coach.py`

**Interfaces:**
- Consumes: `store.get_driver_summary()`, settings `driver` block, `DriverProfile`, `Coach.ask_agentic_stream(..., driver_profile=)`.
- Produces: the coach's assembled system prompt includes the static prefs + the active `driver_summary`, and does NOT include the full memory list.

- [ ] **Step 1: Write the failing test**

Add a test (server or coach level) that builds the coach path with: a settings `driver` block (e.g. braking_style="trail-braker"), an active summary ("Works on late apexes."), and some active memories, then captures the assembled system prompt (use a FakeModel that records the `system` arg, or assert on `build`/`_augment_system` output). Assert:

```python
# pseudocode — adapt to the real assembly point / FakeModel capture
assert "trail-braker" in system_prompt          # static pref injected
assert "Works on late apexes" in system_prompt  # active summary injected
assert "<a full memory's text>" not in system_prompt   # memories are NOT injected per-turn
```

If the cleanest capture point is `Coach._augment_system`, test it directly: build a `DriverProfile(braking_style="trail-braker", learned_notes=["Works on late apexes."])` and assert `to_prompt_text()` / `_augment_system` contains those and nothing else profile-wise.

- [ ] **Step 2: Run to verify fail**

Run the new test — Expected: FAIL (the server doesn't build/pass a DriverProfile yet).

- [ ] **Step 3: Implement injection**

In the WS `/coach` worker (`app.py`), before calling `ask_agentic_stream(...)`, build:

```python
from simcoach.driver import DriverProfile
d = config.get("driver") or {}
summary = store.get_driver_summary()
notes = [summary["text"]] if summary.get("status") == "active" and summary.get("text") else []
profile = DriverProfile(
    braking_style=d.get("braking_style") or None,
    balance_preference=d.get("balance_preference") or None,
    risk_tolerance=d.get("risk_tolerance") or None,
    learned_notes=notes,
)
```

Pass `driver_profile=profile` to `ask_agentic_stream(...)`. Do NOT pass the full memory list. Do the same in `simcoach/app.py run_repl` → `CoachSession(..., driver_profile=profile)`. `_augment_system` already renders `to_prompt_text()`.

> Implementer note: grep the `/coach` worker for how it reads `config`/settings and constructs the `ask_agentic_stream` call; the exact variable names/locations differ from this snippet — match them. Keep it best-effort (a missing summary → empty notes, never crash).

- [ ] **Step 4: Run to verify pass** — Expected: PASS.

- [ ] **Step 5: Full suite + ruff**, then Commit:

```bash
git add simcoach/server/app.py simcoach/app.py tests/
git commit -m "feat(coach): inject concise driver profile (prefs + active summary) into the coach"
```

---

# Phase 2 — Coach tools + update loop

### Task 4: `record_driver_learning` + `get_driver_memories` tools + prompt

**Files:**
- Modify: `simcoach/coach_tools.py` (TOOLS + methods + dispatch), `simcoach/coach.py` (AGENTIC_SYSTEM_PROMPT)
- Test: `tests/test_coach_tools.py`

**Interfaces:**
- Consumes: `store.add_learning`, `store.active_learnings`; `CoachContext.store/track/car/session_id`.
- Produces:
  - `CoachContext.record_driver_learning(text, track=None, car=None) -> str` → `store.add_learning(text, status="pending", track=track or self.track, car=car or self.car, source="coach_tool", session_id=self.session_id)`; returns a short confirmation ("Saved for your review.").
  - `CoachContext.get_driver_memories(track=None, car=None, topic=None) -> str` → active memories (general + tag-matched) as a deterministic text list; optional case-insensitive `topic` substring filter; never returns pending. "No saved memories yet." when empty.
  - Both registered in `TOOLS` and `dispatch`.

- [ ] **Step 1: Write the failing tests** (use the existing `tests/test_coach_tools.py` context helpers; construct a `CoachContext` with a real `store` (LapStore(tmp)) so the tools hit the DB):

```python
def test_record_driver_learning_writes_pending(tmp_path, synthetic_lap):
    ctx, store, _ = _ctx(tmp_path, synthetic_lap)   # _ctx builds CoachContext with a store
    out = ctx.record_driver_learning("consistently late on throttle")
    assert "review" in out.lower()
    rows = store.list_learnings(status="pending")
    assert any(r["text"] == "consistently late on throttle" and r["source"] == "coach_tool" for r in rows)
    # not active -> not returned by get_driver_memories
    assert "consistently late on throttle" not in ctx.get_driver_memories()

def test_get_driver_memories_active_and_topic(tmp_path, synthetic_lap):
    ctx, store, _ = _ctx(tmp_path, synthetic_lap)
    store.add_learning("brakes early in the wet", status="active", source="manual")
    store.add_learning("good tyre management", status="active", source="manual")
    out = ctx.get_driver_memories()
    assert "brakes early in the wet" in out and "good tyre management" in out
    assert "tyre" in ctx.get_driver_memories(topic="tyre")
    assert "brakes early" not in ctx.get_driver_memories(topic="tyre")   # topic filters
    assert "no saved memories" in ctx.get_driver_memories(topic="nonexistent").lower()
```

(If `_ctx` doesn't already pass a real store, use the store it creates — it does: `_ctx` builds `LapStore` then `CoachContext(store, ...)`.)

- [ ] **Step 2: Run to verify fail.** `./.venv/Scripts/python.exe -m pytest tests/test_coach_tools.py -k "driver_learning or driver_memories" -v` → FAIL.

- [ ] **Step 3: Implement the two methods**, add their `TOOLS` specs (names `record_driver_learning` with `text` required + optional `track`/`car`; `get_driver_memories` with optional `track`/`car`/`topic`), and wire both into the `dispatch` dict.

- [ ] **Step 4: Run to verify pass.**

- [ ] **Step 5: Prompt guidance** in `AGENTIC_SYSTEM_PROMPT` (`coach.py`): call `record_driver_learning` ONLY for durable, cross-lap tendencies worth remembering (not per-lap noise), and tell the driver it's saved for review; call `get_driver_memories` when tailoring advice would benefit from the driver's history beyond the summary already in context.

- [ ] **Step 6: Full coach-tools suite + ruff.** Commit:

```bash
git add simcoach/coach_tools.py simcoach/coach.py tests/test_coach_tools.py
git commit -m "feat(coach): record_driver_learning + get_driver_memories tools + prompt"
```

---

### Task 5: Cross-session history tools + `get_last_setup`

**Files:**
- Modify: `simcoach/coach_tools.py` (TOOLS + methods + dispatch), `simcoach/coach.py` (prompt), `simcoach/store.py` (thin read helpers only if a query isn't already exposed)
- Test: `tests/test_coach_tools.py`, `tests/test_store.py` (if new helpers added)

**Interfaces:**
- Consumes: `store.best_lap(track,car)`, `store.recent_laps`, the trend/best-per-session series (grep `store.py` for the trend query the chart uses — reuse it; add a helper only if none is callable), `store.last_setup`.
- Produces `CoachContext` methods + tools:
  - `get_personal_best(track=None, car=None) -> str` — all-time fastest for the combo + when; contrast the session best if known.
  - `get_progress(track=None, car=None) -> str` — best-lap-per-session progression summary.
  - `list_past_sessions(track=None, car=None, limit=5) -> str` — recent past sessions (date, type, result if any, best lap).
  - `get_last_setup(track=None, car=None) -> str` — `store.last_setup(...)` rendered (or "no stored setup for this combo yet").

- [ ] **Step 1: Write failing tests** — seed multi-session laps (and a `session_setups` row) in a real store, build a `CoachContext`, and assert each tool's text: all-time best reflects the fastest across sessions; progress lists sessions in order; past-sessions lists them; `get_last_setup` returns the stored setup and a clean message when absent. Register-in-dispatch assertions like the other tool tests.

- [ ] **Step 2: Run to verify fail.**

- [ ] **Step 3: Implement** the four methods (reuse existing store queries; add a `progress`/`best-per-session` store helper only if the chart's query isn't already a callable method — grep first), their `TOOLS` specs (optional `track`/`car`; `list_past_sessions` optional `limit`), and dispatch wiring.

- [ ] **Step 4: Run to verify pass.**

- [ ] **Step 5: Prompt guidance** in `AGENTIC_SYSTEM_PROMPT`: use these for historical / "all-time" / progress / "what setup did we run here last time" questions; default to the current track/car.

- [ ] **Step 6: Full suite + ruff.** Commit:

```bash
git add simcoach/coach_tools.py simcoach/coach.py simcoach/store.py tests/
git commit -m "feat(coach): cross-session history tools + get_last_setup"
```

---

### Task 6: Profile endpoints + on-demand distill

**Files:**
- Modify: `simcoach/server/app.py`
- Test: `tests/test_server_tracks.py` (reuse its sandboxed harness) or a new `tests/test_server_profile.py`

**Interfaces:**
- Produces (all reuse `store` + settings):
  - `GET /profile` → `{prefs:{braking_style,balance_preference,risk_tolerance}, summary:{text,status}, memories:{active:[...], pending:[...]}}`
  - `PUT /profile` → save prefs into the settings `driver` block (reuse the existing settings-write path).
  - `PUT /profile/summary` → `set_driver_summary(text, status)` (status 'active' when the driver saves/approves).
  - `POST /profile/memories` → manual add (`add_learning(status="active", source="manual", ...)`).
  - `PUT /profile/memories/{id}` → edit text and/or set status (approve pending → active, or retire).
  - `POST /profile/distill` (body: `{session_id}`) → assemble a deterministic digest (per-corner deltas vs best, recurring weak corners, chat history, race arc — reuse existing analysis/store) + call `coach.model.complete(...)` to PROPOSE candidate memories + an updated summary. Write proposals as `status='pending'` (memories) and a pending summary; **never** write anything `active`. Return the proposals.

- [ ] **Step 1: Write failing tests** (sandboxed harness with a `FakeModel`): GET returns the shape; PUT summary/memory round-trip; approve pending→active via PUT; **distill writes only pending** (assert no new active rows and the summary stays whatever it was) and returns proposals. Guard: distill with no such session → 404 or empty proposals (pick one; test it).

- [ ] **Step 2: Run to verify fail.**

- [ ] **Step 3: Implement the endpoints** (reuse the settings read/write helpers for prefs; `store` methods for the rest; the distill mirrors the track `draft-notes` pattern — assemble digest, call `model.complete`, parse JSON defensively → 502 on invalid, write pending only).

- [ ] **Step 4: Run to verify pass.**

- [ ] **Step 5: Full suite + ruff** (+ `git status --short simcoach/tracks/` empty). Commit:

```bash
git add simcoach/server/app.py tests/
git commit -m "feat(server): /profile endpoints + on-demand memory distill (proposes only)"
```

---

# Phase 3 — Memories manager UI

### Task 7: API client methods

**Files:** Modify `frontend/src/lib/api.ts`; Test: covered via component tests (or `api.test.ts` if the repo tests it).

**Interfaces:** `getProfile()`, `putProfilePrefs(prefs)`, `putProfileSummary(text, status)`, `addMemory(m)`, `updateMemory(id, patch)`, `distillProfile(sessionId)`; types `DriverMemory`, `DriverProfileData`.

- [ ] **Step 1:** Add the methods matching the existing `request<T>()` helper pattern (grep `api.ts`; reuse `JSON_HEADERS` for POST/PUT as Task-6-of-the-track-feature did). Map to the Task 6 endpoints/bodies exactly.
- [ ] **Step 2:** `cd frontend && npm test` — no regressions; new exports compile.
- [ ] **Step 3:** Commit `git commit -m "feat(ui): api client for driver profile / memories / distill"`.

### Task 8: "Memories" manager in Settings (remove old section)

**Files:** Modify `frontend/src/lib/SettingsScreen.svelte`; Test: `frontend/src/lib/SettingsScreen.test.ts`.

- [ ] **Step 1: Write failing component tests** (match the existing `SettingsScreen.test.ts` + `vi.spyOn(api, ...)` idioms): mock `getProfile` returning prefs + summary + active + pending memories; assert the Memories manager renders active memories and the pending-review items; assert approving a pending memory calls `updateMemory(id, {status:"active"})`; assert the old standalone "Driver profile" `<h3>` block is gone (the prefs now live inside the Memories section).
- [ ] **Step 2: Run to verify fail.**
- [ ] **Step 3: Implement** — REMOVE the existing `<h3>Driver profile</h3>` block (~190-206) and build the unified **Driver Memories** section (Svelte 5 runes, matching the file's style): static prefs (relocated, same `settings.driver` bind + save path), the editable/approvable **summary**, the **active memories** list (edit/delete=retire/ + Add), the **pending review** queue (approve/edit/reject), a **"Review session learnings"** action calling `distillProfile(sessionId)`, and the active-count-vs-cap hint. Reuse the app's primary-button class.
- [ ] **Step 4: Run to verify pass; full `npm test`.**
- [ ] **Step 5:** Commit `git commit -m "feat(ui): Driver Memories manager in Settings (replaces driver-profile section)"`.

### Task 9: Final verification

- [ ] **Step 1:** Backend `./.venv/Scripts/python.exe -m pytest -q` → green; `git status --short simcoach/tracks/` empty.
- [ ] **Step 2:** `./.venv/Scripts/python.exe -m ruff check simcoach/ tests/` → no NEW findings vs baseline.
- [ ] **Step 3:** `cd frontend && npm test` → green.
- [ ] **Step 4: Manual smoke** (rig/dev): coach answers a "what did we run here last time?" (get_last_setup) and a "how's my pace here progressing?" (get_progress); a coach-proposed learning lands in Settings → Memories pending; approving it makes the coach reference it next session; the concise summary rides prompts while full memories are pulled only when relevant (verify a real transcript — LLM behavior isn't unit-testable).

---

## Self-review notes

- **Spec coverage:** §1 storage → Tasks 1-2; §2 read-path → Task 3; §3 update loop (record tool + distill + review) → Tasks 4 & 6; §3b history + get_last_setup → Tasks 2 & 5; §3c get_driver_memories → Task 4; §4 Memories UI (fold+remove old section) → Tasks 7-8; §5 testing folded per task; §6 boundaries → Global Constraints.
- **Deferred (not tasked, by design):** Deep-dive mode itself (distill is its seam), auto-apply, per-car/track separate profiles, voice/Pit-Wall, i18n.
- **Type/name consistency:** `add_learning`/`active_learnings`/`get_driver_summary`/`set_driver_summary`/`save_session_setup`/`last_setup` used identically across Tasks 1-6; tool names `record_driver_learning`/`get_driver_memories`/`get_personal_best`/`get_progress`/`list_past_sessions`/`get_last_setup` consistent Tasks 4-5; endpoint shapes match between Task 6 and the Task 7 client.
