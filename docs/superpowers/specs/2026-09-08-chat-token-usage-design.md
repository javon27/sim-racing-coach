# Chat token usage & per-session chats — design (2026-09-08)

Status: spec only. No branch yet. **Supersedes** the earlier compaction-based draft
(`2026-09-08-token-budget-and-compaction-design.md`, removed) — compaction dropped for
simplicity per the decision below.
Folds in the deferred backlog item **"token-usage estimator on the chat screen"**
(`2026-09-08-track-narratives-design.md:204`; memory [[track-narratives-spec]]).

## Problem

Two gaps in the agentic coach:

1. **No usage/cost visibility.** The Anthropic response carries `usage`, but nothing reads
   it. On BYO-key the inference cost is the user's, so tokens spent / estimated $ matter
   ([[monetization-options]]).
2. **Unbounded chat history.** `simcoach/session.py` (`CoachSession`) appends every Q+A
   (`:60–61`) with no window; only `reset()` (`:76`, "New chat") clears it. And the tool
   loop re-sends the whole stack on each of up to `MAX_TOOL_ITERATIONS + 1 = 9` API calls
   per question (`simcoach/model/claude.py`), so input cost per question is several× the
   prompt.

## Decision — keep it simple: chat is scoped to the session

> **Superseded by #167 (2026-09-25).** A chat is no longer bound to a session: it
> continues across sessions, tracks and race weekends until the driver presses **New
> chat** (or **New session**). A live-session change (e.g. an online server reading
> offline between races) never resets it. In the history the model sees, each earlier
> answer from a *different* session is labelled with that session's track, car,
> online/offline and phase, and its tool results do not count as known figures for the
> current race (#159). The long-chat soft nudge (`chat_token_soft_limit`) is the only
> length signal. The per-session text below is kept as the original record.

**One chat per session**, where session = `(track, car, online|offline)` (the existing
stable id — [[session-chat-model]]). A session is finite (a practice / qualy / race
stint), so its chat **naturally bounds history** — there is no cross-session transcript to
carry, and therefore **no compaction / summarize-and-handoff** to build.

Cross-session context comes from the **tools the driver-profile work already shipped**, not
from dragging a growing thread forward: `list_recent_laps` (session-scoped), `get_progress`,
`get_driver_memories`, `get_last_setup`, session summaries, and the longitudinal-profile
injection. The coach pulls prior-session data **on demand**.

This **re-binds chat to the session**, superseding the earlier "continuous chat decoupled
from race session" direction ([[v1-desktop-ui]], [[chat-vs-race-session-decoupling]]). A
deliberate simplification, not a regression: the decoupling existed to preserve context
across sessions, and the tools now do that job better than a long transcript.

## Approach

### 1. Token accounting out of `Model.run`
`base.py` / `claude.py`: `run()` returns `RunResult(text, usage: TokenUsage)` with
`TokenUsage(input, output, cache_read=0, cache_write=0)`. `ClaudeModel.run` **sums
`response.usage` across every loop iteration** (the multi-call cost — the subtle,
must-test part), including the final tool-free call. `FakeModel.run` returns deterministic
usage (`len(text)//4`) for network-free tests. Streaming path threads usage out on
`message_stop`.

### 2. Per-session usage accounting — `simcoach/session.py`
`CoachSession` accumulates `input_tokens` / `output_tokens` for the chat and persists them
to the `coach_chat` row (additive migration — storage split unchanged). A **new session
starts a fresh chat with a zeroed counter** — the session boundary is the reset, so no
windowing logic is needed. Expose cumulative usage over the coach WS/HTTP for the live UI.

### 3. Pricing + cost estimate — `simcoach/pricing.py` (new)
Constant table `{model_id: (input_$/Mtok, output_$/Mtok, cache_read, cache_write)}` for the
supported Claude models; `estimate_cost(usage, model) -> float`. `ponytail:` prices drift —
one table to update; a local model later returns 0.

### 4. Chat-screen usage/cost meter — ships the backlog item
A small, unobtrusive meter on the chat screen: cumulative tokens this session + **estimated
$**. This *is* the deferred "token-usage estimator on the chat screen." Settings: show/hide
the cost estimate.

### 5. Soft "long chat" nudge — the simple guardrail (replaces compaction)
If a single session's chat crosses a configurable soft threshold, show a gentle nudge —
**"Long chat — start a New chat to keep it snappy"** — that runs the existing manual reset
(`session.py:76`). **No automatic summarize/handoff.** Cross-session context is already
tool-accessible, so a manual reset loses nothing the coach can't re-fetch.

## Known ceiling
A single very chatty session (e.g. a long endurance race with many questions) can still run
up input tokens via the ×9 tool-loop re-send. The soft nudge + manual New-chat + the
visible meter cover it. `ponytail:` if that becomes a real problem, revisit per-session
history windowing — but don't build it speculatively.

## Invariants preserved
Two-stage, corners-owned, swappable-model (usage + pricing behind the `Model` interface +
the price table; local model → zero cost), storage split (usage persisted additively in
SQLite `coach_chat`; `.npz` untouched).

## Testing
- **Usage accounting** sums across tool-loop iterations (fake model) — not just the last
  call.
- **Cost estimate** table lookup + arithmetic golden per model.
- **Chat continuity (#167, replaces the per-session reset)**: a question from a new
  session continues the same chat (same chat id, other-session turns labelled); only New
  chat / New session starts a fresh chat with a zeroed counter.
- **Meter + nudge**: cumulative usage is surfaced; the soft nudge fires past the threshold.

## Build order
1. Usage out of `Model.run` (+ FakeModel usage) + tests →
2. Per-session cumulative usage: persist to `coach_chat` + WS expose →
3. `pricing.py` + `estimate_cost` →
4. Chat-screen usage/cost meter (**ships the backlog item**) →
5. Soft long-chat nudge.

Steps 1–4 deliver the visibility (and the backlog item); step 5 is the lightweight
guardrail. Relates to [[coach-conversation-memory]], [[session-chat-model]],
[[chat-vs-race-session-decoupling]].
