# Circuit de la Sarthe: proposed corner table (#132)

**Status: PROPOSAL for driver review. Nothing in `simcoach/tracks/circuit_de_la_sarthe.json`
has been changed.** Every name marked *(unsure)* needs your call.

## TL;DR

1. **The detected geometry is mostly right.** 29 of the 30 corners match a real steering
   event within about 25 m, with the correct direction.
2. **One corner is missing.** The left-hand entry of Ford Chicane 2 (about 13 400 m) was
   never detected. Old T30 is only the chicane's exit right, and its apex sits 40 m late.
3. **The notes are the real bug.** `notes.corners` is keyed by the **LMU wiki's own
   21-turn numbering**. The map uses **30 detection-order numbers**, and the keys bind by
   label, so almost every note is served against the wrong corner. For example:
   - The Tertre Rouge note ("critical right-hander") lands on map T6, which is a
     *left* in the Esses.
   - The Karting note ("fast left") lands on T17, which is Mulsanne Corner, a slow *right*.
   - The Ford Chicane 1 note lands on Indianapolis.

   This is also why the coach guessed "Ford chicane = T17-18".
4. **"38 turns" is the official circuit count.** No numbering we have (map 30, LMU wiki
   21, RaceControl 33) reaches 38. The difference is flat kinks that no one brakes or
   steers for.

The recommended fix is:
- keep the map's numbers (lowest churn);
- insert the missing left as **T30** and relabel old T30 as **T31**;
- put the names in `sections`, the way Spa does;
- re-key every note to the map's numbers.

## Method and sources

**Telemetry.** The sample is the 24 complete, valid laps under 250 s at "Circuit de la
Sarthe", read from a copy of the live DB:
- DKR Engineering #4 (ELMS25, LMP2-class);
- Racing Team Turkey #34 (WEC 2026 GT3);
- Heart of Racing #23 (WEC 2026 GT3).

Each lap was resampled onto a 5 m lap-distance grid, and the median was taken across laps
for these channels:
- speed and steering (`+` = right, the same convention `mapbuild` uses and
  `verify_map_directions` checks);
- `lat_g`, brake and throttle;
- heading curvature from `pos_x`/`pos_z`.

Directions were then checked lap by lap for T9, T16, T29, T30 (new) and T31: 24/24 laps
agree on every one. Scratch scripts are not committed. They live in the session scratchpad
as `prof.py` and `apex.py`.

**References.**
- [LMU wiki: Track Guide: Circuit de la Sarthe][wiki]. Uses T1–T21 numbering, with
  T4.1/T4.2/T5 inside the Esses. This is the source of the current `notes`.
- [RaceControl: LMU Circuit de la Sarthe track guide][rc]. Uses a 33-turn numbering and
  gives a direction per turn. Its Porsche Curves and Ford chicane directions match our
  telemetry exactly. Its Mulsanne chicane 1 directions (it says left, then right) do
  **not** match: the telemetry shows right-left-right, 24/24 laps. Treat its per-turn
  directions with care.
- [Wikipedia: Circuit de la Sarthe][wp]. The infobox gives **38 turns** for the Circuit
  des 24 Heures, and 14 for the Bugatti circuit. The article gives history, not a
  per-turn list.

[wiki]: https://lemansultimate.wiki.gg/wiki/Track_Guide:_Circuit_de_la_Sarthe
[rc]: https://news.racecontrol.gg/tracks/le-mans-ultimate-circuit-de-la-sarthe-le-mans-track-guide/
[wp]: https://en.wikipedia.org/wiki/Circuit_de_la_Sarthe

## Current map: where it is wrong

Distances are in metres of lap distance. "Measured" is the median steering peak, with the
minimum speed and peak |lat g| over the corner.

