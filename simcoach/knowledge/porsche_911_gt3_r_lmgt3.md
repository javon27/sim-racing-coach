# Porsche 911 GT3 R (992) tuning notes — LMGT3

Baseline knowledge only — refine against the user's own telemetry, setup sheets,
and stated preferences as they're gathered. Tags mark provenance: [LMU-wiki] =
factual spec from the LMU wiki, [general-sim] = handling knowledge common to the
rear-engined 911 GT3 R across sims (iRacing/ACC/LMU), [needs-refinement] =
confirm/replace with the user's own data. The LMU wiki page for this car is a
**stub (specs only)** — the handling/setup content below is general baseline, not
LMU-verified; validate against captured laps before coaching hard on it.

## Specs
- [LMU-wiki] Class LMGT3; 4.2L naturally-aspirated flat-6, ~550 hp (BoP-adjusted);
  ~1300 kg (BoP); rear-wheel drive; 6-speed sequential; ~290 km/h top speed; no
  hybrid system.

## Handling characteristics
- [general-sim] **Rear-engined, rear-weight-biased.** Signature trait: excellent
  corner-exit traction and strong, stable braking, but a light front end that
  tends toward **entry/mid-corner understeer**, and a rearward mass that can
  **snap-rotate (pendulum)** if the car is unsettled by an abrupt lift or overly
  aggressive trail-brake release.
- [general-sim] Rotation comes from trail-braking to load the front, not from
  throwing the car in — smooth brake release is the key input. The reward for a
  tidy entry is class-leading drive off the corner.
- [general-sim] Common setup direction to fight entry understeer: help the front
  (softer front ARB relative to rear, front ride-height/aero balance forward a
  touch) — but too much forward shift reintroduces the rear-pendulum snap on
  entry. [needs-refinement] confirm the balance point against the user's laps.
- [general-sim] Brake bias: the rear weight bias means a rearward brake bias is
  easy to overdo (rear lockup / spin under braking); many run bias slightly
  forward of a mid-engined car's. Pair with ABS (below). [needs-refinement].

## Driver aids (LMGT3 in LMU) — shared with the Ferrari note
- [LMU] LMGT3 is the only LMU class with native ABS + TC. See
  [[ferrari_296_gt3]] for the ABS 0-9 dial model (1-3 rear-slip/"oversteer",
  4-6 balanced, 7-9 front-slip/"understeer") and the three TC facets.
- [LMU] [needs-refinement, community-sourced] One driver reported **ABS 8
  noticeably stabilized a Porsche GT3 under braking** — directly relevant to this
  car's rear-lockup tendency; a good starting experiment. Source: r/LeMansUltimateWEC
  "New ABS system" thread (in `docs/superpowers/knowledge/sources.md`).

## Driver-fit note
- [needs-refinement] User's stated preference is to "brake later and have a more
  oversteery but stable car." The 911's rear-pendulum is the thing to watch here —
  a more-rotation setup on this platform trades toward the snap it's already prone
  to. Balance later-braking + rotation against the ABS/brake-bias stabilizers above.

## Sources
- LMU wiki, "Porsche 911 GT3 R" (specs only, stub): https://lemansultimate.wiki.gg/wiki/Porsche_911_GT3_R (CC-BY-SA)
- Handling/setup rows are [general-sim] domain baseline pending refinement from the user's telemetry — not from an LMU-specific verified source.
