# Autodromo Enzo e Dino Ferrari (Imola) notes

4.909 km, 19 turns, WEC layout. The wiki guide is written as prose grouped
by named corner complexes rather than a per-turn table, and strongly
emphasizes rhythm: "Imola strongly punishes poor kerb usage in GT3 cars,"
while Hypercars need smoother inputs because "kerbs can unsettle aero load."

## Caveat on corner data completeness
The guide only names and gives advice for 7 corner groups covering 13 of the
19 numbered turns (2-4, 5-6, 7, 9, 11-13, 14-15, 17-18); turns 1, 8, 10, 16
and 19 are not mentioned anywhere in the source. Of the named groups, only
Tamburello (stated as a "left-right-left sequence" for turns 2-4), Piratella
("blind left-hander," turn 9) and Rivazza ("final double left-hander," turns
17-18) give explicit left/right direction -- Villeneuve (5-6), Tosa (7) and
Acque Minerali (11-13) do not. That's roughly two-thirds of the lap without
a stated direction. **The corner-map JSON now exists** (`tracks/autodromo_enzo_e_dino_ferrari.json`),
built from 11 captured laps using FIA/WEC GP 19-corner numbering: identity
(numbers + names) from the FIA circuit map and this wiki note, directions
computed from telemetry and cross-checked against the wiki-stated geometry.
High-confidence corners: Tamburello (2-4, L-R-L), Tosa (7), Piratella (9),
Acque Minerali (11 right / 12 left), Rivazza (17-18). Low-confidence / verify:
turns 1, 13, 14, 15, 16 -- flat kinks plus the Acque Minerali/Variante Alta
grouping are hard to separate from telemetry alone; confirm with a slow lap or
driver input. Rebuild via `simcoach.corners.compute_direction`/`verify_map_directions`.

## Corner-by-corner (as given)
- **T2-4 Tamburello:** High-speed left-right-left sequence, braking at the
  flag-light on the right, focus on balance/positioning; aggressive kerb
  use is possible for GT3, but Hypercar should prioritize aero stability
  over attacking the kerb.
- **T5-6 Villeneuve:** Short braking at the second board on the right, let
  the car roll through T5 and brake lightly again for T6, flat kerbs are
  usable, late apex, prepare early for a clean exit into Tosa.
- **T7 Tosa:** Slow uphill hairpin, use all the track width, brake early,
  late apex, exit traction is critical. Hypercar guide adds smooth throttle
  is essential to rotate with minimal steering.
- **T9 Piratella:** Blind left-hander, brake at the flag-light on the right,
  avoid mid-corner steering corrections and running wide on exit.
- **T11-13 Acque Minerali:** Technical downhill complex; brake lightly for
  T11 and set up left for T12, direction change needs patience with
  mid-to-late throttle, don't attack the inside kerbs, use all the track on
  the left; T12 is a subtle, barely-recognisable left kink.
- **T14-15 Variante Alta:** Aggressive chicane, straight-line braking only,
  kerbs are strongly cuttable (watch track limits), use all of the exit
  kerb, exit stability matters more than entry speed.
- **T17-18 Rivazza:** Final double left-hander, brake early for T17
  (flag-light on the right), mid-apex T17 using all the track and kerb, a
  slight brake tap for T18, prioritize the exit onto the main straight.

## Common issues (both classes)
Overusing kerbs, breaking rhythm by overdriving, poor throttle management at
Tosa and Rivazza specifically.

Source: LMU wiki (lemansultimate.wiki.gg), CC-BY-SA, summarized 2026-08-29.
