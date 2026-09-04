# LMU result-log ingest — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Backfill the user's session + lap history from LMU's `.xml` result logs (no live capture), feeding the same store the UI reads. Pure Python, fully offline-verifiable (real XMLs + pytest).

**Design is spec'd** in `docs/superpowers/specs/2026-08-29-v1-standalone-ui-design.md` → "References & external inspiration" (verified schema, player-ID chain, path detection). Read it first.

**Tech:** Python stdlib `xml.etree.ElementTree`, existing `store`/`config`. Tests: `.venv/Scripts/python.exe -m pytest -q`. Backend green bar = 241.

## Global Constraints

- **Player-scoped, ALIAS SET:** `isPlayer` is 43/43 in MP files → useless. Seed the player name from `<PlayerFile>` → `UserData/player/{PlayerFile}.JSON` `"Player Name"`. BUT real data (this user) shows the profile name ("Jayson G") differs from older `<Driver><Name>` values ("Jayson Gallardo") — display names change over time, and BOTH appear across the corpus. So match against a **set of accepted names** = profile name ∪ config `lmu_player_names` (Settings-managed list of past/alias names). A driver matches if its `<Name>` (case/space-insensitive) is in the set. `Task 5` resolves this set; `parse_result` accepts a str OR an iterable of names.
- **Sectors are `<Lap>` ATTRIBUTES** (`s1`/`s2`/`s3`), NOT `<Sector>` children (those are stream best-sector announcements). Already implemented correctly in Task 3.
- **Summary only, no telemetry:** backfilled laps have lap time + sectors, NO `.npz`. Corner analysis stays on live captures.
- **Cross-platform path detection** for the LMU `UserData` dir (Steam library on any drive, Epic, standalone) + `SIMCOACH_LMU_DIR` override.
- **Idempotent:** re-ingesting a file is a no-op (dedup on path + mtime).
- **DECISION (default, revisit if user objects):** backfilled laps go into the EXISTING `laps` table with a new `source` column (`'live'`|`'result'`), so `/sessions` + the lap list show them for free; the analysis endpoint degrades gracefully (no `.npz` → 404, already handled). Alternative (separate `result_*` tables) rejected — would duplicate the sessions/laps query surface.
- TDD; use trimmed real-XML fixtures committed under `tests/fixtures/lmu_results/`.

---

### Task 1: LMU UserData path detection

**Files:** Create `simcoach/lmu/install.py`; Test `tests/test_lmu_install.py`.

**Interfaces:**
- `lmu_userdata_dir() -> Path | None` — precedence: `$SIMCOACH_LMU_DIR` (or config `lmu_dir`) → Steam (parse `steamapps/libraryfolders.vdf` for the library holding `Le Mans Ultimate`, then `<lib>/steamapps/common/Le Mans Ultimate/UserData`) → common Epic/standalone paths → None. Verify the dir has `Log/Results`.
- `results_dir() -> Path | None`, `player_dir() -> Path | None`.
- Steam base from the Windows registry (`HKCU\Software\Valve\Steam\SteamPath`) with a fallback to `C:\Program Files (x86)\Steam`. Everything degrades to None (never raises).

