# Split `simcoach/server/app.py` into Route Modules: Implementation Plan (#113)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `simcoach/server/app.py` is 2938 lines, and `create_app` (lines 435-2938) is one closure holding every route and about 45 helpers. Split it into 14 sibling modules with **no behaviour change**. Every task moves one module, leaves the full suite green, and ends in its own commit. At the end, `app.py` holds the state setup, the capture-thread glue and a list of `register` calls.

**Architecture:** Each route/helper module exposes `register(app, deps)`. `deps` is a `types.SimpleNamespace` that `create_app` creates once. Inside `register`, the moved code is pasted **verbatim, at the same 4-space indentation**, so it stays a closure over `app` (plus `store = app.state.store`) exactly as it was inside `create_app`. A module publishes the helpers that later modules need with `export(deps, fn, ...)`, which is `setattr(deps, fn.__name__, fn)`. A consumer binds them back under their old names at the top of its `register` (`_track_file_data = deps._track_file_data`). Moved bodies therefore keep the same bare names and the same code, which `moved_verbatim.py` checks mechanically (see Task 0). Module-level code that was already module-level (constants, `_bad_origin`, the cloud and pending-capture helpers) moves as plain module-level code.

**Tech Stack:** Python 3.13, FastAPI/Starlette, pytest, ruff 0.16.8 (pinned; its default set here includes F401/F811/F821/F841, I001, RUF022 and PLC0414).

**Spec:** GitHub issue #113, and the audit's §3 in `.superpowers/sdd/overnight-2026-09-16/audit-report.md`. The audit predates #145/#147/#150/#151/#152/#153, so this plan re-derives the split against `main` @ `39b0d64`. Every line number below is a **baseline line in `39b0d64:simcoach/server/app.py`**. Line numbers drift once earlier tasks cut code, so always find a block by its anchor (`grep -n "def _name\|\"/route\"" simcoach/server/app.py`), not by its number.

## Global Constraints

