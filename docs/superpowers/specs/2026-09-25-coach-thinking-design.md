# Coach thinking: reason before answering, dynamically — design (2026-09-25, rev 2)

Status: APPROVED 2026-09-25 (decisions at the end). Slice 1 tracked as a GitHub issue.
Origin: the driver, 2026-09-25 — "what makes Claude Code and other coding harnesses great is
the ability for the agent to reason and think before responding … It would need to be a bit
dynamic. Some responses should be delivered right away, while others require more thought."
Rev 2 after review: the tier is chosen by a hybrid of code gates and a small classifier
model; both the classifier and "thinking" sit behind the `Model` seam so nothing couples the
harness to one provider (CLAUDE.md: swappable model).
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
- The two chats behind #168/#169 show the cost of no deliberation: the model called one tool
  and narrated its largest number (#244/#250); it answered "how was that race?" from a
  mid-race frame (#245); it did not plan the multi-tool answer "analyze the race in detail"
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

### 1. Three tiers

| Tier | Meaning | Latency target |
|---|---|---|
| **Quick** | No deliberation. The on-track cue: one corner, one change. | first spoken word ≤ ~3 s |
| **Standard** | The model may think as much as it judges the question needs. | ≤ ~8 s to first text |
| **Deep** | Think hard; plan a multi-tool answer; a longer answer is expected. | acknowledged at once; answer when ready |

The tier is the only thinking concept the harness knows. What it means for a given model is
the adapter's business (§4).

### 2. Choosing the tier: code gates first, then a classifier, with a code floor

```
question, voice mode, live state
   │
   ├─ hard gates (code, 0 ms) ──────────────────────────────┐
   │   on track AND moving (live snapshot)      → quick     │
   │   driver override "quick answer"           → quick     │  decided
   │   driver override "think about it" /       → deep      │
   │     "take your time"                                   │
   │   coach.thinking = "off" / "always"        → as set    │
   │                                                        │
   └─ otherwise: classifier model (≤ 800 ms, JSON) ─────────┤
        {tier: standard|deep, intent, entities}             │
        on timeout / error / bad JSON                       │
            └─ keyword rules (code)             → tier ─────┘
```

- **Hard gates are code** so the latency-critical path (a PTT cue while driving) never
  waits on a model call, and the driver's own words always win.
- **The classifier is a second `Model` behind the same seam** (`coach.classifier`), not a
  named vendor model. Its contract: input = the question, the last two turns, and a one-line
  state summary (`garage | on track, stationary | on track, moving`, session phase, race live
  / finished, voice mode, laps this session); output = one JSON object
  `{"tier": "standard"|"deep", "intent": <enum>, "corner": str|null, "lap": str|null,
  "rival": str|null, "wants_detail": bool}`. Any model that can return that JSON qualifies:
  a small hosted model today, a local small model later, or the coach's own model. It is
  configured independently of the coach model (`coach.classifier_model`, default = the
  cheapest hosted model the provider offers; `"none"` disables it and the keyword rules
  decide).
- **Why a classifier and not only rules:** rules only see wording. "Why does the rear keep
  stepping out?" is a deep setup question with no keyword in it; "how was that race" is a
  post-race review only if the race is finished. The classifier reads intent and state
  together.
- **Why the classifier earns more than a tier:** its `intent`/entities steer the tools too
  ("where's the time" → `get_time_on_table`; "how was that race" + race finished → the
  finished-race read), which today relies on prompt rules the coach model sometimes ignores.
  The intent is passed to the coach as one line of context, never as a tool call it must
  obey — the coach model still chooses its tools.
- **Bounded harm:** a wrong tier is a slower or shallower answer, never a wrong fact. A wrong
  intent is one line of context the coach can ignore. The driver overrides by voice.
- **Graded, not trusted:** every classification is logged with the question and the tier
  actually used; the eval (§6) reports agreement with the driver's grading.

### 3. The `Model` seam carries the tier

- `Model.run_stream(..., thinking: str = "standard")` (`model/base.py:94`), threaded from
  `ask_agentic_stream`. `FakeModel` records it per call (like `forced`), so tests assert the
  router's choice end to end.
- `Model.classify(prompt: str, schema: dict) -> dict | None` for the classifier role: one
  short call, JSON out, `None` on any failure. `FakeModel` returns a scripted dict.
- **#159 constraint** (Anthropic-specific, stays inside the adapter): a forced tool round
  runs with thinking disabled. A deep answer that needs a forced round runs that one round
  quick; the tier applies to the other rounds.
- Thinking text is never shown by default and never stored in `coach_chat.answer`. It is
  logged at INFO with the chat/turn id for tuning (§6).

### 4. Provider mapping (all inside the adapters — nothing provider-specific leaves them)

| Tier | Anthropic (`ClaudeModel`) | A provider with a reasoning-effort setting | A model with no thinking mode (most local models) |
|---|---|---|---|
| quick | `{"type": "disabled"}`; on families that reject disabled, adaptive at the lowest effort the API accepts (PENDING: confirm the effort knob in the installed SDK; `claude.py:28` implies one exists — do not guess) | effort low | plain call |
| standard | `{"type": "adaptive"}` | effort medium | plain call |
| deep | adaptive + highest effort; `max_tokens` raised (4096 → 8192) | effort high; longer output limit | **an explicit planning turn in prose**: the adapter runs one extra call that returns a scratchpad ("what the tools say, what contradicts, the one thing to say"), feeds it to the answer call as a hidden assistant note, and strips it before anything is shown or spoken |

The last column is what keeps the feature honest about local models: deep tier exists on
every model, slower where the model has no native thinking. The scratchpad text is subject
to the same rule as native thinking — it is never a source of figures, and the #159 checks
run on the spoken answer, not on it.

### 5. What the driver hears and sees

- **Deep tier acknowledges first**, then thinks: one short spoken/typed line chosen by code
  from the tier and intent ("Give me a moment — going through the race lap by lap."),
  emitted before the model call so it is not model narration and not subject to #168's
  pre-tool-narration drop. Standard and quick tiers say nothing extra.
- **Chat panel:** a "thinking…" state on the pending answer (the existing streaming
  indicator with a different label), and the usage/cost meter counts thinking tokens and the
  classifier call (both billed).
- **Optional reasoning view (setting, off by default):** show the thinking text (or the
  planning scratchpad) collapsed under the answer, the way a coding harness shows its
  reasoning. *Why:* the driver tunes the coach by reading why it said what it said — the
  same review that found #173. It is raw model text, labelled as such, never spoken.

### 6. Evaluation (the part that makes it worth doing)

Prompt and thinking changes cannot be replayed offline. Add `scripts/eval_thinking.py`:
re-ask N stored questions (from `coach_chat`, with their session context and the same tool
results the original turn saw, replayed through the tool layer) with quick / standard /
deep against the configured model, write the answers side by side to
`docs/research/eval-thinking-<date>.md` for the driver to grade, and record tokens,
wall-clock and the classifier's tier per question. Run it on the Long Beach and Algarve
chats first. *Reason:* the driver's grading is the only ground truth for "insight"; the
script makes it cheap to repeat after every prompt change, and it works for any model
behind the seam.

### 7. Settings

- `coach.thinking`: `"auto"` (default, the router) | `"off"` (always quick) | `"always"`
  (standard everywhere, deep on request).
- `coach.classifier_model`: a model id, or `"none"` (keyword rules only).
- Spoken overrides: "think about it" / "take your time" → deep; "quick answer" → quick.

### 8. Cost and latency (estimate, to measure with §6)

Classifier: ~0.3–0.6 s and a fraction of a cent per garage question; never on the on-track
path. Adaptive thinking on a hosted mid-size model: a few hundred tokens on a simple
question, 2–8k on a multi-tool review, billed as output. Deep tier on a full race review: on
the order of $0.05–0.15 and 10–30 s before the first word. A local model's deep tier is one
extra call, so roughly double its answer time. The meter shows it; the driver decides. Quick
tier costs nothing extra.

## Scope

**Slice 1 (one overnight run, no rig):** the tier on `run_stream` through base/fake/factory/
claude; the hard gates + keyword floor (`thinking_tier()`) with tests; `Model.classify` on
the seam with the JSON schema, wired for the Anthropic adapter and `FakeModel`; the router
using it with the 800 ms timeout and fallback; the deep-tier acknowledgement line; the
settings; the chat-panel "thinking…" label; INFO logging of thinking text and every
classification.
**Slice 2:** `eval_thinking.py` and the first graded run; the optional reasoning view; the
prose-planning deep tier for models without native thinking (when a second provider or a
local model is wired in — not before).
**Rig:** first-word latency on quick tier over PTT; whether standard tier in the garage
feels slow; whether the classifier's tiers match what the driver would have chosen.

## Invariants preserved

- **Swappable model:** tiers, the classifier contract and the eval are model-agnostic;
  adapters map them. No vendor name outside `simcoach/model/`.
- **Measured only / code extracts:** thinking and the planning scratchpad never produce a
  figure; the #159 checks run on the spoken answer.
- **Owned corners:** the classifier's `corner` entity is matched to the map by code before
  it reaches the coach; an unmatched name is dropped, not passed through.

## Open questions for the driver — DECIDED 2026-09-25

1. **Garage tier:** standard by default (adaptive thinking on every garage question).
2. **Reasoning view:** collapsed block under the answer, off by default (a setting).
3. **Post-race questions:** automatically deep (finished race + review question).
4. **Spend cap:** none; thinking tokens show on the usage/cost meter.
5. **Classifier default:** the cheapest hosted model, configured on its own
   (`coach.classifier_model`), swappable like the coach model.

Status: design APPROVED for slice 1. Tracked in the issue linked from the commit.

### Original questions (for the record)

1. **Standard by default in the garage?** Thinking on every typed/conversation question adds
   a second or two and some cost; or keep the garage quick and think only when the
   classifier says deep?
2. **Reasoning view:** see the coach's thinking in the chat (collapsed, off by default), or
   keep it log-only?
3. **Deep on post-race questions automatically** (classifier's call), or only when you ask
   for detail?
4. **Spend cap:** a per-answer thinking cap or none?
5. **Classifier default:** the cheapest hosted model, or the coach's own model (simpler, one
   config, slower and dearer per question)?

## Appendix: other harness patterns — driver decisions (2026-09-25)

Reviewed against the coach, with the driver's ruling on each:

| Pattern (from coding harnesses) | Ruling | Why / shape |
|---|---|---|
| Self-review pass before answering | **No** | A coach can't second-guess itself, and it adds latency the answer can't afford. The #159 mechanical grounding stays; no model review pass. |
| Playbooks for recurring asks | **A handful only** | Post-race debrief, time-on-table, setup direction for a balance complaint — fixed tool sequences narrated by the model. More than a handful defeats the point of having the AI. |
| Sub-agents / background jobs | **No** | Don't belong here. Deep tier is one call that acknowledges first, not a background task. |
| In-the-moment feedback capture (thumbs, "that was useless") | **Development/testing only** | Useful for the eval loop; end users must opt in before any of it leaves the machine. |
| Show the work (tool trace) | **Desktop analysis only** | Not in the rig. On desktop, a one-line streaming status ("reading race lap 5 · comparing…"), the way Claude Code shows its current step. |
| Coach-written durable memory | **Yes — the original plan** | Per-track/per-car notes the coach adds after a session, reviewed by the driver before they're kept (the existing Settings memories pattern). |
