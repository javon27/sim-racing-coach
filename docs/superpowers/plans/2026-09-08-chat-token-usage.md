# Chat Token Usage + Cost Meter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make what the coach actually costs visible — real billed tokens summed across every tool-loop iteration, persisted per chat, priced from one table, and shown as an unobtrusive meter on the chat screen — plus a soft nudge when a chat gets long.

**Architecture:** Token accounting enters at the only place that sees a billed API response: `ClaudeModel._stream_turn`. Each model turn yields one `{"type": "usage", ...}` event, so the sum across all `MAX_TOOL_ITERATIONS + 1` calls is structural rather than bookkeeping. `Model.run` drains those into a `RunResult(text, usage)`. The WS `/coach` worker sums the usage events for the turn, persists them onto the `coach_chat` row it already writes (schema v12), and reports chat totals on the terminal `done` event. `simcoach/pricing.py` owns the one price table. The Svelte meter only renders numbers the core computed.

**Tech Stack:** Python 3 / FastAPI / pytest (backend); Svelte 5 + TypeScript + vitest (frontend). LLM behind the `Model` protocol (`simcoach/model/base.py`). SQLite via `simcoach/store.py`.

**Spec:** `docs/superpowers/specs/2026-09-08-chat-token-usage-design.md` (issue #39)

---

## Findings that correct the spec (verified against the code)

Read these before starting — three of them change what the tasks do.

1. **`CoachSession` is NOT on the desktop path.** Spec §2 says "`CoachSession` accumulates … and persists them to the `coach_chat` row". But `simcoach/server/app.py:1151` (`worker()`) never constructs a `CoachSession` — it calls `app.state.coach.ask_agentic_stream(...)` directly and rebuilds history from `store.chat_messages(chat_id)` (`app.py:1246-1256`). `CoachSession` has exactly one production caller: the terminal REPL (`simcoach/app.py:64`). **Therefore accumulation lives in the store (per-message columns, summed per chat), not on a session object** — that serves both clients, survives a sidecar restart, and lets the meter repopulate when the driver reopens a thread. `CoachSession` still gets usage threaded through it for REPL parity, but it is not the accumulator.

2. **Schema version is 11, not 5.** `store.py:41` is `SCHEMA_VERSION = 11` (v5 was the `chat_id` migration; v11 added the driver profile). **The next migration is v12.**

3. **No prompt caching is in use anywhere.** `grep -r cache_control simcoach/` returns nothing; `claude.py` sends no `cache_control`. So `cache_read`/`cache_write` will read 0 in practice today. Capture the fields anyway (they're free — the `usage` object carries them), but **do not** build caching as part of this issue. The ×9 re-send the spec calls a "known ceiling" is precisely what prompt caching fixes; that deserves its own issue (see Open Questions).

4. **The model can change mid-chat.** Settings exposes `model: opus|sonnet|haiku` (`config.py:16`) and `LazyModel` resolves it per server boot. A chat's cost is therefore not priceable from a single model id — **persist the model id on each `coach_chat` row** and price per row. The spec does not mention this.

5. **`run` currently returns `str`, and only 1 production caller + 4 test call sites exist** (`coach.py:269`; `tests/test_claude_stream.py:87,118,154,179`). Changing it to `RunResult` is cheap. `run_stream` has one production caller (`coach.ask_agentic_stream`) that passes events straight through, and `server/app.py:1265` `_emit(ev)`s **every** event to the websocket; `frontend/src/lib/coach.ts:60` `switch`es on `data.type` with no `default`, so an unknown event type is silently ignored. A new `usage` event is therefore backward-safe on every hop.

6. **Existing fake responses have no `.usage`.** `tests/test_claude_stream.py:4` `_Resp` has only `content`/`stop_reason`, and `FakeModel` returns a bare string. Read usage defensively (`getattr(resp, "usage", None)`) so a turn without it contributes zero rather than raising.

---

## Global Constraints

- **Accounting stays behind the `Model` boundary.** No `anthropic` import and no `response.usage` field access outside `simcoach/model/`. Everything downstream sees `TokenUsage` / `RunResult` only.
- **Pricing is data, in exactly one place.** `simcoach/pricing.py` holds the table; no `$`-per-token literal appears anywhere else (not in the server, not in the frontend). The frontend receives a computed number.
- **Unknown model → `None`, not 0.** A price miss must render as "—" / "cost unavailable". Returning `0.0` for an unpriced model is a lie the driver would act on. A deliberately free model (fake, future local) returns `0.0`.
- **UI-agnostic core.** The meter's tokens and dollars are computed in `simcoach/` and handed to the UI. `ChatPanel.svelte` formats and renders; it must not contain arithmetic over prices.
- **Migration is cumulative + idempotent.** Bump `SCHEMA_VERSION` to 12 and add an `if current_version < 12:` block using `_add_column`, guarded like the v4/v5 steps (a fresh `<3` DB creates `coach_chat` with the new columns already). Additive only — `.npz` lap files untouched.
- **Usage must never break an answer.** Accounting is best-effort throughout: a missing `usage`, an unknown model, or a failed persist degrades the meter, never the coach's reply. Mirror the existing persist-then-swallow pattern (`session.py:62-71`, `app.py:1276-1283`).
- **Env:** backend `.venv/Scripts/python.exe -m pytest -q` from the repo root; frontend `cd frontend && npm test`. Lint `.venv/Scripts/python.exe -m ruff check <files>` (line-length 100) — avoid **new** findings only; the repo carries pre-existing lint debt. (CLAUDE.md documents `pip install -e ".[dev]"`; earlier plans note running pytest directly from the root also works. Use whichever the worktree already has.)

---

# Phase 1 — The vertical slice: real billed tokens, priced, on screen (Tasks 1-5)

This is the slice to ship if the full spec is too much for one pass. It delivers issue #39's build-order items 1-4 and closes the deferred "token-usage estimator on the chat screen" backlog item. **Defers:** the Settings show/hide toggle (Task 6 — the meter just always shows cost), the soft long-chat nudge (Task 7), and per-session chat re-binding (Task 8, which needs a user decision anyway).

### Task 1: `TokenUsage` + usage out of the `Model` interface

**Files:**
- Modify: `simcoach/model/base.py` (`TokenUsage`, `RunResult`, protocol docstrings), `simcoach/model/claude.py` (`_stream_turn` yields a usage event; `run` returns `RunResult`), `simcoach/model/fake.py` (deterministic usage), `simcoach/model/factory.py` (`LazyModel.run` passthrough — no change needed beyond confirming), `simcoach/coach.py` (`ask_agentic` returns `RunResult`), `simcoach/session.py` (`CoachSession.ask` unwraps `.text`, records `.usage`)
- Test: `tests/test_claude_stream.py`, `tests/test_model_types.py` (new cases), `tests/test_session.py`

**Interfaces:**
- Produces, in `simcoach/model/base.py`:
  - `@dataclass(frozen=True) class TokenUsage` with `input: int = 0`, `output: int = 0`, `cache_read: int = 0`, `cache_write: int = 0`; `__add__` returning a summed `TokenUsage`; `total` property (`input + output + cache_read + cache_write`); `as_dict()`.
  - `@dataclass(frozen=True) class RunResult` with `text: str`, `usage: TokenUsage`.
  - New stream event shape, documented on the `run_stream` docstring: `{"type": "usage", "usage": {"input": int, "output": int, "cache_read": int, "cache_write": int}, "model": str}` — emitted once per model turn, after that turn's text/tool blocks.
- Consumes: `resp.usage.{input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens}` — read with `getattr(..., 0)`, **only** inside `claude.py`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_claude_stream.py`, give the fakes a usage object and assert the sum across iterations — this is the subtle part the spec flags as must-test:

```python
class _Usage:
    def __init__(self, i, o, cr=0, cw=0):
        self.input_tokens = i; self.output_tokens = o
        self.cache_read_input_tokens = cr; self.cache_creation_input_tokens = cw

# _Resp gains: def __init__(self, content, stop_reason, usage=None)  -> self.usage = usage

def test_run_sums_usage_across_tool_loop_iterations():
    """Two turns (tool_use then text) must bill as the SUM, not the last call."""
    from simcoach.model.base import TokenUsage
    m = ClaudeModel.__new__(ClaudeModel)
    m.model = "claude-sonnet-5"; m.max_tokens = 100; m.thinking = False
    m.client = _FakeClient(usages=[_Usage(100, 10), _Usage(250, 40)])
    res = m.run("sys", [{"role": "user", "content": "q"}],
                tools=[{"name": "get_corner"}], dispatch=lambda n, i: "corner data")
    assert res.text == "Carry more speed through Turn 1."
    assert res.usage == TokenUsage(input=350, output=50)

def test_run_stream_emits_one_usage_event_per_turn():
    m = ... # same two-turn fixture
    events = list(m.run_stream(...))
    usage_events = [e for e in events if e["type"] == "usage"]
    assert len(usage_events) == 2
    assert usage_events[0]["usage"]["input"] == 100
    assert usage_events[0]["model"] == "claude-sonnet-5"

def test_iteration_cap_final_call_is_also_billed():
    """The tool-free call made after hitting MAX_TOOL_ITERATIONS must be counted."""
    from simcoach.model.claude import MAX_TOOL_ITERATIONS
    # _FakeClientIterationCap returns a usage of (10, 1) on every call
    res = m.run(...)
    assert res.usage.input == 10 * (MAX_TOOL_ITERATIONS + 1)

def test_missing_usage_contributes_zero_not_an_error():
    """A response double without .usage must not raise (guards old fixtures)."""
    m = _model()   # the existing usage-less fixture
    res = m.run(...)
    assert res.text == "Carry more speed through Turn 1."
    assert res.usage.total == 0
```

In `tests/test_model_types.py`:

```python
def test_token_usage_adds_and_totals():
    from simcoach.model.base import TokenUsage
    a = TokenUsage(input=10, output=2, cache_read=5, cache_write=1)
    assert (a + TokenUsage(input=3)).input == 13
    assert a.total == 18
    assert sum([TokenUsage(input=1), TokenUsage(input=2)], TokenUsage()).input == 3

def test_fake_model_run_returns_deterministic_usage():
    from simcoach.model.fake import FakeModel
    res = FakeModel(reply="abcd" * 4).run("s", [], [], lambda n, i: "")
    assert res.text == "abcd" * 4
    assert res.usage.output == len(res.text) // 4      # deterministic, network-free
```

In `tests/test_session.py`, pin that the public behavior did not change:

```python
def test_coach_session_ask_still_returns_a_string_and_records_usage():
    sess = CoachSession(Coach(FakeModel(reply="ok")), car="c")
    assert sess.ask(_ctx(), "q") == "ok"          # unwrapped, not a RunResult
    assert sess.last_usage.total > 0
```

- [ ] **Step 2: Run tests to verify they fail**

`.venv/Scripts/python.exe -m pytest tests/test_claude_stream.py tests/test_model_types.py tests/test_session.py -q`
Expected: FAIL — `TokenUsage`/`RunResult` don't exist; `run` returns `str`.

- [ ] **Step 3: Implement**

`base.py`: add the two dataclasses; update the `run` return annotation to `RunResult` and document the `usage` event on `run_stream`.

`claude.py`: in `_stream_turn`, after obtaining the final message (both branches — the `.create` fallback at `:57` and the `stream.get_final_message()` path at `:65`), emit
```python
yield {"type": "usage", "usage": _usage_of(final).as_dict(), "model": self.model}
```
before `return final`. Emitting from `_stream_turn` is the whole trick: every billed call — tool turns **and** the iteration-cap tool-free call at `:103` — goes through it, so nothing can be missed by forgetting a code path. Add a module-level
```python
def _usage_of(resp) -> TokenUsage:
    u = getattr(resp, "usage", None)
    if u is None:
        return TokenUsage()
    return TokenUsage(input=getattr(u, "input_tokens", 0) or 0,
                      output=getattr(u, "output_tokens", 0) or 0,
                      cache_read=getattr(u, "cache_read_input_tokens", 0) or 0,
                      cache_write=getattr(u, "cache_creation_input_tokens", 0) or 0)
```
`run` accumulates both text deltas and usage events into `RunResult`.

`fake.py`: `run` returns `RunResult(self.reply, TokenUsage(input=..., output=len(self.reply)//4))`; `run_stream` yields one `{"type": "usage", ...}` event before its text event so fake-driven stream tests exercise the same path. Keep the input side deterministic too (e.g. `sum(len(str(m)) for m in messages) // 4`).

`coach.py:269`: `ask_agentic` returns the `RunResult` straight through (type annotation updated). `session.py`: `res = self.coach.ask_agentic(...)`; `answer = res.text`; store `self.last_usage = getattr(self, "last_usage", TokenUsage()) + res.usage`; everything else unchanged, and `ask()` still returns `str`.

`factory.py:70`: `LazyModel.run` already returns whatever the impl returns — verify, no change expected.

- [ ] **Step 4: Verify**

`.venv/Scripts/python.exe -m pytest -q` — full suite green (the `run`-calling tests in `test_claude_stream.py` must be updated to unwrap `.text`). Then `ruff check simcoach/model simcoach/coach.py simcoach/session.py`.

**Dependencies:** none. This is the root task.

---

### Task 2: Schema v12 — per-message usage columns + chat totals

**Files:**
- Modify: `simcoach/store.py` (`SCHEMA_VERSION`, `_COACH_CHAT_SCHEMA`, migration block ~`:252`, `save_chat`, new `chat_usage`)
- Test: `tests/test_store.py`

**Interfaces:**
- Produces on `LapStore`:
  - `save_chat(..., usage: dict | None = None, model: str | None = None)` — keyword-only additions, defaulted so all existing call sites keep working (`session.py:67`, `server/app.py:1279`, plus `tests/test_store.py`, `tests/test_session_persist.py`).
  - `chat_usage(chat_id: str) -> dict` → `{"input": int, "output": int, "cache_read": int, "cache_write": int, "messages": int, "by_model": {model_id: {input, output, cache_read, cache_write}}}`. `by_model` is what makes per-row pricing possible when the driver switches models mid-chat (Finding 4). Unknown/NULL model rows key under `""`.
- Columns added to `coach_chat`: `input_tokens INTEGER`, `output_tokens INTEGER`, `cache_read_tokens INTEGER`, `cache_write_tokens INTEGER`, `model TEXT` — all nullable; pre-v12 rows stay NULL and sum as 0 via `COALESCE`.

- [ ] **Step 1: Write the failing tests**

```python
def test_save_chat_persists_usage_and_chat_usage_sums_it(tmp_path):
    s = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    cid = s.create_chat()
    s.save_chat(cid, "sess", "Monza", "GT3", None, "q1", "a1",
                usage={"input": 1000, "output": 100}, model="claude-sonnet-5")
    s.save_chat(cid, "sess", "Monza", "GT3", None, "q2", "a2",
                usage={"input": 2500, "output": 300, "cache_read": 40}, model="claude-sonnet-5")
    u = s.chat_usage(cid)
    assert (u["input"], u["output"], u["cache_read"], u["messages"]) == (3500, 400, 40, 2)
    assert u["by_model"]["claude-sonnet-5"]["input"] == 3500

def test_chat_usage_splits_by_model_and_tolerates_legacy_null_rows(tmp_path):
    s = LapStore(...)
    cid = s.create_chat()
    s.save_chat(cid, "sess", "Monza", "GT3", None, "q", "a")           # no usage at all
    s.save_chat(cid, "sess", "Monza", "GT3", None, "q", "a",
                usage={"input": 10, "output": 1}, model="claude-opus-5")
    u = s.chat_usage(cid)
    assert u["input"] == 10                                            # NULLs count as 0
    assert set(u["by_model"]) == {"", "claude-opus-5"}

def test_chat_usage_is_zero_for_an_unknown_chat(tmp_path):
    assert LapStore(...).chat_usage("nope")["messages"] == 0

def test_schema_migrates_from_v11(tmp_path):
    s = LapStore(db, telem)
    s.conn.execute("PRAGMA user_version = 11"); s.conn.commit(); s.conn.close()
    s2 = LapStore(db, telem)
    assert s2.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 12
    cols = [r[1] for r in s2.conn.execute("PRAGMA table_info(coach_chat)")]
    assert {"input_tokens", "output_tokens", "cache_read_tokens",
            "cache_write_tokens", "model"} <= set(cols)
    cid = s2.create_chat()
    s2.save_chat(cid, "s", "t", "c", None, "q", "a", usage={"input": 5, "output": 1})
    assert s2.chat_usage(cid)["input"] == 5
```

- [ ] **Step 2: Run tests to verify they fail**

`.venv/Scripts/python.exe -m pytest tests/test_store.py -k "usage or migrates_from_v11" -q` → FAIL.

- [ ] **Step 3: Implement**

Bump `SCHEMA_VERSION = 12`. Add the five columns to `_COACH_CHAT_SCHEMA` (fresh installs) and an `if current_version < 12:` block that reads `PRAGMA table_info(coach_chat)` once and `_add_column`s each missing one — the same guard shape as the v4 and v5 steps, tolerant of a `<3` DB that just created the table with them. `save_chat` writes `COALESCE`-friendly values (`(usage or {}).get("input")`). `chat_usage` is one `SELECT model, SUM(COALESCE(input_tokens,0)), … , COUNT(*) FROM coach_chat WHERE chat_id = ? GROUP BY model` under `self._lock`, folded into the totals + `by_model` dict in Python.

- [ ] **Step 4: Verify**

`.venv/Scripts/python.exe -m pytest -q` (watch `tests/test_session_persist.py` and `tests/test_store.py` existing `save_chat` callers) + `ruff check simcoach/store.py`.

**Dependencies:** none (parallel with Task 1). Task 4 needs both.

---

### Task 3: `simcoach/pricing.py` — one price table, one `estimate_cost`

**Files:**
- Create: `simcoach/pricing.py`
- Test: `tests/test_pricing.py` (new)

**Interfaces:**
- Produces:
  - `PRICES: dict[str, tuple[float, float, float, float]]` — `{model_id: (input, output, cache_read, cache_write)}` in **$ per million tokens**.
  - `estimate_cost(usage: dict | TokenUsage, model: str | None) -> float | None` — `None` when the model is unknown or `None`; `0.0` for the free/fake/local providers.
  - `estimate_chat_cost(by_model: dict[str, dict]) -> tuple[float | None, bool]` → `(total_$, complete)`; `complete=False` when at least one model in the chat was unpriceable, so the UI can render "≥ $0.04" rather than a wrong total.

- [ ] **Step 1: Write the failing tests**

```python
def test_estimate_cost_golden_arithmetic():
    from simcoach.pricing import estimate_cost
    # 1M input + 1M output at a known table row == input_rate + output_rate
    rate_in, rate_out, _, _ = __import__("simcoach.pricing", fromlist=["PRICES"]).PRICES["claude-sonnet-5"]
    c = estimate_cost({"input": 1_000_000, "output": 1_000_000}, "claude-sonnet-5")
    assert c == pytest.approx(rate_in + rate_out)

def test_estimate_cost_counts_cache_read_and_write_separately():
    c = estimate_cost({"input": 0, "output": 0, "cache_read": 1_000_000}, "claude-sonnet-5")
    assert c == pytest.approx(PRICES["claude-sonnet-5"][2])

def test_dated_model_id_resolves_to_its_base_price():
    """factory.py's haiku alias is 'claude-haiku-4-5-20251001' — a dated id must
    not fall off the table."""
    assert estimate_cost({"input": 1_000_000}, "claude-haiku-4-5-20251001") == \
           estimate_cost({"input": 1_000_000}, "claude-haiku-4-5")

def test_unknown_model_returns_none_not_zero():
    assert estimate_cost({"input": 1_000_000}, "some-future-model") is None
    assert estimate_cost({"input": 1_000_000}, None) is None

def test_free_providers_cost_zero():
    assert estimate_cost({"input": 1_000_000}, "fake") == 0.0
    assert estimate_cost({"input": 1_000_000}, "local") == 0.0

def test_estimate_chat_cost_flags_an_incomplete_total():
    total, complete = estimate_chat_cost({
        "claude-sonnet-5": {"input": 1_000_000, "output": 0},
        "mystery-model":   {"input": 1_000_000, "output": 0},
    })
    assert complete is False and total == pytest.approx(PRICES["claude-sonnet-5"][0])
    assert estimate_chat_cost({"claude-sonnet-5": {"input": 0, "output": 0}}) == (0.0, True)

def test_every_factory_alias_target_is_priced():
    """Guard against a Settings model the meter can't price."""
    from simcoach.model.factory import MODEL_ALIASES
    for mid in MODEL_ALIASES.values():
        assert estimate_cost({"input": 1}, mid) is not None
```

- [ ] **Step 2: Run tests to verify they fail** — `.venv/Scripts/python.exe -m pytest tests/test_pricing.py -q` → FAIL (no module).

- [ ] **Step 3: Implement**

```python
"""Claude API prices, in $ per MILLION tokens: (input, output, cache_read, cache_write).
ponytail: prices drift — this table is the ONE place to update them. A local model
later returns 0. Unknown model -> None, never 0: a fabricated $0 would mislead a
BYO-key driver more than an honest blank.

Source: Anthropic pricing, last confirmed <DATE>. RE-CONFIRM before shipping.
"""
PRICES = {
    # model_id: (input, output, cache_read, cache_write)
    "claude-opus-5":   (5.00, 25.00, 0.50, 6.25),
    "claude-sonnet-5": (2.00, 10.00, 0.20, 2.50),
    "claude-haiku-4-5": (1.00,  5.00, 0.10, 1.25),
}
FREE = ("fake", "local")
```

> **⚠ CONFIRM THE RATES AT IMPLEMENTATION TIME.** The base input/output figures above come from the bundled `claude-api` skill's model table, cached **2026-06-24**, and the cache columns are derived from its documented multipliers (cache read ≈ 0.1× input, cache write ≈ 1.25× input). Do **not** ship them unverified — re-read the `claude-api` skill (or fetch Anthropic's live pricing page) at implementation time, write the confirmation date into the docstring, and fix any row that moved. Getting this wrong shows a BYO-key driver a wrong dollar figure, which is worse than showing none.

`estimate_cost` normalizes the id: exact hit → longest matching table key that is a prefix of the id (handles `claude-haiku-4-5-20251001`) → `FREE` → `None`. Then `sum(tokens[k] / 1e6 * rate[k])` over the four buckets. Accept a `TokenUsage` or a plain dict (`as_dict()` first if it isn't a dict).

- [ ] **Step 4: Verify** — `.venv/Scripts/python.exe -m pytest tests/test_pricing.py -q` then the full suite; `ruff check simcoach/pricing.py`.

**Dependencies:** none (parallel with Tasks 1-2). Task 4 needs it.

---

### Task 4: WS `/coach` sums, persists, and reports usage

**Files:**
- Modify: `simcoach/server/app.py` (the `worker()` body ~`:1258-1285`; new `GET /chats/{chat_id}/usage` near `:1105`)
- Test: `tests/test_server_ws.py`, `tests/test_server_reads.py`

**Interfaces:**
- The terminal `done` event gains (alongside the existing `chat_id`):
  ```json
  {"type":"done","chat_id":"chat-…","usage":{"input":3500,"output":400,"cache_read":0,"cache_write":0,
   "total":3900,"messages":2},"cost":0.0110,"cost_complete":true,"model":"claude-sonnet-5"}
  ```
  `usage` is the **chat cumulative** total (read back from `chat_usage` after the insert), not this turn's — the meter shows cost-so-far, and reading it back means the number is the persisted truth rather than a drifting in-memory counter.
- Produces `GET /chats/{chat_id}/usage` → the same `{usage, cost, cost_complete}` shape, so reopening a thread seeds the meter without asking a question.
- Internal: the worker filters `{"type": "usage"}` events out of `_emit` (the per-turn breakdown is noise on the wire) and accumulates them into one `TokenUsage` for the turn. The model id comes from the usage event itself — not from `config`, which may have changed since the sidecar resolved the model.

- [ ] **Step 1: Write the failing tests**

```python
def test_coach_ws_done_carries_cumulative_usage_and_cost(...):
    """FakeModel-backed WS ask: the done frame reports tokens + an estimated $."""
    # existing test_server_ws.py harness; app.state.coach wraps a FakeModel
    frames = _ask(client, {"question": "where am I losing time?"})
    done = frames[-1]
    assert done["type"] == "done"
    assert done["usage"]["total"] > 0
    assert done["cost"] is not None and done["cost_complete"] is True
    assert not any(f["type"] == "usage" for f in frames)   # per-turn events stay server-side

def test_coach_ws_second_question_usage_accumulates(...):
    first = _ask(client, {"question": "q1"})[-1]
    second = _ask(client, {"question": "q2", "chat_id": first["chat_id"]})[-1]
    assert second["usage"]["total"] > first["usage"]["total"]
    assert second["usage"]["messages"] == 2

def test_coach_ws_persists_usage_onto_the_chat_row(...):
    done = _ask(client, {"question": "q"})[-1]
    assert app.state.store.chat_usage(done["chat_id"])["total"] == done["usage"]["total"]

def test_get_chat_usage_endpoint_seeds_the_meter(...):
    done = _ask(client, {"question": "q"})[-1]
    r = client.get(f"/chats/{done['chat_id']}/usage").json()
    assert r["usage"]["total"] == done["usage"]["total"]

def test_get_chat_usage_is_zeroed_for_an_unknown_chat(client):
    r = client.get("/chats/nope/usage").json()
    assert r["usage"]["total"] == 0 and r["cost"] == 0.0

def test_an_errored_turn_still_does_not_crash_the_socket(...):
    """A model that raises: error frame, no usage key required, socket closes clean."""
```

- [ ] **Step 2: Run tests to verify they fail** — `.venv/Scripts/python.exe -m pytest tests/test_server_ws.py -q` → FAIL.

- [ ] **Step 3: Implement**

In `worker()`: accumulate `turn_usage` / `used_model` from `usage` events inside the existing `for ev in ...ask_agentic_stream(...)` loop and `continue` instead of `_emit`ing them. In the persist block (`:1276`), pass `usage=turn_usage.as_dict(), model=used_model` to `save_chat`, then — still inside the same `try` — read `chat_usage(chat_id)`, price it with `pricing.estimate_chat_cost(by_model)`, and attach `usage`/`cost`/`cost_complete`/`model` to `terminal`. The whole block is already `except Exception: pass`, so a pricing or aggregate failure degrades to a `done` with no meter data rather than losing the answer. Add the `GET /chats/{chat_id}/usage` route next to the existing `/chats/{chat_id}` (`:1105`) calling the same two functions.

- [ ] **Step 4: Verify** — `.venv/Scripts/python.exe -m pytest -q` + `ruff check simcoach/server/app.py`.

**Dependencies:** Tasks 1, 2, 3.

---

### Task 5: Chat-screen usage/cost meter

**Files:**
- Modify: `frontend/src/lib/coach.ts` (capture `usage`/`cost` off the `done` frame), `frontend/src/lib/api.ts` (`ChatUsage` type + `getChatUsage`), `frontend/src/lib/ChatPanel.svelte` (the meter in `.chat-bar`)
- Test: `frontend/src/lib/coach.test.ts`, `frontend/src/lib/api.test.ts`, `frontend/src/lib/ChatPanel.test.ts`

**Interfaces:**
- `CoachStream` gains `usage: ChatUsage | null` and `cost: number | null` / `costComplete: boolean`, set on the `done` frame and reset on `ask()` like `answer`/`chatId` already are.
- `api.ts`: `export interface ChatUsage { input: number; output: number; cache_read: number; cache_write: number; total: number; messages: number }` and `getChatUsage(chatId): Promise<{usage: ChatUsage; cost: number | null; cost_complete: boolean}>`.
- `ChatPanel` renders in the existing `.chat-bar`, left of "+ New session": `12.4k tokens · $0.011`. Rules: hidden entirely when `total === 0`; `—` in place of the dollar figure when `cost === null`; `≥ $x` when `cost_complete === false`; thousands as `k` past 1000. No price arithmetic in the component.

- [ ] **Step 1: Write the failing tests**

```ts
// coach.test.ts
it("captures usage and cost from the done frame", () => { /* feed a done frame with usage/cost */ });
it("clears usage when a new ask starts", () => { /* ask() resets to null */ });
it("ignores an unknown event type without breaking the stream", () => { /* {type:"usage"} arrives anyway */ });

// api.test.ts
it("getChatUsage fetches /chats/:id/usage", async () => { /* fetch mock asserts the path */ });

// ChatPanel.test.ts
it("shows the token + cost meter after an answer", async () => { /* expect 1.2k tokens, $0.011 */ });
it("hides the meter on an empty new chat", () => { /* total 0 -> no meter node */ });
it("shows a dash instead of a cost when the model is unpriced", () => { /* cost: null */ });
it("shows a >= prefix when the chat total is incomplete", () => { /* cost_complete: false */ });
it("seeds the meter from the server when an existing thread is opened", async () => { /* getChatUsage mocked */ });
```

- [ ] **Step 2: Run tests to verify they fail** — `cd frontend && npm test` → FAIL.

- [ ] **Step 3: Implement** — add the `done`-frame fields in `coach.ts`'s `case "done"` (and the resets in `ask()`); `getChatUsage` alongside `getChatMessages` (`api.ts:424`); in `ChatPanel`, call `getChatUsage(id)` inside `loadThread` (same `try`/`catch {}` shape as `getChatMessages`), zero it in `newChat()`, and adopt the `done` snapshot in the `onMount` subscriber next to the existing `snap.chatId` adoption. Style the meter with the existing `--faint`/`--mono` tokens.

- [ ] **Step 4: Verify** — `cd frontend && npm test` (all green), then `.venv/Scripts/python.exe -m pytest -q` to confirm nothing backend moved.

**Dependencies:** Task 4.

---

# Phase 2 — Guardrails and polish (Tasks 6-7)

### Task 6: Settings toggle for the cost estimate

**Files:** `simcoach/config.py` (`DEFAULTS`), `simcoach/server/app.py` (`_settings_view`), `frontend/src/lib/api.ts` (`SettingsResponse`), `frontend/src/lib/SettingsScreen.svelte`, `frontend/src/lib/ChatPanel.svelte`
**Tests:** `tests/test_config.py`, `tests/test_server_settings.py`, `frontend/src/lib/SettingsScreen.test.ts`, `frontend/src/lib/ChatPanel.test.ts`

- [ ] **Step 1:** Failing tests — `show_cost` defaults to `true` in `config.DEFAULTS`; `PUT /settings {"show_cost": false}` round-trips through the existing whitelist and appears in `GET /settings`; `SettingsScreen` renders the toggle; `ChatPanel` with `showCost=false` renders tokens but no `$`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Add `"show_cost": True` to `DEFAULTS` (the `put_settings` whitelist at `app.py:1492` picks it up with no code change — verify). `ChatPanel` takes `showCost` as a prop fed from App's settings load, defaulting to `true`.
- [ ] **Step 4:** `.venv/Scripts/python.exe -m pytest -q` + `cd frontend && npm test`.

**Dependencies:** Task 5. Tokens stay visible regardless — the toggle hides only the dollar figure, per spec §4.

---

### Task 7: Soft long-chat nudge

**Files:** `simcoach/config.py` (`DEFAULTS`), `simcoach/server/app.py` (`done` frame), `frontend/src/lib/coach.ts`, `frontend/src/lib/ChatPanel.svelte`
**Tests:** `tests/test_server_ws.py`, `frontend/src/lib/ChatPanel.test.ts`

- [ ] **Step 1:** Failing tests — `chat_token_soft_limit` (default `150_000`) in `DEFAULTS`; the WS `done` frame carries `"long_chat": true` once `usage.total` crosses it and `false` below; `ChatPanel` shows a dismissible "Long chat — start a New chat to keep it snappy" banner whose action calls the same `newChat()` path the "+ New session" button uses; the banner does not reappear after dismissal within the same thread.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Threshold comparison in the server (core owns the decision, UI renders it). The banner's button reuses `newChat()` — no new reset logic. **No** auto-summarize, per spec §5.
- [ ] **Step 4:** `.venv/Scripts/python.exe -m pytest -q` + `cd frontend && npm test`.

**Dependencies:** Task 5 (Task 6 independent).

---

# Phase 3 — Gated / optional

### Task 8: Bind a chat to its race session  ⛔ NEEDS A USER DECISION FIRST

Do not start this until Open Question 1 is answered — it changes behavior the driver will notice, and it partly contradicts Task 7.

**Files:** `simcoach/server/app.py` (`worker()` chat-id resolution ~`:1278`), `frontend/src/lib/coach.ts`, `frontend/src/lib/ChatPanel.svelte`
**Tests:** `tests/test_server_ws.py`, `frontend/src/lib/ChatPanel.test.ts`

What the code already does (verified): `ChatPanel`'s `$effect` at `:135` already starts an **empty new chat** unless a `chatId` prop is pinned, and the server already stamps every message with `session_id` and already has `store.chats_for_session()`. So "one chat per session" is mostly emergent. The one missing piece is ~2 lines in the worker: when no `chat_id` was sent and the current session already has a chat, **join it** instead of minting a new one —
`chat_id = req.get("chat_id") or (rows[0]["chat_id"] if (rows := store.chats_for_session(ctx.session_id)) else None) or store.create_chat()`.

- [ ] **Step 1:** Failing tests — two asks with no `chat_id` inside one session land in the same `chat_id`; asks under different `session_id`s do not; an explicit `new_chat: true` in the request still forces a fresh chat (this is what keeps Task 7's nudge meaningful); a `session_id`-less ask (garage, pre-lap) still mints a chat rather than erroring.
- [ ] **Step 2-4:** implement, then `.venv/Scripts/python.exe -m pytest -q` + `cd frontend && npm test`.

**Dependencies:** Task 4, plus the user's answer to Open Question 1.

---

### Task 9: `[LIVE-LLM]` Live verification against a real key

**Off the critical path.** Everything above is completable offline with `FakeModel` and the scripted `_FakeClient`. This task is the one thing fakes cannot prove: that Anthropic's real `usage` object has the field names `claude.py` reads, and that the summed total matches what the account is actually billed.

- [ ] With a real key and one real question asked through the desktop app:
  - [ ] Log the raw `usage` of each turn; confirm `input_tokens` / `output_tokens` / `cache_read_input_tokens` / `cache_creation_input_tokens` all exist and that `run_stream` emitted one event per API call.
  - [ ] Confirm the meter's cumulative total matches the sum of the logged turns (and that a multi-tool question really does show several× the single-prompt input — the ×9 re-send the spec predicts).
  - [ ] Compare the estimated $ against the Anthropic console's usage for that window; reconcile any delta against the `PRICES` table and record the confirmation date in the `pricing.py` docstring.
  - [ ] Confirm `cache_read`/`cache_write` read 0 (Finding 3) — if they don't, caching is on somewhere and the finding needs revisiting.

Label the GitHub issue `rig-verify` for this step. **Dependencies:** Tasks 1-5.

---

## Recommended vertical slice

**Tasks 1-5.** That is issue #39's build-order items 1-4 end to end: real billed tokens summed across the tool loop, persisted per chat and per model, priced from one table, and visible on the chat screen — which also closes the long-deferred "token-usage estimator on the chat screen" backlog item.

**It defers:** the Settings show/hide toggle (Task 6 — meter always shows cost), the soft long-chat nudge (Task 7), per-session chat re-binding (Task 8, needs a decision), and live-key verification (Task 9). Nothing in 6-9 is a prerequisite for 1-5 being useful: the meter alone tells the driver what a session costs, which is the actual complaint behind the issue.

---

## Open questions needing the user's decision

1. **Is per-session chat re-binding (Task 8) actually wanted, given the code already nearly does it?** `ChatPanel` already opens an empty new chat on every cold load and session switch, and every message is already stamped with `session_id`. The spec's re-binding would add: two asks in the same session *join one chat* rather than each starting a new one. That directly fights Task 7's nudge (after a "New chat" reset, the next ask would re-join the very chat the driver just left) unless an explicit `new_chat: true` override ships with it. **Recommendation: ship Tasks 1-7 without Task 8.** The meter + nudge + existing empty-new-chat default already bound history; re-binding mostly adds a way for the driver's deliberate reset to be ignored. Ask the user whether they want the join behavior anyway.

2. **Prompt caching is the real fix for the ×9 re-send — should it be its own issue now?** The spec names the re-send as a "known ceiling" and answers it with a nudge. But the system prompt (`AGENTIC_SYSTEM_PROMPT`, ~3.5k tokens) plus the `TOOLS` array is a large, byte-stable prefix re-sent on every one of the up-to-9 calls per question, and the repo sends no `cache_control` at all. Caching that prefix is a one-line-ish change that would cut input cost on the repeated turns by roughly an order of magnitude — far more than a nudge can. It is **out of scope for #39** (and the meter is what makes it measurable), but it should be filed. **Recommendation: ship the meter first, then open a follow-up issue to cache the system+tools prefix, and use the meter to prove the saving.**

3. **Does the meter show the chat total, or the session total?** The spec says "cumulative tokens **this session**" (§4) while everything it persists is keyed by `chat_id`. These diverge whenever a chat spans sessions or a session has several chats. **Recommendation: chat total** (what the plan implements) — it is what `coach_chat` can aggregate cheaply, it matches the thread the driver is looking at, and it stays correct whether or not Task 8 lands. A session-wide total is a later `SUM … WHERE session_id = ?` if ever wanted.

4. **`FREE` models return `$0.00` — is that the right display?** A local model genuinely costs nothing in API dollars (it costs electricity and a GPU). **Recommendation: render `—` rather than `$0.00` for a free provider** so the meter never implies "this was free" for a non-free thing; keep `estimate_cost` returning `0.0` so the arithmetic stays sane. This is a one-line rendering choice in Task 5 — flagging it rather than silently picking.

---

## Invariants preserved

Corners-owned data and the two-stage pipeline are untouched (this work never looks at telemetry). Swappable model: usage and pricing sit behind the `Model` protocol and one price table; a future local model returns zero. Storage split: usage is additive columns on SQLite `coach_chat`, `.npz` lap files unchanged. UI-agnostic core: tokens and dollars are computed in `simcoach/` and handed to the Svelte component to format.

Relates to [[chat-token-usage]], [[coach-conversation-memory]], [[session-chat-model]], [[chat-vs-race-session-decoupling]], [[monetization-options]].
