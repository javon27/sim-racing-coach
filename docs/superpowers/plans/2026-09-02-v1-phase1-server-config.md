# v1 Phase 1 — FastAPI server + installer-ready config + chat/session migration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up a localhost FastAPI adapter over the existing synchronous Python core, move all configuration (including the Anthropic key) into an installer-ready config layer with no reliance on system env vars, and migrate `coach_chat` so chat is decoupled from the race session — all testable without a frontend.

**Architecture:** The core stays synchronous and unchanged in behavior; FastAPI endpoints are plain `def` handlers (FastAPI runs them in a threadpool, matching the blocking core). A new `simcoach/config` layer resolves an OS app-data dir, persists a flat JSON config, and stores secrets in the OS keyring; `make_model` reads the key from it. A SQLite migration (v4→v5) adds `chat_id` to `coach_chat`, wrapping existing rows into one legacy chat. Streaming, websockets, and the frontend are later phases.

**Tech Stack:** Python ≥3.11, FastAPI + Starlette `TestClient`, uvicorn, `keyring`, numpy, anthropic. Tests: `python -m pytest -q`.

**Spec:** `docs/superpowers/specs/2026-08-29-v1-standalone-ui-design.md` (decisions locked 2026-09-02, amended same day with the installer/config/Settings scope).

## Global Constraints

- **Windows-only target.** LMU is Windows-only; app-data dir is `%APPDATA%\SimCoach\`. Use `os.environ["APPDATA"]` with a fallback to `Path.home()/".simcoach"` so tests pass on any OS/CI.
- **No required env vars.** Env vars are honored only as *overrides*. Precedence everywhere: `env override > config file > default`. Nothing may *require* an env var.
- **Secrets never in JSON.** The Anthropic API key lives only in the OS keyring (service name `"SimCoach"`, username `"anthropic_api_key"`). `GET /settings` returns `api_key_set: bool`, never the key.
- **Server binds to `127.0.0.1` only.** Never `0.0.0.0`.
- **Keep the suite green.** Baseline is 158 `def test_` functions across `tests/test_*.py`, run with `python -m pytest -q`. Every task ends green.
- **Core behavior unchanged.** `store`, `coach`, `coach_tools`, `analysis`, `smm`, `LmuSource` are reused, not rewritten. The terminal harness (`__main__`/`app.run_repl`) must keep working after the migration.
- **TDD + frequent commits.** Test first, watch it fail, minimal implementation, watch it pass, commit.

---

### Task 1: App-data path resolution

**Files:**
- Create: `simcoach/paths.py`
- Test: `tests/test_paths.py`

**Interfaces:**
- Produces:
  - `app_data_dir() -> pathlib.Path` — the SimCoach data root; created if missing. `%APPDATA%\SimCoach` on Windows, `~/.simcoach` elsewhere, overridable by `SIMCOACH_DATA_DIR`.
  - `config_path() -> Path` → `app_data_dir()/"simcoach.config.json"`
  - `db_path() -> Path` → `app_data_dir()/"simcoach.sqlite"`
  - `telemetry_dir() -> Path` → `app_data_dir()/"telemetry"` (created if missing)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_paths.py
import os
from pathlib import Path
from simcoach import paths


def test_override_env_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path / "custom"))
    d = paths.app_data_dir()
    assert d == tmp_path / "custom"
    assert d.is_dir()  # created


def test_derived_paths_live_under_root(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path))
    assert paths.config_path() == tmp_path / "simcoach.config.json"
    assert paths.db_path() == tmp_path / "simcoach.sqlite"
    assert paths.telemetry_dir() == tmp_path / "telemetry"
    assert paths.telemetry_dir().is_dir()


def test_default_root_no_env(monkeypatch):
    monkeypatch.delenv("SIMCOACH_DATA_DIR", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    d = paths.app_data_dir()
    assert d == Path.home() / ".simcoach"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_paths.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'simcoach.paths'`

- [ ] **Step 3: Write minimal implementation**

```python
# simcoach/paths.py
"""Resolve the OS app-data locations for config, the SQLite DB, and telemetry
files. Windows-first (LMU is Windows-only); a home-dir fallback keeps tests and
non-Windows CI working. SIMCOACH_DATA_DIR overrides the root for developers."""
from __future__ import annotations

import os
from pathlib import Path


def app_data_dir() -> Path:
    override = os.environ.get("SIMCOACH_DATA_DIR")
    if override:
        root = Path(override)
    else:
        appdata = os.environ.get("APPDATA")
        root = Path(appdata) / "SimCoach" if appdata else Path.home() / ".simcoach"
    root.mkdir(parents=True, exist_ok=True)
    return root


def config_path() -> Path:
    return app_data_dir() / "simcoach.config.json"


def db_path() -> Path:
    return app_data_dir() / "simcoach.sqlite"


def telemetry_dir() -> Path:
    d = app_data_dir() / "telemetry"
    d.mkdir(parents=True, exist_ok=True)
    return d
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_paths.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add simcoach/paths.py tests/test_paths.py
git commit -m "feat(config): app-data path resolution with SIMCOACH_DATA_DIR override"
```

---

### Task 2: Config layer — defaults, env overrides, save_config

**Files:**
- Modify: `simcoach/config.py` (whole file; currently `DEFAULTS`/`CONFIG_PATH`/`load_config` only)
- Test: `tests/test_config.py` (may already exist — add these tests; if the file exists, append)

