# Autodromo Nazionale Monza notes

5.793 km, 11 turns, WEC layout with both chicanes (Variante del Rettifilo,
Variante della Roggia, Variante Ascari). Nicknamed the "Temple of Speed" but
the wiki guide stresses it's more technical than it looks in GT3/Hypercar --
lap time is mostly braking efficiency, kerb discipline and exit speed, not
raw entry pace, because most corners are slow chicanes or tight-radius turns
followed by long straights where a bad exit is very costly.

## General
- [LMU] Aggressive kerb use is often required but the angle matters -- the
  wrong kerb angle destabilizes the car.
- [LMU] Good exit is repeatedly emphasized as more valuable than defending
  or attacking on entry, across every chicane on the lap.
- [LMU] Hypercar guide adds that aero sensitivity through the chicanes needs
  smoother inputs, and early throttle at Parabolica costs downforce.

## Corner-by-corner
- **T1-2 Variante del Rettifilo:** Braking from the far left, starting just
  after the 150m board (100m in qualifying). T1 (right): late apex, careful
  inside kerb, avoid the orange sausage kerb. T2 (left): let the car flow
  naturally, inside kerb okay but keep steering straight. Good exit matters
  more than defending into T1.
- **T3 Curva Grande:** High-speed downhill right, flat out, minimal steering
  to cut drag; car drifts naturally left toward Roggia.
- **T4-5 Variante della Roggia:** Braking just before the 100m board. T4
  (left): short sharp turn-in, light kerb. T5 (right): trail-brake and
  engine-brake to rotate, late apex, use all the exit kerb. Overusing the
  inside kerbs here costs speed into Lesmo 1.
- **T6 Lesmo 1:** Braking at the orange marshal stand, progressive braking
  with light trail-braking, late apex, exit patience is critical to avoid a
  rear slip. Running wide ruins the whole Lesmo sequence. (Direction not
  stated in the source -- see completeness note below.)
- **T7 Lesmo 2:** Braking at the 50m board, let the car rotate, late apex,
  watch track limits on exit, avoid the inside kerb. (Direction not stated.)
- **T8-10 Variante Ascari:** Braking just after the 100m board or where the
  right-side road ends. T8 (left): light trail-brake, early apex, open up
  for the next right. T9 (right): straighten the car, controlled kerb use.
  T10 (left): late apex, full commitment to the exit, light exit-kerb use.
  Rhythm matters -- corrections here kill back-straight speed.
- **T11 Curva Parabolica (Alboreto):** Braking between the 100m board and
  the flag-light depending on fuel/tyres, progressive braking with long
  trail-braking, very late apex, gradual and patient throttle, use all the
  track width. Early throttle destroys speed down the main straight.
  (Direction not stated.)

## Common mistakes
Over-aggressive kerb usage, prioritizing entry over exit, overdoing
trail-brake and overheating the fronts, rushing throttle at Parabolica,
defending too aggressively in the chicanes and breaking rhythm.

## Corner-map completeness
`simcoach/tracks/autodromo_nazionale_monza.json` covers 8 of the 11
corners. T6 (Lesmo 1), T7 (Lesmo 2) and T11 (Curva Parabolica) are omitted
because the wiki source never states a left/right direction for them
(confirmed by direct re-query of both the GT3 and Hypercar sections) -- left
out per the never-invent-direction rule rather than guessed. This leaves a
large undetected gap in the map between Roggia and Ascari.

## Other layouts
The user also ran `monza_curva_grande_circuit` (the historic no-chicane
layout), but the wiki has no separate guide for that configuration -- no
data exists to seed a map or note for it.

Source: LMU wiki (lemansultimate.wiki.gg), CC-BY-SA, summarized 2026-08-29.
