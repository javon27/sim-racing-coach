# LMU Fuel-to-Energy Ratio & Fuel-Saving/Lift Mechanics — Research Findings

Date: 2026-08-28
Scope: web research only, no code touched. Sources are secondary community
articles/wikis/forums plus a few third-party tool READMEs; no official
Studio 397 manual or patch-notes PDF was found and fetchable (the official
`guide.lemansultimate.com` articles 403'd on direct fetch — findings below
that reference it come from search-result summaries only, so are marked
accordingly).

## 1. Virtual Energy (NRG) system — overview

**What it is:** LMU (mirroring real WEC 2023+ regs) caps each stint's total
energy consumption via a "Virtual Energy" (NRG) allowance, on top of (or
instead of) a physical fuel-tank limit. It's a Balance-of-Performance lever:
faster/thirstier cars get less energy per stint, slower cars get more, so
lap-time potential is roughly equalized.

- **Hypercar (LMH/LMDh):** NRG is the combined budget for fuel burned by the
  ICE *and* electrical energy used by the MGU (hybrid) during a stint.
  Estimated total ~251 kWh per stint (varies by car/BoP/track).
  Confidence: **likely** (two independent community articles — trophi.ai,
  simracingcockpit.gg — agree on this figure and description).
- **LMGT3:** These cars have no hybrid system, but LMU still applies an NRG
  allowance to them, computed purely from fuel (no battery term). A forum
  thread (simracers4life) discusses tuning "Fuel Ratio" specifically for
  GT3 cars at Bahrain, and a Steam community thread ("Save virtual energy
  with the LMGT3s") confirms GT3 drivers actively manage an NRG/VE budget
  via lift-and-coast. This directly contradicts two articles (trophi.ai,
  simracingcockpit.gg) that state NRG is Hypercar-exclusive — those two
  appear to be describing only the hybrid-deployment portion of NRG, not a
  claim that GT3 has none. Confidence: **likely** GT3 has its own NRG/fuel-only
  allowance, **not-found** for exact GT3 kWh/MJ figures.
- **Units:** Community sources use kWh (~251 kWh Hypercar stint estimate) and
  a 0–100% "NRG gauge"/bar in the HUD. No source gave a raw MJ figure
  in-game (the WEC real-world regs use MJ; whether LMU's UI ever shows MJ
  directly was **not found** in these sources — treat the in-game unit as
  %/kWh until confirmed otherwise).
- **Penalty for running out:** Draining NRG to 0% during a stint triggers an
  instant stop-and-go penalty (100 seconds cited by one source), with
  steeper penalties for repeat offenses in the same session. Confidence:
  **likely** (consistent across multiple sources).
- **Replenishment:** NRG resets to 100% at each pit stop (you choose how
  much to take on for the next stint, see below). Confidence: **likely**.

## 2. "Fuel Ratio" setting — what it does mechanically

Location: Powertrain section of the car setup/garage menu (both classes
appear to expose it, per the GT3-specific forum thread).

**Mechanism (Hypercar):** Fuel Ratio sets the split between the two
consumable components of the NRG budget — physical fuel (liters) vs.
electrical energy deployed/regenerated via the MGU — for a given stint's NRG
allocation. Example from Traxion: a fuel ratio of 0.96 means only ~4% of the
stint's Virtual Energy comes from the battery, the rest from ICE fuel.
Since "electrical energy weighs nothing," shifting more of the budget to
battery reduces carried weight — but battery deployment is track/corner
dependent (long straights favor more ICE fuel; twisty tracks can use more
electric share). One source (trophi.ai, via search summary) said fuel ratio
is "typically set around 85%" and is track-dependent (Le Mans's long
straights wanting a higher ICE-fuel share than a track like Algarve).
Confidence: **likely**, consistent across trophi.ai and Traxion.

**Mechanism (GT3, non-hybrid):** Since GT3 has no battery term, "Fuel
Ratio" instead determines how many liters of physical fuel are added when
you request a given % of Virtual Energy for the next stint. Per the
simracers4life forum: "Increasing this number increases the physical amount
of fuel added to the tank — too much and you are carrying dead weight, too
little and you may have to pit early." The tuning goal stated there: pick a
ratio so the fuel-based laps-remaining estimate is equal to (or just under)
the NRG-based laps-remaining estimate — i.e. you run out of both at roughly
the same time, rather than carrying excess weight in one resource while
starving the other. Example values given: ~0.83 for a Mustang GT3, ~0.82
for a McLaren GT3 at Bahrain Outer — car-specific, tuned per track.
Confidence: **likely** (single forum source, but internally consistent and
plausible given the class has no battery to trade against).

**Pit-stop interaction:** You set the desired NRG% (and thus, via Fuel
Ratio, the fuel liters) to take on for the next stint before/at the pit
stop; a higher Fuel Ratio means more physical fuel gets loaded, which
increases time stationary in the pits (refuel rate is presumably
liters/second, same as classic sim-racing fuel stops). Fuel and NRG appear
to be replenished together, not as two separate pit actions. Confidence:
**likely**, sourced from Traxion + simracers4life, both independently
describing the same "more fuel ratio = longer pit stop" relationship.

**Per-lap depletion / HUD feedback:** LMU's MFD/HUD "Fuel" tab shows, and
refreshes every lap based on actual driving style: (a) estimated laps
remaining on physical fuel, and (b) estimated laps remaining on Virtual
Energy — plus average and previous-lap usage. Confidence: **confirmed**
(explicitly described by both Traxion and trophi.ai/search-summary as an
existing in-game display).

## 3. Fuel-saving / lift-and-coast indicator

**Finding: not-found.** No source — official guide, community articles,
YouTube tutorial summaries, or the Steam community thread specifically about
LMGT3 energy-saving — describes an automated in-game HUD marker, lift
point, or "fuel saving mode" that tells the driver *where on track* to lift.
What exists instead:

- The per-lap laps-remaining readout described above (fuel laps vs. NRG
  laps), which tells you *whether* you're on pace to make a stint, not
  *where* to lift.
- Lift-and-coast itself is treated uniformly across sources as a **manual
  driving technique** the driver must learn and apply with self-picked
  reference points (braking markers, board numbers) — e.g. Coach Dave
  Academy's tutorial frames it purely as driver discipline, with a Spa
  example (lift-and-coast run saved ~1.5 L/lap for ~0.7s lap-time cost) and
  no mention of any UI aid.
- The Steam thread on LMGT3 energy saving lists community techniques (lift
  before every corner on some tracks, short-shifting, avoiding mid-corner
  throttle, drafting, wing/TC tuning) — again all manual, no mention of an
  assist or indicator.

Confidence: **not-found** for an automated lift/fuel-saving indicator or
per-lap-target-driven lift marker in LMU. This appears to genuinely not
exist as a feature (unlike, e.g., some real WEC cars' dash "fuel save"
lights) — but given the sparse official documentation available, this is
absence-of-evidence rather than a confirmed absence.

## 4. Telemetry / REST API exposure

- LMU exposes both a shared-memory interface (`rFactor2SharedMemoryMapPlugin`-
  compatible, `$rFactor2SMMP_Telemetry$` / `$rFactor2SMMP_Scoring$`, used by
  tools like Second Monitor, SimHub, Z1 Dashboard) and a **local REST API on
  port 6397**. Confidence: **confirmed** (multiple independent third-party
  tool repos target both interfaces; the REST API port/existence is stated
  directly in the `lmu-pitwall` README).
- Two REST endpoints specifically relevant here, per the `lmu-pitwall`
  README (Swizzjack/lmu-pitwall on GitHub):
  - `GET /rest/strategy/usage` — described as returning **Virtual Energy
    history**. Confidence: **likely** (primary-source README, but no field
    schema seen — only the endpoint's stated purpose).
  - `GET /rest/garage/UIScreen/RepairAndRefuel` — wear/repair data (aero,
    brakes, suspension), not directly fuel/energy but adjacent (garage
    screen data family). Confidence: **likely**.
- No source enumerated exact JSON field names for fuel, NRG %, or fuel
  ratio in either the shared-memory struct or the REST responses. The
  shared-memory route is confirmed to carry a basic "fuel" telemetry field
  (e.g. `NikMusy/le-mans-ultimate-telemetry` dashboard displays a raw fuel-
  liters readout), but Virtual Energy specifically was only positively
  located on the **REST API** side (`/rest/strategy/usage`), not confirmed
  present in shared memory. Confidence: **uncertain** on whether NRG is
  also in shared memory — worth a direct spike (hit `localhost:6397/rest/
  strategy/usage` and/or dump the shared-memory struct) rather than trusting
  secondary docs further.
- Fuel Ratio (the garage *setting*, as opposed to live NRG/fuel telemetry)
  was not confirmed to be readable via either interface in any source found
  — it may only be readable indirectly (inferred from the live fuel/VE
  numbers) rather than exposed as its own field.

## How this should shape the coach's fuel/energy model

1. **Model energy as a stint-level, class-conditional resource, not a
   single fuel number.** Hypercar stints are governed by a combined
   fuel+battery NRG budget; GT3 stints are governed by a fuel-only NRG
   budget. Since v0.1 is scoped to "one car, one track" (per CLAUDE.md),
   confirm which class that car belongs to before deciding whether the
   planner needs a two-term (fuel, battery-share) or one-term (fuel-only)
   resource model — this determines whether "Fuel Ratio" needs to be
   represented as a tunable split at all in v0.1, or just as a fuel-liters
   knob.
2. **Treat "Fuel Ratio" as a pre-stint garage decision, not a per-lap
   driving input.** It's set once per stint (or at each pit visit) and
   determines both the physical fuel loaded and, for hybrid cars, the
   battery-share ceiling for that stint — not something the coach advises
   on mid-corner. The coach's between-laps advice can reasonably say "your
   fuel-ratio is unbalanced — you'll run out of NRG N laps before you run
   out of fuel" (or vice versa) using the same laps-remaining logic the
   in-game HUD already surfaces, per the GT3 forum's stated tuning goal
   (fuel-laps ≈ NRG-laps).
3. **No lift-point coaching feature exists to lean on or replicate from the
   base game** — this is genuinely open ground for the coach to add value
   (per the deferred-scope discipline in CLAUDE.md, decide explicitly
   whether an LMU-specific lift-and-coast advisor is v0.1 or deferred; it
   is not something to build "because the game almost has it" — it doesn't).
   If pursued, it would need per-corner braking-point/speed-target data
   (which the existing corner-map architecture already owns) correlated
   against measured per-lap fuel/NRG delta — a genuinely new feature, not a
   port of an in-game mechanic.
4. **Confirm the REST API before relying on it.** `/rest/strategy/usage` on
   `localhost:6397` is the most promising lead for reading live Virtual
   Energy state without polling shared memory, but no field schema was
   found — this needs a direct spike (the project already has an LMU
   data-access feasibility spike planned; add "does /rest/strategy/usage
   return NRG %, and in what units" as a concrete question for it) before
   any code assumes its shape.
5. **Don't hardcode MJ.** The WEC real-world regs use MJ per stint, but no
   LMU in-game source in this research confirmed the game itself surfaces a
   raw MJ number anywhere (UI or API) — everything found was %/kWh/liters.
   Keep the internal resource model unit-agnostic (or default to %/kWh)
   rather than assuming MJ, until the feasibility spike confirms otherwise.

## Sources

- [How to manage Virtual Energy in Le Mans Ultimate | Traxion](https://traxion.gg/how-to-manage-virtual-energy-in-le-mans-ultimate/)
- [How to Make the Most of Le Mans Ultimate's Hybrid Systems | trophi.ai](https://www.trophi.ai/post/how-to-make-the-most-of-le-mans-ultimates-hybrid-systems)
- [Le Mans Ultimate Virtual Energy Tank System: NRG guide | simracingcockpit.gg](https://simracingcockpit.gg/le-mans-ultimate-virtual-energy-tank-system-nrg-guide/)
- [LMU - Fuel Ratio setting suggestion for Bahrain Outer (GT3s) | simracers4life forum](https://forum.simracers4life.com/showthread.php?tid=2784)
- [Save virtual energy with the LMGT3s | Steam Community Discussions](https://steamcommunity.com/app/2399420/discussions/0/594032343582364725/)
- [Mastering Lift and Coast | Coach Dave Academy](https://coachdaveacademy.com/tutorials/mastering-lift-and-coast-how-to-save-fuel-without-sacrificing-speed/)
- [Hypercar Electric Motor Maps in Le Mans Ultimate Explained | Coach Dave Academy](https://coachdaveacademy.com/tutorials/hypercar-electric-motor-maps-in-le-mans-ultimate-explained/)
- [What is Virtual Energy? (NRG) | official LMU guide](https://guide.lemansultimate.com/hc/en-gb/articles/13152376674191-What-is-Virtual-Energy-NRG) (title/URL only — page 403'd on direct fetch, not independently verified beyond search snippets)
- [Hypercar Category (LMH & LMDh) | official LMU guide](https://guide.lemansultimate.com/hc/en-gb/articles/13152322265231-Hypercar-Category-LMH-LMDh) (same caveat — 403'd on direct fetch)
- [GitHub - Swizzjack/lmu-pitwall](https://github.com/Swizzjack/lmu-pitwall) — REST API port/endpoint primary source
- [GitHub - andrejsstepanovs/lmu-fuel](https://github.com/andrejsstepanovs/lmu-fuel) — manual GT3 fuel/VE calculator, no telemetry integration
- [GitHub - NikMusy/le-mans-ultimate-telemetry](https://github.com/NikMusy/le-mans-ultimate-telemetry) — shared-memory-based dashboard, raw fuel-liters field confirmed
- [Telemetry | Le Mans Ultimate Wiki](https://lemansultimate.wiki.gg/wiki/Telemetry)
- [Le Mans Ultimate | DR Sim Manager docs](https://docs.departedreality.com/dr-sim-manager/general/sources/le-mans-ultimate/) — confirms rFactor2SharedMemoryMapPlugin basis, no fuel/VE field detail