**Interfaces:**
- Consumes: `simcoach.paths.config_path` (Task 1)
- Produces:
  - `DEFAULTS` dict extended with: `input_device` (`"wheel"`), `model` (`"sonnet"`), `corner_naming` (`"names"`), and `driver` (a dict `{"braking_style": None, "balance_preference": None, "risk_tolerance": None}`). `units` stays `"metric"`.
  - `load_config(path: str | None = None) -> dict` — merges file over `DEFAULTS`, then applies env overrides for the documented keys, using `paths.config_path()` when `path` is None.
  - `save_config(cfg: dict, path: str | None = None) -> None` — writes the flat JSON (pretty). Never writes secrets (there are none in `cfg`).
  - `ENV_OVERRIDES: dict[str, str]` mapping config key → env var name: `{"input_device": "SIMCOACH_INPUT_DEVICE", "units": "SIMCOACH_UNITS", "model": "SIMCOACH_MODEL"}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config.py  (append if file exists)
import json
from simcoach import config


def test_defaults_present():
    cfg = config.load_config.__globals__["DEFAULTS"]
    for k in ("units", "input_device", "model", "corner_naming", "driver"):
        assert k in cfg


def test_load_merges_file_over_defaults(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"units": "imperial"}))
    cfg = config.load_config(str(p))
    assert cfg["units"] == "imperial"
    assert cfg["model"] == "sonnet"  # default fills the rest


def test_env_override_beats_file(tmp_path, monkeypatch):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"input_device": "wheel"}))
    monkeypatch.setenv("SIMCOACH_INPUT_DEVICE", "controller")
    cfg = config.load_config(str(p))
    assert cfg["input_device"] == "controller"


def test_save_round_trips(tmp_path):
    p = tmp_path / "c.json"
    config.save_config({"units": "imperial", "model": "opus"}, str(p))
    assert json.loads(p.read_text())["model"] == "opus"
    assert config.load_config(str(p))["units"] == "imperial"


def test_malformed_file_falls_back(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("{ not json")
    assert config.load_config(str(p))["units"] == "metric"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_config.py -v`
Expected: FAIL — `AttributeError: module 'simcoach.config' has no attribute 'save_config'` (and env/defaults assertions)

- [ ] **Step 3: Write minimal implementation**

```python
# simcoach/config.py
"""User settings, persisted as flat JSON so the desktop UI's settings page and
the terminal app read/write the same file. Secrets are NOT stored here (see
simcoach.secrets). Env vars override the file for the documented keys only;
nothing requires them. Malformed/unknown files fall back to defaults."""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import paths

DEFAULTS = {
    "units": "metric",            # "metric" (km/h, °C) or "imperial" (mph, °F)
    "input_device": "wheel",      # "wheel" or "controller"
    "model": "sonnet",            # "opus" | "sonnet" | "haiku" -> make_model
    "corner_naming": "names",     # "names" or "numbers"
    "driver": {                   # DriverProfile fields; None = unset
        "braking_style": None,
        "balance_preference": None,
        "risk_tolerance": None,
    },
}

ENV_OVERRIDES = {
    "input_device": "SIMCOACH_INPUT_DEVICE",
    "units": "SIMCOACH_UNITS",
    "model": "SIMCOACH_MODEL",
}


def _path(path: str | None) -> Path:
    return Path(path) if path else paths.config_path()


def load_config(path: str | None = None) -> dict:
    p = _path(path)
    cfg = json.loads(json.dumps(DEFAULTS))  # deep copy (nested driver dict)
    if p.exists():
        try:
            loaded = json.loads(p.read_text())
            if isinstance(loaded, dict):
                cfg.update(loaded)
        except (json.JSONDecodeError, OSError):
            pass
    else:
        try:
            p.write_text(json.dumps(cfg, indent=2))
        except OSError:
            pass
    for key, env in ENV_OVERRIDES.items():
        val = os.environ.get(env)
        if val:
            cfg[key] = val
    return cfg


def save_config(cfg: dict, path: str | None = None) -> None:
    _path(path).write_text(json.dumps(cfg, indent=2))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_config.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add simcoach/config.py tests/test_config.py
git commit -m "feat(config): env-override precedence, new fields, save_config"
```

---

### Task 3: Secrets via keyring

**Files:**
- Create: `simcoach/secrets.py`
- Test: `tests/test_secrets.py`
- Modify: `tests/conftest.py` (add the shared `mem_keyring` fixture below — reused by Task 8)
- Modify: `pyproject.toml` (add `keyring>=25` to `dependencies`)

**Interfaces:**
- Produces:
  - `set_api_key(key: str) -> None`
  - `get_api_key() -> str | None` — env `ANTHROPIC_API_KEY` overrides the keyring; keyring read guarded (no backend → None); strips whitespace; empty → None.
  - `has_api_key() -> bool`
  - `clear_api_key() -> None`
  - Module constants `SERVICE = "SimCoach"`, `KEY_USER = "anthropic_api_key"`.
- Test fixture (conftest, reused by Task 8): `mem_keyring` — installs an in-memory keyring backend and yields it, so no test touches a real OS backend.

- [ ] **Step 1: Add the conftest fixture, then write the failing test**

```python
# tests/conftest.py  (append)
import keyring as _keyring
from keyring.backend import KeyringBackend
import pytest


class MemoryKeyring(KeyringBackend):
    priority = 1
    def __init__(self): self._store = {}
    def get_password(self, service, username): return self._store.get((service, username))
    def set_password(self, service, username, password): self._store[(service, username)] = password
    def delete_password(self, service, username): self._store.pop((service, username), None)


@pytest.fixture
def mem_keyring():
    prev = _keyring.get_keyring()
    kr = MemoryKeyring()
    _keyring.set_keyring(kr)
    yield kr
    _keyring.set_keyring(prev)
```

```python
# tests/test_secrets.py
from simcoach import secrets


def test_set_get_clear(monkeypatch, mem_keyring):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert secrets.get_api_key() is None
    assert secrets.has_api_key() is False
    secrets.set_api_key("sk-test-123")
    assert secrets.get_api_key() == "sk-test-123"
    assert secrets.has_api_key() is True
    secrets.clear_api_key()
    assert secrets.get_api_key() is None


def test_env_overrides_keyring(monkeypatch, mem_keyring):
    secrets.set_api_key("sk-stored")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env")
    assert secrets.get_api_key() == "sk-env"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_secrets.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'simcoach.secrets'` (install keyring first if import of `keyring` fails: `pip install "keyring>=25"` into `.venv`)

- [ ] **Step 3: Write minimal implementation**

```python
# simcoach/secrets.py
"""Anthropic API key storage in the OS keyring (Windows Credential Manager,
per-user DPAPI). Entry stays in the app's own UI; keyring is silent storage.
The ANTHROPIC_API_KEY env var overrides the stored key for developers."""
from __future__ import annotations

import os

import keyring

SERVICE = "SimCoach"
KEY_USER = "anthropic_api_key"


def get_api_key() -> str | None:
    env = os.environ.get("ANTHROPIC_API_KEY")
    if env and env.strip():
        return env.strip()
    try:
        stored = keyring.get_password(SERVICE, KEY_USER)
    except Exception:
        # No keyring backend (e.g. headless CI) -> behave as "no key set"
        return None
    return stored.strip() if stored and stored.strip() else None


def set_api_key(key: str) -> None:
    keyring.set_password(SERVICE, KEY_USER, key.strip())


def has_api_key() -> bool:
    return get_api_key() is not None


def clear_api_key() -> None:
    try:
        keyring.delete_password(SERVICE, KEY_USER)
    except keyring.errors.PasswordDeleteError:
        pass
```

