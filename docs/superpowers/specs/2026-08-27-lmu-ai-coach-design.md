# LMU AI Coach — v0.1 Design

Date: 2026-08-27
Status: approved for spec review

## Goal

A single long-running terminal app that captures laps live from Le Mans
Ultimate (LMU), then answers between-laps questions about what to change —
**setup and driving** — in one conversation. Corners are always identified
correctly, never guessed by the LLM.

## Scope

**v0.1 is:** LMU only, one car, one track. Silent live capture; pull-based
chat ("how was that lap?"). Setup + driving advice in one coach conversation.

**v0.1 is not (deferred, each bolts onto a left-open seam):** voice, in-ear
live coaching, Assetto Corsa, multiple tracks/cars, local model, vector-DB
RAG, HTTP server, web UI, GUI, CrewChief replacement.

## Architecture

One process. A UI-agnostic **core engine** exposed via an internal function
API; the terminal is the first thin client. A future local HTTP server + web
UI wraps the *same* core with no engine rework.

```
core engine (UI-agnostic)
  Capture (bg thread) -> lap store (+ setup snapshot)
  Feature extractor -> Coach -> Model
        ^ internal API (function calls)
  v0.1: terminal client   |   later: local HTTP server -> web view
```

Discipline this buys: coach/analysis logic lives behind function calls both
the terminal and a future server call — never inside the terminal loop.

## Components

1. **Capture service** — background thread polling LMU shared memory (~50 Hz).
   Buffers current lap; on lap completion writes a lap record (all channels +
   track/car/laptime) to the lap store, plus a snapshot of the active setup.
   Silent — no per-lap notification. *Built on interfaces verified by the
   spike (see Risk).*
2. **Corner map** — per-track JSON we own: `lap-distance -> {number, name,
   direction, apex}`. Direction **computed** from telemetry geometry
   (lateral-G / heading sign); names/numbers **hand-curated** from public
   references. Doubles as the "learn corner names" feature.
3. **Feature extractor** — pure, tested functions. In: lap record + corner map
   (+ optional reference lap). Out: per-corner metrics (entry/apex/min/exit
   speed, brake point, brake & throttle traces, trail-brake overlap, tire
   temps/pressures) and deltas. No LLM. The deterministic anti-hallucination
   core.
4. **Knowledge base** — small curated markdown notes on LMU/car-specific
   tuning, injected into the coach prompt. Flat files in v0.1 (Gemini notebook
   mined as a source); grown as gaps appear. No vector DB until notes outgrow
   whole-file loading.
5. **Coach** — takes structured analysis + corner **facts** + relevant
   knowledge notes -> LLM -> conversational answer with follow-ups. Claude API
   behind a swappable `Model` interface (cloud now, local-capable later).

## Data flow

LMU -> Capture -> lap store + setup snapshot. On user question: Feature
extractor builds a structured summary -> Coach loads corner facts + knowledge
notes -> LLM answers -> follow-ups. The LLM only ever **receives** corner
identity as prompt fact; it is structurally prevented from guessing left/right
or names.

## Data model & persistence

Captured data must support **historical, cross-session** analysis (trends,
best-lap selection, setup comparison) — not just the current run. Split by
access pattern:

- **SQLite** (stdlib `sqlite3`) holds everything queried across laps/sessions:
  - `laps(id, ts, sim, track, car, lap_time, valid, setup_id, telemetry_path)`
  - `setups(id, ts, track, car, params_json, source)`
  - `corner_metrics(lap_id, corner_no, entry_spd, apex_spd, min_spd, exit_spd,
    brake_point, throttle_point, trailbrake_overlap, ...)` — one row per corner
    per lap, so per-corner history is a plain query.