- [ ] **Step 1: failing test** — a `tmp_path` fake tree (a `libraryfolders.vdf` pointing at a fake library dir containing `.../Le Mans Ultimate/UserData/Log/Results`), a monkeypatched registry/base lookup, assert `lmu_userdata_dir()` finds it; assert `$SIMCOACH_LMU_DIR` override wins; assert None when absent.
- [ ] **Step 2: run → fail.**
- [ ] **Step 3: implement** `install.py` — a small `.vdf` path extractor (regex for `"path"  "..."` lines is enough; don't add a vdf dependency), the precedence chain, the `Log/Results` existence check.
- [ ] **Step 4: run → pass;** full suite green.
- [ ] **Step 5: commit** `feat(lmu): cross-platform UserData path detection`.

---

### Task 2: Player-name resolution

**Files:** Create `simcoach/lmu/results.py` (start it here); Test `tests/test_lmu_results.py`.

**Interfaces:**
- `player_name_for(result_root: ET.Element, player_dir: Path) -> str | None` — read `<PlayerFile>` text → load `player_dir/{PlayerFile}.JSON` → return its `"Player Name"`. Degrade to None (missing file/field). A `config` override (`lmu_player_name`) short-circuits.

- [ ] Steps: fixture = a trimmed result XML (with `<PlayerFile>Settings</PlayerFile>`) + a fake `Settings.JSON` (`{"Player Name":"Jayson G"}`); assert the name resolves; assert override wins; assert None when profile missing. Implement, green, commit `feat(lmu): resolve player name from PlayerFile profile`.

---

### Task 3: Result XML parser

**Files:** Extend `simcoach/lmu/results.py`; extend `tests/test_lmu_results.py`; add `tests/fixtures/lmu_results/sample_race.xml` (a TRIMMED real file — a few drivers incl. the player, a handful of laps, 1–2 incidents/penalties).

**Interfaces:**
- `@dataclass ResultSession`: `sim, track, car, car_class, date_ts, session_type, position, class_position, best_lap, finish_status, laps: list[ResultLap], incidents: list[str], penalties: list[dict]`.
- `@dataclass ResultLap`: `lap_no, lap_time, sectors: list[float], valid`.
- `parse_result(path: str, player_name: str) -> ResultSession | None` — parse the XML; pick the `<Driver>` whose `<Name>` == player_name (case-insensitive, trimmed); pull session meta (`TrackCourse`→track, `VehName`/`CarClass`→car, `TimeString`/`DateTime`→date, `<Race>`/etc → session_type), the player's `<Lap>`/`<Sector>` rows, `BestLapTime`, `Position`, and the `<Incident>`/`<Penalty>`/`<TrackLimits>` lines naming the player. Returns None if the player isn't in the file. Never raises on a malformed file — return None + (optionally) log.

- [ ] Steps: fixture-driven tests (player found → fields correct incl. laps/sectors/best/position; player absent → None; malformed XML → None). Implement, green, commit `feat(lmu): parse player session/laps from result XML`.

---

### Task 4: Store — `source` column + result-lap insert (schema v5→v6)

**Files:** Modify `simcoach/store.py`; Test `tests/test_store.py` (append).

**Interfaces:**
- Migration `<6`: `ALTER TABLE laps ADD COLUMN source TEXT DEFAULT 'live'` (guarded, dup-column-safe like the v5 pattern); `SCHEMA_VERSION=6`. Existing rows → `'live'`.
- `save_result_session(rs: ResultSession, session_id: str) -> None` — insert its laps as `source='result'`, `telemetry_path=NULL`, mapping lap_time/valid/track/car/phase(session_type). Reuse the existing insert path where possible.
- `ingested_files` table + `is_ingested(path, mtime) -> bool` / `mark_ingested(path, mtime)` for dedup.

- [ ] Steps: test the migration adds `source` (existing rows `'live'`, dup-safe on re-open); test `save_result_session` inserts result-laps queryable via `recent_race_sessions`/`session_laps` with `source='result'`; test dedup. Implement mirroring the v5 migration idioms (`self.conn`, `_add_column`). Full suite green (≥241+new). Commit `feat(store): source column + result-session backfill (schema v6)`.

---

### Task 5: Ingest orchestration + config/Settings wiring

**Files:** Extend `simcoach/lmu/results.py` (or a small `ingest.py`); Modify `simcoach/config.py` (`lmu_dir`, `lmu_player_name` defaults=None); Modify `simcoach/server/app.py` (a `POST /ingest` endpoint); Tests.

**Interfaces:**
- First, extend `parse_result` (results.py) to accept `player_names: str | Iterable[str]` — normalize to a lowercased/trimmed set, match a driver whose name is in it (backward-compatible with the single-str tests).
- `ingest_all(store, userdata_dir=None, player_names=None, limit=None) -> dict` — resolve dir (Task 1); resolve the NAME SET = `player_name_for(...)` (profile seed) ∪ `config.lmu_player_names` (aliases) unless `player_names` is passed; scan `results_dir()/*.xml`; skip `is_ingested`; `parse_result(path, names)` + `save_result_session` (session_id = stable filename stem); `mark_ingested`; return `{"scanned","imported","skipped","errors","unmatched"}` (`unmatched` = files parsed but no name in the set → signals the user should add an alias). Incremental (dedup).
- `config.DEFAULTS` gains `lmu_dir: None`, `lmu_player_names: []`.
- `POST /ingest` → runs `ingest_all` (in a threadpool — it's blocking file I/O), returns the summary. (A one-time "Import history" button in Settings later calls this.)

- [ ] Steps: test `ingest_all` against a fixture dir (2 files: one player-present, one absent) → correct counts + rows in store + dedup on re-run; test the endpoint returns the summary. Implement, green, commit `feat(lmu): result-log ingest orchestration + POST /ingest`.

---

## Non-goals

`.Vcr` decoding (opaque handle only — a future "open replay in LMU" link); weather/full-incident-analysis beyond the player's own; a live file-watcher (manual/one-shot ingest for now); UI for the import (a Settings action lands with the Settings screen, Phase 5).

## Self-Review

Player-ID via PlayerFile (not isPlayer) ✓; path detection cross-platform + override ✓; summary-only/no-telemetry ✓; idempotent dedup ✓; unified laps table + `source` flag so the UI shows backfilled sessions for free with graceful analysis degradation ✓. Risk: real result XMLs vary (offline vs MP, session types P/Q/R, endurance multi-driver stints) — Task 3 must tolerate missing fields + return None rather than raise; validate against several real files during execution, not just the trimmed fixture.

## Execution notes

Order 1→2→3→4→5. Tasks 1–3 pure/offline; Task 4 is a schema migration (risky — review like the v5 one); Task 5 wires it. Validate Task 3's parser against SEVERAL real files in `S:\SteamLibrary\...\UserData\Log\Results` (offline P/Q, MP race, endurance) before trusting it — the trimmed fixture won't cover all shapes.