| Map | Dir | Apex (map) | Measured | Verdict |
|---|---|---|---|---|
| T1 | R | 655 | 655, 206 km/h at exit braking, 1.9 g | OK: Dunlop Curve |
| T2 | L | 846 | 835, 91 km/h | OK: Dunlop Chicane, left |
| T3 | R | 928 | 900, 95 km/h | OK, apex ~25 m late |
| T4 | L | 1119 | 1100, flat, 1.1 g | OK: flat left after the chicane |
| T5 | R | 1255 | 1300, flat, 1.8 g | OK |
| T6 | L | 1501 | 1485, 133 km/h, braking | OK: Esses, left |
| T7 | R | 1610 | 1600, 146 km/h, part-throttle | OK: Esses, right |
| T8 | R | 1937 | 1925, 164 km/h, braking | OK: Tertre Rouge |
| T9 | R | 2128 | flat sweep, R ≈ 400–600 m, 0.6 g | Real but **flat**. As mild as four unmapped bends (below) |
| T10 | R | 4039 | 4045, 115 km/h | OK: Mulsanne Chicane 1, entry |
| T11 | L | 4148 | 4125, 99 km/h | OK: Mulsanne Chicane 1, middle |
| T12 | R | 4257 | 4245, flat, 1.0 g | OK: Mulsanne Chicane 1, exit (flat) |
| T13 | L | 6031 | 6010, 117 km/h | OK: Mulsanne Chicane 2, entry |
| T14 | R | 6113 | 6095, 98 km/h | OK: Mulsanne Chicane 2, middle |
| T15 | L | 6222 | 6200, flat, 1.0 g | OK: Mulsanne Chicane 2, exit (flat) |
| T16 | R | 7587 | 7525–7550, 270 km/h, 0.9 g; braking starts 7550 | Real. **Apex 40–60 m late** (it sits in the braking zone) |
| T17 | R | 7751 | 7745, 79 km/h | OK: Mulsanne Corner |
| T18 | R | 9661 | 9670, 193 km/h, braking, 1.8 g | OK: Indianapolis entry bend |
| T19 | L | 9852 | 9845, 103 km/h | OK: Indianapolis |
| T20 | R | 10180 | 10160, **73 km/h (slowest on the lap)** | OK: Arnage |
| T21 | R | 11544 | 11535, 185 km/h, braking | OK: Porsche Curves, first right |
| T22 | L | 11790 | 11775, 195 km/h | OK: Porsche Curves, left (1 of 2) |
| T23 | L | 11981 | 12005, 205 km/h, 2.0 g | OK: Porsche Curves, left (2 of 2) |
| T24 | R | 12172 | 12155, 171 km/h; steering held to **~12 330** | OK, but the **window ends at 12 227**, about 100 m short |
| T25 | L | 12445 | 12435, 169 km/h, lift | OK: Karting *(name unsure, see below)* |
| T26 | R | 12691 | 12675, flat, 1.0 g | OK: flat right |
| T27 | L | 12800 | 12785, flat, 1.1 g | OK: flat left |
| T28 | L | 13237 | 13250, 125 km/h, braking | OK: Ford Chicane 1, left |
| T29 | R | 13318 | 13320, 116 km/h | OK: Ford Chicane 1, right |
| — | **L** | — | **13 400, 89 km/h, braking, 24/24 laps** | **MISSING: Ford Chicane 2, left.** It falls in the gap between T29's end (13 373) and T30's start (13 428) |
| T30 | R | 13482 | 13445, 88 km/h | Ford Chicane 2, right. **Apex ~40 m late** (throttle is already at 100 % by 13 475) |

Flat curvature that is **not** mapped. Each is taken at full throttle with ≤ 0.7 g:
- ~2 600 (R, Mulsanne straight)
- ~8 500 (R, between Mulsanne Corner and Indianapolis)
- ~10 750 (L, after Arnage)
- ~11 210 (L, before the Porsche Curves)
- ~300–475 (R, pit straight)

One of the two lefts after Arnage is most likely the wiki's "Turn 13, No Name corner:
slight bend … isn't important" *(unsure which one)*. Adding any of these is not
recommended: there is nothing to brake or steer for, so there is nothing to coach.

No **spurious** corners were found. None sits on a straight with no steering.

## Proposed corner list

Numbering is kept, except for the inserted T30 and old T30 becoming T31. Named single
corners carry a `name`. Paired or complex corners stay `Turn N` and get a `sections` span
(the Spa convention, commit a0c69d2), so the coach reads "T11 (left) [part of Mulsanne
Chicane 1]".

The windows below are the current values unless a change is listed.