- **Per-lap channel files** hold the raw ~50 Hz time-series (referenced by
  `laps.telemetry_path`). Stored as compressed numpy `.npz` — loaded whole by
  the feature extractor, never queried row-by-row, so no columnar/Parquet
  dependency is needed. (Parquet is a drop-in swap later only if external tools
  must read channels directly.) Channels are stored at **full capture rate**
  with both a time and a **lap-distance** axis — distance is the alignment key
  a future replay / side-by-side overlay needs (motorsport overlays align on
  distance, not time). Recording this fidelity now is what keeps that feature
  possible later; nothing in v0.1 consumes it yet.

The feature extractor writes `corner_metrics` as a side effect of analyzing a
lap, so history accumulates automatically. "Best stored lap" (the reference
default) becomes a `SELECT min(lap_time)` on `laps`. Schema is versioned
(a `schema_version` pragma) so it can evolve without orphaning old data.

## Defaults

- **Reference lap** = user's own best stored lap for that track/car. None yet
  -> absolute analysis (min speeds, brake traces), no delta. No alien-lap
  sourcing in v0.1.
- **Stack** = Python on Windows. Terminal chat client.
- **Model** = Claude API behind a `Model` interface; local impl deferred.

## Model providers (multi-provider, added 2026-08-28, user)

The `Model` protocol (`complete(system, messages) -> str`) is already provider-agnostic
— the same seam left for local models supports other ONLINE providers too. v1.0 ships
Anthropic (`ClaudeModel`), but the design supports OpenAI, Google Gemini, others, and a
local model, each as a `Model` implementation with no change to the coach/pipeline.

