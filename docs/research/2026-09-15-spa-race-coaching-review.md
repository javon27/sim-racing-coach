# Coaching review — Spa race chat, 2026-09-15 (Porsche 911 RSR-19, Iron Dames #85)

Chat `chat-20260915T222947-5e55b9`, 11 turns, all by voice (PTT), during practice → race →
debrief. Reviewed against the stored tool calls and the captured laps. Purpose: what the
coach did well, where it went wrong, and which of the misses are **data** problems we can
fix in the pipeline rather than prompt problems.

## What worked

- **Grounded, specific, right tools.** Every quantitative claim traced to a tool result
  (`get_lap_summary`, `get_corner`, `get_handling`, `get_wheel_report`, `get_tire_trend`,
  `get_setup`, `get_track_notes`, `get_race_result`). No raw-telemetry hallucination.
- **Corner identity was owned data.** Every name used (La Source, Raidillon, Les Combes,
  Malmedy, Bruxelles, Pouhon, Campus, Blanchimont, Bus Stop) is in the shipped Spa map.
- **Tone and length** matched the voice channel: one cue per answer, encouragement without
  padding ("I'm not doing too well" was handled well), garage-value caveat applied to
  brake balance and TC exactly as instructed.
- **Self-correction and learning capture.** When the driver explained the T10 spin →
  flat-spot chain, the coach corrected its earlier causal read and saved a driver
  learning for review.

## What went wrong

1. **Apparent self-contradiction on the T1 lockup.** Turn 4: "no lockup flags at La
   Source". Turn 5 (one minute later): "La Source **is** showing a lockup". Both were
   true — `which: "latest"` pointed at different laps because the driver was still
   lapping. The coach never said which lap it was reading, so the driver hears a
   contradiction.
2. **"143% of the theoretical grip envelope."** Over 100% is meaningless to a driver.
   The envelope (p98 of observed combined g over recent laps) is too small early in a
   session, so the first hot lap "exceeds" it.
3. **Wrong cause for the FR wear.** Turn 8 blamed cornering load through Raidillon and
   Blanchimont; the real cause was a flat spot from the T10 spin two laps earlier. The
   coach had no way to see a spin, and the wear report gives a per-lap slope, not a
   step change on one lap at one corner.
4. **"Spun at turn 10 a few laps ago" → checked only the latest lap.** `get_handling`
   has no way to scan a stint for events, so the answer was "nothing flagged on the
   latest lap" plus generic track-note advice.
5. **Car fact from model memory.** "The RSR-19 is a Cup car, not LMGT3." It is a GTE car;
   the no-ABS conclusion was right, the class was invented.
6. **Electronics semantics asserted without data.** "Nudging the TC slip-angle map down a
   step lets TC cut in sooner" — the direction of that map is not in any car note; the
   coach guessed.
7. **Garage vs live values** (#33): brake balance "51:49 in the garage" vs "52 on the
   dash" — the caveat worked, but the driver had to reconcile it. The live shared-memory
   values (`tc_map`, `abs_map`, brake bias if exposed) now demonstrably follow on-track
   changes; the coach should read those.
8. **Minor:** the coach said "the wiki's exact advice" — track notes are ours; say
   "the track notes".

## Data / pipeline improvements (the fixable part)

| # | Gap | Change | Where |
|---|-----|--------|-------|
| 1 | Tool results carry no lap identity | Every per-lap tool result starts with `lap N (game lap M, HH:MM:SS)`; prompt rule: cite the lap when reporting a per-lap fact; "latest" resolves once per turn | `coach_tools.py`, `format.py`, `coach.py` |
| 2 | Grip envelope reported as % > 100 | Report % only when the envelope rests on ≥ 5 laps; otherwise report peak combined g and say the envelope is still forming | `analysis.py` (`grip_envelope`), `format.py` |
| 3, 4 | No per-lap event log | Detect per lap + corner: spin (yaw-rate reversal + speed collapse), off-track (centre-of-mass past edge, already computed), lockup, wheelspin; store on the lap row; new tool `get_stint_events(laps=N)`; `get_lap_summary` leads with "events this lap" | `analysis.py`, `store.py` (schema), `coach_tools.py` |
| 3 | Flat spots invisible | Per-wheel wear **step** per lap (Δ% vs the stint slope) and where in the lap it happened; flag "wear step of X% at T10 on lap N" in the wheel report | `analysis.py` (`_wheel_summary`), `get_wheel_report` |
| 5 | Car class from memory | `get_car_notes` / the setup line states class and ABS availability from `car_lookup` + car notes; prompt: never assert class or aids not present in the data | `coach_tools.py`, `knowledge/cars/*.md` |
| 6 | Electronics direction unknown | Car notes carry the semantics of each electronics map (which direction = more intervention) or the coach says the direction is unknown | `knowledge/cars`, `car_notes.py` |
| 7 | Garage vs live electronics | Prefer live `tc_map`/`abs_map` (and brake bias when exposed) from the latest lap over the REST garage value; drop the caveat for those | `coach_tools.py` (`get_setup`) — issue #33 |
| 8 | Wording | Prompt: "track notes", never "wiki" | `coach.py` |

Suggested order: 1 (cheap, removes the most confusing failure), 7 (evidence already
in hand), 3+4 (one analysis pass + one tool), 2, 5, 6, 8.

## Spoken vs written (driver feedback, 23:30)

The driver's read from the headset: several spoken cues were not useful in the moment,
especially when they leaned on corner **names** the driver did not know yet ("Malmedy",
"Campus", "Bruxelles" on a first Spa outing). The written answers held up; the spoken
line is where the loss was. The spoken line is stripped before storage, so this review
cannot compare the two texts — a gap in itself.

| # | Gap | Change | Where |
|---|-----|--------|-------|
| 9 | `spoken` is not stored | Persist the spoken cue on the chat row (`coach_chat.spoken`) and show it in the thread as a small "spoken:" line under the answer; `get_chat_messages` returns it | `store.py` (schema), `app.py` `coach_ws`, `ChatPanel.svelte` |
| 10 | Spoken cue uses names the driver may not know | `SPOKEN_SENTENCE`: refer to corners as "Turn N" first, add the name only when `corner_naming = names` ("Turn 10, Pouhon"), never the name alone; one cue, one action, no figures beyond a single number | `coach.py` |
| 11 | No review loop | After #9 lands, review a session's spoken vs written pairs: does the spoken line carry the written answer's top item, and is it actionable before the next corner? | this doc, next rig session |
| 12 | Numbers alone don't locate a corner in the headset (the driver only followed "Turn 10" because OpenKneeboard showed the map in VR) | Code-generated per-corner **spoken descriptor** from owned data — computed direction, speed class from min speed (hairpin / slow / medium / fast / flat), what precedes it (long straight ≥ N m, chicane, the corner before), position (first / last corner, start of the lap) — e.g. "Turn 10, the fast left after the long straight". Exposed on the corner line and the corner tool; `SPOKEN_SENTENCE` uses the descriptor, not the name, for corners the driver has not driven before on this track (few stored laps), and the number + name once they have | `corners.py` / `format.py` (descriptor), `coach_tools.py`, `coach.py` |