| Label | Dir | Name | Section | Apex / window change | Evidence (telemetry; reference) |
|---|---|---|---|---|---|
| 1 | R | **Dunlop Curve** | — | — | 655, flat to braking, 1.9 g; wiki T1, RC T1 |
| 2 | L | Turn 2 | **Dunlop Chicane** (2–3) | — | 835, 91 km/h; wiki T2 "Dunlop Chicane left" |
| 3 | R | Turn 3 | Dunlop Chicane | apex 928 → ~905 (optional) | 900, 95 km/h; wiki T3 |
| 4 | L | Turn 4 | *(unsure: Esses, or none)* | — | 1100, flat left; RC T4 "left, flat-out". Not in the wiki |
| 5 | R | Turn 5 | **Esses** (5–7) *(unsure on the span)* | — | 1300, flat, 1.8 g; wiki T4.1 "high-speed right, flat-out" |
| 6 | L | Turn 6 | Esses | — | 1485, 133 km/h; wiki T4.2 "medium-speed left" |
| 7 | R | Turn 7 | Esses | — | 1600, 146 km/h; wiki T5 "right-hand continuation … sets up Tertre Rouge" |
| 8 | R | **Tertre Rouge** | — | — | 1925, 164 km/h; wiki T6, RC T8 |
| 9 | R | Turn 9 | — | *(keep or fold into T8, see Q3)* | flat sweep onto the Mulsanne straight; RC T9 "gentle bend" |
| 10 | R | Turn 10 | **Mulsanne Chicane 1** (10–12) | — | 4045, 115 km/h; wiki T7 "First Mulsanne Chicane" |
| 11 | L | Turn 11 | Mulsanne Chicane 1 | — | 4125, 99 km/h |
| 12 | R | Turn 12 | Mulsanne Chicane 1 | — | 4245, flat exit |
| 13 | L | Turn 13 | **Mulsanne Chicane 2** (13–15) | — | 6010, 117 km/h; wiki T8 "Second Mulsanne Chicane" |
| 14 | R | Turn 14 | Mulsanne Chicane 2 | — | 6095, 98 km/h |
| 15 | L | Turn 15 | Mulsanne Chicane 2 | — | 6200, flat exit |
| 16 | R | **Mulsanne Kink** *(unsure)* | — | apex 7587 → ~7540 | 7525–7550, 270 km/h, braking for T17 starts here. Neither guide names it |
| 17 | R | **Mulsanne Corner** | — | — | 7745, 79 km/h; wiki T9 |
| 18 | R | Turn 18 | **Indianapolis** (18–19) | — | 9670, 193 km/h under braking; wiki T10 "Indianapolis – entry bend … high-speed kink under braking" |
| 19 | L | Turn 19 | Indianapolis | — | 9845, 103 km/h; wiki T11 "Indianapolis – apex/exit, left-hand" |
| 20 | R | **Arnage** | — | — | 10160, 73 km/h (slowest point of the lap); wiki T12 "slowest corner on the circuit" |
| 21 | R | Turn 21 | **Porsche Curves** (21–24) | — | 11535, 185 km/h; wiki T14 "fast right-hander" |
| 22 | L | Turn 22 | Porsche Curves | — | 11775, 195 km/h; wiki T15 "double left" (1 of 2) |
| 23 | L | Turn 23 | Porsche Curves | — | 12005, 205 km/h; wiki T15 (2 of 2) |
| 24 | R | Turn 24 | Porsche Curves | end 12227 → ~12330 | 12155, steering held to 12 330; wiki T16 "long right-hand continuation" |
| 25 | L | **Karting** *(unsure)* | — | — | 12435, 169 km/h, lift; wiki T17 "Karting: fast left, tap the brakes" |
| 26 | R | Turn 26 | — | — | 12675, flat; RC T28 "right, flat-out". Unnamed in both guides |
| 27 | L | Turn 27 | — | — | 12785, flat; RC T29 "left, flat-out" |
| 28 | L | Turn 28 | **Ford Chicane 1** (28–29) | — | 13250, 125 km/h; wiki T18–19 |
| 29 | R | Turn 29 | Ford Chicane 1 | — | 13320, 116 km/h |
| **30 (new)** | **L** | Turn 30 | **Ford Chicane 2** (30–31) | **start ~13 375, apex ~13 400, end ~13 415** | 13 400, 89 km/h, 24/24 laps left; wiki T20–21, RC T32–33 "tight left-right" |
| **31** (was 30) | R | Turn 31 | Ford Chicane 2 | **apex 13482 → ~13 445**, start 13 428 → ~13 415 | 13445, 88 km/h |

Total: **31 corners.**

Proposed `sections`:

| Name | Corners | Distance span |
|---|---|---|
| Dunlop Chicane | T2–T3 | 764–1010 |
| Esses | T5–T7 | 1187–1692 *(unsure)* |
| Mulsanne Chicane 1 | T10–T12 | 3958–4340 |
| Mulsanne Chicane 2 | T13–T15 | 5950–6305 |
| Indianapolis | T18–T19 | 9580–9935 |
| Porsche Curves | T21–T24 | 11463–12330 |
| Ford Chicane 1 | T28–T29 | 13155–13373 |
| Ford Chicane 2 | T30–T31 | 13375–13565 |

