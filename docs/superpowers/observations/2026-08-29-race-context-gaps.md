# Observations — race-context gaps in the coach (2026-08-29)

Logged after a live session at Spa (IDEC Sport 2025 #28 LM, practice + qualy +
race). The coach answered from **corner geometry only** and had no idea about
track conditions, tire state over the lap, traffic, or which session a lap came
from. This file records what's missing and candidate fixes to pick up Monday.
No code was changed. All line refs are current as of commit 7ad2e58.

## What the last session's data shows

- `simcoach.sqlite` → `laps`: 17 laps. 16 Spa laps (~131–201 s) + one 230 s Le
  Mans lap left over from a prior session, all in one undifferentiated pool.
- **`phase` and `session_id` are NULL on every row.** The columns exist (schema
  v2, `store.py:23-24`) and `best_lap()` already accepts a `phase` filter
  (`store.py:120-130`), but capture never writes them, so nothing can be
  filtered by session type.

## Gap 1 — no practice/qualy/race separation (the "all recent laps" problem)

- **Reference lap is chosen across ALL laps.** `app.py:76` calls
  `store.best_lap(record.track, record.car, exclude_lap_id=...)` with **no
  `phase`**. So a race lap gets its per-corner deltas measured against your
  fastest *qualy* lap (lighter fuel, fresh tires, hot lap) — an unfair mismatch
  that makes every race lap look worse than it is.
- Root cause is upstream: `build_lap_record()` (`smm.py:240-267`) never sets
  `phase`/`session_id`, so the discriminator the delta logic needs doesn't
  exist in the data.
- **Fix candidates (Monday):**
  1. Populate `phase` at capture. Scoring already exposes `mSession`
     (`rf2structs.py:279`) — map its session-index ranges to
     practice/qualy/race and stamp each `LapRecord`. Also stamp a
     `session_id` (e.g. a per-app-run uuid, or reset when track/session
     changes) so `session_lap_ids()` (`store.py:145`) actually returns rows.
  2. Pass `phase=record.phase` into the `best_lap()` call at `app.py:76` so the
     reference lap comes from the same session type. Consider preferring the
     best lap in the *current* `session_id` first, then falling back to same
     phase, then all-time.
  3. Surface the phase in the prompt so the coach frames advice correctly
     ("this was a race stint lap on worn tires" vs "a qualy hot lap").

## Gap 2 — no track conditions (wet/dry, track temp, ambient)

- **Not captured at all.** The weather/session-state fields live in the
  `rF2ScoringInfo` tail, which is an unbroken 440-byte placeholder — see the
  docstring at `rf2structs.py:271-287` ("breaking it out (weather, session
  flags, etc.) is future work"). Nothing reaches the prompt; `build_prompt()`
  (`coach.py:73-161`) has no conditions section.
- Consequence: coach can't tell a wet lap from a dry one, or account for a cold
  vs. hot track — so its setup/driving advice is blind to the single biggest
  driver of grip.
- **Fix candidates (Monday):**
  1. Break out the weather fields from the ScoringInfo tail (ambient temp,
     track temp, min/max path wetness, rain/dark flags). This needs the
     empirical offset work the docstring flags as pending — a spike, not a
     one-liner. **Alternative:** the LMU REST client (`lmu/rest.py`, spike
     already done) may expose weather more cheaply than reverse-engineering the
     struct tail — check there first.
  2. Snapshot conditions once per lap onto `LapRecord` (they barely change
     within a lap) rather than as a 50 Hz channel.
  3. Add a "Track conditions" section to `build_prompt()` and a line to
     `SYSTEM_PROMPT` telling the coach to weigh grip/temp/wet before reading
     the corner data.

## Gap 3 — tire temps are captured but never reach the coach

- **The data already exists.** `sample_from_telem()` records
  `tire_temp_fl/fr/rl/rr` every sample (`smm.py:233-236`). It's dropped before
  the LLM: `CornerMetrics` (`analysis.py:10-22`) has no tire-temp field, and
  `build_prompt()` never surfaces it. So "tire temps throughout" is a
  presentation gap, not a capture gap — cheapest of the three to close.
- **Fix candidates (Monday):**
  1. In `_metrics_for_corner()`, reduce the per-corner tire-temp window to
     summary stats (e.g. mean/peak per axle, or FL/FR/RL/RR at apex) and add
     fields to `CornerMetrics`.
  2. Optionally a lap-level tire summary (warm-up trend: cold early corners vs.
     up-to-temp later) so the coach can flag "front-left never came in."
  3. Emit into the corner-facts line in `build_prompt()` (`coach.py:87-95`).
  4. Ties into the existing cold-tire caveat already in `SYSTEM_PROMPT`
     (`coach.py:53-55`) — right now that instruction fires with no tire data
     behind it.

## Gap 4 — no traffic awareness

- Not captured. `mNumVehicles`/`mPlace` exist in scoring
  (`rf2structs.py:222,285`) but scoring **vehicle lookups are flagged
  UNRELIABLE** until the tail sizing is confirmed (`rf2structs.py:17-23`), and
  nothing feeds "were you in traffic this lap" into the lap or prompt.
- Consequence: a lap wrecked by a slow GT car through a corner reads as a
  driver/setup mistake.
- **Fix candidates (Monday):** lower priority; depends on the same
  ScoringInfo-tail reliability work as Gap 2. Minimal first cut: a per-corner
  "clear / traffic" flag derived from an abnormal speed loss vs. reference that
  the driver can confirm, rather than trusting opponent positions yet.

## Suggested Monday order (cheapest → hardest)

1. **Gap 3 (tire temps to prompt)** — data already captured; pure
   analysis+prompt plumbing. Highest value-per-effort.
2. **Gap 1 (phase/session split)** — populate `phase`/`session_id` at capture +
   pass `phase` to `best_lap()`. Medium effort, fixes the unfair-delta problem.
3. **Gap 2 (conditions)** — check REST client first; struct-tail break-out is a
   spike. High value, higher cost.
4. **Gap 4 (traffic)** — gated on the same tail-reliability work; do last.

Architecture note: none of this violates the invariants — conditions/tire/phase
are all *computed or read facts* handed to the LLM, never model-guessed, and
all flow through the same two-stage `analyze_lap → build_prompt` seam.
