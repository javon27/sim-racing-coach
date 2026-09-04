# v1 Phase 2 — Streaming agentic `WS /coach` — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stream the agentic coach's answer over a localhost websocket — surfacing tool activity as it happens and delivering the answer text — reusing the Phase 1 core unchanged, with a lazy first cut (tool events + whole-answer block) and a token-delta upgrade as a scoped increment.

**Architecture:** Add a streaming event path to the synchronous `Model` interface (`run_stream(...)` yielding `tool`/`text`/`done` dicts) alongside the existing `run`/`complete`. `Coach.ask_agentic_stream` yields those events. A FastAPI `WebSocket /coach` endpoint bridges the sync generator to the async socket via a worker thread + `asyncio.Queue`, builds a `CoachContext` from the store/cmap/rest per request, streams events as JSON, and persists the final answer via `store.save_chat`. The core stays synchronous; only the server endpoint is async.

**Tech Stack:** Python ≥3.11, FastAPI/Starlette websockets, anthropic 1.x (`messages.stream` in the token-delta increment), pytest with `TestClient.websocket_connect`. Tests: `.venv/Scripts/python.exe -m pytest -q`.

**Spec:** `docs/superpowers/specs/2026-08-29-v1-standalone-ui-design.md` — "Agentic streaming" + Server API `WS /coach`. Builds on the Phase 1 plan `docs/superpowers/plans/2026-09-02-v1-phase1-server-config.md` (server, store chat_id, LazyModel).

## Global Constraints

- **Green bar = 220 tests** (current). Every task ends green via `.venv/Scripts/python.exe -m pytest -q`.
- **Core stays synchronous.** Do NOT rewrite `store`, `coach`, `coach_tools`, or `ClaudeModel` as async. Only the websocket endpoint is `async`; the agentic loop runs in a worker thread.
- **Event schema (JSON-serializable dicts), stable across the whole phase:**
  - `{"type": "tool", "name": <str>, "input": <dict>}` — emitted as the coach calls a data tool.
  - `{"type": "text", "text": <str>}` — answer text (one whole-block event in the first cut; many delta events after the token increment).
  - `{"type": "done", "chat_id": <str>}` — terminal success; carries the chat the message was persisted under.
  - `{"type": "error", "message": <str>}` — terminal failure; the socket then closes.
