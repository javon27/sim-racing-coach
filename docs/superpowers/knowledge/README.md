# Knowledge base

Two kinds of static, per-track/per-car data feed the coach. Neither is
telemetry — both are hand-authored reference facts loaded alongside a lap's
computed corner data.

## Track corner maps (`simcoach/tracks/<track>.json`)

Schema (see `simcoach/corners.py: CornerMap`, `Corner`):

```json
{"track": "<name>", "corners": [
  {"number": 1, "name": "Corner Name", "direction": "left"|"right",
   "start_dist": 800, "apex_dist": 1000, "end_dist": 1200}
]}
```

- `number`, `name` — facts (public corner names/numbers for the real
  circuit). Cite sources when adding a track.
- `direction` — the *declared* value in the file. The coach never trusts
  this blindly: `compute_direction()` derives the actual left/right from
  live steering telemetry over `[start_dist, end_dist]`, and
  `verify_map_directions()` flags any mismatch. Corners are owned data —
  the LLM only ever receives the resolved fact, never guesses it.
- `start_dist` / `apex_dist` / `end_dist` — lap-distance positions (meters)
  bounding each corner. **These are estimates** until calibrated against
  real telemetry for that track: `CornerMap.load` only enforces
  `start_dist < end_dist` and `start_dist <= apex_dist <= end_dist`, it
  does not verify the window actually contains the real corner. Mark a
  freshly-added track's distances as estimated (see `spa.json`'s
  `_distances_note`) until someone drives it and narrows the windows from
  real `lap_distance` data.

### Adding a new track
1. Research the real corner names/numbers/directions from a citable
   source (F1/circuit guides, sim community references). Do not guess.
2. Estimate start/apex/end distances spread across the track's known
   length, spaced in the real corner order. Mark them as estimates.
3. Verify it loads: `CornerMap.load("simcoach/tracks/<track>.json")` must
   not raise (ascending, non-overlapping, `start < apex < end` per
   corner).
4. Calibrate for real once live telemetry is available: narrow each
   window from actual `lap_distance` samples through the corner, and
   drop the "estimated" note once confirmed.

## Car notes (`simcoach/knowledge/<car>.md`)

Freeform markdown, loaded as `{knowledge_dir}/{car}.md` and injected into
the coach's prompt verbatim — this is where car-specific tuning guidance
the coach can't derive from telemetry alone lives (setup levers, known
handling quirks, driver-aid behavior).

No fixed schema, but tag provenance so the coach (and a human reader) can
tell fact from inference:
- general sim-racing knowledge that likely transfers (mark it as such)
- LMU-specific findings (cite the source)
- open items that need the user's own setup sheets/logs to refine

See `simcoach/knowledge/ferrari_296_gt3.md` for the pattern.

### Adding a new car note
1. Research general handling characteristics for that car/class and, where
   findable, LMU-specific notes (driver-aid rules, known quirks). Cite
   sources.
2. Do not invent specific numeric setup values (spring rates, wing angles,
   etc.) — those come from the user's own setups, not research.
3. Flag anything that needs confirmation against current LMU rules/BoP
   rather than asserting it.

## Roster reference

`docs/superpowers/knowledge/lmu-roster.md` is the current (cited, dated)
list of LMU tracks and cars — check it before adding a new track/car file
so the name matches what's actually in the game.