- **Provider selector.** A `make_model(provider, model, api_key)` factory picks the
  implementation from config/env, so switching providers is configuration, not code.
  v1.0: "anthropic" (built) + "fake" (tests); "openai"/"gemini"/"local" are clear
  extension points (implement the protocol + read that provider's key). Keep the
  message format provider-neutral (system + role/content messages — already so).
- **Keys.** Each provider reads its own key from env (ANTHROPIC_API_KEY now; others as
  added). The user needs to procure an Anthropic API key for real coaching (the coach
  path has only run against FakeModel so far — see [LUNCH]).
- Do NOT scatter SDK calls: a provider's SDK is imported ONLY inside its own
  `simcoach/model/<provider>.py` (as `anthropic` is confined to `model/claude.py`).

## Testing

Feature extractor gets real unit tests against a recorded-lap fixture,
including an assert that computed corner direction matches known geometry.
Capture and coach stay thin.

## Online racing context + tier model (added post-approval)

The user races LMU **online**: sign up for a daily race, practice on servers until it starts. Daily races come in tiers, which constrain what the coach may advise and imply a fuel strategy:

| Tier | Setup freedom | Duration | Pit |
|------|---------------|----------|-----|
| Bronze | FIXED setup — only TC, brake bias, ABS (where applicable) adjustable | ~20 min | none |
| Silver | Partial customization (more than Bronze, less than open) | ~30 min | usually none |
| Gold | Fully open setup | ~40 min | ≥1 pitstop for refueling typical |

Design implications:
- **Coach must be tier-aware (correctness, not polish).** Suggesting an illegal adjustment (e.g. springs in Bronze) is wrong advice. The coach takes a `tier` (or explicit allowed-adjustments set) and constrains setup suggestions to what that tier permits; driving advice is unconstrained. Folded into the coach (Task 9) since it is not yet built.
- **Fuel calculator (new, early feature).** Deterministic module (no LLM): from the telemetry fuel channel compute `fuel_per_lap` (lap-over-lap consumption), estimate race laps ≈ race_duration ÷ representative_lap_time, output total fuel needed (+ margin), whether a pitstop is required, and fuel-to-add. Requires adding a `fuel` channel to `LapRecord` + the fixture. Standalone; independent of the coaching pipeline. YAGNI: no tire-wear/weather modelling — just fuel-per-lap × laps + margin, with a calibration margin knob.

## v1.0 vision: race-weekend workflow (roadmap, not v0.1)

The tool walks a race weekend, carrying data forward between phases:

1. **Practice** — offer a baseline setup for the car/track combo to tweak.
2. **Qualifying** — monitor; estimate qualy fuel level.
3. **Race** — final setup + driving-guide tweaks; fuel calculated from practice+qualy data (see fuel calculator).
4. **Post-race** — analyze what to improve for the NEXT race (daily races run on a timed cycle: 3 per tier, you finish in time to rejoin the same tier).
5. **Post-v1.0** — lap-by-lap in-ear guidance.

Post-v1.0 backlog:
- **Lap-by-lap in-ear guidance** (real-time spotter).
- **Incident detection + causal analysis.** From telemetry, detect spin-outs (yaw-rate/lateral-G spike + speed drop + wheel slip) and race contact (sudden longitudinal deceleration; damage flags if LMU exposes them). Then the harder, valuable layer: causal interpretation — e.g. "you spun because you lifted mid-corner when you should have stayed on the brake," or whether a crash was the user's fault / avoidable. Same deterministic-features -> LLM-interpretation pattern as the coach, applied to incident windows. Feasibility: detection is straightforward from existing channels; causal attribution is the research-worthy part.

Architectural affordance this requires (and how v0.1 already allows it):
- A **session/phase model** (practice/qualy/race grouping; fuel + baseline carried across phases) sits ABOVE the core. It is a future `session_id` / `phase` tag on stored laps — additive, and the `laps` schema is already versioned (`user_version`), so it evolves without orphaning data. No v0.1 change needed.
- Constraint for v0.1 code: nothing may hardcode "one lap in isolation" — the store already keys laps by sim/track/car and supports history, which is what phase-aware analysis will build on.
- Each phase reuses the SAME core (feature extractor, coach, fuel calc); phases differ in which laps feed in and which output is emphasized, not in new pipelines.

## Energy as BoP + hybrid regen (added 2026-08-28, user)

LMU balances performance partly via ENERGY, not just fuel volume. Hypercars (LMH/
LMDh) have a per-stint ENERGY allocation (MJ) and a hybrid system that regenerates
under braking and deploys on power; regen blends with friction braking and shifts
brake behavior. GT3s are closer to straight fuel-limited. The tool must accommodate
all of it:

- **Energy is universal.** ALL cars (GT3 included, not just Hypercars) consume an
  energy allowance, replenished at pit stops together with fuel. So the energy model
  is not a Hypercar special case — it applies across the board.
- **Resource-agnostic stint planning.** The fuel calculator generalizes so the
  binding per-stint constraint can be fuel (liters) OR energy (MJ), whichever forces
  the pit first. Compute both plans and take the binding one. The stint energy
  budget comes from BoP tables / user config, not telemetry.
- **Adjustable fuel↔energy ratio (mechanic researched 2026-08-28; confidence
  likely).** LMU's fuel-to-energy ratio splits the stint's Virtual Energy (NRG)
  budget between physical fuel and battery share (Hypercar; e.g. 0.96 ⇒ ~4% from
  battery, track-dependent). GT3 has no hybrid, so the setting instead sets liters
  of fuel per requested NRG%. NRG resets to 100% at every pit stop; draining NRG to
  0% mid-stint triggers an instant stop-and-go penalty. **The tuning goal is
  fuel-laps-remaining ≈ NRG-laps-remaining** — neither should run out first. This
  VALIDATES `binding_stint`: the binding resource is whichever depletes first, and
  the ratio's job is to equalize them. Units seen as %/kWh in-game (no confirmed MJ).
  Source: docs/superpowers/research/2026-08-28-lmu-energy-mechanics.md (community
  sources; official S397 manual was unreachable — treat as likely, confirm live).
- **NRG is universal.** Both GT3 and Hypercar carry a Virtual Energy allowance
  (GT3's is fuel-only, no battery term) — energy is not a Hypercar special case.
- **Lift-and-coast / fuel-save advisor (corrected 2026-08-28 by user).** LMU DOES
  have a real-life-style fuel-save system: you set a target ("how many laps to save
  for") in the **MFD**, and the game then drives the **in-car fuel-save lights** that
  cue you when to lift — no track-position HUD marker, but a genuine target-driven
  indicator. So the coach's role is not to replace the lights but to set the STRATEGY:
  `stint_save_advice` computes the per-lap save % / the target the driver punches into
  the MFD, and `ratio_balance` says which of fuel/NRG is short. Deterministic, built
  (T-H). Open questions (live): is the fuel-save light state and/or the MFD target
  exposed via telemetry (shared memory) or REST? If so, the coach can read whether the
  driver is on target and coach the lift directly. Add to the spike.
- **New telemetry lead:** `GET http://localhost:6397/rest/strategy/usage` returns
  Virtual Energy history — add to the feasibility spike / REST client.
- **Energy telemetry channels (seam).** Capture state-of-charge, energy deployed,
  energy regenerated where LMU exposes them (exact struct/REST fields pending live
  validation). `energy_per_lap` is derived like `fuel_per_lap` (channel delta).
- **Regen-braking coaching (deferred, needs domain input + live data).** For
  Hypercars the coach's braking advice must account for regen blending (brake
  balance, lift-and-coast for energy, deployment strategy). Modeled later with the
  user; knowledge-base + coach-context concern, not a deterministic calc.

## Knowledge freshness: physics/tire-model updates (added 2026-08-28, user)

LMU is in active development; each update can change the physics / tire model / BoP,
which can invalidate setup and driving advice in the knowledge base (e.g. a tire-model
change shifts optimal pressures; a BoP change alters energy/fuel). Concrete example:
the LMGT3 **ABS control was reworked** — originally a plain 0–9 "higher = more" scale,
later changed to the current system (a 0–9 dial organized into oversteer / balanced /
understeer groupings, per the community "New ABS system" thread now in the KB). Advice
written for the old scale would be wrong; the KB currently reflects the CURRENT system,
but this is exactly the kind of mechanic that silently changes under an update. So the
knowledge base is VERSIONED against a game build and re-validated on updates:

- **Tag knowledge with the LMU build it was validated against.** We can read the live
  game version from shared memory (`gameVersion`, seen as 14130/14237 in the spike).
  Store the build each track/car note + setup guide was validated against.
- **On every game update, research the patch notes** for physics / tire-model / BoP /
  aero changes (official LMU notes + CrewChief changelog + community), update the
  affected knowledge, and flag advice that needs re-validation. This is a recurring
  maintenance task, not one-time.
- **Staleness warning (seam).** If the live `gameVersion` differs from the build the
  loaded knowledge was validated against, the coach should note that its setup/tire
  advice may be out of date for the current build — degrade to lower confidence rather
  than assert stale numbers. (Deterministic check; buildable when we wire live capture.)
- **Sources to watch:** official LMU patch notes/roadmap, the CrewChief changelog
  (already tracked), and the community — same monitoring cadence as the MMF-rename check.

### Knowledge-base UPDATE MECHANISM (design — decide with user)

The KB must stay current over time. Two poles (user framing) + a recommended hybrid:

- **A. Agent/maintenance-time refresh (recommended primary).** A repeatable
  "knowledge refresh" workflow — a research agent (the pattern already used here:
  ingest patch notes + curated sources → update versioned KB files → reviewed commit),
  run per game update. Keeps curation quality high (sourced, human-reviewable, shipped
  to all users). Formalize the ad-hoc research runs into a documented procedure / skill
  with a checklist (what to re-check on an update: tire model, BoP, aids like ABS/TC,
  aero, MMF names). This is the backbone.
- **B. In-engine runtime updates.** The app itself: (i) STALENESS DETECTION (live
  gameVersion vs KB's validated build → warn / lower confidence) — cheap, build early;
  (ii) ON-DEMAND RESEARCH HOOK — when knowledge is missing/stale for the current build,
  the coach asks the Model (with web tools) to look up that specific mechanic live, and
  optionally caches the answer for review; (iii) KB PACK FETCH — pull updated,
  version-matched KB bundles from a remote the maintenance workflow publishes.
- **C. Continuous personal learning (already seeded).** Driver profile + observed_style
  + the user's own lap history update at RUNTIME from driving — distinct from curated
  knowledge; always on, no game-update gate.

Recommended: **A as the backbone** (versioned file KB + a refresh skill/checklist),
**B(i) staleness detection early**, **B(ii) on-demand research as a strong enhancement**
(fits the coach's Model+web capability), B(iii) only if distributing to other users.
Keep curated knowledge (A/B) separate from personal/learned data (C) in storage and in
the prompt. DECISION for the user: how much lives in-engine vs. maintained by an agent,
and whether this ships to just the user or to others (affects B(iii)).

## Capture robustness: game updates + anti-cheat (added 2026-08-28, user)

LMU updates can rename/reshape the shared-memory files (CrewChief has flagged such
renames), and anti-cheat changes could affect external access. The capture layer
must survive both:

- **Multi-candidate, version-checked MMF reader.** Do NOT hardcode a single MMF name.
  Keep a config-driven ordered list of known names ($rFactor2SMMP_Telemetry$ /
  $rFactor2SMMP_Scoring$, native LMU_Data, and future ones), probe each, and use the
  first that opens. On open, VALIDATE: expected struct version field + a plausibility
  sanity check (e.g. numVehicles in range, inputs within [0,1]). On mismatch or none
  found, raise a clear "shared-memory interface changed — update MMF names/struct
  (see CrewChief for canonical names)" error — never silently misread.
- **Compatibility/health check on startup** (and a periodic re-check): confirm a live,
  consistent, sane frame before trusting capture; surface interface drift to the user.
- **Anti-cheat stance (hard rule).** Use ONLY the sanctioned, plugin-published shared
  memory (and the local REST API), READ-ONLY. NEVER inject into or ReadProcessMemory
  the game process — that is what trips kernel anti-cheat. This is the same footprint
  as CrewChief / SimHub, which coexist with LMU's AC. Monitor CrewChief/community for
  AC-related breakage; degrade gracefully (clear message) if the interface stops
  responding rather than escalating access.
- **Monitoring source (canonical).** CrewChief's auto-update changelog tracks LMU
  shared-memory + anti-cheat changes:
  https://thecrewchief.org/downloads/change_log_for_auto_updated.html — check it for
  MMF renames / struct changes when capture breaks or before a compatibility bump;
  keep the MMF-candidate list aligned with what CrewChief uses.

## Future feature: in-game chat read/send (user 2026-08-28)

Idea: let the coach read/post to LMU's in-game chat. Likely a SANCTIONED path exists:
`ChatTransceiver.dll` is present in LMU's `Plugins/` folder (seen in the feasibility
spike) — it is the plugin CrewChief uses to inject chat messages into rF2/LMU, so
SENDING is probably writing to its shared-memory/message interface (research its API).
READING chat may come via the rF2 messages/scoring stream. Keyboard emulation (Enter →
type → Enter) is the crude fallback. Research spike later; keep it read/send behind a
small interface. (Anti-cheat: prefer the plugin interface over synthetic input.)

## First steps: two spikes (before implementation, parallelizable)

1. **Feasibility spike (technical).** Whole design rests on reading LMU live
   telemetry, current setup, and lap boundaries. Those interfaces (rF2
   shared-memory plugin, LMU local REST API, setup file format) are
   **plausible but unverified**. Prove we can read all three on the user's
   machine. If interfaces differ, only the Capture component changes — seams
   isolate it.
2. **Research spike (product / prior art).** Survey what CoachDave Academy
   (setups, reference laps, Delta overlay) and similar tools (e.g. Track
   Titan, RaceStudio/MoTeC, Simulator-Controller) actually offer. Output: a
   findings doc feeding the roadmap/backlog. Read-only; does **not** by itself
   expand v0.1 scope — any feature it surfaces is triaged into v0.1 or
   deferred explicitly.