The corner geometry fields (`radius_m`, `width_m`, `banking_deg`, `elev_delta_m`) for new
T30 and the reshaped T24/T31 must be re-baked with `scripts/backfill_corner_geometry.py`,
not hand-copied.

## Current label → proposed

| Current | Action |
|---|---|
| T1–T29 | **Keep the label.** Rename or section as above. T3 and T16 get an apex nudge; T24 a window extension |
| — | **Insert T30** (Ford Chicane 2, left) |
| T30 | **Relabel to T31**, and nudge apex/start |

Nothing is split, merged or deleted. The one exception is T9, if you pick "fold into T8"
(see Q3).

**Cost of the relabel.** Any stored per-corner history keyed on label "30" (the exit right
of the last chicane) will stop lining up with the new T30 (the entry left). This is the
only label whose meaning changes.

## Notes re-keyed (wiki numbering → map labels)

| Current key (wiki T#) | What it describes | Currently served on (map) | Proposed key |
|---|---|---|---|
| `1` | Dunlop Curve | T1 ✓ | `1` |
| `2-3` | Dunlop Chicane | T2–T3 ✓ | `2-3` |
| `4-5` | Esses (wiki T4.1 R, T4.2 L, T5 R) | T4 (flat L), T5 | `5-7` |
| `6` | Tertre Rouge, "critical right-hander" | **T6 = a LEFT in the Esses** | `8` |
| `7` | First Mulsanne Chicane | **T7 = Esses right** | `10-12` |
| `8` | Second Mulsanne Chicane | **T8 = Tertre Rouge** | `13-15` |
| `9` | Mulsanne Corner, "tight right" | **T9 = flat sweep** | `17` |
| `10` | Indianapolis entry kink | **T10 = Mulsanne Chicane 1** | `18` |
| `11` | Indianapolis, left | **T11 = Mulsanne Chicane 1 left** | `19` |
| `12` | Arnage, "slowest corner" | **T12 = flat chicane exit** | `20` |
| `13` | "No Name" bend, "not important" | **T13 = Mulsanne Chicane 2** | drop, or fold into the overview (no mapped corner, Q5) |
| `14-16` | Porsche Curves | **T14–T16 = chicane 2 + kink** | `21-24`. Also rewrite the text's own "T14/T15/T16" to T21/T22–23/T24 |
| `17` | Karting, "fast left" | **T17 = Mulsanne Corner (right)** | `25` |
| `18-19` | Ford Chicane 1 | **T18–T19 = Indianapolis** | `28-29` |
| `20-21` | Ford Chicane 2 | **T20–T21 = Arnage + Porsche R** | `30-31` |
| `22`…`30` | empty strings | — | delete |

**Invariant.** In two cases today, a note's stated direction contradicts the computed
direction of the corner it is served on:
- key `6` ("right-hander") is served on a left;
- key `17` ("fast left") is served on a right.

The "corners are owned data" rule exists to stop exactly this. The re-key removes both.

## Proposed `notes.overview`

Bindings, with the rest of the current wording kept:

> 13.626 km, **38 turns by the official count (this map numbers the 31 you steer or brake
> for; its T-numbers are not the LMU wiki's 21-turn numbering)**, WEC layout — by far the
> longest in the roster, and losing focus over the lap length is itself a common mistake.
> ABS activation is a recurring GT3 risk through the **Dunlop Chicane (T2–T3)** and the
> **Ford Chicanes (T28–T29, T30–T31)**: brake early and straight rather than trail-braking
> hard. Hypercar's main differentiator is hybrid-torque wheelspin on exits (**Tertre Rouge
> T8**, **Mulsanne Chicane 1 T10–T12**, **Mulsanne Chicane 2 T13–T15**) rather than
> different lines, plus energy management across the long straights. Common mistakes:
> overdriving the chicane braking zones, aggressive kerbs through the **Porsche Curves
> (T21–T24)**, early throttle at **Tertre Rouge (T8)** and **Arnage (T20)**, and fading
> concentration late in the lap.

The current overview says "the three long straights". The telemetry shows **four
full-throttle runs of 1 km or more**, plus the shorter pit straight:

| From | To | Length |
|---|---|---|
| T9 | T10 | ~1.7 km |
| T15 | T16/T17 | ~1.2 km |
| T17 | T18 | ~1.7 km |
| T20 | T21 | ~1.1 km |
| T31 | T1 | ~0.7 km to the Dunlop Curve braking point (via the pit straight) |

I have replaced "three" with "the long straights" rather than guess which three the wiki
meant (Q6).

## 38 vs 30, reconciled

| Source | Count | What it counts |
|---|---|---|
| Wikipedia infobox, LMU wiki header | 38 | Official circuit turn count. I could not find the official per-turn list, so I **cannot say which 38** |
| RaceControl guide | 33 | Same sequence as the proposed map, T1–T17 and T18–T31. It adds two flat bends between Mulsanne Corner and Indianapolis (its T18 R, T19 L). The telemetry shows only one there, a right at ~8 500 m |
| LMU wiki guide | 21 | One number per chicane or complex, plus T4.1/T4.2 inside the Esses. Skips map T4, T9, T16, T26 and T27 |
| This map (current) | 30 | Detected steering events. Misses the Ford Chicane 2 left |
| This map (proposed) | 31 | Every braking or steering event, plus flat bends at ≥ ~1 g (T4, T5, T12, T15, T16, T26, T27). T9 (~0.6 g) is the weak one, see Q3 |

The gap from 31 to 38 is most plausibly the flat bends we deliberately leave unmapped
(~300, ~2 600, ~8 500, ~10 750, ~11 210 m) plus one or two more. That is an educated
guess, not verified.

## Pronunciation hints (#123)

These follow Spa's precedent (commit 0d792bf): **anglicised, English vowels, no French
nasals**. They are respellings for your approval. The phonemes themselves come from
`scripts/harvest_pronunciations.py circuit_de_la_sarthe` on the dev machine.

| Name | Suggested sound | Note |
|---|---|---|
| Tertre Rouge | "TAIR-truh ROOZH" | "Rouge" as in Spa's "Eau Rouge" (`ɹˈuːʒ`), so it stays consistent |
| Mulsanne (Chicane 1/2, Corner, Kink) | "mool-SAN" | English broadcast usage varies ("MUL-san"); your call |
| Arnage | "ar-NAHZH" | |
| Esses | "ESS-iz" | Plain English. The harvester may treat it as a common word; check it isn't |
| Dunlop Curve, Dunlop Chicane | English | Harvest normally |
| Indianapolis | English | Harvest normally |
| Porsche Curves | "POR-shuh" | Kokoro may say "porsh"; your call |
| Karting | English | Single common English word: **pass it via `--exclude`**, per the harvest script's rule |
| Ford Chicane 1/2 | English | |

The 30 existing `Turn N` entries in `pronunciations` stay valid. Add `Turn 31`, and drop
any entry for a name that disappears.

## Open questions for you

1. **Numbering.** Should we keep the map's detection-order numbers (recommended: lowest
   churn, and T1–T29 keep their meaning)? Or switch labels to the LMU wiki's scheme, with
   letter suffixes like Road Atlanta's `10A/10B` (e.g. `7A/7B/7C` for Mulsanne Chicane 1)?
   The wiki scheme matches the guides you read, but it relabels 28 corners and breaks
   per-corner history. The wiki's own T4.1/T4.2/T5 also doesn't line up with our four
   Esses-area corners.