Add to `pyproject.toml` `dependencies`: `"keyring>=25"`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_secrets.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add simcoach/secrets.py tests/test_secrets.py tests/conftest.py pyproject.toml
git commit -m "feat(config): keyring-backed API key storage with env override"
```

---

### Task 4: make_model reads key from config layer + LazyModel wrapper

**Files:**
- Modify: `simcoach/model/factory.py`
- Test: `tests/test_factory.py` (append if exists)

**Interfaces:**
- Consumes: `simcoach.secrets.get_api_key` (Task 3); the `model` alias set (`opus`/`sonnet`/`haiku`).
- Produces:
  - `make_model(provider=None, model=None, api_key=None) -> Model` — unchanged signature; new behavior: when `api_key is None`, fall back to `secrets.get_api_key()` (not `os.environ`); `model` accepts the friendly aliases and maps to the Claude model id. Explicit args win. Raises `RuntimeError("No Anthropic API key set")` for the anthropic provider when no key is resolvable.
  - `LazyModel(provider=None, model=None)` — implements the `Model` protocol (`complete`, `run`) but defers `make_model(...)` until the **first** `complete`/`run` call, caching the result. Lets the server construct a `Coach` with **no key set** without crashing at boot; the `RuntimeError` only surfaces when the coach is actually invoked. Used by the server entry point (Task 9).
  - `MODEL_ALIASES = {"opus": "claude-opus-5", "sonnet": "claude-sonnet-5", "haiku": "claude-haiku-4-5-20251001"}` (verified against this environment's stated model IDs).

**Why the module-level import matters:** `factory.py` today imports `ClaudeModel` *inside* `make_model`. This task moves `from .claude import ClaudeModel` to **module level** (anthropic is an unconditional dependency, so it's safe) so the test can `monkeypatch.setattr(factory, "ClaudeModel", ...)` and have it bind. A local import would shadow the patch and construct the real client.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_factory.py
import pytest
from simcoach.model import factory


def test_fake_provider_needs_no_key():
    assert factory.make_model(provider="fake") is not None


def test_anthropic_uses_secrets_when_no_key(monkeypatch):
    monkeypatch.setattr(factory, "get_api_key", lambda: "sk-from-secrets")
    captured = {}
    monkeypatch.setattr(factory, "ClaudeModel",
                        lambda model, api_key: captured.update(model=model, api_key=api_key) or "M")
    m = factory.make_model(provider="anthropic", model="opus")
    assert m == "M"
    assert captured["api_key"] == "sk-from-secrets"
    assert "opus" in captured["model"]  # alias mapped to a claude-opus id


def test_anthropic_no_key_raises(monkeypatch):
    monkeypatch.setattr(factory, "get_api_key", lambda: None)
    with pytest.raises(RuntimeError, match="API key"):
        factory.make_model(provider="anthropic")


def test_lazy_model_defers_construction(monkeypatch):
    # No key: constructing LazyModel must NOT raise; only calling it does.
    monkeypatch.setattr(factory, "get_api_key", lambda: None)
    lm = factory.LazyModel(provider="anthropic", model="sonnet")  # no raise
    with pytest.raises(RuntimeError, match="API key"):
        lm.complete("sys", [{"role": "user", "content": "hi"}])


def test_lazy_model_builds_once(monkeypatch):
    calls = {"n": 0}
    def fake_make(provider, model):
        calls["n"] += 1
        class M:
            def complete(self, s, m): return "ok"
            def run(self, s, m, t, d): return "ok"
        return M()
    monkeypatch.setattr(factory, "make_model", fake_make)
    lm = factory.LazyModel(provider="fake")
    assert lm.complete("s", []) == "ok"
    assert lm.complete("s", []) == "ok"
    assert calls["n"] == 1  # built once, cached
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_factory.py -v`
Expected: FAIL — factory reads `os.environ` for the key / no `get_api_key` / no alias mapping / no `RuntimeError` / no `LazyModel`.

- [ ] **Step 3: Write minimal implementation**

Edit `simcoach/model/factory.py`:
- Move to module level: `from .claude import ClaudeModel` and `from ..secrets import get_api_key`.
- Add `MODEL_ALIASES` (above).
- In `make_model`, keep the provider default `provider or os.environ.get("SIMCOACH_MODEL_PROVIDER") or "anthropic"` (env override for provider is allowed), then:

```python
    if provider == "fake":
        from .fake import FakeModel
        return FakeModel()
    if provider == "anthropic":
        key = api_key or get_api_key()
        if not key:
            raise RuntimeError("No Anthropic API key set — add one in Settings.")
        model_id = MODEL_ALIASES.get(model or "sonnet", model or "claude-sonnet-5")
        return ClaudeModel(model=model_id, api_key=key)
    # openai/gemini/local keep their existing NotImplementedError branches
```

- Add the wrapper:

```python
class LazyModel:
    """Model that defers make_model() until first use, so a server can boot with
    no API key set and only fail when the coach is actually called."""
    def __init__(self, provider=None, model=None):
        self._provider, self._model, self._impl = provider, model, None

    def _get(self):
        if self._impl is None:
            self._impl = make_model(provider=self._provider, model=self._model)
        return self._impl

    def complete(self, system, messages):
        return self._get().complete(system, messages)

    def run(self, system, messages, tools, dispatch):
        return self._get().run(system, messages, tools, dispatch)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_factory.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add simcoach/model/factory.py tests/test_factory.py
git commit -m "feat(model): key from keyring layer, model aliases, LazyModel wrapper"
```

---

### Task 5: coach_chat migration — add chat_id (schema v4→v5)

**Files:**
- Modify: `simcoach/store.py` (schema constants ~13–32, migration block ~80–99, `save_chat` ~204, add new methods)
- Modify: `simcoach/session.py` (CoachSession mints/uses a `chat_id`)
- Modify: `simcoach/app.py` (create a chat at REPL start, pass to CoachSession)
- Test: `tests/test_store.py` (append new tests + fix line 166 caller)
- Modify (caller fixes): `tests/test_session_persist.py` (lines 27, 36, 37)

