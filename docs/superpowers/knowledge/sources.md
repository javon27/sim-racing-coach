# Sources — LMU setup research (citation index)

Research date 2026-08-27/28. IDs are referenced from `lmu-setup-guide.md`
and `lmu-classes.md`. S1–S9 are the user-supplied sources (numbered as
given); S10+ are this research pass's own finds. "Mined" = text/claims
actually extracted and cited elsewhere in this KB; "not mined" = found but
not fetched/extracted, listed as a lead for future research.

**R-3 update (2026-08-28):** S1–S7 were re-attempted with tools WebFetch
couldn't use — the `youtube-transcript-api` Python package (auto-generated
caption text) for the 6 videos, and a `chrome-devtools` MCP browser session
(loading `old.reddit.com`, saving the rendered HTML, extracting post + all
comment text with a stdlib `html.parser` script) for the Reddit thread. All
7 succeeded and are now mined below; entries moved out of the "not
text-mineable" list.

## User-supplied (mined this pass)

- **S8** — LMU official site, "LMP3 Quick Setup Guide":
  https://lemansultimate.com/lmp3-quick-setup-guide/
  Type: text article, dated 22 Dec 2025 on the page. Creator/author not
  explicitly credited on the page. Covers: full LMP3 setup surface
  (powertrain/TC/diff, tires/brakes/ducts, suspension/ride-height/packers,
  dampers, toe/ARB/wing), with symptom→fix tables and a recommended setup
  sequence, specific to the Ligier JS P325 and Ginetta G61 LT-P3 Evo.
  **Richest source for LMP3-specific, numbered guidance.**

- **S9** — Coach Dave Academy, "The Ultimate Le Mans Ultimate Setup Guide":
  https://coachdaveacademy.com/tutorials/the-ultimate-le-mans-ultimate-setup-guide/
  Type: text article. Author: Simon Baggus. Covers: general LMU setup
  philosophy across tire pressure, wings/ride-height/aero, suspension,
  dampers, ARB, diff, brake bias, TC/hybrid electronics; explicit
  methodology (change one thing at a time, long-run testing, endurance vs.
  sprint philosophy) and common mistakes. **Richest source overall — the
  main methodology backbone of `lmu-setup-guide.md`.**

## User-supplied (mined this pass, R-3 — video transcripts + Reddit thread)

- **S1** — Reddit thread, "New ABS system":
  https://www.reddit.com/r/LeMansUltimateWEC/comments/1l9ud8z/new_abs_system/
  (the supplied `/s/qFAaeiVkok` short-link resolves here). Type: Reddit
  (r/LeMansUltimateWEC) discussion thread, OP + ~15 comments. **Retrieved**
  via chrome-devtools browser (`old.reddit.com` render, saved HTML,
  extracted with a stdlib `html.parser` script — WebFetch is still blocked
  from reddit.com entirely). Content: community reverse-engineering of
  LMGT3's ABS setting (0–9 scale), disputing/clarifying the simple
  "more/less ABS" mental model. Cited in `lmu-classes.md` (LMGT3) and
  `simcoach/knowledge/ferrari_296_gt3.md`.

- **S2** — YouTube, "How to Be Fast at Spa in LMU - McLaren G Challenge":
  https://youtu.be/N4UbalQeGW8 (canonical: https://www.youtube.com/watch?v=N4UbalQeGW8)
  Creator: Luke Whitehead (per WebSearch snippet). Uploaded ~7 May 2026.
  **Retrieved** via `youtube-transcript-api` (auto-generated captions,
  1015 lines). Content: a Spa delta-lap driving-line breakdown for the
  McLaren 720S GT3 **fixed setup** (Logitech McLaren G Challenge, bronze
  daily race) — mostly driving line (out of scope for this KB), but the
  opening segment lists exactly what a fixed/bronze setup leaves adjustable
  (TC, TC power cut, TC slip angle, ABS strength, brake bias, fuel
  ratio/virtual energy, max pedal force) and gives the creator's own
  electronics values. Cited in `lmu-classes.md` (LMGT3) and
  `lmu-setup-guide.md`.

- **S3** — YouTube, "How To Build A Setup On Le Mans Ultimate":
  https://youtu.be/NfY8FFNibR8
  Creator: KrypticTMG (per WebSearch snippet). Uploaded ~2 Oct 2025.
  **Retrieved** via `youtube-transcript-api` (691 lines). Content: a
  full setup-building walkthrough on a Hypercar (BMW M Hybrid V8, identified
  by the fuel-ratio/virtual-energy discussion — GT3 has no hybrid layer per
  `lmu-classes.md`) at Silverstone — camber, ABS balance, brake bias/max
  pedal force, spring rates, packers, ride height/rake, dampers (Motech
  caveat), wing, toe, ARB, diff preload, with two full contrasting setups
  (low vs. high downforce) demonstrated. Cited throughout
  `lmu-setup-guide.md` and `lmu-classes.md` (Hypercar).

