# Ferrari 296 GT3 tuning notes

Baseline knowledge only — refine against the user's own setup sheets/logs as
they're gathered. Tags mark provenance: [general-sim] = handling knowledge
common across GT3 sims (iRacing/ACC/LMU), [LMU] = specific to this game,
[needs-refinement] = confirm/replace with the user's own data.

## Handling characteristics
- [general-sim] Mid-engine, well-balanced baseline; not a car that demands
  immediate fixes. Stock setup tends toward mild entry understeer and
  exit-of-corner oversteer under power.
- [general-sim] Front ride height is a key lever: lower front ride height
  generally improves front tire grip and reduces entry understeer.
- [general-sim] Front ARB stiffer than rear is a common starting point for
  a balanced platform; retune per track (high-speed vs. technical).
- [general-sim] Aero balance shifted more forward increases oversteer risk
  in mid-to-high-speed corners.
- [LMU] Reported quirk specific to LMU: instability in high-speed corners —
  the rear can snap suddenly, worse if balance is shifted forward via low
  rear wing or low rear ride height. Source: Coach Dave Academy LMU 296
  guide (see below). [needs-refinement] — confirm against the user's own
  high-speed-corner incidents once logged.
- [general-sim] Trail braking: front end is sensitive to late-brake inputs,
  so smooth trail-brake release matters more than outright late braking.

## Driver aids (LMGT3 in LMU)
- [LMU] LMGT3 is the only LMU class with native ABS. ABS has two axes:
  intervention level and front/rear intervention distribution.
- [LMU] [needs-refinement, community-sourced from a Reddit discussion, not
  verified against official patch notes] The two axes above collapse into a
  single 0-9 in-game dial: 1-3 lets the rear slip more than the front before
  ABS activates ("oversteer" settings), 4-6 is front/rear-balanced, 7-9 lets
  the front slip more ("understeer" settings); the specific value within
  each band (e.g. 1 vs. 3) sets how early ABS activates, not a second
  balance axis. Community reports: pairing a rearward brake bias with a low
  ABS value (1-3) makes the car very easy to spin under braking; one driver
  found ABS 8 noticeably stabilized a Porsche GT3 under braking. Source:
  r/LeMansUltimateWEC, "New ABS system" thread (see sources.md S1 in
  `docs/superpowers/knowledge/`).
- [LMU] Traction control has three facets in LMU: TC, TC power cut, TC slip
  angle — adjustable on the fly via MFD. Higher settings suit cold tires.
- [LMU] Rearward brake bias + aggressive ABS can help rotate the car under
  braking; some drivers prefer biasing forward slightly to curb oversteer
  under trail-braking instead.
- [needs-refinement] Which aids (TC/ABS/brake-bias range) Bronze-rated
  drivers are specifically permitted to run was not confirmed by this
  research pass — verify against LMU's current driver-category rules
  before coaching on it.

## Sources
- Coach Dave Academy, "LMU Ferrari 296 LMGT3 Guide & Setups": https://coachdaveacademy.com/tutorials/lmu-ferrari-296-lmgt3-guide-the-inside-line/
- Coach Dave Academy, "The ACC Ferrari 296 Drivers Guide & Setups": https://coachdaveacademy.com/tutorials/tips-and-tricks-to-driving-the-ferrari-296-gt3/
- OverTake.gg, "LMGT3 ABS in Le Mans Ultimate Explained": https://www.overtake.gg/news/guide-to-le-mans-ultimate-abs-in-lmgt3.3535/
- Steam Community discussions on LMGT3 setup (general, not car-specific): https://steamcommunity.com/app/2399420/discussions/0/694251179408083282/
- Reddit r/LeMansUltimateWEC, "New ABS system": https://www.reddit.com/r/LeMansUltimateWEC/comments/1l9ud8z/new_abs_system/