**Interfaces:**
- Produces (store):
  - `SCHEMA_VERSION = 5`; migration `< 5` adds `chat_id TEXT` to `coach_chat` and sets existing rows to `"legacy"`.
  - `create_chat() -> str` — mints and returns a new chat id with a uuid suffix to avoid same-second collisions: `f"chat-{time.strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:6]}"` (import `from uuid import uuid4`). No row needed (chat is implied by its messages).
  - `save_chat(self, chat_id: str, session_id: str, track, car, lap_id, question, answer, tool_calls=None, ts=None) -> None` — `chat_id` is now the first positional arg.
  - `list_chats(self) -> list[dict]` — one entry per `chat_id`: keys `("chat_id", "started", "last", "message_count", "tracks")`, newest last-activity first.
  - `chat_messages(self, chat_id: str) -> list[dict]` — keys `("ts","session_id","track","car","lap_id","question","answer","tool_calls")`, tool_calls JSON-decoded, chronological.
- Consumes (session/app): `store.create_chat`, updated `save_chat`. `CoachSession.__init__` gains `chat_id: str | None = None` (defaulted so the 5 existing `CoachSession(...)` constructions in `tests/test_session.py:16,32,41` and `tests/test_session_persist.py:47,70` keep working); `CoachSession.ask` lazily mints one on first use if still `None`, so no message is ever stored with a NULL `chat_id`.

**Real store attribute names (verified — use these exactly, the earlier draft used wrong ones):** the connection is `self.conn` (NOT `self._con`), the data dir is `self.data_dir` (NOT `self._data_dir`), the lock is `self._lock`. The existing `__init__` migration uses `self.conn.execute(...)` with no `cur` variable. Mirror the file's existing `save_chat`/`session_chat` commit idiom exactly.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_store.py  (append)
def test_migration_wraps_legacy_rows(tmp_path):
    import sqlite3
    from simcoach.store import LapStore
    db = str(tmp_path / "c.sqlite")
    # simulate a v4 DB with a legacy chat row lacking chat_id
    con = sqlite3.connect(db)
    con.execute("PRAGMA user_version=4")
    con.execute("""CREATE TABLE coach_chat(id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL, session_id TEXT, track TEXT, car TEXT, lap_id INTEGER,
        question TEXT, answer TEXT, tool_calls TEXT)""")
    con.execute("INSERT INTO coach_chat(ts,session_id,question,answer) VALUES(1.0,'s1','q','a')")
    con.commit(); con.close()

    store = LapStore(db, str(tmp_path / "data"))  # opening runs the migration
    chats = store.list_chats()
    assert len(chats) == 1
    assert chats[0]["chat_id"] == "legacy"
    assert chats[0]["message_count"] == 1


def test_new_chat_round_trip(tmp_path):
    from simcoach.store import LapStore
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    cid = store.create_chat()
    store.save_chat(cid, "s1", "Le Mans", "Corvette", None, "q1", "a1")
    store.save_chat(cid, "s2", "Le Mans", "Corvette", 7, "q2", "a2", tool_calls=[{"t": "get_corner"}])
    msgs = store.chat_messages(cid)
    assert [m["question"] for m in msgs] == ["q1", "q2"]
    assert msgs[1]["lap_id"] == 7
    assert msgs[1]["tool_calls"] == [{"t": "get_corner"}]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_store.py -k "migration_wraps or new_chat" -v`
Expected: FAIL — `create_chat`/`list_chats`/`chat_messages` don't exist; `save_chat` signature mismatch.

- [ ] **Step 3: Write minimal implementation**

In `store.py`:
- Bump `SCHEMA_VERSION = 5`. Add `chat_id TEXT` to `_COACH_CHAT_SCHEMA` (fresh installs get the column).
- In the migration block, after the `< 4` step, add:

```python
        if version < 5:
            cols = [r[1] for r in self.conn.execute("PRAGMA table_info(coach_chat)")]
            if "chat_id" not in cols:
                self.conn.execute("ALTER TABLE coach_chat ADD COLUMN chat_id TEXT")
            self.conn.execute("UPDATE coach_chat SET chat_id='legacy' WHERE chat_id IS NULL")
```

Note the `UPDATE ... WHERE chat_id IS NULL` is OUTSIDE the `if "chat_id" not in cols` guard so a half-applied migration still backfills. Also add, once, in `LapStore.__init__` right after the `sqlite3.connect(...)` line, a `PRAGMA busy_timeout` so the terminal harness and the server (two processes, two connections) wait instead of erroring on a locked DB:

```python
        self.conn.execute("PRAGMA busy_timeout=5000")
```

- Add methods (mirror the existing `save_chat`/`session_chat` locking style):

```python
    def create_chat(self) -> str:
        from uuid import uuid4
        return f"chat-{time.strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:6]}"

    def save_chat(self, chat_id, session_id, track, car, lap_id,
                  question, answer, tool_calls=None, ts=None) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO coach_chat(ts,chat_id,session_id,track,car,lap_id,"
                "question,answer,tool_calls) VALUES(?,?,?,?,?,?,?,?,?)",
                (ts if ts is not None else time.time(), chat_id, session_id, track,
                 car, lap_id, question, answer,
                 json.dumps(tool_calls) if tool_calls is not None else None))
            self.conn.commit()

    def list_chats(self) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT chat_id, MIN(ts), MAX(ts), COUNT(*), "
                "GROUP_CONCAT(DISTINCT track) FROM coach_chat "
                "GROUP BY chat_id ORDER BY MAX(ts) DESC").fetchall()
        return [{"chat_id": r[0], "started": r[1], "last": r[2],
                 "message_count": r[3], "tracks": r[4]} for r in rows]

    def chat_messages(self, chat_id: str) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT ts,session_id,track,car,lap_id,question,answer,tool_calls "
                "FROM coach_chat WHERE chat_id=? ORDER BY ts", (chat_id,)).fetchall()
        return [{"ts": r[0], "session_id": r[1], "track": r[2], "car": r[3],
                 "lap_id": r[4], "question": r[5], "answer": r[6],
                 "tool_calls": json.loads(r[7]) if r[7] else None} for r in rows]
