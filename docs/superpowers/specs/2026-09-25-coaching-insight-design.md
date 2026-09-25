# Coaching insight: from stating the obvious to finding the time — design (2026-09-25)

Status: design for the driver's review (issue #169). No code yet.
Evidence: chats `chat-20260924T223706-cca7c5` (Long Beach, turns #236-#242) and
`chat-20260924T232917-c69dd5` (Algarve, #243-#254), their stored tool results, and the
session laps re-analysed read-only from a copy of the store with the shipped analysis code.
Builds on `2026-09-08-engineer-grade-analysis-design.md` (Phase 1 shipped) and
`docs/research/2026-09-08-race-engineer-lap-analysis-gaps.md`.

## Problem

> "the coach needs to provide good insight. oftentimes, it's just stating the obvious."

Two answers the driver called out:

- Long Beach #237: *"brake later and harder once the tires are up to temp, don't creep
  the point back every lap — pick a landmark and trust it"*. The driver's reply (#238):
  "Thanks, Captain Obvious."
- Algarve #244 and #250, "on my best lap, where can I save 3 tenths?": *"This IS your best
  lap, so there's no reference lap ahead of it to compare against — but it's carrying a
  lot of coasting, 7.7 seconds total"*, then (#251) "that closes most of the 1.3 seconds
  you're losing" at T14.

The first is a technique tip anyone could give without data. The second is worse: the
coach had 12 other clean laps to compare against and said it had none. It then took a
coasting duration (1.3 s at T14) and presented it as time lost. No comparison supports
that: the most any lap gained there was 0.20 s (see 1.1).

## 1. Why the answers are obvious

### 1.1 Algarve #244/#250: the best lap is compared to itself

The model called `get_lap_summary(which="best")` both times. The path:

- `_resolve_lap("best")` returns the record with `lap_id=None`
  (`simcoach/coach_tools.py:1066-1071`).
- `_analysis_for(record, None)` picks the reference with `exclude_lap_id=None`, so the
  session best it finds **is the same lap** (`coach_tools.py:1043-1048`).
- `get_lap_summary` resolves the time-loss reference the same way
  (`coach_tools.py:1244-1245` → `_reference_lap`, `:1268-1274`). `get_time_loss` has a
  self-comparison guard (`:1294-1296`). `get_lap_summary` does not.

What the model received: `Net -0.00s vs your session best. No single section stands out —
this lap tracks the reference closely.` (`format.py:366-367`), then 15 corner lines each
ending `(min vs best: +0.0 mph)`, plus `Coasting this lap: 7.7 s`. The only comparison was
empty, so the only thing left to say was the biggest absolute number: coasting. Coasting
is a description, not a loss. Nothing in the tool says whether it cost time.

What the store could have said. There were 12 other valid laps in the session before
#250 (qualifying 2-6 and race 1-9, minus the partial race lap 6):

- **The game's own sector times** (`laps.s1/s2/s3`, `store.py:233`, not read by any coach
  tool): the best lap is 22.652 / 38.012 / 45.573. The driver's best sectors are 22.547
  (race lap 5), 37.940 (qualifying lap 4) and 45.479 (qualifying lap 3), which add up to
  **1:45.966, 0.27 s under the best lap**. That is his three tenths, measured by the game.
- **Per-corner delta-t against every other lap** (`align_time_axes`/`time_loss_buckets`,
  `analysis.py:361-469`, run once per lap against the best). This data disagrees with the
  coach's *reasoning*, not always with its location. Through T14 to T15, the typical lap
  is level with the best, but race lap 4 was 0.20 s quicker. It got back to throttle 5 m
  sooner and coasted 0.65 s against 1.28 s. At T11 the relationship reverses: race lap 2
  coasted 1.3 s against 0.3 s and carried 6 mph more minimum speed. So coasting is not the
  loss. The time delta shows where the loss is, and the cue comes from what the faster
  pass did.

### 1.2 Algarve #243 and Long Beach #237: "vs best" when the lap is the best

When the subject lap *is* the session best, `_reference_lap` excludes it and falls back to
the next-fastest lap (`coach_tools.py:1268-1272`). The result is still labelled
"your session best", and the corner header says "Deltas below are vs the driver's best lap"
(`format.py:533`).

- Algarve #243 ("I need to find 0.3 s"): the latest lap was qualifying lap 5, the best. The
  reference was qualifying lap 4 (1:46.381), which is slower, labelled "session best".
- Long Beach #237: the latest lap, practice lap 5 (1:23.439), was the best. The only other
  valid lap was practice lap 2, a first exploration lap at 1:36.882. The tool said
  `Net +13.42s vs your session best ... Biggest gain: T2 +9.85s`. So every per-corner delta
  was against a lap 13 s slower. The model saw noise and fell back to general technique
  ("pick a landmark and trust it").

### 1.3 Two metric artifacts the model read as diagnosis

- **"@ 0% peak".** The brake point is searched up to 250 m *before* the corner window
  (`analysis.py:1020-1037`). `peak_brake` is read only *inside* the window
  (`analysis.py:1057`). So a corner braked entirely before turn-in prints
  `brake 43m before apex @ 0% peak` (`format.py:141-143`). Long Beach #241 turned that into
  "you braked way too early there … at basically zero real pressure".
- **Brake point split by modulation.** The brake point is the start of the nearest
  contiguous run with `brake > 0.1` before the apex (`analysis.py:1025-1037`). On the Algarve
  best lap at T10, pressure dips to 0.07-0.08 just before the first apex and is re-applied.
  The detected point becomes the re-application: `brake 1m before apex`. The trace says he
  starts braking about 85 m out. #243 told him he brakes "71 meters later than your best"
  and should brake earlier. In fact his quicker qualifying lap 3 starts braking about
  15 m *later* (2.3). The same artifact inflates `get_consistency`'s brake-point spread
  (`T1 Primeira … brake point ±39 m`, #247).

### 1.4 Root causes

1. **No comparison exists for the best lap.** It gets compared either to itself (1.1) or to
   a slower lap under the wrong label (1.2). "Where is the time on my best lap?" is the
   question a driver asks most, and it is exactly the case the tools cannot answer.
2. **One reference, one axis.** Every tool compares this lap with one other lap. The
   driver's *distribution* is stored but never computed: his typical pass, his best pass
   per corner, his best sectors.
3. **Absolute metrics sit next to comparative ones, with no marker.** A 15-line table of
   about 25 fields per corner mixes descriptions (coasting, trail %, ABS %, peak g) with
   deltas. When the deltas are empty, the model narrates the largest description as a loss.
4. **The prompt licenses generic technique.** "You have deep knowledge of vehicle
   dynamics, car setup, and driving technique — apply it freely" (`coach.py:153-155`). No
   rule says advice must come from a measured difference. No rule says what to do when
   there is none.
5. **Fragile metrics are rendered as facts** (1.3).

## 2. What to compute (code extracts, the model narrates)

### 2.0 Prerequisite fixes (small, all in the first slice)

- **Honest reference.** `get_lap_summary("best")` resolves the lap id through
  `_lap_id_for` (`coach_tools.py:956`), so the lap is excluded from its own comparison. When
  the subject is the best lap, the fallback reference is labelled `vs your next-best lap
  (qualifying lap 4, +0.14 s)`, never "best". The header in `format_lap_facts` takes
  the label instead of hard-coding "best lap". *Reason:* the label is a fact, and a wrong
  fact is worse than no fact.
- **Comparable-pace gate.** A lap slower than 107% of the subject's time is never a
  reference, and never a form sample (2.2). Race lap 1 (the start) and laps with
  `partial`/`pit`/`mapping` are excluded too. With no comparable lap, the tool says
  `No comparable lap yet (practice lap 2 is 16% slower)` instead of printing deltas.
  *Reason:* Long Beach #237.
- **Peak brake from the braking run, not the window.** `peak_brake` becomes the max over
  the same run `brake_point` walked (`analysis.py:1027-1037`), so "@ 0%" is impossible when
  a brake point exists. *Reason:* #241 read it as "zero pressure".
- **A braking-zone start that survives modulation.** New field `brake_zone_start`: the
  first sample with `brake ≥ 0.1` after the last sample with `throttle ≥ 0.8` before the
  apex, inside the existing 250 m look-back. The diagnosis (2.3) uses it. `brake_point`
  keeps its definition and stored `corner_metrics` rows, so no history is rewritten.
  *Reason:* the smallest change that fixes the diagnosis. Reconciling the two fields is
  a follow-up (4).

### 2.1 Optimal lap from the game's sector times (keep)

- **Data:** `laps.s1/s2/s3`, already stored for every lap (`store.py:233`, returned by
  `session_laps`, `store.py:1491`). Sector boundaries come from `laps.sector_bounds`
  (`store.py:1094`) and map corners to sectors. No capture change.
- **Computation:** over the comparable laps (2.0), the minimum of each sector. A sector
  counts only if the lap had no spin/off event in a corner inside that sector
  (`lap_events`, `analysis.py:1648`). Optimal = the sum of the three. Also computed across
  all of the driver's laps at this track and car (one SQL query).
- **Why sectors and not summed corners:** summing each corner's best pass overstates the
  gain. On Algarve it gives 0.9-1.5 s depending on how the lap is cut, against 0.27 s from
  the sectors. Adjacent buckets trade time: race lap 9 is 0.16 s quicker than the best
  lap from T14's apex to its exit, and 0.18 s slower on the run to T15. With 15 segments,
  the minimum of each also collects lap-to-lap noise. The game's sectors are measured, and
  three splits can't be gamed that way. Corners *locate* the time inside a sector (2.2).
  They are never summed into a headline.
- **Emits (Algarve, before #250):**
  ```
  Optimal lap from your best sectors this session: 1:45.966, 0.27 s under this lap:
    S1 (T1-T4) 0.11 s on race lap 5; S3 (T10-T15) 0.09 s on qualifying lap 3;
    S2 (T5-T9) 0.07 s on qualifying lap 4.
  All-time here in this car: best 1:46.237 (this lap).
  ```
  (All-time: the Algarve store also holds a 1:45.886 from 00:23, after the chat. The example
  uses only laps that existed at #250.)
- **Long Beach at #241** shows why the incident rule is needed. Two laps have a faster S1
  (practice lap 5 by 0.71 s, qualifying lap 6 by 0.60 s), and both ran off at T2. Without the
  rule, the optimal lap would claim 0.71 s at "Turn 2 braking", which is the corner the
  driver was struggling with, taken from laps where he left the track. With the rule:
  `S2 (T4-T6) 0.13 s on practice lap 5. Your two quicker S1s both ran off at T2 and don't
  count.`

### 2.2 Corner form table: this lap against his own typical and best passes (keep)

- **Data:** `.npz` channels of the comparable laps, loaded the way `_stint_analyses`
  already does (`coach_tools.py:1306-1344`). No capture change.
- **Computation:** a *segment* is a corner with its approach and its run-out, from the
  previous corner's end to the next corner's start. Braking-point time lands in the
  approach, and exit speed pays off on the run-out. Neighbouring segments overlap, which
  is fine because they are only ranked, never summed. For each other comparable lap L,
  `align_time_axes(L, subject)` gives delta-t, and each segment's `d` is its difference
  across the segment. `d < 0` means L was quicker there. Per segment:
  - `typical` = median over L,
  - `repeatable` = the second-smallest value: at least two laps were this much quicker
    ("two of your laps"), which damps one-off flukes,
  - `donor` = the lap with the smallest value (the diagnosis compares against it, 2.3).

  Rank by `repeatable`. List a segment only if `repeatable ≤ -0.03 s`. The exact axis's
  per-bucket noise is 0.005 s (`format.py:339`). The floor sits well above it, and is a
  tuning knob. At least 5 comparable laps are required. Otherwise:
  `Not enough clean laps near this pace to read your form (2 of 5)`. Segments with an
  event on L skip L.
- **Emits (Algarve, 11 other comparable laps; race lap 1 is excluded):**
  ```
  Where your own laps were quicker than this one (corner with approach and run-out, 11 laps):
    T14 Sagres: level with your typical lap; two laps 0.16 s+ quicker (race lap 4 by 0.20 s).
    T4: level with typical; two laps 0.15 s+ quicker (qualifying lap 4 by 0.16 s).
    T7: 0.03 s slower than typical; two laps 0.13 s+ quicker.
    T10 Portimão: 0.02 s slower than typical; two laps 0.08 s+ quicker (qualifying lap 3 by 0.12 s).
  At your best on this lap (no other lap quicker by 0.03 s): T2, T8, T9, T13.
  ```
  The last line is the honest "nothing measurable here" (2.4).
- **Per-corner consistency** comes along for free: the spread of the same per-segment
  deltas is *time* consistency, which matters more than the min-speed/brake-point sigma
  `get_consistency` reports today. In the first slice it stays inside this table (a
  `varies ±0.08 s` suffix when the spread is ≥ 0.05 s), not a new tool. *Cut:* a separate
  "you nail T3 every lap" tool. The table already says it.

### 2.3 Diagnosis for the top corners: which phase, and what differs (keep)

For the top 2 segments (3 when the driver asks for detail), compare the subject with that
segment's **donor lap**:

- **Phase split** from the aligned delta-t (`align_time_axes`, `analysis.py:361`), cut at
  owned map points: approach `[previous corner end, corner start]`, entry
  `[start, apex]`, exit `[apex, end]`, run-out `[end, next corner start]`. For a
  multi-apex corner, cut at the first apex. The phase with the largest loss is the
  **where**. Code picks it. The model never infers it.
- **What differs**, each emitted only above its existing floor (`format.py` `_DELTA_*`):
  speed at the map apex, min speed, `brake_zone_start` (2.0), peak brake in the zone,
  `throttle_point`, exit speed, coasting inside the window. All are subject minus donor,
  measured the same way on both laps.
- **Emits (Algarve T14, qualifying lap 5 vs race lap 4, the top segment):**
  ```
  T14 Sagres vs race lap 4: 0.20 s, 0.14 of it on the run to T15 after the exit.
    You: throttle 67 m after the apex, coasting 1.3 s, 74 mph at the exit.
    Race lap 4: throttle 62 m after the apex, coasting 0.65 s, 75 mph at the exit.
  ```
  The coach's #251 location was right. With this, it would give the real size (0.2 s, not
  "most of 1.3 s") and a cue from his own lap: get back to throttle about 5 m sooner, as on
  race lap 4.
- **Emits (Algarve T10 vs qualifying lap 3, fourth in the table, and the corner #243
  advised on):**
  ```
  T10 Portimão vs qualifying lap 3: 0.14 s lost before the first apex, 0.12 s net to T11.
    You: braking from ~85 m, peak 82%, released to under 10% before the first apex, then
    braked again for T11; 90 mph at the first apex.
    Qualifying lap 3: braking from ~70 m, peak 70%, one steady pressure to the apex; 95 mph.
  ```
  The coach can now say something no stock tip gives, and it reverses #243's "brake
  earlier": brake 15 m later and lighter, and hold one pressure to the first apex instead
  of releasing and braking twice. That is also the track note's own advice for T10-11
  ("straight-line braking to the first apex, then trail-brake…"), now shown in *his* data.
- **Emits (Algarve T11 vs race lap 2):** `0.12 s before the apex; min 50 vs 56 mph; race
  lap 2 coasted 1.3 s into it vs your 0.3 s`. Here the faster lap coasted *more*. At T14
  it coasted less. That is why coasting is never called a loss without a time delta (3).
- *Cut:* a "cause classifier" that picks one label (brake point, min speed, throttle or
  coasting). The phase split and the differing facts already let the model name the
  cause, and a classifier would only add a heuristic to tune.

### 2.4 "Nothing measurable" (keep, it is one line)

When no segment clears the floor, and the subject's sectors are its own best:
`No corner where another lap of yours was measurably quicker: this lap is your best
everywhere by more than 0.03 s. The remaining gap is to the class benchmark (2.5).` The
coach then says he is at his own limit, and the next step is a reference: a faster
driver's lap, a setup change, or more laps. It does not invent a fix.

### 2.5 Class benchmark (keep, header line only)

`notes.reference_times` holds a total per class, with no splits
(`coach_tools.py:1672`). The tool adds one line:
`Class benchmark (track notes, community): LMGT3 1:42.879, 3.36 s ahead of this lap. Total
only, no sector splits, so it can't say where.` *Reason:* it answers "how far off am I"
and stops the model from pretending it can place the benchmark gap on corners.

### 2.6 Evaluated and deferred

- **Trend across laps at a corner** (improving, plateaued, regressing), and across
  sessions. The per-segment deltas are ordered by lap, so a slope is cheap, but it answers
  a different question ("am I improving at T10?"). Deferred to a follow-up, where it
  extends `get_progress` with the all-time optimal lap per session.
- **Rival comparison after the race.** `battle_laps` **is stored** per lap: 276 rows over
  15 of the Algarve race laps (`store.py:931-960`). `build_battle` (`battle.py:327`) runs on
  stored `CarLap`s. `get_battle` refuses outside a live race only because it reads the live
  snapshot (`coach_tools.py:1820-1828, 1986-1989`). So #248-#249's "that data's gone now"
  was false. Follow-up: when no race is live, `get_battle(rival, which='race lap N')` builds
  from `store.battle_laps(lap_id)`. It is small, but it is a separate question from #169.
- **G-G / setup channels:** no comparative signal for these questions. The setup channels
  still need capture (engineer-grade Phase 0). `mGripFract`/`mTireLoad` stay dead and are
  never stated.

### 2.7 Tool shape

A new tool, **`get_time_on_table(which="best")`**, returns 2.1 + 2.5 + 2.2 + 2.3 for the
top 2 + 2.4, about 15 lines. `which` accepts any lap. For a non-best lap, "form" is
still that lap against the other comparable laps. `get_lap_summary` on the best lap drops
its empty time-loss line and says instead: `This is your best lap: get_time_on_table
finds the time left in it.` *Reason:* keeping this out of `get_lap_summary` keeps the
per-corner dump from growing. The question "where is the time" gets one call that answers
it, which is the pattern the race tools use (`coach.py:237-240`).

Cost: one `align_time_axes` per comparable lap, plus up to 3 `analyze_lap` calls. It
reuses `_analysis_for`'s cache and the loaded records, so for 12-25 laps it costs about
the same as `get_consistency`.

## 3. How the model narrates

Prompt changes in `AGENTIC_SYSTEM_PROMPT` (`coach.py:152`):

1. Replace "apply it freely" (`coach.py:154-155`) with: *"Use that knowledge to explain
   and act on what the tools measure, not in place of it."*
2. Route (next to the `get_lap_summary` rule, `coach.py:164-168`): *"'Where's the time',
   'find me N tenths', 'how do I go faster', or any question about the best lap →
   get_time_on_table FIRST. There is ALWAYS something to compare the best lap against
   (his other laps, his best sectors). Never say there is no reference."*
3. New block, **LEAD WITH THE DIAGNOSIS**:
   *"Answer with the corner, the phase the time is lost in, and what the driver's own
   faster pass did differently, then ONE change taken from that difference ('brake about
   15 m later and hold one pressure to the first apex, like qualifying lap 3'). One
   actionable thing per corner, at most two corners unless asked for more. Coasting, trail
   %, ABS %, TC %, peak g and steering are descriptions: call one a loss only where a time
   delta in the tool says the time went there. Give a general technique tip (landmarks,
   'trust it', smoothness, tire temperature) only when the driver asks for technique, or
   the tool has no comparison. Then say that is general advice. When the tool lists a
   corner as 'at your best', say he is at his own limit there. When nothing clears the
   floor, say so and name the next step the tool gives (the benchmark gap, a setup change,
   more laps). Never invent a fix."*
4. Voice (`VOICE_PTT`, `coach.py:366`): the default 2-3 sentences become one corner:
   the corner locator, the phase, the one change. Example: *"Turn 14, the slow right onto
   the run to Turn 15: two tenths on the exit. Get back to throttle about five meters
   sooner, like you did on race lap 4."*

The existing grounding filter (`ground_names`/`states_data`, `coach.py:506-540`) still
applies. Every figure in the answer must come from this turn's tool results.

## 4. Scope and order

**Slice 1: one overnight run (executor, then review and red-team), stored laps only,
no rig:**

1. 2.0 prerequisites: the best-lap self-reference fix, the honest next-best label, the
   107% / lap-1 / partial gate, peak brake over the braking run, `brake_zone_start`.
2. `optimal_sectors()` and `corner_form()` as pure functions in `analysis.py`, and
   `phase_split()` using `align_time_axes`. Formatter `format_time_on_table` in
   `format.py`.
3. `get_time_on_table` in `TOOLS` and `CoachContext`. `get_lap_summary`'s best-lap pointer.
4. The prompt changes in section 3.

**Follow-ups (each its own issue):**
- The phase-split diagnosis line on `get_lap_summary`/`get_corner` for the top loss
  corner of *any* lap vs its reference, so "how was that lap" gets it too.
- Post-race `get_battle` from stored `battle_laps` (2.6).
- Per-corner trend and the all-time optimal lap in `get_progress` (2.6).
- Reconcile `brake_point` with `brake_zone_start`. Re-read `get_consistency` on it.
- **Rig-verify:** hear the slice-1 answers in the headset on a known track. Check the
  T10-style diagnosis against feel. Tune the 0.03 s floor and the 5-lap minimum.

**Needs new capture (rig):** nothing in #169. Setup-channel diagnosis stays with the
engineer-grade spec's Phase 0.

## Invariants preserved

- **Owned corners:** segments, phase cut points and sector membership come from the map
  and the game's sector bounds.
- **Two-stage:** pure functions over stored channels. Only the ~15-line summary reaches
  the model.
- **Measured only:** the benchmark line says it has no splits, sectors a lap ran off in
  are dropped, and fragile fields are fixed before they're used.
- **Code extracts:** reference choice, ranking, phase attribution and "at your best" are
  all computed. The swappable model is unchanged.

## Testing

- `optimal_sectors`: each sector's best comes from a different lap, and the fastest S1 is
  on a lap with an off in S1. Assert that it is excluded, with values where excluding it
  and keeping it give different totals.
- `corner_form`: synthetic laps from one base lap, with time injected into one segment on
  two laps and a larger one-lap fluke elsewhere. The two-lap segment must lead.
- `phase_split`: an injected entry-only loss lands in `entry`. An exit gain given back on
  the run-out shows both phases (the race-lap-9 T14 shape).
- Reference honesty: `get_lap_summary("best")` never prints `+0.0`-only deltas. A
  best-latest lap is labelled "next-best". A lap slower than 107% is refused as reference.
- `peak_brake`: a corner braked entirely before the window reports the run's peak, not 0.
- The prompt, via a scripted fake-model tool call: "on my best lap where's 3 tenths"
  routes to `get_time_on_table`.

## 5. Open questions for the driver — DECIDED 2026-09-25

1. **Headline number:** game-sector optimal lap. Summed corners are never a headline.
2. **Which laps count:** the whole session, each cited lap's phase named.
3. **Session vs all-time:** this session first; all-time as one line.
4. **Voice:** one corner per PTT answer (locator, phase, the one change).
5. **At the limit everywhere:** say so, then name the next step (benchmark gap + one
   direction). Never an invented fix.
6. **Post-race rival read:** its own issue, scheduled next after slice 1.

**Driver idea to fold in (not yet designed):** what makes coding harnesses good is that
the agent reasons before it answers. The coach should get the same, dynamically: a quick
path for answers that must come at once (on-track cues over PTT) and a deliberate path
for answers that deserve thought (post-session analysis, "analyze the race in detail",
setup questions). The `Model` seam has only an on/off thinking flag today (off by
default on sonnet-5; a token budget is rejected), so the routing is a new parameter by
question class and voice mode. Designed separately: `2026-09-25-coach-thinking-design.md`.

### Original questions (for the record)

1. **Headline number:** is the game-sector optimal lap (Algarve: 0.27 s) the right "time
   on the table", or do you want the bigger summed-corner figure (0.9-1.5 s) with a
   caveat? The design says sectors.
2. **Which laps count as "your laps":** mix qualifying and race laps (different fuel and
   tires) for form and sectors, or keep to the same phase as the lap asked about? The
   design says the whole session, with each lap's phase named.
3. **Session vs all-time:** lead with this session and give all-time as one line, or
   the other way round?
4. **Voice:** in PTT, is one corner per answer right, or two?
5. **When you're at your limit everywhere:** should the coach go quiet ("nothing
   measurable, that's your pace today"), or offer the benchmark gap and a setup direction
   every time?
6. **Post-race rival read** (stored `battle_laps`): fold into this work, or keep it as
   its own issue?
