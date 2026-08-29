# Bahrain International Circuit (GP/WEC layout) notes

5.412 km, 15 turns. A stop-and-go, traction-heavy circuit with high thermal
load on tyres and brakes — the wiki's guide is split into separate GT3 and
Hypercar sections, summarized here.

## Caveat on corner data completeness
The wiki guide is written as prose sector-by-sector commentary, not a
per-turn reference table. It gives clear direction/character for Turns 4-9
but does not state left/right for Turns 1-3, 10-15. **No corner-map JSON
was created for this track** — building one would mean inventing directions
for roughly two-thirds of the 15 corners, which violates the "never invent
corner data" rule for this project. Notably, the wiki's own GT3 section
calls Turn 8 (the hairpin) a left-hander while its Hypercar section calls
it a right-hander — an apparent inconsistency in the source itself, flagged
here rather than silently resolved. Once telemetry is captured, use
`simcoach.corners.compute_direction`/`verify_map_directions` against real
lap_distance + steering data to build the map instead of the wiki.

## General
- [LMU] Extremely punishing on rear tyres for GT3; success favors smooth
  exits over aggressive entries. High in-sim track temps accelerate wear.
- [LMU] Hypercar guide frames the lap around braking precision and hybrid
  energy deployment discipline rather than outright entry speed.

## Sector guidance (by class, where they diverge)
- **Turns 1-3 (heavy braking complex):** Compromise Turn 1 entry slightly to
  set up a better Turn 2 exit; Turn 2 is flat/near-flat. Both classes stress
  avoiding ABS/over-braking here; Hypercar notes the brake marker is the end
  of the left-side fencing.
- **Turn 4 (right-hander, both classes):** Mid/medium-speed corner easy to
  over-drive. Mid-apex, then use full exit width. Hypercar guide adds that
  hitting a consistent minimum speed matters more than entry speed.
- **Turns 5-7 (fast left-right-left sequence):** Prioritize a good Turn 7
  entry over a perfect Turn 6 exit. Attack the kerbs; GT3 guide recommends
  positioning left afterward to set up the hairpin. Hypercar favors light
  brake taps through the sequence rather than full lifts.
- **Turn 8 (hairpin, direction disputed — see caveat above):** Downhill
  braking zone; brake earlier than instinct suggests, rotate the car before
  applying throttle, and stay off the exit kerb. Hypercar guide adds that
  hybrid torque needs patient throttle to avoid rear instability.
- **Turns 9-10 (technical, combined braking/turning):** Early turn-in,
  trail-brake into Turn 9, keep exit stable, avoid aggressive kerb use.
- **Turns 11-13:** Turn 11 brakes around the 100 m marker with light inside
  kerb use; Turn 12 is an early, controlled turn-in; Turn 13 is a late
  turn-in with full-width, full-throttle exit.
- **Final corner (Turns 14-15):** Called out as one of the most
  lap-time-critical sections in both guides. Late turn-in, progressive
  throttle only — getting greedy here costs the following straight.
  Hypercar guide specifically warns hybrid boost "magnifies mistakes" if
  the exit isn't clean.

## Setup baselines
**GT3:** medium-to-high rear wing for traction/stability; traction control
slightly above default; conservative tyre pressures to manage heat; brake
bias slightly forward. Common mistakes: overheating the rears early,
over-attacking kerbs, trying to rotate the car with throttle instead of
brake/steering.

**Hypercar:** medium-high downforce for a stable platform; brake bias
slightly forward; conservative energy deployment early in corners,
more aggressive on exits; front-tyre-focused pressure management. Common
mistakes: over-attacking braking zones, excess mid-corner steering input,
triggering ABS, poor hybrid throttle discipline.

## Other layouts
The main circuit page lists "Endurance Circuit" and "Outer Circuit" as
additional configurations at this venue but gives no length, corner count,
or corner data for either — not enough to seed `bahrain_outer_circuit` or
`bahrain_paddock_circuit` from the wiki. Both skipped.

Source: LMU wiki (lemansultimate.wiki.gg), CC-BY-SA, summarized 2026-08-29.