```

(Attribute names above (`self.conn`, `self._lock`) are the real ones — do not "correct" them back to `self._con`. Match the file's existing commit idiom for `save_chat`.)

**Update `session.py` and `app.py`:**
- `session.py`: `CoachSession.__init__` gains `chat_id: str | None = None`; store it. In `ask(...)`, before `store.save_chat(...)`: `if self.chat_id is None: self.chat_id = self.store.create_chat()`, then pass `self.chat_id` as the FIRST arg to `save_chat`.
- `app.py`: in `run_repl`, before the question loop, `chat_id = store.create_chat()`; pass `chat_id=chat_id` into `CoachSession(...)`.

**Update the existing callers broken by the chat_id-first signature (REQUIRED to keep the suite green — verified call sites):**
- `tests/test_store.py:166` — the `s.save_chat("s1","t","c",None,"q","a")` call: add a leading `chat_id` arg, e.g. `s.save_chat(s.create_chat(), "s1", "t", "c", None, "q", "a")` and adjust its assertions (it currently looks up by `session_chat("s1")`, which still works since `session_id="s1"` is unchanged).
- `tests/test_session_persist.py:27, 36, 37` — the three direct `save_chat(...)` calls: prepend a `chat_id` arg the same way.
- The `CoachSession(...)` constructions at `tests/test_session.py:16,32,41` and `tests/test_session_persist.py:47,70` need NO change (chat_id defaults to None and is minted lazily).
Run `python -m pytest -q` as the real gate before committing this task.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_store.py tests/test_session.py tests/test_session_persist.py -v`
Expected: PASS. Then the full suite is the real gate: `python -m pytest -q` — 158 baseline + new, ALL green. If any `save_chat`/`CoachSession` caller is still red, fix it before committing (the enumerated list above is exhaustive per the plan review, but grep `save_chat(` to be sure).

- [ ] **Step 5: Commit**

```bash
git add simcoach/store.py simcoach/session.py simcoach/app.py tests/test_store.py tests/test_session_persist.py
git commit -m "feat(store): decouple chat from race session via chat_id (schema v5)"
```

---

### Task 6: FastAPI app skeleton + dependency wiring + health

**Files:**
- Create: `simcoach/server/__init__.py` (empty)
- Create: `simcoach/server/app.py`
- Test: `tests/test_server_health.py`
- Modify: `pyproject.toml` (add `fastapi>=0.115`, `uvicorn>=0.30` to `dependencies`)

**Interfaces:**
- Produces:
  - `create_app(store, coach, cmap=None, rest=None) -> fastapi.FastAPI` — a factory so tests inject a fixture store + `FakeModel`-backed `Coach`. Stores the deps on `app.state`.
  - `GET /health` → `{"status": "ok"}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_server_health.py
from fastapi.testclient import TestClient
from simcoach.server.app import create_app
from simcoach.store import LapStore
from simcoach.coach import Coach
from simcoach.model.fake import FakeModel


def _client(tmp_path):
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    coach = Coach(FakeModel(), "simcoach/knowledge")
    return TestClient(create_app(store, coach), base_url="http://127.0.0.1")


def test_health(tmp_path):
    r = _client(tmp_path).get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_spoofed_host_forbidden(tmp_path):
    r = _client(tmp_path).get("/health", headers={"host": "evil.com"})
    assert r.status_code == 403
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_server_health.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'simcoach.server'` (install `fastapi`/`uvicorn` into `.venv` if import fails).

- [ ] **Step 3: Write minimal implementation**

```python
# simcoach/server/app.py
"""Localhost FastAPI adapter over the synchronous core. Handlers are plain def
(FastAPI runs them in a threadpool), matching the blocking store/coach. Binds to
127.0.0.1 only, validates the Host header (DNS-rebinding defense), and disables
the docs/openapi surface. Reuses store/coach/analysis unchanged."""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

ALLOWED_HOSTS = {"127.0.0.1", "localhost"}  # port stripped before compare


def create_app(store, coach, cmap=None, rest=None) -> FastAPI:
    app = FastAPI(title="SimCoach", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    app.state.coach = coach
    app.state.cmap = cmap
    app.state.rest = rest

    @app.middleware("http")
    async def _host_guard(request: Request, call_next):
        host = (request.headers.get("host") or "").split(":")[0]
        if host and host not in ALLOWED_HOSTS:
            return JSONResponse({"detail": "forbidden host"}, status_code=403)
        return await call_next(request)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    return app
```

The Host guard is `async` (middleware must be) even though the route handlers stay sync — that's fine, FastAPI supports mixing. Test it: a request with `headers={"host": "evil.com"}` gets 403; the default `TestClient` host (`testserver`)... note the `TestClient` sends `Host: testserver`, so **add `testserver` to `ALLOWED_HOSTS`** OR construct `TestClient(app, base_url="http://127.0.0.1")` in every server test so the allowed host is sent. Use the `base_url` approach (keeps `ALLOWED_HOSTS` production-honest) and add one test asserting a spoofed Host → 403.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_server_health.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add simcoach/server/__init__.py simcoach/server/app.py tests/test_server_health.py pyproject.toml
git commit -m "feat(server): FastAPI app factory + health endpoint"
```

---

### Task 7: Read endpoints — sessions, laps, analysis, setup, chats

**Files:**
- Modify: `simcoach/store.py` (add two new read methods — see below)
- Modify: `simcoach/server/app.py`
- Test: `tests/test_server_reads.py`

**Interfaces:**
- New store methods this task adds (race sessions come from the `laps` table, NOT `recent_session_ids`, which reads `coach_chat` and would miss capture-only sessions):
  - `recent_race_sessions(self, limit: int = 20) -> list[dict]` — keys `("session_id","track","car","laps","best","started","last")`:
    ```sql
    SELECT session_id, track, car, COUNT(*),
           MIN(CASE WHEN valid THEN lap_time END), MIN(ts), MAX(ts)
    FROM laps WHERE session_id IS NOT NULL
    GROUP BY session_id ORDER BY MAX(ts) DESC LIMIT ?
    ```
    (`best` is null when the session has no valid lap.)
  - `session_laps(self, session_id: str) -> list[dict]` — keys `("id","lap_time","valid","ts")`:
    `SELECT id, lap_time, valid, ts FROM laps WHERE session_id=? ORDER BY ts`.
- Consumes: the two methods above; `store.load`, `store.best_lap`, `store.list_chats`, `store.chat_messages`; `analysis.analyze_lap(record, cmap, reference)`; `rest.get_setup()` (optional; `{}` if `rest` is None). Corner map from `app.state.cmap`.
- Produces endpoints:
  - `GET /sessions` → `store.recent_race_sessions()`.
  - `GET /sessions/{sid}/laps` → `store.session_laps(sid)` → `[{"id","lap_time","valid","ts"}]` (matches the spec's shape).
  - `GET /laps/{lap_id}/analysis` → per-corner metrics dict from `analyze_lap`, **NaN-sanitized** (see code); 404 if lap or cmap missing. Corner identity (no/name/direction) comes from the map — authoritative, never inferred client-side.
  - `GET /setup` → `rest.get_setup()` raw dict (or `{}` when no rest). NOTE: spec wants a *filtered* setup; filtering is deferred to the Setup-panel phase (Phase 4) — Phase 1 returns the raw dict. Flag in the endpoint docstring.
  - `GET /chats` → `store.list_chats()`; `GET /chats/{chat_id}` → `store.chat_messages(chat_id)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_server_reads.py