2. **Esses span.** Is it T5–T7 (the wiki's T4.1 flat right, T4.2 left, T5 right), or
   T6–T7 only (the left-right braking pair)? And is T4 (the flat left right after the
   Dunlop Chicane, near the Dunlop bridge) part of it or just "Turn 4"?
3. **T9** (flat right sweep after Tertre Rouge). Keep it as its own corner, or fold it
   into T8's window as Tertre Rouge's exit? It is no stronger than the four flat bends we
   leave unmapped.
4. **T16 name.** Is "Mulsanne Kink" what you call the fast right just before the Mulsanne
   Corner braking zone? Neither guide names it.
5. **Wiki "Turn 13, No Name corner".** Is it the flat left at ~10 750 or the one at
   ~11 210? Either way, should its note be dropped (it says "isn't important"), or should
   we map it?
6. **"Three long straights"** in the overview: which three do you mean? Or should it
   just say "the long straights"?
7. **Karting = T25?** I'm moderately sure; it rests on the wiki's T17 "fast left" after
   the Porsche Curves' long right. Also, should T26–T27 (the flat right-left before the
   Ford chicanes) get a name? I don't have a reliable source for one. "Maison Blanche" is
   the *old* corner the Porsche Curves bypass, so I would not use it here.
8. **Pronunciations**: Mulsanne as "mool-SAN" or "MUL-san"; Porsche as "POR-shuh" or
   "porsh".
