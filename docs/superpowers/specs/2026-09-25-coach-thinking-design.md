# Coach thinking: reason before answering, dynamically — design (2026-09-25)

Status: DRAFT for the driver's review. No code, no issue yet.
Origin: the driver, 2026-09-25 — "what makes Claude Code and other coding harnesses great is
the ability for the agent to reason and think before responding … It would need to be a bit
dynamic. Some responses should be delivered right away, while others require more thought."
Companion to `2026-09-25-coaching-insight-design.md` (#169), which fixes *what the tools
compute*; this fixes *how much the model deliberates over it*.

## Where we are

- `ClaudeModel(thinking: bool = False)` (`simcoach/model/claude.py:15-33`). The factory never
  passes it (`model/factory.py:40`), so on the default `claude-sonnet-5` **every coach answer
  today runs with thinking disabled**. The only knob is on/off per model instance:
  `{"type": "adaptive"}` or `{"type": "disabled"}`; a token budget is rejected by sonnet-5
  (`claude.py:32`). Opus 5.5 / Fable / Mythos reject *disabled* thinking, so on those the
  coach always thinks and can never take a #159 forced tool round (`claude.py:23-29`).
- The tool loop (`coach.py` `ask_agentic_stream`) is one model call per tool round with no
  notion of "how hard to think"; latency is whatever the round takes.
- The two chats that prompted #168/#169 show the cost of no deliberation: the model called
  one tool and narrated its largest number (#244/#250); it answered "how was that race?" from
  a mid-race frame (#245); it did not plan the multi-tool answer "analyze the race in detail"
  deserved (#247 called four tools, then wrote a list it had not reasoned over).

## What thinking is for here (and what it is not)

Thinking is the model's private reasoning before it speaks. In this coach it buys:

1. **Tool planning** — which tools answer the question, in what order, and whether one
   result contradicts another (race result vs race state; the driver's word vs a stale row).
2. **Cross-checking against the driver's question** — "he asked where the *time* is, the
   tool gave me coasting seconds; that is not a loss" (the failure in #250).
3. **Choosing the one thing to say** — the diagnosis block in #169 §3 asks for one corner,
   one phase, one change; picking it well is reasoning, not formatting.

It does **not** loosen any invariant. Thinking cannot produce a figure: every number and
name still comes from this turn's tool results (`ground_names`/`states_data`, #159), corners
stay owned data, and code still extracts while the model narrates. Thinking changes the
quality of narration and tool use, never the source of facts.

## Design

### 1. Three tiers, chosen by code, per question

| Tier | When | Thinking | Latency target |
|---|---|---|---|
| **Quick** | On track and moving (live snapshot: on_track, speed above a floor), PTT, no detail request | disabled | first spoken word ≤ ~3 s |
| **Standard** | Everything else by default: garage, typed, conversation mode, a finished race | adaptive | ≤ ~8 s to first text |
| **Deep** | Explicit: "in detail", "analyze", "walk me through", "explain", "think about it", "everything"; setup questions; post-race review ("how was that race", "where could I have passed"); a question with ≥ 2 sub-questions | adaptive + the provider's highest effort where supported, higher `max_tokens` | acknowledged at once; answer when ready |

- **The router is deterministic code**, not a model call: `coach.thinking_tier(question,
  voice, live) -> "quick" | "standard" | "deep"`, a pure function next to the #168
  detail-word list (`VOICE_PTT`), tested like any other extractor. *Reason:* a model call to
  decide how much to think is itself latency and cost; the signals are all already in hand.
- **Quick ≠ dumb.** Quick answers are the on-track cues (#162/#168): one corner, one change.
  The tools already did the analysis; the model only picks words. Disabling thinking there
  is what keeps the first word inside 3 s over PTT.
- **Deep may be slower than the driver expects**, so it says so first (§3).

### 2. The Model seam carries the tier

- `Model.run_stream(..., thinking: str = "standard")` (`model/base.py:94`), threaded from
  `ask_agentic_stream`. `FakeModel` records it per call (like `forced`), so tests assert the
  router's choice end to end.
- `ClaudeModel` maps tiers to the API per model family:
  - quick → `{"type": "disabled"}`; on families that reject disabled, adaptive at the lowest
    effort the API accepts (PENDING: confirm the effort knob and its name in the installed
    SDK — `claude.py:28` mentions "default effort" for Opus 5, so one exists; do not guess).
  - standard → `{"type": "adaptive"}`.
  - deep → adaptive + highest effort where the family supports it, `max_tokens` raised
    (e.g. 4096 → 8192) so a long spoken answer plus thinking fits.
- **#159 constraint:** a forced tool round must run with thinking disabled (`claude.py:82-86`).
  So a deep answer that needs a forced round runs that one round quick; the tier applies
  to the other rounds. On families that cannot disable thinking nothing changes (#159
  already answers UNCONFIRMED there).
- Thinking blocks are never shown to the driver by default and never stored in
  `coach_chat.answer`. They are logged at INFO with the chat/turn id for tuning (§5).

### 3. What the driver hears and sees

- **Deep tier acknowledges first**, then thinks: one short spoken/typed line chosen by code
  from the tier ("Give me a moment — going through the race lap by lap."), emitted before
  the model call so it is not model narration and not subject to #168's pre-tool-narration
  drop. Standard and quick tiers say nothing extra.
- **Chat panel:** a "thinking…" state on the pending answer (the existing streaming
  indicator with a different label), and the usage/cost meter counts thinking tokens
  (they are billed output tokens; `_usage_of` already sums output).
- **Optional reasoning view (setting, off by default):** show the thinking text collapsed
  under the answer, the way a coding harness shows its reasoning. *Why:* the driver tunes
  the coach by reading why it said what it said — the same review that found #173. It is
  raw model text, so it is labelled as such and never spoken.

### 4. Settings

`coach.thinking`: `"auto"` (default, the router) | `"off"` (always quick) | `"always"`
(standard everywhere, deep on request). Plus the spoken override "think about it" /
"take your time" → deep for that question; "quick answer" → quick.

### 5. Evaluation (the part that makes it worth doing)

Prompt and thinking changes cannot be replayed offline. Add `scripts/eval_thinking.py`:
re-ask N stored questions (from `coach_chat`, with their session context and the same
tool results the original turn saw, replayed through the tool layer) with thinking
off / standard / deep against the real model, write the three answers side by side to
`docs/research/eval-thinking-<date>.md` for the driver to grade, and record tokens and
wall-clock per tier. Run it on the Long Beach and Algarve chats first. *Reason:* the
driver's grading is the only ground truth for "insight"; the script makes it cheap to
repeat after every prompt change.

### 6. Cost and latency (estimate, to measure with §5)

Adaptive thinking on Sonnet 5 is usually a few hundred tokens on a simple question and
2–8k on a multi-tool review; billed as output. Deep tier on a full race review: on the
order of $0.05–0.15 and 10–30 s before the first word. The meter shows it; the driver
decides. Quick tier costs nothing extra.

## Scope

**Slice 1 (one overnight run, no rig):** `thinking_tier()` + tests; the `run_stream`
parameter through base/fake/factory/claude; the deep-tier acknowledgement line; the
`coach.thinking` setting; the chat-panel "thinking…" label; INFO logging of thinking text.
**Slice 2:** `eval_thinking.py` and the first graded run; the optional reasoning view.
**Rig:** first-word latency on quick tier over PTT; whether standard tier in the garage
feels slow.

## Open questions for the driver

1. **Standard by default in the garage?** Adaptive thinking on every typed/conversation
   question adds a second or two and some cost; or keep the garage quick and think only on
   request/deep triggers?
2. **Reasoning view:** do you want to see the coach's thinking in the chat (collapsed,
   off by default), or keep it log-only?
3. **Deep on post-race questions automatically**, or only when you ask for detail?
4. **Spend cap:** a per-answer thinking cap (tokens) or none?