from fastapi.testclient import TestClient
from simcoach.server.app import create_app
from simcoach.store import LapStore
from simcoach.coach import Coach
from simcoach.model.fake import FakeModel
from simcoach.corners import CornerMap


def _app(tmp_path, synthetic_lap):
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    lap_id = store.save(synthetic_lap)
    cid = store.create_chat()
    store.save_chat(cid, synthetic_lap.session_id or "s1", synthetic_lap.track,
                    synthetic_lap.car, lap_id, "q", "a")
    coach = Coach(FakeModel(), "simcoach/knowledge")
    return create_app(store, coach), lap_id, cid


def _tc(app):
    return TestClient(app, base_url="http://127.0.0.1")


def test_chats_and_messages(tmp_path, synthetic_lap):
    app, lap_id, cid = _app(tmp_path, synthetic_lap)
    c = _tc(app)
    chats = c.get("/chats").json()
    assert any(x["chat_id"] == cid for x in chats)
    msgs = c.get(f"/chats/{cid}").json()
    assert msgs[0]["question"] == "q"


def test_setup_empty_without_rest(tmp_path, synthetic_lap):
    app, *_ = _app(tmp_path, synthetic_lap)
    assert _tc(app).get("/setup").json() == {}


def test_sessions_from_laps_table(tmp_path, synthetic_lap):
    # synthetic_lap has no session_id; save() a lap under a known session_id.
    # Read tests/conftest.py + store.save to set session_id on the record first,
    # then assert /sessions lists it with laps>=1. (Grounding note, not a guess.)
    app, lap_id, cid = _app(tmp_path, synthetic_lap)
    rows = _tc(app).get("/sessions").json()
    assert isinstance(rows, list)
```

(Extend with `/sessions`, `/sessions/{id}/laps`, and `/laps/{id}/analysis` asserts once the `synthetic_lap` fixture's `track`/`car`/`session_id` values are confirmed — read `tests/conftest.py` to use the real field values; do not invent them.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_server_reads.py -v`
Expected: FAIL — 404s / missing routes.

- [ ] **Step 3: Write minimal implementation**

Add the routes to `create_app` using the store methods above. For analysis:

```python
    import math
    from dataclasses import asdict

    def _clean(v):
        # CornerMetrics.min_spd et al. can be float('nan') on real data;
        # NaN/Inf are invalid JSON, so map non-finite floats to None.
        if isinstance(v, float) and not math.isfinite(v):
            return None
        if isinstance(v, dict):
            return {k: _clean(x) for k, x in v.items()}
        if isinstance(v, list):
            return [_clean(x) for x in v]
        return v

    @app.get("/laps/{lap_id}/analysis")
    def lap_analysis(lap_id: int):
        from fastapi import HTTPException
        from ..analysis import analyze_lap
        if app.state.cmap is None:
            raise HTTPException(404, "no corner map")
        try:
            rec = app.state.store.load(lap_id)
        except Exception:
            raise HTTPException(404, "lap not found")
        ref = app.state.store.best_lap(rec.track, rec.car, exclude_lap_id=lap_id)
        a = analyze_lap(rec, app.state.cmap, reference=ref)
        return _clean({
            "lap_time": a.lap_time,
            "corners": [asdict(c) for c in a.corners],
        })
```

Use `dataclasses.asdict` for `CornerMetrics`. For `/setup`: `app.state.rest.get_setup() if app.state.rest else {}`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_server_reads.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add simcoach/store.py simcoach/server/app.py tests/test_server_reads.py
git commit -m "feat(server): read endpoints for sessions, laps, analysis, setup, chats"
```

---

### Task 8: Settings endpoints + data reset

**Files:**
- Modify: `simcoach/server/app.py`
- Test: `tests/test_server_settings.py`

**Interfaces:**
- Consumes: `config.load_config`, `config.save_config`, `config.DEFAULTS`; `secrets.set_api_key`, `secrets.has_api_key`; `paths.app_data_dir`; a new `store.clear_all() -> None` (delete all rows in `laps`, `corner_metrics`, `coach_chat`, `setups` and remove `.npz` files from `self.data_dir`).
- Produces endpoints:
  - `GET /settings` → `load_config()` merged with `{"api_key_set": secrets.has_api_key(), "data_dir": str(paths.app_data_dir()), "app_version": <version>, "lmu_build": None}`. Never includes the key. (`app_version` from `importlib.metadata.version("simcoach")` guarded in try/except → `"dev"`; `lmu_build` stays `None` until the live-status WS in a later phase.)
  - `PUT /settings` → body is a config dict; **whitelist to `config.DEFAULTS` keys before saving**: `save_config({k: body[k] for k in DEFAULTS if k in body})`. This drops `api_key_set`/`data_dir` echoes AND — critically — refuses any `api_key`/`key` field, so a secret can never reach the JSON on disk. Returns the reloaded GET-shaped config.
  - `POST /settings/api-key` → body `{"key": "..."}`; `secrets.set_api_key(key)`; returns `{"api_key_set": True}`. Rejects an empty/missing key with 400.
  - `POST /settings/test-key` → builds a model via `make_model(...)` and makes one cheap `complete(...)` call; returns `{"ok": bool, "error": str|None}`. Wrapped in try/except — never raises, and `str(e)` from anthropic does not contain the key.
  - `DELETE /data` → **confirm-gated**: requires body `{"confirm": true}`; without it returns 400 `{"detail": "confirmation required"}` and deletes nothing. With it, `store.clear_all()`; returns `{"cleared": True}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_server_settings.py
