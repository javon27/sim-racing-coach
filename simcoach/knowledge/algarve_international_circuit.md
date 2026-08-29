# Autodromo Internacional do Algarve (Portimao) notes

4.653 km, 15 turns, WEC layout. Extreme elevation change and blind crests
make this a confidence/rhythm track rather than an aggression track --
several corners can't be seen on entry, so braking references matter more
than what the driver can see.

## General
- [LMU] Overdriving an entry causes snap oversteer more readily here than on
  flatter tracks, because elevation change amplifies weight transfer.
- [LMU] Brake release must be smooth over the many crests/compressions to
  avoid front lock-up.
- [LMU] Hypercar guide frames the lap around aero trust and brake precision;
  crests reduce downforce right when braking load is highest.

## Corner-by-corner
- **T1 Primeira Curva:** Heavy downhill braking, compression right at the
  apex means braking earlier than it looks. Late apex; avoid the sausage
  kerb (very unstable) and don't trail-brake too far into the corner.
- **T2 Right Kink:** Flat out, minimal input, set up left for T3.
- **T3 Lagos:** Strong braking, late apex with heavy trail-braking; avoid
  overdoing exit understeer since it costs the next corner.
- **T4 Flat-out Left:** Fast uphill left, use full width; a light kerb touch
  can help rotate the car (but can also unsettle the rear).
- **T5 Tore Vip:** One of the slowest corners, heavy downhill-to-uphill
  braking in a straight line, gentle early rotation, no throttle until the
  car is nearly straight, only flat kerbs, let the car drift wide on exit.
- **T6 Uphill left bend:** Flat out and blind -- stay smooth, sets up T7.
- **T7 Medium-speed right:** Light brake/lift, entry speed sets exit
  stability, avoid aggressive kerb use, exit left for T8.
- **T8 Samsung Corner:** Slow uphill signature corner; any mid-corner
  steering correction is heavily punished; throttle only once straight.
- **T9 Craig Jones:** Use all the track, prepare for T10 braking.
- **T10-11 Portimao Curve:** Double-apex right-hander, brake earlier than it
  looks, straight-line braking to the first apex, then trail-brake to
  rotate for a late second apex; don't stay too tight after T10 or it closes
  off T11.
- **T12 Long Left:** Flat-out, long-radius, high front-tyre load from the
  downhill-to-uphill transition; smooth steering to cut drag.
- **T13 Tight left:** Strong uphill braking, deep trail-braking, kerb use
  is advised.
- **T14 Sagres:** Slow long right-hander (right per the Hypercar section),
  early apex, early progressive throttle, avoid the inside kerb, stay
  outside to set up T15.
- **T15 Galp:** Very fast downhill right-hander with outside banking, small
  lift or flat depending on tyres/fuel, mid-corner apex used to rotate,
  clean exit is essential onto the front straight.

## Common mistakes
Braking too late into downhill zones, hesitating in blind corners,
overusing kerbs at high speed, throttle too early while unloaded, fighting
the car instead of letting it flow.

## Corner-map completeness
`simcoach/tracks/algarve_international_circuit.json` covers 10 of the 15
corners. T1, T3, T5, T8, T9 are omitted because the wiki source never states
a left/right direction for them (confirmed by direct re-query of both the
GT3 and Hypercar sections) -- left out per the never-invent-direction rule
rather than guessed.

Source: LMU wiki (lemansultimate.wiki.gg), CC-BY-SA, summarized 2026-08-29.