- **S4** — YouTube, "3 SETUP CHANGES That Make Hypercars Faster in Le Mans
  Ultimate": https://youtu.be/dVJqcyzhOuY
  Creator: not identified by WebSearch (channel handle "Alex" per the
  transcript's self-introduction). Uploaded ~20 Mar 2026. **Retrieved** via
  `youtube-transcript-api` (311 lines). Content: a systematic beginner
  tuning method on the Porsche 963 at COTA — brakes → TC → ARB, one click
  at a time, each validated with a test lap, gaining ~1.2s total over the
  default setup. Cited in `lmu-setup-guide.md`.

- **S5** — YouTube, "THE HYPERCAR GUIDE PART 5 - Anti-Roll Bar Systems in Le
  Mans Ultimate": https://youtu.be/CEbvld8Iiyg
  Creator: not identified (part of a numbered "THE HYPERCAR GUIDE" series;
  channel name not recovered). **Retrieved** via `youtube-transcript-api`
  (145 lines). Content: ARB mechanics explainer + a worked BMW-at-Monza
  example (front ARB 5→2, then rear ARB 2→3) showing the lap-time gain from
  each change. Cited in `lmu-setup-guide.md`.

- **S6** — YouTube, "THE HYPERCAR GUIDE PART 4 - Traction Control Systems in
  Le Mans Ultimate": https://youtu.be/QoHCXB2374k
  Creator: not identified, same series as S5/S7. **Retrieved** via
  `youtube-transcript-api` (211 lines). Content: TC/TC-slip/TC-cut mechanics
  explainer with a BMW-at-Spa example (dry vs. rain TC-cut values) and
  numeric starting-point guidance. Cited in `lmu-setup-guide.md`.

- **S7** — YouTube, "THE HYPERCAR GUIDE PART 3 - Brake Bias & Migration Le
  Mans Ultimate": https://youtu.be/wiI2ulZPgaM
  Creator: not identified, same series as S5/S6. Uploaded ~12 Aug 2024.
  **Retrieved** via `youtube-transcript-api` (267 lines). Content: the
  brake-migration explainer this KB previously flagged as a gap — mechanics,
  a worked Ferrari 499P-at-Bahrain example (52.3/47.7 → 51/49, gaining
  ~0.5s), and the risk/reward tradeoff of running migration at its max
  (2.5%). Fills the brake-migration gap noted in `lmu-setup-guide.md`.

## This pass's own finds (mined)

- **S10** — GitGud Racing Academy, "How to Be Faster in Le Mans Ultimate":
  https://gitgudracingacademy.com/guides/how-to-be-faster-in-le-mans-ultimate/
  Type: text article. Author: Marian Barbieru. Covers: wheel rotation
  matching, force-feedback tuning, FOV, and one specific brake-bias
  recommendation (default is too far forward; move to 54–56% rearward).
  Notably does *not* cover tire pressure/ARB/springs/ride-height/dampers/
  diff/aero — focused on driver setup + technique, not chassis setup.

## Found but not mined (leads for future research passes)

- **S11** — OverTake.gg, "Guide: How Brake Migration Works in Le Mans
  Ultimate": https://www.overtake.gg/news/guide-to-brake-migration-in-le-mans-ultimate-hypercars.3592/
  Surfaced via WebSearch while chasing S7. Not fetched this pass. Would
  directly fill the Hypercar brake-migration gap.
- **S12** — trophi.ai, "Le Mans Ultimate Hypercar Braking Guide":
  https://www.trophi.ai/post/le-mans-ultimate-hypercar-braking-guide
  Surfaced via WebSearch. Not fetched.
- **S13** — Traxion.gg, "Le Mans Ultimate: How to master Hypercar braking":
  https://traxion.gg/le-mans-ultimate-how-to-master-hypercar-braking/
  Surfaced via WebSearch. Not fetched.
- **S14** — simracingcockpit.gg, "Le Mans Ultimate: How to Drive the LMGT3
  and Hypercar": https://simracingcockpit.gg/le-mans-ultimate-how-to-drive-the-lmgt3-and-hypercar/
  Surfaced via WebSearch. Not fetched. Could help the LMGT3/Hypercar
  driving-technique gap.
- **S15** — simgasm.com, "LMU: Le Mans Hypercar Setup" (marked WIP on the
  site): https://simgasm.com/blogs/articles/wip-lmu-le-mans-hypercar-setup
  Surfaced via WebSearch. Not fetched; site itself flags the article as
  work-in-progress.

## Already cited elsewhere in the knowledge base (not re-mined here)

- Coach Dave Academy, "LMU Ferrari 296 LMGT3 Guide & Setups" and OverTake.gg,
  "LMGT3 ABS in Le Mans Ultimate Explained" — both already cited in
  `simcoach/knowledge/ferrari_296_gt3.md`. Reused (not re-fetched) for the
  LMGT3 ABS claim in `lmu-classes.md`.
- Traxion.gg track/car list articles — already cited in `lmu-roster.md`.

## Explicit non-findings

- No source (supplied or found) gave a definitive, class-by-class table of
  which driver-category (Bronze/Silver/Gold/Pro) is legally permitted which
  aids (TC/ABS/brake-bias range). Flagged as a gap in `lmu-classes.md`
  rather than guessed at.
- No source gave numeric spring-rate, ARB-click, or damper-click starting
  values for any car. The brief explicitly prohibits inventing these; they
  must come from the user's own setup sheets/logs.
- LMP2 had no dedicated setup source in either the supplied list or the
  broadened search — flagged as a gap in `lmu-classes.md`.