from fastapi.testclient import TestClient
from simcoach.server.app import create_app
from simcoach.store import LapStore
from simcoach.coach import Coach
from simcoach.model.fake import FakeModel


def _client(tmp_path, monkeypatch, mem_keyring):  # mem_keyring: conftest fixture (Task 3)
    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    app = create_app(store, Coach(FakeModel(), "simcoach/knowledge"))
    return TestClient(app, base_url="http://127.0.0.1"), store


def test_get_settings_hides_key(tmp_path, monkeypatch, mem_keyring):
    c, _ = _client(tmp_path, monkeypatch, mem_keyring)
    body = c.get("/settings").json()
    assert body["api_key_set"] is False
    assert "key" not in body and "api_key" not in body
    assert body["units"] == "metric"


def test_put_settings_persists_and_rejects_secret(tmp_path, monkeypatch, mem_keyring):
    c, _ = _client(tmp_path, monkeypatch, mem_keyring)
    # include a bogus secret + echoed non-config keys; server must drop them all
    c.put("/settings", json={"units": "imperial", "api_key": "sk-should-not-persist",
                             "api_key_set": True, "data_dir": "/tmp"})
    body = c.get("/settings").json()
    assert body["units"] == "imperial"
    assert body["api_key_set"] is False  # the secret never reached storage
    # and it never landed in the JSON file
    from simcoach import paths
    assert "sk-should-not-persist" not in paths.config_path().read_text()


def test_delete_requires_confirmation(tmp_path, monkeypatch, mem_keyring, synthetic_lap):
    c, store = _client(tmp_path, monkeypatch, mem_keyring)
    store.save(synthetic_lap)
    r = c.request("DELETE", "/data")  # no confirm body
    assert r.status_code == 400
    assert store.list_lap_ids(synthetic_lap.track, synthetic_lap.car)  # untouched


def test_delete_data_clears_with_confirm(tmp_path, monkeypatch, mem_keyring, synthetic_lap):
    c, store = _client(tmp_path, monkeypatch, mem_keyring)
    store.save(synthetic_lap)
    r = c.request("DELETE", "/data", json={"confirm": True})
    assert r.json() == {"cleared": True}
    assert store.list_lap_ids(synthetic_lap.track, synthetic_lap.car) == []