- **Worktree + own venv.** The main checkout has the driver's uncommitted files (`simcoach/tracks/weathertech_raceway_laguna_seca.json`, `src-tauri/Cargo.toml`, untracked `simcoach/tracks/fuji_speedway.json` and `simcoach/tracks/silverstone_national_circuit.json`). Never touch, stage, stash or reset them. Work in `git worktree add ../sim-racing-coach-113 -b refactor/113-split-server-app main`. Inside it, run `python -m venv .venv && .venv/Scripts/python.exe -m pip install -e ".[dev]"`. **Never** `pip install -e` with the root `.venv` (memory: worktree-editable-install). This plan file is untracked in the main checkout, so read it from there.
- **Stage by name only.** Never `git add -A`, `git add .`, `git stash` or `git reset`.
- **Pure moves.** Do not change route paths, methods, response shapes, status codes, middleware order (keep the CORS-then-host-guard insert-at-0 comment with the code), websocket preambles (**do not** collapse the three identical host/origin preambles into a helper; that is the audit's optional "cheap win" and is left as a follow-up), or startup behaviour (the same `app.state.*` initialised in the same order, and the voice/kokoro lazy imports still run during `create_app`). Comments move with their code. Do not reword, reflow or "tidy" moved code: ruff's import sort (I001) and `__all__` sort (RUF022) autofixes are the only permitted rewrites.
- **Every new module starts with a one-line docstring and then `from __future__ import annotations`**, as `app.py` does. FastAPI evaluates a route's string annotations against the **route function's module globals**. So `Body`, `Request` and `WebSocket` must be module-level imports in whichever module now defines the route. A missing one is an F821 in ruff and a crash in `route_snapshot.py`.
- **One alias per line** (`_x = deps._x`), never tuple-unpacked. That way ruff F841 flags any alias the remaining code no longer uses, and the alias blocks left in `create_app` shrink to nothing by Task 14.
- **Monkeypatch targets:** a test must patch the module whose code **reads** the name. Each task lists the exact test edits. `patch_targets.py` (Task 0) must print `MISS` for exactly those targets after the move and before the test edit (red), then all `OK` after the edit (green).
- **Commits:** `refactor(server): #113 - ...`, with trailer `Refs #113` on Tasks 1-13 and `Fixes #113` on Task 14 only. Every commit message ends with:
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01Pywcy5ZyceRZNDLyjn3F35
  ```
- **Do not merge or push to `main`** until an independent code review **and** a red-team pass are done (standing user preference). `Fixes #113` closes the issue on merge, so the merge is the gate.
- **The gate at the end of every task** is `bash "$T/gate.sh" <new module(s)>` (Task 0). It runs ruff on `simcoach tests scripts`, the verbatim check, the patch-target check, the route/middleware snapshot diff, the **full** `pytest -q` (the passed count must equal the Task 0 baseline) and `wc -l`. `app.py` must shrink every task. Total `simcoach/server/*.py` lines may grow only by the new module's header (docstring, imports, `def register`, aliases, `export`), about 15-45 lines per module.

## Decisions made while planning (recorded; the driver was not available)

1. **Sharing pattern: `register(app, deps)` closures plus a `SimpleNamespace` of published helpers.** Why:
   - (a) Moved bodies stay byte-identical, with the same indentation and the same bare names, so an AST comparison against the baseline proves each move is mechanical.
   - (b) State stays per-app. The tests build hundreds of apps, so the helpers must remain closures and cannot become module globals.
   - (c) `create_app`'s signature and `app.state` are untouched.

   Alternatives rejected:
   - FastAPI `APIRouter` plus `request.app.state` would rewrite every helper to take a request, and would thread one into websocket workers and executor threads.
   - The audit's `ServerDeps` dataclass with explicit-argument helpers would change about 200 call sites, so the moves could no longer be verified as verbatim.
   - Hanging the helpers on `app.state` (like the existing `app.state.map_for`) would pollute the state bag the tests inspect.
2. **14 modules, not the audit's 11.**
   - `deps.py` is named `core.py`. It also takes `_ensure_rest`, `_live_track` and `_live_session_id`, which breaks the cycle status↔maps: `_status_view` needs the map helpers, and the map/capture routes need `_live_track`.
   - The track-file helpers get their own `trackfile.py`. The track routes need the canonical-map helpers, and the canonical-map helpers need the track-file helpers. Splitting helpers from routes turns that cycle into a DAG.
   - `maps.py` (canonical-map helpers) is split from `mapcapture.py` (the `/mapcapture/*` routes, about 330 lines).
   - `serialize.py` keeps only `_clean` and `_usage_view`. The cloud and pending-capture helpers (#150, added after the audit) go in `captures.py`.
   - The status module is **`status_routes.py`**, not `status.py`. A module named `status` would collide with the local `status = deps.status` alias that `create_app` needs from Task 7 to Task 10, and a local assignment anywhere in `create_app` makes `status.register(...)` an `UnboundLocalError`.
3. **`_capture_loop` and `_race_commit` stay in `app.py`.** They are the capture-thread glue that `__main__` imports. As a result the `_append_pending_lap` patch (read by `_capture_loop`) keeps working unchanged.
4. **Re-exports from `app.py` go through `__all__`**, because the repo's ruff flags `x as x` (PLC0414). The re-exported names are `SIDECAR_PORT` (for `__main__`), `_in`, `_save_cloud`, `_drop_pending`, `_append_pending_lap` (tests import these) and `_load_cloud` (for `scripts/backfill_corner_geometry.py`). `patch_targets.py` ignores `__all__`, so a re-export can never mask a stale patch.
5. **Closure-local constants stay closure-local.** `_PROFILE_PREF_KEYS`, `_LEARNING_STATUSES`, `_LIVE_PERIOD`, `_MAX_SETTINGS_BODY_BYTES` and `VOICE_MAX_BODY` move inside their module's `register`, unchanged. Only the constants that were already module-level stay module-level: `REST_PROBE_TTL`/`REST_PROBE_TIMEOUT` (read by core), `_EVENTS_PERIOD` (read by live_ws), `_CLEAN_LAP_RATIO`, and the notes caps.
6. **Logger names change** from `simcoach.server.app` to the new module's `__name__`, because the moved `logging.getLogger(__name__)` calls now resolve there. This is accepted: the change is visible only as the record name in `server.log`, and logging is configured on the `simcoach` parent. `test_server_tracks.py:1182` is retargeted (Task 6). `test_server_tracks.py:254` is left alone, because its warning comes from `simcoach.track_lookup` and is captured at the root default of WARNING either way.
7. **Registration order.** Helpers come first (core, trackfile, maps), then the middleware (the same point as before, ahead of every route), then the route modules. Relative route order changes slightly: `/setup` and `/chats` now register before `/tracks/*`. That is behaviour-neutral, because no two routes can match the same path (checked by hand: every `{param}` route differs from every literal route in segment count or prefix). The snapshot diff compares the route table sorted and the middleware list unsorted.

## File Structure

New, all under `simcoach/server/`. The table shows what each module holds, the helpers it publishes on `deps`, and the helpers it reads from `deps`.

| Module | Holds | Publishes | Reads |
|---|---|---|---|
| `security.py` | `ALLOWED_HOSTS`, `SIDECAR_PORT`, `VITE_DEV_PORT`, `FRONTEND_ORIGINS`, `ALLOWED_ORIGINS`, `_bad_origin`, `install(app)` (CORS + `_host_guard`) | n/a | n/a |
| `serialize.py` | `_clean`, `_usage_view` | n/a | n/a |
| `captures.py` | `_POS_CHANNELS`, `_REBUILD_CHANNELS`, `_usable_cloud`, `_save_cloud`, `_load_cloud`, `_pending_dir`, `_append_pending_lap`, `_pending_laps`, `_one_track`, `_drop_pending`, `_pending_files` | n/a | n/a |
| `core.py` | `REST_PROBE_TTL`, `REST_PROBE_TIMEOUT`, `_in`, `export`, `register` | `_map_for` (also `app.state.map_for`), `_curating`, `_map_dirs`, `_capture_path`, `_newest_capture`, `_invalidate_canon`, `_ensure_rest`, `_live_track`, `_live_session_id` | none |
| `trackfile.py` | `register` | `_write_track_json`, `_track_file_data`, `_track_rotation`, `_is_shipped`, `_shipped_extras` | `_map_dirs`, `_curating` |
| `maps.py` | `_CLEAN_LAP_RATIO`, `register` | `_live_lap_paths`, `_rebuild_clouds`, `_clean_lap_channels`, `_cmap_for_map`, `_frozen_outline`, `_canonical_map`, `_has_canonical`, `_map_payload`, `_live_map_key`, `_slug_map_key` | `_map_for`, `_curating`, `_capture_path`, `_track_file_data`, `_track_rotation`, `_is_shipped`, `_shipped_extras` |
| `status_routes.py` | `register`: `/health`, `/status` | `_status_view`, `status` | `_has_canonical`, `_live_map_key`, `_ensure_rest`, `_live_session_id` |
| `tracks.py` | notes caps, `_utf8`, `register`: `/tracks*`, `PUT /laps/{id}/map`, rotation routes | none | `_track_file_data`, `_write_track_json`, `_track_rotation`, `_invalidate_canon`, `_map_dirs`, `_frozen_outline`, `_clean_lap_channels`, `_canonical_map`, `_cmap_for_map`, `_map_payload`, `_slug_map_key`, `_live_lap_paths`, `_rebuild_clouds` |
| `mapcapture.py` | `register`: `_place_corners`, `/mapcapture/*` | `mapcapture_status` | `_live_track`, `_track_file_data`, `_track_rotation`, `_write_track_json`, `_capture_path`, `_newest_capture`, `_invalidate_canon`, `_live_lap_paths`, `_rebuild_clouds` |
| `reads.py` | `register`: `/sessions*`, `/laps/{id}/analysis\|inputs`, `GET /laps/{id}/map`, `/track/map`, `/track/inputs`, `/setup`, `/setup/capture`, `/chats*` | none | `_map_for`, `_map_payload`, `_live_map_key`, `_ensure_rest`, `status` |
| `profile.py` | `register`: `/profile*` | none | `_cmap_for_map` |
| `settings_routes.py` | `_clamp_tts_cache_mb`, `register`: `/settings*`, `/voice/*`, `/ingest`, `DELETE /data` | none | `_live_track`, `_track_file_data` |
| `coach_ws.py` | `register`: `/coach` websocket | none | `_ensure_rest`, `_map_for`, `_live_session_id` |
| `live_ws.py` | `_EVENTS_PERIOD`, `register`: `/live`, `/events` | none | `_live_track`, `_cmap_for_map`, `_status_view`, `_map_payload`, `_live_map_key`, `mapcapture_status` |

Modified:
- `simcoach/server/app.py` (shrinks to about 200 lines).
- Tests, patch/identity targets only: `test_server_entry.py`, `test_server_status.py`, `test_map_save_geometry.py`, `test_server_reads.py`, `test_server_live.py`, `test_server_events.py`, `test_server_tracks.py`, `test_mapcapture_durable.py`.
- `CLAUDE.md`, the one Layout line (Task 14).

Unchanged: `simcoach/server/__main__.py`, which still imports `SIDECAR_PORT`, `_capture_loop` and `create_app` from `.app`. Also unchanged is `packaging/simcoach-sidecar.spec`, whose `collect_submodules("simcoach")` picks up the new modules.

### Monkeypatch and identity targets: the full list

These were found with `grep -rnE 'simcoach\.server|app_mod|app_module' tests scripts`.

| Test (baseline line) | Target today | Read after the split by | Action | Task |
|---|---|---|---|---|
| `test_map_save_geometry.py:621` | `app_module.sys` `frozen` | `core._curating` | → `core_mod.sys` | 4 |
| `test_server_reads.py:1339`, `:1357` | `app_module.sys` `frozen` | `core._curating` | → `core_mod.sys` | 4 |
| `test_server_entry.py:71` | `app_module.REST_PROBE_TTL` | `core._ensure_rest` | → `core_mod` | 4 |
| `test_server_status.py:194` | `app_module.REST_PROBE_TIMEOUT` | `core._ensure_rest` | → `core_mod` | 4 |
| `test_server_live.py:117-118` | `app_mod.load_map_data` | `trackfile._track_file_data` | → `trackfile_mod` | 5 |
| `test_server_events.py:164` | `app_module.average_racing_line` | `maps._canonical_outline` (map tick) | → `maps_mod` | 6 |
| `test_server_tracks.py:1026` | `"simcoach.server.app.np.load"` | `maps._rebuild_clouds` | → `"simcoach.server.maps.np.load"` | 6 |
| `test_server_tracks.py:1182` | caplog `logger="simcoach.server.app"` | `maps._rebuild_clouds` warning | → `"simcoach.server.maps"` | 6 |
| `test_map_save_geometry.py:544-546` | `app_module.measurable_geometry is analysis...`, `not hasattr(app_module, ...)` | `trackfile` + `tracks` | assert on both modules | 8 |
| `test_mapcapture_durable.py:222` | `app_mod._save_cloud` | `mapcapture.mapcapture_save` | → `mapcapture_mod` | 9 |
| `test_server_reads.py:1323` | `app_module.analyze_lap` | `reads.lap_analysis` | → `reads_mod` | 10 |
| `test_server_events.py:21` | `app_module._EVENTS_PERIOD` | `live_ws.events_ws` | → `live_ws_mod` | 14 |
| `test_mapcapture_durable.py:243` | `app_mod._append_pending_lap` | `app._capture_loop` (stays) | **unchanged** | n/a |
| `test_server_tracks.py:254` | caplog `logger="simcoach.server.app"` | emitter is `simcoach.track_lookup` | **unchanged** (see Decision 6) | n/a |

Imports that must keep resolving from `simcoach.server.app`:
- `create_app` and `_capture_loop`, which stay defined in `app.py`.
- `_append_pending_lap` (`test_mapcapture_durable.py:159`), `_drop_pending` (`:341`), `_save_cloud` (`test_server_tracks.py:1216,1233`), `_in` (`test_server_tracks.py:1573`), `_load_cloud` (`scripts/backfill_corner_geometry.py:36`) and `SIDECAR_PORT` (`simcoach/server/__main__.py:22`). All of these go through `__all__`.

Every other patch in the server tests targets a core module attribute that is looked up lazily at call time: `smm.read_*`, `simcoach.lmu.live.read_field_snapshot`, `raceline.solve_corners`, `config.load_config`, `factory.make_model`, `install.lmu_userdata_dir`, `simcoach.race.resolve_car`, and the `store`/`coach` objects. The moves do not affect them.

---

### Task 0: Worktree, baseline and check scripts (no commit)

**Files:** none in the repo. Scripts go in `$T` (outside the repo).

- [ ] **Step 1: Worktree and venv.**
  ```bash
  cd path/to/sim-racing-coach
  git worktree add ../sim-racing-coach-113 -b refactor/113-split-server-app main
  cd ../sim-racing-coach-113
  python -m venv .venv && .venv/Scripts/python.exe -m pip install -e ".[dev]"
  .venv/Scripts/python.exe -c "import simcoach; print(simcoach.__file__)"   # must contain sim-racing-coach-113
  T="$(cygpath -m "$TEMP")/split113"; mkdir -p "$T"; echo "$T"
  git show 39b0d64:simcoach/server/app.py > "$T/app_base.py"
  ```
  Every later command runs from the worktree root with `T` set as above.

- [ ] **Step 2: Write the four check scripts with the Write tool.** Do not use a Bash heredoc: heredocs have turned `\b` into a backspace byte in this repo (memory: heredoc-backslash-collapse). Write them to `$T/moved_verbatim.py`, `$T/patch_targets.py`, `$T/route_snapshot.py` and `$T/gate.sh`:

  `moved_verbatim.py`: proves every function in a new module is an unchanged copy of the baseline.
  ```python
  """usage: python moved_verbatim.py <baseline app.py> <new module.py> [...]
  Every function defined at module level, or directly inside `register`/`install`, must
  exist in the baseline (module level or directly inside create_app) with identical code
  (ast.unparse: comments/blank lines ignored, everything else compared). Exit 1 on a miss."""
  import ast
  import difflib
  import sys

  GLUE = {"register", "install", "export"}   # new wiring, not moved code


  def funcs(tree, containers):
      out = {}
      for node in tree.body:
          if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
              if node.name in containers:
                  for sub in node.body:
                      if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                          out[sub.name] = sub
              else:
                  out[node.name] = node
      return out


  base = funcs(ast.parse(open(sys.argv[1], encoding="utf-8").read()), {"create_app"})
  bad = 0
  for path in sys.argv[2:]:
      for name, node in funcs(ast.parse(open(path, encoding="utf-8").read()), GLUE).items():
          if name in GLUE:
              continue
          if name not in base:
              print(f"NEW  {path}: {name} (not in baseline)")
              bad += 1
              continue
          a, b = ast.unparse(base[name]), ast.unparse(node)
          if a != b:
              bad += 1
              print(f"DIFF {path}: {name}")
              print("\n".join(difflib.unified_diff(a.splitlines(), b.splitlines(), lineterm="")))
          else:
              print(f"OK   {path}: {name}")
  sys.exit(1 if bad else 0)
  ```
  (Decorators are part of the unparsed node, so route paths and methods are compared too.)

  `patch_targets.py`: every test monkeypatch on a `simcoach.server` module must hit the module that reads the name.
  ```python
  """Every monkeypatch in tests/ that targets a simcoach.server module must patch the
  module whose code READS the name -- else the patch is a silent no-op. Exit 1 on a miss."""
  import re
  import sys
  from pathlib import Path

  SERVER = Path("simcoach/server")
  # A re-export in __all__ and a comment are not reads: drop both before scanning.
  src = {p.stem: re.sub(r"__all__\s*=\s*\[.*?\]", "", p.read_text(encoding="utf-8"), flags=re.S)
         for p in SERVER.glob("*.py")}


  def readers(name):
      word = re.compile(rf"(?<![\w.]){re.escape(name)}\b")
      out = []
      for mod, text in src.items():
          for line in text.splitlines():
              s = line.strip()
              if s.startswith(("import ", "from ", "#")) or re.match(rf"(async )?def {name}\b", s):
                  continue
              if word.search(line.split("#", 1)[0]):
                  out.append(mod)
                  break
      return out


  bad = 0
  for test in sorted(Path("tests").glob("*.py")):
      t = test.read_text(encoding="utf-8")
      alias = {}
      for m in re.finditer(r"import simcoach\.server\.(\w+) as (\w+)", t):
          alias[m[2]] = m[1]
      for m in re.finditer(r"from simcoach\.server import (\w+)(?: as (\w+))?", t):
          alias[m[2] or m[1]] = m[1]
      hits = [(m[1], m[2]) for m in re.finditer(
          r'monkeypatch\.setattr\(\s*"simcoach\.server\.(\w+)\.(\w+)', t)]
      for m in re.finditer(r'monkeypatch\.setattr\(\s*(\w+)(?:\.(\w+))?\s*,\s*"(\w+)"', t):
          if m[1] in alias:
              hits.append((alias[m[1]], m[2] or m[3]))   # A.sys -> check `sys` is read in A
      for mod, name in hits:
          r = readers(name)
          ok = mod in r
          bad += not ok
          print(f"{'OK  ' if ok else 'MISS'} {test.name}: {mod}.{name}  (read in: {', '.join(r) or '-'})")
  sys.exit(1 if bad else 0)
  ```

  `route_snapshot.py`: the route table and middleware stack of a freshly built app.
  ```python
  import os
  import sys
  import tempfile

  d = tempfile.mkdtemp()
  os.environ["SIMCOACH_DATA_DIR"] = d          # never touch the real app-data dir
  import simcoach                               # noqa: E402
  from simcoach.coach import Coach              # noqa: E402
  from simcoach.model.fake import FakeModel     # noqa: E402
  from simcoach.server.app import create_app    # noqa: E402
  from simcoach.store import LapStore           # noqa: E402

  print("#", simcoach.__file__, file=sys.stderr)
  app = create_app(LapStore(os.path.join(d, "c.sqlite"), os.path.join(d, "data")),
                   Coach(FakeModel()), tracks_dir=d)
  for r in app.routes:
      print(type(r).__name__, r.path, ",".join(sorted(getattr(r, "methods", None) or [])),
            getattr(getattr(r, "endpoint", None), "__name__", ""))
  for m in app.user_middleware:
      disp = (getattr(m, "kwargs", None) or {}).get("dispatch")
      print("MW", m.cls.__name__, getattr(disp, "__name__", ""))
  ```

  `gate.sh`: the per-task gate.
  ```bash
  #!/usr/bin/env bash
  # usage (from the worktree root): bash "$T/gate.sh" simcoach/server/<new>.py ...
  set -euo pipefail
  T="$(cd "$(dirname "$0")" && pwd -W 2>/dev/null || pwd)"
  PY=.venv/Scripts/python.exe
  $PY -c "import simcoach; p = simcoach.__file__; print(p); assert 'sim-racing-coach-113' in p"
  $PY -m ruff check --fix --select I001,RUF022 simcoach/server tests   # import/__all__ sort only
  $PY -m ruff check simcoach tests scripts
  if [ $# -gt 0 ]; then $PY "$T/moved_verbatim.py" "$T/app_base.py" "$@"; fi
  $PY "$T/patch_targets.py"
  $PY "$T/route_snapshot.py" > "$T/routes_now.txt"
  diff <(grep -v '^MW' "$T/routes_base.txt" | sort) <(grep -v '^MW' "$T/routes_now.txt" | sort)
  diff <(grep '^MW' "$T/routes_base.txt") <(grep '^MW' "$T/routes_now.txt")
  $PY -m pytest -q -p no:cacheprovider | tail -3
  echo "baseline: $(cat "$T/pytest_base.txt")"
  wc -l simcoach/server/*.py
  ```

- [ ] **Step 3: Record the baselines.**
  ```bash
  PY=.venv/Scripts/python.exe
  $PY "$T/route_snapshot.py" > "$T/routes_base.txt"; grep -c . "$T/routes_base.txt"
  $PY "$T/patch_targets.py"                   # expect 16 lines, all OK, exit 0
  $PY -m pytest -q -p no:cacheprovider | tail -1 | tee "$T/pytest_base.txt"   # the passed count every task must match
  $PY -m ruff check simcoach tests scripts    # "All checks passed!"
  wc -l simcoach/server/*.py                  # app.py 2938, total 3019
  ```
  `routes_base.txt` has 58 lines: 56 routes, then exactly two `MW` lines, `MW BaseHTTPMiddleware _host_guard` and then `MW CORSMiddleware`. Planning verified this against `39b0d64`. Any failure here is a pre-existing problem. Stop and report it; do not start moving code.

**Pin subset** (run after each move before the full gate, for a fast signal):
`.venv/Scripts/python.exe -m pytest -q tests/test_server_*.py tests/test_map_save_geometry.py tests/test_mapcapture_*.py`

---

### Task 1: `security.py`: host/origin guard + middleware

**Files:** Create `simcoach/server/security.py`. Modify `simcoach/server/app.py`.

**Interfaces:** `ALLOWED_HOSTS`, `SIDECAR_PORT`, `VITE_DEV_PORT`, `FRONTEND_ORIGINS`, `ALLOWED_ORIGINS`, `_bad_origin(origin) -> bool`, `install(app) -> None`.

- [ ] **Step 1: Create the module.** Move these blocks **verbatim** from `app.py`:
  - baseline 149-155 (the Host-allowlist comment plus `ALLOWED_HOSTS`);
  - 162-163 (`SIDECAR_PORT`, `VITE_DEV_PORT`, with their trailing comments);
  - 164-188 (the #54 comment, `FRONTEND_ORIGINS`, the FULL-origins comment, `ALLOWED_ORIGINS`);
  - 191-204 (`_bad_origin`).

  Leave lines 156-161 (`REST_PROBE_TTL`, `REST_PROBE_TIMEOUT` and its comment, `_EVENTS_PERIOD`) in `app.py` for now. Then add `install`, whose body is baseline 1004-1033 (the insert-at-0 comment, `app.add_middleware(CORSMiddleware, ...)`, and the `@app.middleware("http")` `_host_guard` with its whole comment) at unchanged indentation:
  ```python
  """Host/Origin guard for the localhost sidecar (DNS-rebinding + CSRF). Moved from app.py (#113)."""
  from __future__ import annotations

  from fastapi import Request
  from fastapi.responses import JSONResponse
  from starlette.middleware.cors import CORSMiddleware

  # <baseline 149-155, 162-188, 191-204 here, verbatim>


  def install(app) -> None:
      # <baseline 1004-1033 here, verbatim (already 4-space indented)>
  ```
- [ ] **Step 2: Edit `app.py`.** Delete the moved lines. In `create_app`, replace baseline 1004-1033 with the single line `    security.install(app)`, at the same position (after `_live_track`, before `@app.get("/health")`). Add `from . import security` and `from .security import ALLOWED_HOSTS, SIDECAR_PORT, _bad_origin`, because the three websocket preambles still read the first and last of those names. Add after the imports:
  ```python
  # Imported from here by __main__, tests and scripts; defined in the sibling modules (#113).
  __all__ = ["SIDECAR_PORT", "_capture_loop", "create_app"]
  ```
  Remove the imports that ruff now reports unused (`JSONResponse`, `CORSMiddleware`).
- [ ] **Step 3: Pin, then gate.** Run the pin subset (at least `tests/test_server_cors.py tests/test_server_ws.py tests/test_server_ingest.py tests/test_server_entry.py`), then `bash "$T/gate.sh" simcoach/server/security.py`. Expect `OK` for `_bad_origin` and `_host_guard`, the route and MW diffs empty (MW order unchanged), the same passed count, and `app.py` smaller.
- [ ] **Step 4: Commit.**
  ```bash
  git add simcoach/server/security.py simcoach/server/app.py
  git commit -F - <<'EOF'
  refactor(server): #113 - move the host/origin guard and middleware to security.py

  Pure move: the allowlists, _bad_origin and the CORS + host-guard middleware
  (install) are unchanged; create_app calls security.install(app) at the point the
  middleware used to be added, so the middleware order is identical.

  Refs #113

  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01Pywcy5ZyceRZNDLyjn3F35
  EOF
  ```

### Task 2: `serialize.py`: `_clean`, `_usage_view`

**Files:** Create `simcoach/server/serialize.py`. Modify `app.py`.

**Interfaces:** `_clean(v)`, `_usage_view(agg) -> dict`.

- [ ] **Step 1:** Move `_usage_view` (baseline 130-146) and `_clean` (207-216) verbatim. Be careful: `_CLEAN_LAP_RATIO = 1.07 ...` (147) directly follows `_usage_view` with no blank line and **stays** in `app.py` (Task 6 moves it).
  ```python
  """JSON wire shaping for the sidecar: non-finite floats to null, chat usage view. Moved from app.py (#113)."""
  from __future__ import annotations

  import math

  from ..pricing import estimate_chat_cost

  # <_usage_view, _clean verbatim>
  ```
- [ ] **Step 2:** In `app.py`, add `from .serialize import _clean, _usage_view` and drop the `estimate_chat_cost` import. `math` is still used, so keep it until ruff says otherwise.
- [ ] **Step 3:** Pin subset, then `bash "$T/gate.sh" simcoach/server/serialize.py`.
- [ ] **Step 4:** Commit `serialize.py` and `app.py`: `refactor(server): #113 - move _clean and _usage_view to serialize.py`, with `Refs #113` and the trailers.

### Task 3: `captures.py`: raw clouds and the pending mapping capture

**Files:** Create `simcoach/server/captures.py`. Modify `app.py`.

**Interfaces:** `_POS_CHANNELS`, `_REBUILD_CHANNELS`, `_usable_cloud`, `_save_cloud`, `_load_cloud`, `_pending_dir`, `_append_pending_lap`, `_pending_laps`, `_one_track`, `_drop_pending`, `_pending_files`.

- [ ] **Step 1:** Move baseline 219-353 verbatim, as one contiguous block from `_POS_CHANNELS` through `_pending_files`.
  ```python
  """Raw mapping point clouds on disk: the saved capture and the pending one (#150). Moved from app.py (#113)."""
  from __future__ import annotations

  import logging
  import os
  import uuid
  from pathlib import Path

  import numpy as np

  from ..track_lookup import track_slug

  # <baseline 219-353 verbatim>
  ```
- [ ] **Step 2:** In `app.py`, import from `.captures` every name the remaining code uses (`_capture_loop` uses `_append_pending_lap`, and the closure uses the rest). Extend `__all__` with `"_append_pending_lap", "_drop_pending", "_load_cloud", "_save_cloud"`. Keep `numpy` for now, because `_rebuild_clouds` still uses `np.load`.
- [ ] **Step 3:** Patch targets: `app_mod._save_cloud` (read by `mapcapture_save`, still in the closure) and `app_mod._append_pending_lap` (read by `_capture_loop`) both still resolve in `app`. `patch_targets.py` must stay all `OK`, with no test edits.
- [ ] **Step 4:** Pin subset, then `bash "$T/gate.sh" simcoach/server/captures.py`.
- [ ] **Step 5:** Commit `captures.py` and `app.py`: `refactor(server): #113 - move the cloud and pending-capture helpers to captures.py`, with `Refs #113`.

### Task 4: `core.py`: the deps namespace and the state-bound core helpers

**Files:** Create `simcoach/server/core.py`. Modify `app.py`, `tests/test_server_entry.py`, `tests/test_server_status.py`, `tests/test_map_save_geometry.py`, `tests/test_server_reads.py`.

**Interfaces:**
- Module level: `REST_PROBE_TTL`, `REST_PROBE_TIMEOUT`, `_in(base, slug) -> Path`, `export(deps, *fns)`.
- `register(app, deps)` publishes `_map_for`, `_curating`, `_map_dirs`, `_capture_path`, `_newest_capture`, `_invalidate_canon`, `_ensure_rest`, `_live_track` and `_live_session_id`, and sets `app.state.map_for`.

- [ ] **Step 1: Create the module.** Move these verbatim:
  - module level: baseline 156-160 (`REST_PROBE_TTL` plus the probe-timeout comment and `REST_PROBE_TIMEOUT`) and 356-368 (`_in`);
  - inside `register`, in this order: 488-503 (`_map_for` **and** the `app.state.map_for = _map_for` line), 505-509 (`_curating`), 511-516 (`_map_dirs`), 568-574 (`_capture_path`), 576-579 (`_newest_capture`), 933-943 (`_invalidate_canon`), 945-988 (`_ensure_rest`), 990-1002 (`_live_track`), 1039-1049 (`_live_session_id`).
  ```python
  """Per-app core helpers every route module shares (#113): corner-map resolution, map/capture
  paths, cache invalidation, the rate-capped LMU REST probe, the live track/session reads."""
  from __future__ import annotations

  import sys
  import time
  from pathlib import Path

  from fastapi import HTTPException

  from ..track_lookup import track_slug

  # <baseline 156-160 verbatim>

  # <baseline 356-368 (_in) verbatim>


  def export(deps, *fns) -> None:
      # Publish closures on the shared namespace under their own names, so a consumer binds
      # `_x = deps._x` and its moved body reads exactly as it did inside create_app.
      for f in fns:
          setattr(deps, f.__name__, f)


  def register(app, deps) -> None:
      store = app.state.store

      # <the nine blocks above, verbatim, in baseline order>

      export(deps, _map_for, _curating, _map_dirs, _capture_path, _newest_capture,
             _invalidate_canon, _ensure_rest, _live_track, _live_session_id)
  ```
- [ ] **Step 2: Edit `app.py`.**
  - Delete the moved lines.
  - Add `from types import SimpleNamespace`, `from . import core` and `from .core import _in`, and add `"_in"` to `__all__`.
  - At the old `_map_for` position (right after `app.state.last_ribbon = None ...`), insert the lines below, then **one alias line per published helper the remaining closure still calls**. Today that is all nine; ruff F841 reports any that are not used.
    ```python
        deps = SimpleNamespace()   # helpers each module publishes for the next (#113)
        core.register(app, deps)
        _map_for = deps._map_for
        _curating = deps._curating
        # ... one line each: _map_dirs, _capture_path, _newest_capture, _invalidate_canon,
        #     _ensure_rest, _live_track, _live_session_id
    ```
  - Remove `import sys` and `import time` when ruff reports them unused.
- [ ] **Step 3: Red check.** Run `$PY "$T/patch_targets.py"`. It must print `MISS` for exactly `test_map_save_geometry.py: app.sys`, `test_server_reads.py: app.sys` (×2), `test_server_entry.py: app.REST_PROBE_TTL` and `test_server_status.py: app.REST_PROBE_TIMEOUT`.
- [ ] **Step 4: Retarget those tests** to the module that reads the name:
  - `tests/test_server_entry.py` (~65-71): replace `from simcoach.server import app as app_module` with `from simcoach.server import core as core_mod`, and replace `monkeypatch.setattr(app_module, "REST_PROBE_TTL", 0.0)` with `monkeypatch.setattr(core_mod, "REST_PROBE_TTL", 0.0)`.
  - `tests/test_server_status.py` (~187-194): the same swap, giving `monkeypatch.setattr(core_mod, "REST_PROBE_TIMEOUT", 0.1)`.
  - `tests/test_server_reads.py` (~1332-1339 and ~1351-1357): in both tests, `from simcoach.server import core as core_mod` and `monkeypatch.setattr(core_mod.sys, "frozen", True/False, raising=False)`.
  - `tests/test_map_save_geometry.py`: add `import simcoach.server.core as core_mod` beside the existing `import simcoach.server.app as app_module` (line 20; `app_module` is still used at 544-546 until Task 8), and change line 621 to `monkeypatch.setattr(core_mod.sys, "frozen", True, raising=False)`.

  Re-run `patch_targets.py`: all `OK`.
- [ ] **Step 5:** Pin subset, then `bash "$T/gate.sh" simcoach/server/core.py`. `moved_verbatim` must print `OK` for `_in` and the nine helpers.
- [ ] **Step 6:** Commit `core.py`, `app.py` and the four test files by name: `refactor(server): #113 - core.py: the deps namespace and the shared core helpers`, with `Refs #113`. In the body, name the four retargeted patches.

### Task 5: `trackfile.py`: the one track-JSON writer and its readers

**Files:** Create `simcoach/server/trackfile.py`. Modify `app.py`, `tests/test_server_live.py`.

**Interfaces:** `register(app, deps)` publishes `_write_track_json`, `_track_file_data`, `_track_rotation`, `_is_shipped` and `_shipped_extras`. `_validated_track_data` and `_clean_corner_geometry` stay internal to the closure.

- [ ] **Step 1:** Move these into `register` verbatim, in baseline order: 518-566 (`_write_track_json`), 661-665 (`_track_file_data`), 667-716 (`_validated_track_data`), 718-738 (`_clean_corner_geometry`), 740-743 (`_track_rotation`), 745-753 (`_is_shipped`), 755-766 (`_shipped_extras`).
  ```python
  """Track JSON files (#113): the one validated writer (#65) and the merged shipped+overlay reads."""
  from __future__ import annotations

  import logging
  import math
  import os
  import tempfile

  from fastapi import HTTPException

  from ..analysis import CORNER_GEOMETRY_KEYS, measurable_geometry
  from ..corners import validated_sections, validated_sectors
  from ..track_lookup import dump_track_json, guard_corner_notes, load_map_data, track_slug
  from .core import _in, export


  def register(app, deps) -> None:
      _map_dirs = deps._map_dirs
      _curating = deps._curating

      # <the seven blocks verbatim>

      export(deps, _write_track_json, _track_file_data, _track_rotation, _is_shipped,
             _shipped_extras)
  ```
- [ ] **Step 2:** In `app.py`, call `trackfile.register(app, deps)` right after the core aliases, followed by one alias line per published helper still used in `create_app` (all five today). Drop the aliases for `_map_dirs`/`_curating` only if ruff reports them unused (`list_tracks` and `_map_payload` still use them). Drop imports that are now unused (`tempfile`, `dump_track_json`, `guard_corner_notes`, `load_map_data`, `validated_sections`, `validated_sectors`, ...). Keep whatever ruff says is still used (`measurable_geometry` and `CORNER_GEOMETRY_KEYS` are still used by `_persist_map`).
- [ ] **Step 3: Red check.** `patch_targets.py` must print `MISS test_server_live.py: app.load_map_data (read in: trackfile, __main__)`.
- [ ] **Step 4:** In `tests/test_server_live.py` (~117-118), add `import simcoach.server.trackfile as trackfile_mod` to the imports, then change the code to:
  ```python
      real = trackfile_mod.load_map_data
      monkeypatch.setattr(trackfile_mod, "load_map_data",
                          lambda *a, **k: calls.append(a) or real(*a, **k))
  ```
  Remove `import simcoach.server.app as app_mod` if ruff reports it unused. Re-run `patch_targets.py`: all `OK`.
- [ ] **Step 5:** Pin subset, then `bash "$T/gate.sh" simcoach/server/trackfile.py`.
- [ ] **Step 6:** Commit `trackfile.py`, `app.py` and `tests/test_server_live.py`: `refactor(server): #113 - move the track-file writer and readers to trackfile.py`, with `Refs #113`.

### Task 6: `maps.py`: the canonical-map helpers (no routes)

**Files:** Create `simcoach/server/maps.py`. Modify `app.py`, `tests/test_server_events.py`, `tests/test_server_tracks.py`.

**Interfaces:**
- Module level: `_CLEAN_LAP_RATIO`.
- `register(app, deps)` publishes `_live_lap_paths`, `_rebuild_clouds`, `_clean_lap_channels`, `_cmap_for_map`, `_frozen_outline`, `_canonical_map`, `_has_canonical`, `_map_payload`, `_live_map_key` and `_slug_map_key`. `_any_pos_lap` and `_canonical_outline` stay internal.

- [ ] **Step 1:** Move `_CLEAN_LAP_RATIO` (baseline 147, with its comment) to module level. Move into `register`, verbatim and in baseline order: 581-598 (`_live_lap_paths`), 600-659 (`_rebuild_clouds`), 768-794 (`_clean_lap_channels`), 796-809 (`_cmap_for_map`), 811-821 (`_any_pos_lap`), 823-829 (`_frozen_outline`), 831-845 (`_canonical_outline`), 847-856 (`_has_canonical`), 858-879 (`_canonical_map`), 881-894 (`_map_payload`), 896-909 (`_live_map_key`), 911-931 (`_slug_map_key`).
  ```python
  """The per-track canonical map (#113): outline (frozen or averaged), corner map, the cached
  screen payload, and which laps a rebuild consumes (#69)."""
  from __future__ import annotations

  import logging
  import os

  import numpy as np

  from ..corners import CornerMap
  from ..track_lookup import track_slug
  from ..trackmap import average_racing_line, build_track_map
  from .captures import _POS_CHANNELS, _load_cloud, _usable_cloud
  from .core import export
  from .serialize import _clean

  _CLEAN_LAP_RATIO = 1.07  # keep laps within +7% of the track/car best (drop in/out/off laps)


  def register(app, deps) -> None:
      store = app.state.store
      _map_for = deps._map_for
      _curating = deps._curating
      _capture_path = deps._capture_path
      _track_file_data = deps._track_file_data
      _track_rotation = deps._track_rotation
      _is_shipped = deps._is_shipped
      _shipped_extras = deps._shipped_extras

      # <the twelve blocks verbatim>

      export(deps, _live_lap_paths, _rebuild_clouds, _clean_lap_channels, _cmap_for_map,
             _frozen_outline, _canonical_map, _has_canonical, _map_payload, _live_map_key,
             _slug_map_key)
  ```
- [ ] **Step 2:** In `app.py`, call `maps.register(app, deps)` right after the trackfile aliases (still before `security.install(app)`), plus one alias per published helper still used. Drop unused imports: `numpy`, `build_track_map`, `CornerMap` if unused, `_load_cloud`/`_usable_cloud` from the `.captures` import (the `__all__` names stay imported). Keep `average_racing_line`, because `_persist_map` still uses it.
- [ ] **Step 3: Red check.** `patch_targets.py` must print `MISS test_server_tracks.py: app.np`. The `average_racing_line` patch still prints `OK`, because `_persist_map` in `app` reads it too. That is exactly the case the checker cannot see, which is why it is listed here: without the edit, `test_server_events` fails loudly (a `map` frame appears).
- [ ] **Step 4:** Retarget:
  - `tests/test_server_events.py`: add `from simcoach.server import maps as maps_mod`, and change ~164 to `monkeypatch.setattr(maps_mod, "average_racing_line", ...)`. `app_module` is still used by `_EVENTS_PERIOD` at line 21 until Task 14.
  - `tests/test_server_tracks.py:1026`: `monkeypatch.setattr("simcoach.server.maps.np.load", counting_load)`.
  - `tests/test_server_tracks.py:1182`: `caplog.at_level(logging.WARNING, logger="simcoach.server.maps")` (the "skipping unreadable lap" warning is emitted by `_rebuild_clouds`). Leave `:254` unchanged (Decision 6).
- [ ] **Step 5:** Pin subset, then `bash "$T/gate.sh" simcoach/server/maps.py`. Run `test_server_tracks.py` and `test_server_capture.py` **now**, before Task 8 or Task 9 builds on this module. They are the two heaviest route suites.
- [ ] **Step 6:** Commit `maps.py`, `app.py`, `tests/test_server_events.py` and `tests/test_server_tracks.py`: `refactor(server): #113 - move the canonical-map helpers to maps.py`, with `Refs #113`.

### Task 7: `status_routes.py`: `/health`, `/status`

**Files:** Create `simcoach/server/status_routes.py`. Modify `app.py`.

**Interfaces:** `register(app, deps)` registers `GET /health` and `GET /status`, and publishes `_status_view` and `status`, the route function itself: `/track/inputs` and `/setup/capture` call `status()`.

- [ ] **Step 1:** Move into `register` verbatim: baseline 1035-1037 (`health`), 1051-1104 (`_status_view`), 1106-1108 (`status`).
  ```python
  """GET /health and GET /status: the live track/car/session strip (#113)."""
  from __future__ import annotations

  from ..car_lookup import resolve_car
  from .core import export


  def register(app, deps) -> None:
      store = app.state.store
      _has_canonical = deps._has_canonical
      _live_map_key = deps._live_map_key
      _ensure_rest = deps._ensure_rest
      _live_session_id = deps._live_session_id

      # <health, _status_view, status verbatim>

      export(deps, _status_view, status)
  ```
- [ ] **Step 2:** In `app.py`, add `from . import status_routes`. **Never** add `from . import status`: see Decision 2. Put `status_routes.register(app, deps)` directly after `security.install(app)`, followed by `_status_view = deps._status_view` and `status = deps.status` (still used by `track_inputs`, `setup_capture` and `_events_tick`). Drop `resolve_car` from the `app.py` imports only if ruff says so (`coach_ws` still uses it).
- [ ] **Step 3:** Pin (`tests/test_server_status.py tests/test_server_health.py tests/test_server_events.py` + subset), then `bash "$T/gate.sh" simcoach/server/status_routes.py`.
- [ ] **Step 4:** Commit `status_routes.py` and `app.py`: `refactor(server): #113 - move /health and /status to status_routes.py`, with `Refs #113`.

### Task 8: `tracks.py`: track notes, map saves, rotation, the Tracks list

**Files:** Create `simcoach/server/tracks.py`. Modify `app.py`, `tests/test_map_save_geometry.py`.

**Interfaces:** module-level `_MAX_NOTE_KEYS`, `_MAX_NOTE_KEY_CHARS`, `_MAX_NOTE_CHARS`, `_MAX_NOTES_DOC_BYTES`, `_utf8`. `register(app, deps)` registers:
- `GET`/`PUT /tracks/{slug}/map`
- `PUT /laps/{lap_id}/map`
- `POST /tracks/{slug}/draft-notes`
- `PUT`/`GET /tracks/{slug}/notes`
- `GET /tracks`
- `POST /laps/{lap_id}/rotation`
- `POST /tracks/{slug}/rotation`

It publishes nothing.

- [ ] **Step 1:** Move to module level, verbatim: baseline 74-116 (the notes-caps comments and the four constants) and 119-127 (`_utf8`). Move into `register`, verbatim and in order: 1245-1255 (`track_map_by_slug`), 1257-1479 (`_persist_map`), 1481-1488, 1490-1498, 1500-1544 (`draft_notes`), 1546-1559 (`_notes_rev`), 1561-1632 (`save_notes`), 1634-1646, 1648-1701 (`list_tracks`), 1703-1730 (`_persist_rotation`), 1732-1739, 1741-1746.
  ```python
  """Track routes (#113): map saves from the editor, authored notes, rotation, the Tracks list."""
  from __future__ import annotations

  import hashlib
  import json
  import math

  from fastapi import Body, HTTPException

  from ..analysis import CORNER_GEOMETRY_KEYS, corner_geometry, geometry_clause, measurable_geometry
  from ..corners import in_window
  from ..track_lookup import corner_apex_key, fill_blank_labels, track_slug
  from ..trackmap import average_racing_line, direction_from_path

  # <baseline 74-116, 119-127 verbatim>


  def register(app, deps) -> None:
      store = app.state.store
      _track_file_data = deps._track_file_data
      _write_track_json = deps._write_track_json
      _track_rotation = deps._track_rotation
      _invalidate_canon = deps._invalidate_canon
      _map_dirs = deps._map_dirs
      _frozen_outline = deps._frozen_outline
      _clean_lap_channels = deps._clean_lap_channels
      _canonical_map = deps._canonical_map
      _cmap_for_map = deps._cmap_for_map
      _map_payload = deps._map_payload
      _slug_map_key = deps._slug_map_key
      _live_lap_paths = deps._live_lap_paths
      _rebuild_clouds = deps._rebuild_clouds

      # <the twelve blocks verbatim>
  ```
  (`from .core import export` is not needed here: nothing is published.)
- [ ] **Step 2:** In `app.py`, call `tracks.register(app, deps)` after `status_routes.register(...)` and its aliases. Prune now-unused aliases, including `_map_dirs`, `_frozen_outline`, `_clean_lap_channels`, `_slug_map_key` and `_write_track_json` if ruff reports them. Prune now-unused imports: `hashlib`, `corner_geometry`, `geometry_clause`, `measurable_geometry`, `CORNER_GEOMETRY_KEYS`, `in_window`, `corner_apex_key`, `fill_blank_labels`, `average_racing_line`, `direction_from_path`, `Body` (only if ruff says so; mapcapture still uses it).
- [ ] **Step 3: Red check.** Run the pin subset: `test_map_save_geometry.py::...measurable...` now fails with `AttributeError: ... app ... has no attribute 'measurable_geometry'`. Replace baseline lines 544-546 with:
  ```python
      from simcoach.server import trackfile, tracks
      for mod in (trackfile, tracks):   # every server module that applies the rule (#65)
          assert mod.measurable_geometry is analysis.measurable_geometry   # not a local copy
          assert not hasattr(mod, "_measurable")
          assert not hasattr(mod, "SIGNED_GEOMETRY_KEYS")
  ```
  Remove `import simcoach.server.app as app_module` (line 20) if ruff reports it unused.
- [ ] **Step 4:** Pin subset, then `bash "$T/gate.sh" simcoach/server/tracks.py`.
- [ ] **Step 5:** Commit `tracks.py`, `app.py` and `tests/test_map_save_geometry.py`: `refactor(server): #113 - move the track routes to tracks.py`, with `Refs #113`.

### Task 9: `mapcapture.py`: the mapping-capture routes

**Files:** Create `simcoach/server/mapcapture.py`. Modify `app.py`, `tests/test_mapcapture_durable.py`.

**Interfaces:** `register(app, deps)` registers `POST /mapcapture/start|cancel|build|save|save-cloud|rebuild` and `GET /mapcapture/status`, and publishes `mapcapture_status` (for the `/events` tick). `_place_corners` stays internal.

- [ ] **Step 1:** Move into `register`, verbatim and in order: baseline 1748-1795 (`_place_corners`), 1797-1803 (the CSRF `Body(...)` comment), then 1804-2074 (`mapcapture_start` through `mapcapture_rebuild`).
  ```python
  """Mapping capture (#113): drive boundary laps, build a ribbon, save it and its point cloud."""
  from __future__ import annotations

  from fastapi import Body, HTTPException

  from ..model_types import LapRecord
  from ..ribbon import multi_signal_direction, reconstruct_surface, ribbon_to_screen
  from ..track_lookup import track_slug
  from .captures import (
      _POS_CHANNELS,
      _drop_pending,
      _one_track,
      _pending_dir,
      _pending_files,
      _pending_laps,
      _save_cloud,
  )
  from .core import export
  from .serialize import _clean


  def register(app, deps) -> None:
      store = app.state.store
      _live_track = deps._live_track
      _track_file_data = deps._track_file_data
      _track_rotation = deps._track_rotation
      _write_track_json = deps._write_track_json
      _capture_path = deps._capture_path
      _newest_capture = deps._newest_capture
      _invalidate_canon = deps._invalidate_canon
      _live_lap_paths = deps._live_lap_paths
      _rebuild_clouds = deps._rebuild_clouds

      # <baseline 1748-2074 verbatim>

      export(deps, mapcapture_status)
  ```
- [ ] **Step 2:** In `app.py`, call `mapcapture.register(app, deps)` after `tracks.register(...)`, then `mapcapture_status = deps.mapcapture_status` (still used by `_events_tick`). Prune aliases and imports as ruff reports: `LapRecord`, the `ribbon` imports, the `.captures` names outside `__all__`, `_newest_capture`, `_capture_path`, `_live_lap_paths`, `_rebuild_clouds`, `_write_track_json`, `_track_rotation`.
- [ ] **Step 3: Red check.** `patch_targets.py` must print `MISS test_mapcapture_durable.py: app._save_cloud (read in: captures, mapcapture)`. This is the silent-no-op case the checker exists for: `_save_cloud` is still importable from `app` via `__all__`.
- [ ] **Step 4:** In `tests/test_mapcapture_durable.py`, add `from simcoach.server import mapcapture as mapcapture_mod`, and change ~222 to `monkeypatch.setattr(mapcapture_mod, "_save_cloud", boom)`. Leave ~243 (`app_mod._append_pending_lap`) as it is: `_capture_loop` in `app.py` reads it. Re-run `patch_targets.py`: all `OK`.
- [ ] **Step 5:** Pin subset, then `bash "$T/gate.sh" simcoach/server/mapcapture.py`.
- [ ] **Step 6:** Commit `mapcapture.py`, `app.py` and `tests/test_mapcapture_durable.py`: `refactor(server): #113 - move the /mapcapture routes to mapcapture.py`, with `Refs #113`.

### Task 10: `reads.py`: sessions, lap reads, live track reads, setup, chats

**Files:** Create `simcoach/server/reads.py`. Modify `app.py`, `tests/test_server_reads.py`.

**Interfaces:** `register(app, deps)` registers:
- `GET /sessions`, `/sessions/summary`, `/sessions/{sid}/laps|chats|race`
- `GET /laps/{lap_id}/analysis|inputs|map`
- `GET /track/map`, `/track/inputs`
- `GET /setup`, `POST /setup/capture`
- `GET /chats`, `/chats/{chat_id}`, `/chats/{chat_id}/usage`

It publishes nothing.

- [ ] **Step 1:** Move into `register`, verbatim and in order: baseline 1110-1243 (`sessions` through `track_inputs`, including the nested `_trace`), then 2076-2133 (`setup` through `chat_usage`).
  ```python
  """Read routes (#113): sessions, per-lap analysis/inputs/map, the live track's map and
  inputs, the live setup (+ its explicit capture), and chats."""
  from __future__ import annotations

  import logging
  from dataclasses import asdict

  from fastapi import Body, HTTPException

  from ..analysis import analyze_lap, input_traces
  from ..tiers import adjustable_from_setup
  from .serialize import _clean, _usage_view


  def register(app, deps) -> None:
      store = app.state.store
      _map_for = deps._map_for
      _map_payload = deps._map_payload
      _live_map_key = deps._live_map_key
      _ensure_rest = deps._ensure_rest
      status = deps.status

      # <baseline 1110-1243, 2076-2133 verbatim>
  ```
- [ ] **Step 2:** In `app.py`, call `reads.register(app, deps)` right after `status_routes.register(...)` (Decision 7), and prune the `status` alias and imports as ruff reports (`asdict`, `input_traces`, `adjustable_from_setup`, `_clean`). Keep `analyze_lap`, because `distill_profile` still uses it.
- [ ] **Step 3: Red check.** `patch_targets.py` prints `OK` for `app.analyze_lap` (`distill_profile` in `app` still reads it), so this is the second invisible case. The pin subset proves it instead: `test_analysis_unanalyzable_is_422` now fails (200, not 422). In `tests/test_server_reads.py` (~1316-1323), replace `from simcoach.server import app as app_module` with `from simcoach.server import reads as reads_mod`, and use `monkeypatch.setattr(reads_mod, "analyze_lap", _boom)`.
- [ ] **Step 4:** Pin (`tests/test_server_reads.py tests/test_server_inputs.py` + subset), then `bash "$T/gate.sh" simcoach/server/reads.py`.
- [ ] **Step 5:** Commit `reads.py`, `app.py` and `tests/test_server_reads.py`: `refactor(server): #113 - move the read routes to reads.py`, with `Refs #113`.

### Task 11: `profile.py`: driver profile and memories

**Files:** Create `simcoach/server/profile.py`. Modify `app.py`.

**Interfaces:** `register(app, deps)` registers `GET`/`PUT /profile`, `PUT /profile/summary`, `POST /profile/memories`, `PUT /profile/memories/{learning_id}` and `POST /profile/distill`.

- [ ] **Step 1:** Move baseline 2454-2590 into `register` verbatim. That includes `_PROFILE_PREF_KEYS` and `_LEARNING_STATUSES`, which stay closure-local (Decision 5).
  ```python
  """Driver profile routes (#113): prefs, the always-on summary, reviewed memories, distill."""
  from __future__ import annotations

  import json

  from fastapi import Body, HTTPException

  from .. import config
  from ..analysis import analyze_lap
  from ..coach_tools import race_result_text, scrub_stored_answer


  def register(app, deps) -> None:
      _cmap_for_map = deps._cmap_for_map

      # <baseline 2454-2590 verbatim>
  ```
- [ ] **Step 2:** In `app.py`, call `profile.register(app, deps)` after `mapcapture.register(...)`. Prune per ruff (`analyze_lap`, `race_result_text`).
- [ ] **Step 3:** Pin (`tests/test_server_profile.py` + subset), then `bash "$T/gate.sh" simcoach/server/profile.py`.
- [ ] **Step 4:** Commit `profile.py` and `app.py`: `refactor(server): #113 - move the profile routes to profile.py`, with `Refs #113`.

### Task 12: `settings_routes.py`: settings, voice, ingest, data

**Files:** Create `simcoach/server/settings_routes.py`. Modify `app.py`.

**Interfaces:**
- Module level: `_clamp_tts_cache_mb(voice_cfg) -> int`.
- `register(app, deps)` registers `GET`/`PUT /settings`, `POST /settings/api-key`, `POST /settings/test-key`, `POST /voice/transcribe`, `POST /voice/setup`, `GET /voice/status`, `POST /voice/kokoro/setup`, `GET /voice/kokoro/status`, `POST /voice/speak`, `GET`/`DELETE /voice/tts-cache`, `POST /ingest` and `DELETE /data`.

- [ ] **Step 1:** Move `_clamp_tts_cache_mb` (baseline 371-378) to module level. Move baseline 2732-2936 into `register` verbatim (from `_settings_view` through `delete_data`, including the closure-local `_MAX_SETTINGS_BODY_BYTES` and `VOICE_MAX_BODY`). The voice names these routes use were **lazy** imports inside `create_app`. Keep them lazy, as the first lines of `register`, so they still import during `create_app` and not at module import:
  ```python
  """Settings, voice (STT/TTS), ingest and data-reset routes (#113)."""
  from __future__ import annotations

  import asyncio
  import json

  from fastapi import Body, HTTPException, Request, Response

  from .. import config, logging_setup, paths, secrets
  from ..lmu.ingest import ingest_all
  from ..model.factory import make_model
  from ..pronunciations import table_for
  from ..track_lookup import track_slug

  # <baseline 371-378 (_clamp_tts_cache_mb) verbatim>


  def register(app, deps) -> None:
      from ..tts_kokoro import EngineUnavailable, Unpronounceable, gpl_free
      from ..voice import NotInstalled, VoiceInputError, decode_wav
      store = app.state.store
      _live_track = deps._live_track
      _track_file_data = deps._track_file_data

      # <baseline 2732-2936 verbatim>
  ```
- [ ] **Step 2:** In `app.py`, call `settings_routes.register(app, deps)` **last** among the route modules (it was last in the baseline). Add `from .settings_routes import _clamp_tts_cache_mb` (still used for the `TtsCache` init). Trim `create_app`'s lazy imports to what the init still needs: `from ..tts_kokoro import KokoroEngine` and `from ..voice import KokoroInstaller, ModelInstaller, WhisperTranscriber, kokoro_dir, migrate_model_dir`. Keep `TtsCache` and `_paths`. Prune per ruff (`logging_setup`, `paths`, `secrets`, `ingest_all`, `make_model`, `table_for`, `Request`, `Response`, `json` if unused, `asyncio` if unused).
- [ ] **Step 3:** Pin (`tests/test_server_settings.py tests/test_server_voice.py tests/test_server_ingest.py` + subset), then `bash "$T/gate.sh" simcoach/server/settings_routes.py`.
- [ ] **Step 4:** Commit `settings_routes.py` and `app.py`: `refactor(server): #113 - move the settings/voice/ingest/data routes to settings_routes.py`, with `Refs #113`.

### Task 13: `coach_ws.py`: the `/coach` websocket

**Files:** Create `simcoach/server/coach_ws.py`. Modify `app.py`.

**Interfaces:** `register(app, deps)` registers the `WS /coach` websocket.

- [ ] **Step 1:** Move baseline 2135-2452 into `register` verbatim (`coach_ws`, with its `_emit` and `worker` closures and its unchanged host/origin preamble).
  ```python
  """The /coach websocket (#113): one question in, streamed coach events out, turn persisted."""
  from __future__ import annotations

  import asyncio
  import logging
  import threading
  from pathlib import Path

  from fastapi import WebSocket
  from starlette.websockets import WebSocketDisconnect, WebSocketState

  from .. import config
  from ..car_lookup import resolve_car
  from ..car_notes import load_car_notes, load_setup_concepts
  from ..coach_tools import (
      _PARTIAL_LIST_WORD,
      CoachContext,
      _lap_age,
      current_race_fact,
      race_turn_note,
      scrub_stored_answer,
  )
  from ..driver import DriverProfile
  from ..format import format_laptime
  from ..model.base import TokenUsage
  from ..model.factory import CoachUserError
  from ..race import live_race
  from ..tiers import legal_adjustments
  from .security import ALLOWED_HOSTS, _bad_origin
  from .serialize import _usage_view


  def register(app, deps) -> None:
      store = app.state.store
      _ensure_rest = deps._ensure_rest
      _map_for = deps._map_for
      _live_session_id = deps._live_session_id

      # <baseline 2135-2452 verbatim>
  ```
- [ ] **Step 2:** In `app.py`, call `coach_ws.register(app, deps)` right after `mapcapture.register(...)`, before `profile.register(...)` (the baseline order). Prune per ruff: `threading`, `WebSocketDisconnect`/`WebSocketState` if `live_ws` no longer needs them in `app` (it still does until Task 14), `config` if unused, the `coach_tools`/`car_notes`/`driver`/`format`/`TokenUsage`/`CoachUserError`/`live_race`/`legal_adjustments`/`resolve_car` imports, and the `_ensure_rest`, `_map_for` and `_live_session_id` aliases.
- [ ] **Step 3:** Pin (`tests/test_server_ws.py` + subset), then `bash "$T/gate.sh" simcoach/server/coach_ws.py`.
- [ ] **Step 4:** Commit `coach_ws.py` and `app.py`: `refactor(server): #113 - move the /coach websocket to coach_ws.py`, with `Refs #113`.

### Task 14: `live_ws.py`: `/live` and `/events`; `create_app` becomes wiring

**Files:** Create `simcoach/server/live_ws.py`. Modify `app.py`, `tests/test_server_events.py`, `CLAUDE.md`.

**Interfaces:** module-level `_EVENTS_PERIOD`. `register(app, deps)` registers `WS /live` and `WS /events`.

- [ ] **Step 1:** Move `_EVENTS_PERIOD` (baseline 161, with its comment) to module level. Move baseline 2592-2730 into `register` verbatim (`_LIVE_PERIOD` stays closure-local; also `live_ws` with its nested `_next_corner_view`/`_live_tick`, and `events_ws` with `_events_tick`).
  ```python
  """The push websockets (#113): /live (rail-map position + next corner, ~4 Hz) and /events
  (status/laps/map/mapcapture/voice-setup topics, #106)."""
  from __future__ import annotations

  import asyncio
  import json
  import logging

  from fastapi import WebSocket
  from starlette.websockets import WebSocketDisconnect, WebSocketState

  from ..corners import next_corner
  from .security import ALLOWED_HOSTS, _bad_origin

  _EVENTS_PERIOD = 1.0  # s: /events push loop period (module constant so tests can monkeypatch it)


  def register(app, deps) -> None:
      store = app.state.store
      _live_track = deps._live_track
      _cmap_for_map = deps._cmap_for_map
      _status_view = deps._status_view
      _map_payload = deps._map_payload
      _live_map_key = deps._live_map_key
      mapcapture_status = deps.mapcapture_status

      # <baseline 2592-2730 verbatim>
  ```
- [ ] **Step 2:** `create_app` now contains no route or helper code. Make it exactly: the baseline 438-486 state setup (with the trimmed lazy imports from Task 12), then:
  ```python
      deps = SimpleNamespace()   # helpers each module publishes for the next (#113)
      core.register(app, deps)
      trackfile.register(app, deps)
      maps.register(app, deps)
      security.install(app)      # before any route; see the order note inside install()
      status_routes.register(app, deps)
      reads.register(app, deps)
      tracks.register(app, deps)
      mapcapture.register(app, deps)
      coach_ws.register(app, deps)
      profile.register(app, deps)
      live_ws.register(app, deps)
      settings_routes.register(app, deps)
      return app
  ```
  No alias lines may remain; ruff F841 flags any leftover. The module-level imports should reduce to `logging`, `ThreadPoolExecutor`, `Path`, `SimpleNamespace`, `FastAPI`, `config`, `RaceTracker`, the `from . import ...` of the modules, `.captures` (`_append_pending_lap`, plus the `__all__` re-exports), `from .core import _in`, `from .security import SIDECAR_PORT` and `from .settings_routes import _clamp_tts_cache_mb`. `__all__` (sorted by RUF022) is `SIDECAR_PORT`, `_append_pending_lap`, `_capture_loop`, `_drop_pending`, `_in`, `_load_cloud`, `_save_cloud`, `create_app`. Append one sentence to the module docstring: `Routes and helpers live in the sibling modules; create_app wires them (#113).`
- [ ] **Step 3: Red check.** `patch_targets.py` must print `MISS test_server_events.py: app._EVENTS_PERIOD (read in: live_ws)`. In `tests/test_server_events.py`, replace `from simcoach.server import app as app_module` (line 9) with `from simcoach.server import live_ws as live_ws_mod`, and change line 21 to `monkeypatch.setattr(live_ws_mod, "_EVENTS_PERIOD", 0.01)`. Re-run: all `OK`.
- [ ] **Step 4:** In `CLAUDE.md` → Layout, replace the line `- `simcoach/server/` — the FastAPI sidecar the desktop app talks to.` with:
  `- `simcoach/server/` — the FastAPI sidecar the desktop app talks to. `app.py` is wiring only (state + `register` calls); routes live in `reads`/`tracks`/`mapcapture`/`profile`/`settings_routes`/`status_routes`/`coach_ws`/`live_ws`, shared per-app helpers in `core`/`trackfile`/`maps` (published on a `deps` namespace), guard in `security`.`
- [ ] **Step 5: Final gate.** Run `bash "$T/gate.sh" $(ls simcoach/server/*.py | grep -vE '/(app|__main__|__init__)\.py$')` (all 14 new modules), then confirm each of these:
  - `moved_verbatim` prints `OK` for every function and nothing `NEW`/`DIFF`. Also run `$PY "$T/moved_verbatim.py" "$T/app_base.py" simcoach/server/app.py`: the only `DIFF` allowed is `create_app` (it is now the wiring). `_race_commit` and `_capture_loop` must be `OK`.
  - The route and MW diffs are empty.
  - The passed count equals the baseline.
  - `app.py` is roughly 200 lines, and the total is within about 450 lines of 3019.
  - `grep -rn "simcoach.server.app\|app_mod\b\|app_module\b" tests` shows only the unchanged uses (`test_mapcapture_durable.py` `_append_pending_lap` and the imports; `test_server_tracks.py:254` caplog; `create_app`/`_capture_loop`/re-export imports).
  - `grep -nE "^\s{4}(_\w+|status|mapcapture_status) = deps\." simcoach/server/app.py` prints nothing.
- [ ] **Step 6:** Commit `live_ws.py`, `app.py`, `tests/test_server_events.py` and `CLAUDE.md`:
  ```bash
  git add simcoach/server/live_ws.py simcoach/server/app.py tests/test_server_events.py CLAUDE.md
  git commit -F - <<'EOF'
  refactor(server): #113 - move /live and /events to live_ws.py; create_app is wiring

  Last step of the split: app.py keeps the app.state setup, the capture-thread glue
  (_capture_loop, _race_commit) and one register call per module. Route table,
  middleware order and the full suite are unchanged against 39b0d64.

  Fixes #113

  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01Pywcy5ZyceRZNDLyjn3F35
  EOF
  ```
- [ ] **Step 7: Hand off for review.** Do not merge. Request an independent code review and a red-team pass. Reviewers should run `git log -p --color-moved=dimmed-zebra main..refactor/113-split-server-app`: every non-dimmed `+` line is new glue (a header, alias, `export`, `register` call or test retarget) and must be justified. They should re-run `moved_verbatim.py` from their own checkout (a `git archive` of the branch tip, and print `simcoach.__file__`; see memory "stale worktrees + venv crosstalk").

---

## Risks

1. **Silent no-op monkeypatch.** A test keeps patching `app.X` after the route moved elsewhere. Mitigated three ways: the explicit per-task table, `patch_targets.py` (red before the test edit, green after), and the unchanged passed count. Two cases are invisible to the checker because `app` still reads the name for one more task: `average_racing_line` (Task 6) and `analyze_lap` (Task 10). Both are listed, and both make their test fail loudly if skipped.
2. **Re-exports masking stale patches.** `_save_cloud` stays importable from `app` forever. `patch_targets.py` strips `__all__` before scanning, so the Task 9 `MISS` is real.
3. **Module/local name collision.** An `UnboundLocalError` at `create_app` time. Avoided with `status_routes`. The `coach_ws`/`live_ws` route functions and the modules of the same name never coexist in `app.py`, because each task deletes the function when it imports the module.
4. **FastAPI annotation resolution.** Route annotations resolve in the new module's globals. A missing `Body`/`Request`/`WebSocket` import is caught by ruff F821 and by `route_snapshot.py` (it builds the app).
5. **Lost comment.** `ast.unparse` ignores comments, so the verbatim check cannot see a dropped comment. The `--color-moved` review in Step 7 can.
6. **Logger names change** (Decision 6). This is visible only in `server.log` record names. If the driver wants `simcoach.server.app` kept, the fix is a follow-up: each module would use `logging.getLogger("simcoach.server.app")`.
7. **Conflicts with other branches.** Any open branch that touches `app.py` must rebase onto this split afterwards. Land it when nothing else is mid-flight on `simcoach/server/app.py`.
8. **Venv crosstalk.** A worktree-local venv, plus `gate.sh` asserting that `simcoach.__file__` is in `sim-racing-coach-113`.

## Self-Review

- **Issue coverage:** every route and helper of `create_app` is assigned. The table, rebuilt from the baseline, covers these blocks: 488-1002 (helpers), 1004-1033 (middleware), 1035-1108 (status), 1110-1243 (reads), 1245-1746 (tracks), 1748-2074 (mapcapture), 2076-2133 (reads), 2135-2452 (coach_ws), 2454-2590 (profile), 2592-2730 (live_ws) and 2732-2936 (settings_routes). Module-level lines 72-433 are split across security/serialize/captures/core/tracks/maps/live_ws/settings_routes, apart from `_DEFAULT_TRACKS_DIR`, `_race_commit` and `_capture_loop`, which stay.
- **One module per task, green after each, one commit each:** Tasks 1-14. Each task's moved code depends only on modules that already exist (core → trackfile → maps → status_routes → tracks/mapcapture/reads/profile/settings → coach_ws → live_ws). The code still inside `create_app` reads the new helpers through aliases.
- **`create_app` signature and `app.state`:** unchanged. The state init is not moved. `app.state.map_for` is still set during `create_app` (by `core.register`, at the same point).
- **Constraints honoured:** paths, shapes, middleware order and websocket preambles are unchanged, and the snapshot diff enforces the route and middleware part. `Refs #113` is on Tasks 1-13 and `Fixes #113` on Task 14. Staging is by name. The driver's files are never touched (work happens in a worktree).
- **Placeholder scan:** each `<baseline a-b verbatim>` marker names an exact baseline range and anchor, and `moved_verbatim.py` is the executable check that the paste is exact. No TODO steps.
- **Known limits:** comments are not machine-checked (review with `--color-moved`), and two invisible patch cases are called out explicitly above.