- **WS is NOT covered by the HTTP Host-guard middleware** (Starlette http middleware skips the websocket scope). The `/coach` handler MUST do its own Host + Origin check against `ALLOWED_HOSTS` and close with code 1008 on mismatch — same DNS-rebinding / cross-site-WS defense as the HTTP side.
- **Secret safety unchanged.** No key in any event or log. `LazyModel` means a no-key server still accepts the socket; the coach call then fails → emit one `error` event (don't crash the handler).
- **Persistence:** on `done`, persist exactly once via `store.save_chat(chat_id, session_id, track, car, lap_id, question, answer, tool_calls)`; mint `chat_id` with `store.create_chat()` when the client didn't supply one. `answer` = the accumulated text; `tool_calls` = `context.tool_calls`.
- **TDD + frequent commits.** Everything is unit-testable offline with `FakeModel` + `TestClient.websocket_connect`; real Anthropic token streaming is a marked `[LIVE]` validation, never faked green.

---

### Task 1: Streaming event contract + `Model.run_stream` + `FakeModel.run_stream`

**Files:**
- Modify: `simcoach/model/base.py` (add `run_stream` to the `Model` Protocol)
- Modify: `simcoach/model/fake.py` (add `run_stream`)
- Test: `tests/test_fake_stream.py`

**Interfaces:**
- Produces: `Model.run_stream(self, system: str, messages: list[dict], tools: list[dict], dispatch: Callable[[str, dict], str]) -> Iterator[dict]` — yields the event dicts from Global Constraints (no `done`/`error` here — the *coach/server* own those terminal markers; `run_stream` yields `tool` events then exactly one or more `text` events and returns). Wait: to keep the contract simple, `run_stream` yields only `tool` and `text` events and returns normally when finished; `done`/`error` are added by the server layer.
- `FakeModel.run_stream` mirrors `FakeModel.run`: for each scripted `(tool_name, input)` in `tool_script`, dispatch it and yield `{"type":"tool","name":tool_name,"input":input}`; then yield `{"type":"text","text":self.reply}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fake_stream.py
from simcoach.model.fake import FakeModel


def test_run_stream_emits_tool_then_text():
    seen = []
    fm = FakeModel(reply="Turn 1 looks good.",
                   tool_script=[("get_corner", {"corner": "1"})])
    events = list(fm.run_stream("sys", [{"role": "user", "content": "q"}],
                                tools=[], dispatch=lambda n, i: seen.append((n, i)) or "ok"))
    assert seen == [("get_corner", {"corner": "1"})]          # tool was dispatched
    assert events[0] == {"type": "tool", "name": "get_corner", "input": {"corner": "1"}}
    assert events[-1] == {"type": "text", "text": "Turn 1 looks good."}
    assert all(e["type"] in ("tool", "text") for e in events)  # no done/error here
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_fake_stream.py -v`
Expected: FAIL — `AttributeError: 'FakeModel' object has no attribute 'run_stream'`

- [ ] **Step 3: Write minimal implementation**

In `simcoach/model/base.py`, add to the `Model` Protocol (after `run`):

```python
    def run_stream(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
        dispatch: Callable[[str, dict], str],
    ) -> "Iterator[dict]":
        """Like `run`, but yield events as they occur instead of returning a string:
        {"type":"tool","name","input"} as each tool is called, then one or more
        {"type":"text","text"} events for the answer. Terminal done/error markers are
        added by the caller (coach/server), not here."""
        ...
```
(add `from typing import Iterator` to the imports.)

In `simcoach/model/fake.py`, add (read the real `FakeModel.__init__`/`run` first to match attribute names `reply`/`tool_script`):

```python
    def run_stream(self, system, messages, tools, dispatch):
        for name, inp in (self.tool_script or []):
            dispatch(name, inp)
            yield {"type": "tool", "name": name, "input": inp}
        yield {"type": "text", "text": self.reply}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_fake_stream.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add simcoach/model/base.py simcoach/model/fake.py tests/test_fake_stream.py
git commit -m "feat(model): run_stream event contract + FakeModel streaming"
```

---

### Task 2: `ClaudeModel.run_stream` (first cut: tool events + whole-answer text)

**Files:**
- Modify: `simcoach/model/claude.py`
- Modify: `simcoach/model/factory.py` (add `LazyModel.run_stream` — see Step 3b; without it every REAL `WS /coach` request errors, because the production server wires `Coach(LazyModel(...))`, not `FakeModel`)
- Test: `tests/test_claude_stream.py` (mock the anthropic client — no live call)

**Interfaces:**
- Consumes: the event contract (Task 1).
- Produces:
  - `ClaudeModel.run_stream(...)` — the SAME tool loop as `run`, but yields `{"type":"tool",...}` for each `tool_use` block (before dispatching, so the UI shows activity), and yields one `{"type":"text","text":_first_text(resp)}` when `stop_reason != "tool_use"` (and after the iteration-cap final call). `run` is re-implemented to drain `run_stream` and return the last text event's text — so the two paths can't diverge.
  - `LazyModel.run_stream(self, system, messages, tools, dispatch)` — delegates to the built model. **Must be a generator** so the no-key `RuntimeError` from `make_model` surfaces lazily on iteration (as an `error` event), not at call time: `yield from self._get().run_stream(system, messages, tools, dispatch)`.

- [ ] **Step 1: Write the failing test** (a fake anthropic client, no network)

```python
# tests/test_claude_stream.py
from simcoach.model.claude import ClaudeModel


class _Resp:
    def __init__(self, content, stop_reason): self.content = content; self.stop_reason = stop_reason


class _Text:
    type = "text"
    def __init__(self, t): self.text = t


class _ToolUse:
    type = "tool_use"
    def __init__(self, id, name, inp): self.id = id; self.name = name; self.input = inp


class _FakeClient:
    """Scripts two turns: a tool_use, then a final text."""
    def __init__(self):
        self._turns = [
            _Resp([_ToolUse("t1", "get_corner", {"corner": "1"})], "tool_use"),
            _Resp([_Text("Carry more speed through Turn 1.")], "end_turn"),
        ]
        self.messages = self
    def create(self, **kw):
        return self._turns.pop(0)


def _model():
    m = ClaudeModel.__new__(ClaudeModel)          # bypass __init__ (no real anthropic client)
    m.model = "x"; m.max_tokens = 100; m.thinking = False
    m.client = _FakeClient()
    return m


def test_run_stream_yields_tool_then_text():
    m = _model()
    calls = []
    events = list(m.run_stream("sys", [{"role": "user", "content": "q"}],
                               tools=[{"name": "get_corner"}],
                               dispatch=lambda n, i: calls.append((n, i)) or "corner data"))
    assert calls == [("get_corner", {"corner": "1"})]
    assert events[0] == {"type": "tool", "name": "get_corner", "input": {"corner": "1"}}
    assert events[-1] == {"type": "text", "text": "Carry more speed through Turn 1."}


def test_run_drains_run_stream():
    m = _model()
    out = m.run("sys", [{"role": "user", "content": "q"}],
                tools=[{"name": "get_corner"}], dispatch=lambda n, i: "corner data")
    assert out == "Carry more speed through Turn 1."
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_claude_stream.py -v`
Expected: FAIL — no `run_stream`.

- [ ] **Step 3: Write minimal implementation**

In `simcoach/model/claude.py`, add `run_stream` mirroring the existing `run` loop but yielding, and re-point `run` at it:

```python
    def run_stream(self, system, messages, tools, dispatch):
        convo = list(messages)
        for _ in range(MAX_TOOL_ITERATIONS):
            resp = self.client.messages.create(
                model=self.model, max_tokens=self.max_tokens, system=system,
                messages=convo, tools=tools, thinking=self._thinking_param())
            if resp.stop_reason != "tool_use":
                yield {"type": "text", "text": _first_text(resp)}
                return
            convo.append({"role": "assistant", "content": resp.content})
            results = []
            for block in resp.content:
                if getattr(block, "type", None) != "tool_use":
                    continue
                yield {"type": "tool", "name": block.name, "input": block.input}
                try:
                    out = dispatch(block.name, block.input)
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": out})
                except Exception as e:
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": f"error: {type(e).__name__}: {e}", "is_error": True})
            convo.append({"role": "user", "content": results})
        # iteration cap: one final tool-free answer
        resp = self.client.messages.create(
            model=self.model, max_tokens=self.max_tokens, system=system,
            messages=convo, thinking=self._thinking_param())
        yield {"type": "text", "text": _first_text(resp)}

    def run(self, system, messages, tools, dispatch):
        text = ""
        for ev in self.run_stream(system, messages, tools, dispatch):
            if ev["type"] == "text":
                text = ev["text"]
        return text
```
Delete the old `run` body (replaced above). `run` now returns the last text event — behaviorally identical to before for the first cut (one text event), and correct once the token increment yields deltas (last-wins → full text; the token increment will change `run` to accumulate — see Task 5).

**Step 3b — `LazyModel.run_stream` (`simcoach/model/factory.py`).** Add, next to `LazyModel.complete`/`run`:

```python
    def run_stream(self, system, messages, tools, dispatch):
        yield from self._get().run_stream(system, messages, tools, dispatch)
```
It MUST be a generator (has `yield`), so `self._get()` — which calls `make_model` and can raise `RuntimeError("No Anthropic API key set — add one in Settings.")` — runs on first iteration inside the worker's try/except and becomes a clean `error` event, not a call-time crash. Add a test (in `tests/test_factory.py`) that a no-key `LazyModel().run_stream(...)`, when iterated, raises that `RuntimeError` (mirrors the existing `test_lazy_model_defers_construction`).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_claude_stream.py -v` then the model tests `.venv/Scripts/python.exe -m pytest tests/ -k "model or coach or claude or factory" -q`
Expected: PASS, no regression in existing coach/factory tests.

- [ ] **Step 5: Commit**

```bash
git add simcoach/model/claude.py simcoach/model/factory.py tests/test_claude_stream.py tests/test_factory.py
git commit -m "feat(model): ClaudeModel+LazyModel run_stream (tool+text events); run drains it"
```

---

### Task 3: `Coach.ask_agentic_stream`

**Files:**
- Modify: `simcoach/coach.py`
- Test: `tests/test_coach_stream.py`

**Interfaces:**
- Consumes: `Model.run_stream`; the same `TOOLS` + `context.dispatch` `ask_agentic` uses.
- Produces: `Coach.ask_agentic_stream(self, context, question, car, history=None, driver_profile=None, input_device=None, units="metric") -> Iterator[dict]` — builds the identical system prompt + messages as `ask_agentic` (reuse `_augment_system(AGENTIC_SYSTEM_PROMPT...)`), then `yield from self.model.run_stream(system, messages, TOOLS, context.dispatch)`. Does NOT persist (the caller does) and does NOT emit done/error.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_coach_stream.py
from simcoach.coach import Coach
from simcoach.model.fake import FakeModel
from simcoach.coach_tools import CoachContext


def test_ask_agentic_stream_passes_events_and_dispatches(tmp_path):
    from simcoach.store import LapStore
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    ctx = CoachContext(store, track="T", car="C")
    coach = Coach(FakeModel(reply="Brake later into 1.",
                            tool_script=[("get_corner", {"corner": "1"})]),
                  "simcoach/knowledge")
    events = list(coach.ask_agentic_stream(ctx, "where do I lose time?", car="C"))
    assert {"type": "tool", "name": "get_corner", "input": {"corner": "1"}} in events
    assert events[-1] == {"type": "text", "text": "Brake later into 1."}
    # dispatch records dicts (verified against CoachContext.dispatch), not tuples:
    assert {"name": "get_corner", "input": {"corner": "1"}} in ctx.tool_calls
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_coach_stream.py -v`
Expected: FAIL — no `ask_agentic_stream`.

- [ ] **Step 3: Write minimal implementation**

In `simcoach/coach.py`, add next to `ask_agentic`:

```python
    def ask_agentic_stream(self, context, question: str, car: str, history=None,
                           driver_profile=None, input_device=None, units: str = "metric"):
        from simcoach.coach_tools import TOOLS
        _, _, spd_u, temp_u = unit_fns(units)
        system = self._augment_system(
            AGENTIC_SYSTEM_PROMPT.format(car=car, speed_unit=spd_u, temp_unit=temp_u),
            context.allowed_adjustments, driver_profile, input_device,
        )
        messages = list(history) if history else []
        messages.append({"role": "user", "content": question})
        yield from self.model.run_stream(system, messages, TOOLS, context.dispatch)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_coach_stream.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add simcoach/coach.py tests/test_coach_stream.py
git commit -m "feat(coach): ask_agentic_stream yields tool/text events"
```

---

### Task 4: Server `WS /coach` — bridge, guard, context, persistence

**Files:**
- Modify: `simcoach/server/app.py`
- Test: `tests/test_server_ws.py`

**Interfaces:**
- Consumes: `coach.ask_agentic_stream`; `store.create_chat`/`save_chat`/`load`; `CoachContext`; `tiers.legal_adjustments`; `ALLOWED_HOSTS`.
- Produces: `WebSocket /coach`. Client sends one JSON `{question, session_id?, lap_id?, chat_id?, input_device?}`; server streams `tool`/`text` events, then a terminal `{"type":"done","chat_id":...}` (or `{"type":"error","message":...}`), then closes.

**Design — sync generator → async socket bridge (the risk area):** run `ask_agentic_stream` in a worker thread; push each event onto an `asyncio.Queue` via `loop.call_soon_threadsafe`; the async handler drains the queue and `await websocket.send_json(ev)` until a sentinel. This keeps the blocking Anthropic calls off the event loop.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_server_ws.py
from fastapi.testclient import TestClient
from simcoach.server.app import create_app
from simcoach.store import LapStore
from simcoach.coach import Coach
from simcoach.model.fake import FakeModel


def _client(tmp_path):
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    coach = Coach(FakeModel(reply="Brake later into Turn 1.",
                            tool_script=[("get_corner", {"corner": "1"})]),
                  "simcoach/knowledge")
    app = create_app(store, coach)
    return TestClient(app, base_url="http://127.0.0.1"), store


def test_ws_coach_streams_events_and_persists(tmp_path):
    client, store = _client(tmp_path)
    with client.websocket_connect("/coach") as ws:
        ws.send_json({"question": "where do I lose time?", "session_id": "s1"})
        events = []
        while True:
            ev = ws.receive_json()
            events.append(ev)
            if ev["type"] in ("done", "error"):
                break
    types = [e["type"] for e in events]
    assert "tool" in types and "text" in types and types[-1] == "done"
    chat_id = events[-1]["chat_id"]
    msgs = store.chat_messages(chat_id)
    assert msgs and msgs[0]["question"] == "where do I lose time?"
    assert msgs[0]["answer"] == "Brake later into Turn 1."


def test_ws_coach_rejects_bad_host(tmp_path):
    client, _ = _client(tmp_path)
    import pytest
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/coach", headers={"host": "evil.com"}) as ws:
            ws.receive_json()


def test_ws_coach_rejects_hostile_origin_good_host(tmp_path):
    # The real cross-site-WS attack: valid Host (TestClient sends 127.0.0.1) but a
    # hostile browser Origin. Must be rejected before accept.
    client, _ = _client(tmp_path)
    import pytest
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/coach", headers={"origin": "http://evil.com"}) as ws:
            ws.receive_json()


def test_ws_coach_malformed_message_errors_not_crash(tmp_path):
    client, _ = _client(tmp_path)
    with client.websocket_connect("/coach") as ws:
        ws.send_text("not json at all")
        ev = ws.receive_json()
    assert ev["type"] == "error"


def test_ws_coach_through_lazymodel(tmp_path, monkeypatch):
    # Covers the production delegation path (Coach(LazyModel(...))), which the
    # FakeModel tests bypass — this is where the missing run_stream bit.
    from simcoach.store import LapStore
    from simcoach.coach import Coach
    from simcoach.model import factory
    from simcoach.model.fake import FakeModel
    monkeypatch.setattr(factory, "make_model",
                        lambda provider=None, model=None: FakeModel(reply="ok"))
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    app = create_app(store, Coach(factory.LazyModel(), "simcoach/knowledge"))
    c = TestClient(app, base_url="http://127.0.0.1")
    with c.websocket_connect("/coach") as ws:
        ws.send_json({"question": "hi"})
        types = []
        while True:
            ev = ws.receive_json();  types.append(ev["type"])
            if ev["type"] in ("done", "error"): break
    assert types[-1] == "done" and "text" in types
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_server_ws.py -v`
Expected: FAIL — no `/coach` websocket route (connection rejected/404).

- [ ] **Step 3: Write minimal implementation**

Add a module-level helper (top of `simcoach/server/app.py`, `import urllib.parse`):

```python
def _bad_origin(origin: str | None) -> bool:
    """True if a browser Origin is present AND its host isn't allowed. A missing
    Origin (native/Tauri client, or a non-browser client) is allowed — only a
    browser sets Origin, and that's the cross-site-WS vector we're blocking.
    Port-blind by design (any localhost port is fine for a local desktop app)."""
    if not origin:
        return False
    host = (urllib.parse.urlparse(origin).hostname or "").lower()
    return host not in ALLOWED_HOSTS
```

Add to `create_app` (imports: `asyncio`, `threading`, `from fastapi import WebSocket`, `from starlette.websockets import WebSocketDisconnect, WebSocketState`, `from ..coach_tools import CoachContext`, `from ..tiers import legal_adjustments`):

```python
    @app.websocket("/coach")
    async def coach_ws(ws: WebSocket):
        # HTTP Host-guard middleware does NOT run for websockets — check here.
        host = (ws.headers.get("host") or "").split(":")[0].lower()
        if host not in ALLOWED_HOSTS or _bad_origin(ws.headers.get("origin")):
            await ws.close(code=1008)      # reject before accept
            return
        await ws.accept()

        # Read + validate the one request message. receive_json raises ValueError on
        # non-JSON; a non-dict body has no .get — guard both, don't crash the socket.
        try:
            req = await ws.receive_json()
        except WebSocketDisconnect:
            return
        except ValueError:
            await ws.send_json({"type": "error", "message": "invalid JSON"})
            await ws.close(code=1003);  return
        if not isinstance(req, dict):
            await ws.send_json({"type": "error", "message": "expected a JSON object"})
            await ws.close(code=1003);  return
        question = (req.get("question") or "").strip()
        if not question:
            await ws.send_json({"type": "error", "message": "empty question"})
            await ws.close();  return

        # Coerce lap_id ONCE (used for load AND persisted context stamp).
        lap_id = req.get("lap_id")
        try: lap_id = int(lap_id) if lap_id is not None else None
        except (TypeError, ValueError): lap_id = None
        session_id = req.get("session_id")

        record = None
        if lap_id is not None:
            try: record = app.state.store.load(lap_id)
            except Exception: record = None
        setup = {}
        if app.state.rest:
            try: setup = app.state.rest.get_setup()
            except Exception: setup = {}
        ctx = CoachContext(app.state.store, record=record, lap_id=lap_id, cmap=app.state.cmap,
                           setup=setup, on_track=record is not None,
                           allowed_adjustments=(legal_adjustments(setup) if setup else None),
                           session_id=session_id)
        car = ctx.car or "car"

        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()   # unbounded; localhost token rate is the ceiling
        _SENTINEL = object()

        def _emit(ev):
            # Worker may outlive a client disconnect / the test loop; swallow a
            # closed-loop RuntimeError so it dies quietly instead of tracebacking.
            try: loop.call_soon_threadsafe(q.put_nowait, ev)
            except RuntimeError: pass

        def worker():
            text = ""
            try:
                for ev in app.state.coach.ask_agentic_stream(
                        ctx, question, car=car, input_device=req.get("input_device")):
                    if ev["type"] == "text":
                        text = ev["text"]        # first cut: whole block; Task 5: accumulate deltas
                    _emit(ev)
                terminal = {"type": "done"}
            except Exception as e:
                # Surface our own guidance (no-key RuntimeError, bad input ValueError);
                # hide any other internal error text from the client.
                msg = str(e) if isinstance(e, (RuntimeError, ValueError)) else "coach error"
                terminal = {"type": "error", "message": msg}
            # Persist best-effort REGARDLESS of success, so the turn isn't lost from
            # history (matches CoachSession's persist-then-swallow pattern).
            try:
                chat_id = req.get("chat_id") or app.state.store.create_chat()
                app.state.store.save_chat(chat_id, session_id, ctx.track, ctx.car, lap_id,
                                          question, text, ctx.tool_calls)
                if terminal["type"] == "done":
                    terminal["chat_id"] = chat_id
            except Exception:
                pass
            _emit(terminal)
            _emit(_SENTINEL)

        threading.Thread(target=worker, daemon=True).start()
        try:
            while True:
                ev = await q.get()
                if ev is _SENTINEL:
                    break
                await ws.send_json(ev)
        except WebSocketDisconnect:
            pass          # client left mid-stream; worker still finishes + persists (known ceiling)
        finally:
            if ws.application_state != WebSocketState.DISCONNECTED:
                try: await ws.close()
                except RuntimeError: pass
```

**Known ceiling (documented, not fixed):** on a mid-stream client disconnect the daemon worker still runs the (billed) Anthropic call to completion and persists — no cancellation. Acceptable for a localhost single-user app; a stop-flag through the tool loop is deferred. Add a `# ponytail:` note at the worker.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_server_ws.py -v` then FULL suite `.venv/Scripts/python.exe -m pytest -q` → ≥220+new, no regressions.
Expected: PASS. (If `TestClient.websocket_connect` + the thread/queue bridge deadlocks, verify events are pushed via `call_soon_threadsafe` and the sentinel always fires in `finally`.)

- [ ] **Step 5: Commit**

```bash
git add simcoach/server/app.py tests/test_server_ws.py
git commit -m "feat(server): WS /coach streams tool/text/done + persists (host/origin guarded)"
```

---

### Task 5 (increment): token-level text deltas via Anthropic streaming

**Files:**
- Modify: `simcoach/model/claude.py`
- Test: `tests/test_claude_stream.py` (extend — fake client with a streamed final turn)

**Interfaces:**
- Changes `ClaudeModel.run_stream` so the FINAL answer turn uses `self.client.messages.stream(...)` and yields a `{"type":"text","text":<delta>}` per `text_stream` chunk instead of one whole-block event. Tool-use turns stay on `messages.create` (tool loop unchanged). `run` changes from "last text wins" to **accumulate** text deltas: `text += ev["text"]`.
- **Double-count guard:** the final turn yields deltas XOR a whole block — never both. Drain `stream.get_final_message()` ONLY to capture `stop_reason`/advance the loop; never re-yield its text (that would double the answer once `run` accumulates). The test asserts `sum(len(delta) for delta in text events) == len(final answer)`.
- **Fake-client approach (don't break Task 2's tests):** extend the SAME `_FakeClient` from Task 2 by adding a `stream(...)` context manager (with `text_stream` + `get_final_message()`) — do NOT rewrite Task 2's `create()`-only fixture. Both the create-loop (tool turns) and the stream (final turn) fakes coexist.

- [ ] **Step 1: Write the failing test**

Extend `_FakeClient` with a `stream(...)` context manager whose `text_stream` yields `["Brake ", "later ", "into 1."]` and a `get_final_message()` returning a text resp; assert `run_stream` yields three `text` delta events for the final turn and `run` returns the concatenation `"Brake later into 1."`.

```python
def test_run_stream_token_deltas():
    # _FakeClient.stream(...) yields deltas; final turn streams instead of create()
    m = _model_streaming()   # fake client scripted with a .stream() final turn
    events = [e for e in m.run_stream("s", [{"role":"user","content":"q"}], [], lambda n,i:"") ]
    text_events = [e["text"] for e in events if e["type"] == "text"]
    assert text_events == ["Brake ", "later ", "into 1."]
    assert m.run("s", [{"role":"user","content":"q"}], [], lambda n,i:"") == "Brake later into 1."
```

- [ ] **Step 2: Run → fail** (`run_stream` still yields one block; `run` returns only the last chunk).
- [ ] **Step 3: Implement** the `messages.stream` final-turn path + `run` accumulation. Guard so a provider without `.stream` (or the tool-loop path) still works. Keep the iteration-cap final call streamed too.
- [ ] **Step 4: Run** the stream tests + FULL suite green. **[LIVE] validation:** against a real key, drive one lap and confirm `WS /coach` streams tokens end-to-end (real Anthropic tool-use + streaming) — do NOT fake this; mark it a live check in HANDOFF.
- [ ] **Step 5: Commit** `feat(model): token-delta streaming for the final answer turn`.

---

## Non-goals for Phase 2

- **`WS /telemetry`** (live fuel/energy/on-track ticks for the session bar) — deferred to Phase 3 with the frontend, which is its only consumer and needs a live `LmuSource` wired into the server (Phase 1 resolves cmap/rest once at boot; live capture in the server is not yet wired).
- Multi-message conversational websocket sessions (one question per connection is fine for v1; the chat history lives in `coach_chat` and is replayed via `GET /chats/{id}`).
- Reconnect/resume of an in-flight stream.

## Self-Review

**Spec coverage:** streaming `WS /coach` yielding `tool`/`text`/`done` (Tasks 1–4) ✓; lazy fallback = whole-answer first (Tasks 1–4), token deltas after (Task 5) ✓; server relays events ✓; corner identity still authoritative (unchanged — the coach tools own it) ✓. `WS /telemetry` explicitly deferred with rationale ✓.

**Risk areas flagged:** the sync→async thread/queue bridge (Task 4) is the one non-obvious mechanism — its test exercises the full round-trip incl. persistence; the sentinel in `finally` guarantees the drain loop ends even on error/disconnect. WS bypassing the HTTP Host-guard is handled by an in-handler Host+Origin check (Global Constraints + Task 4).

**Type consistency:** event dicts identical across Tasks 1–5; `run_stream` signature identical in base/fake/claude; `ask_agentic_stream` mirrors `ask_agentic`'s signature.

## Plan-review fold-in (2026-09-02, Sonnet review + Opus red-team)

Folded before execution (both agents read the real code + Starlette 1.6.0 source):
- **CRITICAL:** `LazyModel.run_stream` added (Task 2 Step 3b) — production wires `Coach(LazyModel(...))`; without it every real WS request errored while FakeModel tests stayed green. + `test_ws_coach_through_lazymodel`.
- **HIGH:** malformed/non-JSON/non-dict first message now → clean `error` + close (was an uncaught `ValueError`/`AttributeError` crashing the socket). + `test_ws_coach_malformed_message_errors_not_crash`.
- **MAJOR:** Task 3 assertion fixed (`dispatch` records `{"name","input"}` dicts, not tuples); `_bad_origin` written in full + `test_ws_coach_rejects_hostile_origin_good_host` (the actual cross-site-WS attack — valid Host, hostile Origin).
- **MEDIUM:** `finally` close now state-guarded (avoids `RuntimeError` after client disconnect); worker `call_soon_threadsafe` wrapped (`_emit`) against a closed loop; persist is **best-effort regardless of success** so a failed turn isn't lost from history; error messages **curated** (surface our own no-key/ValueError guidance, hide other internals).
- **Task 5:** double-count guard (deltas XOR block) + extend the same fake client.

Confirmed sound by the reviews (do not re-litigate): the thread/queue+`call_soon_threadsafe` bridge under TestClient's anyio portal; close-before-accept raising `WebSocketDisconnect`; `run`-via-`run_stream` preserving the existing contract (no test calls `ClaudeModel.run` directly); worker-thread store access safe under `check_same_thread=False`+RLock; HTTP Host-middleware genuinely skipping the WS scope.

Documented ceilings (deliberately NOT built): no worker cancellation on mid-stream disconnect (wasted API spend, localhost single-user); unbounded queue; port-blind Origin.

## Execution notes for the orchestrator

- Order: 1→2→3→4 deliver a working streamed coach (whole-answer). Task 5 is an independent upgrade — can ship later / after a live key test.
- Review calibration: Task 4 (server WS, the bridge + the WS security guard) is the risky one → Opus review + red-team (WS Host/Origin bypass, thread/queue deadlock, disconnect leak, secret-in-error). Tasks 1–3 → one Sonnet review. Task 5 → Sonnet review + a `[LIVE]` note (real streaming can't be unit-verified).
- Fresh executor per task, plan+spec only.