def test_test_key_never_raises(tmp_path, monkeypatch, mem_keyring):
    c, _ = _client(tmp_path, monkeypatch, mem_keyring)
    # no key set -> make_model raises -> endpoint returns ok:false, not 500
    r = c.post("/settings/test-key")
    assert r.status_code == 200
    assert r.json()["ok"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_server_settings.py -v`
Expected: FAIL — routes / `store.clear_all` missing.

- [ ] **Step 3: Write minimal implementation**

Add `LapStore.clear_all(self)`:

```python
    def clear_all(self) -> None:
        with self._lock:
            for t in ("corner_metrics", "coach_chat", "laps", "setups"):
                self.conn.execute(f"DELETE FROM {t}")
            self.conn.commit()
        from pathlib import Path
        for f in Path(self.data_dir).glob("*.npz"):  # self.data_dir is the real attr
            f.unlink(missing_ok=True)
```

Add the routes:
- `PUT /settings`: `save_config({k: body[k] for k in config.DEFAULTS if k in body})` — the whitelist is the secret-leak guard; then return the GET-shaped config.
- `POST /settings/test-key`: wrap `make_model(provider=cfg.get_provider_or_default, model=cfg["model"]).complete("ping", [{"role":"user","content":"ping"}])` in `try/except Exception as e: return {"ok": False, "error": str(e)}`; on success `{"ok": True, "error": None}`.
- `DELETE /data`: read the JSON body; if `not body.get("confirm")` → `HTTPException(400, "confirmation required")`; else `store.clear_all()`.
- `POST /settings/api-key`: `key = body.get("key","").strip()`; if not key → 400; else `secrets.set_api_key(key)`.

The `DELETE`/`PUT` handlers need the request body — use a Pydantic model or `body: dict = Body(...)`; keep handlers `def` (sync) — FastAPI still parses the body.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_server_settings.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add simcoach/server/app.py simcoach/store.py tests/test_server_settings.py
git commit -m "feat(server): settings + api-key + test-key + data reset endpoints"
```

---

### Task 9: Server entry point (sidecar) wired to the real core

**Files:**
- Create: `simcoach/server/__main__.py`
- Test: `tests/test_server_entry.py`

**Interfaces:**
- Consumes: `paths.db_path`/`telemetry_dir`, `factory.LazyModel`, `config.load_config`, `Coach`; existing `__main__` helpers for track/car resolution + corner-map load (reuse, do not duplicate).
- Produces: `build_app() -> FastAPI` — constructs store from app-data paths and `Coach(LazyModel(model=cfg["model"]), knowledge_dir)` (LazyModel so a machine with **no key** still boots), resolves the corner map/rest if LMU is up (degrade to `cmap=None`/`rest=None` when not), and returns `create_app(...)`. `main()` runs `uvicorn.run(build_app(), host="127.0.0.1", port=8765)`.

- [ ] **Step 1: Write the failing test** (honest offline: NO provider override, NO key — this is the fresh-install scenario)

```python
# tests/test_server_entry.py
def test_build_app_offline_no_key(tmp_path, monkeypatch, mem_keyring):
    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("SIMCOACH_MODEL_PROVIDER", raising=False)  # real default = anthropic
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)         # no key anywhere
    from simcoach.server.__main__ import build_app
    app = build_app()  # must NOT raise despite no key + no LMU
    from fastapi.testclient import TestClient
    tc = TestClient(app, base_url="http://127.0.0.1")
    assert tc.get("/health").json() == {"status": "ok"}
    assert tc.get("/settings").json()["api_key_set"] is False  # reachable to enter a key
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_server_entry.py -v`
Expected: FAIL — no `build_app` (and, if you wrongly use `make_model` eagerly, a `RuntimeError: No Anthropic API key set` proving the show-stopper).

- [ ] **Step 3: Write minimal implementation**

`build_app()`:
```python
def build_app():
    from ..config import load_config
    from ..paths import db_path, telemetry_dir
    from ..store import LapStore
    from ..coach import Coach
    from ..model.factory import LazyModel
    from .. import __main__ as m  # reuse resolution helpers, don't duplicate
    cfg = load_config()
    store = LapStore(str(db_path()), str(telemetry_dir()))
    coach = Coach(LazyModel(model=cfg["model"]), str(m.PKG_DIR / "knowledge"))
    cmap = rest = None
    try:
        tc = m.read_session_track_car()          # None when LMU is closed
        if tc:
            # reuse __main__'s resolve_track_map + CornerMap.load + LmuRest wiring
            cmap, rest = m.resolve_live_context(tc)   # extract this helper if not present
    except Exception:
        cmap = rest = None                        # offline: degrade, never raise
    return create_app(store, coach, cmap, rest)


def main():
    import uvicorn
    uvicorn.run(build_app(), host="127.0.0.1", port=8765)
```
The `LazyModel` is the key change: no `make_model` call at boot, so no key is required to start and serve `/settings`. The RuntimeError only fires if/when a coach endpoint (Phase 2) is actually invoked with no key.

**Known Phase-1 limitation (documented, not fixed here):** `build_app` resolves `cmap` once at startup. The terminal harness's live `maybe_reresolve` (re-point the corner map when the driver changes track mid-session, `app.py:65-94`) is NOT ported to the server. For Phase 1 no endpoint needs live re-resolution (there is no live coach/telemetry endpoint yet); it lands with the live session-bar WS in Phase 2/3. Add a one-line `# ponytail: cmap fixed at boot; live re-resolution arrives with WS /telemetry (Phase 2/3)` note at the resolution site.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_server_entry.py -v`
Expected: PASS. Then full suite `python -m pytest -q` — all green.

- [ ] **Step 5: Commit**

```bash
git add simcoach/server/__main__.py tests/test_server_entry.py
git commit -m "feat(server): sidecar entry point (build_app + uvicorn on 127.0.0.1)"
```

---

## Self-Review

**Spec coverage (Phase 1 scope):**
- Installer/no-env-vars (decision 5) → Tasks 1–4 (app-data paths, config precedence, keyring secrets, make_model). ✓
- BYO key (decision 6) → Tasks 3, 4, 8 (`/settings/api-key`, `test-key`). ✓
- Config layer + `save_config` + app-data paths → Tasks 1–2. ✓
- `chat_id` migration wrapping legacy rows (data-model section) → Task 5. ✓
- Server read endpoints (`/sessions`, `/sessions/{id}/laps`, `/laps/{id}/analysis`, `/setup`, `/chats`, `/chats/{id}`) → Tasks 6–7. ✓
- Settings endpoints (`GET/PUT /settings`, api-key, test-key, `DELETE /data`) → Task 8. ✓
- Sidecar launch (Tauri will spawn this) → Task 9. ✓
- Corner identity authoritative from the map, never inferred → enforced in Task 7 analysis payload. ✓
- Deferred to later phases (out of this plan): `WS /coach` streaming (Phase 2), `WS /telemetry` (Phase 2/3), Svelte/Tauri frontend + Settings screen UI (Phases 3–5), packaging (Phase 6). ✓

**Placeholder scan:** Two tasks (7, 8) intentionally say "read the real fixture/attribute names before finalizing" — these are *grounding* instructions, not deferred implementation: the conftest `synthetic_lap` fields and the store's connection/lock/data-dir attribute names must be quoted from the actual files (the executor has them open), not guessed. All code steps carry runnable code.

**Type consistency:** `save_chat(chat_id, session_id, ...)` — chat_id-first — is used consistently in Tasks 5, 7, 8. `create_app(store, coach, cmap=None, rest=None)` consistent across Tasks 6–9. `make_model(provider, model, api_key)` signature unchanged (Task 4). `load_config`/`save_config(cfg, path)` consistent (Task 2, 8).

## Plan-review fold-in (2026-09-02, critic + architect red-team)

Folded before execution:
- **Show-stopper:** `LazyModel` (Task 4) + Task 9 use it → server boots with no key so the user can reach Settings; offline test now uses the *real* default provider + no key (was masked by `provider=fake`).
- **Secret leak:** `PUT /settings` whitelists to `config.DEFAULTS` keys (Task 8) → a `key`/`api_key` field can never reach the JSON; test asserts it.
- **Remote abuse:** Host-header allowlist middleware + `docs_url/openapi_url=None` (Task 6); `DELETE /data` confirm-gated (Task 8).
- **False green gate:** Task 5 enumerates the exact broken callers (`test_store.py:166`, `test_session_persist.py:27,36,37`) and defaults `CoachSession.chat_id=None` (minted lazily) so the 5 construction sites survive; `python -m pytest -q` is the gate.
- **Correctness:** real store attrs `self.conn`/`self.data_dir` (not `_con`/`_data_dir`); module-level `ClaudeModel` import so the Task 4 monkeypatch binds; `/sessions` sourced from a new `recent_race_sessions()` over the `laps` table (not `recent_session_ids` on `coach_chat`); `/sessions/{id}/laps` returns `{id,lap_time,valid,ts}` per spec; NaN→None in the analysis payload; `create_chat` uuid suffix; `PRAGMA busy_timeout` for cross-process; keyring reads guarded + shared `mem_keyring` conftest fixture.
- **Deferred with a note (not fixed in Phase 1):** filtered `/setup` (→ Setup-panel phase); live `cmap` re-resolution (→ WS phase); `lmu_build` field (→ live-status phase).

## Execution notes for the orchestrator

- **Dependency install:** Tasks 3/6 add `keyring`, `fastapi`, `uvicorn` to `pyproject.toml`; install into `.venv` (`.venv/Scripts/python.exe -m pip install -e .`) before those tasks' tests run.
- **Verification lanes:** Task 5 (the migration — data-loss risk) gets a dedicated Opus verification pass against a real pre-existing DB copy. Tasks 1–4, 6–9 get a Sonnet verifier/reviewer pass. No self-approval.
- **Order:** 1→2→3→4 (config foundation), then 5 (migration, independent), then 6→7→8→9 (server, depends on 1–5).
