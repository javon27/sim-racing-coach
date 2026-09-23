# SimCoach — Product Vision

The north star: **on the rig, spend time competing, not narrating.** The
agent should automatically have all the race context the driver used to type by
hand (into a Gemini notebook) — fastest lap, which corners they struggled with,
qualifying position, how the race went — with zero manual entry.

## Four modes, one core, one evolving driver model

All modes share the UI-agnostic agentic core (tool-use, no hardcoded commands)
and a swappable Model, per the architecture invariants in `CLAUDE.md`.

1. **Coach** — between laps. Setup + driving technique, grounded in trustworthy
   telemetry-computed corner data (the edge over a general LLM notebook).
   *Built (v1).*
2. **Race questions over PTT** — live, in-race. The coach answers race
   questions (position and gap trend, fuel/energy to the end, pit window, tire
   life, where a rival is faster) over push-to-talk, from figures computed at
   lap boundaries. Agentic: it has the tools to answer whatever comes up, not a
   fixed command set. **Not pursuing:** an in-ear spotter or a CrewChief
   replacement. The separate Pit Wall screen was retired 2026-09-22 (#151); its
   one used feature, the live corner map, is now the Coach screen's rail map.
   *Tools: #152.*
3. **Deep-dive Analyst** — after sessions. Reflective, cross-lap/cross-session
   analysis to nail down concrete improvements for next time. *Vision.*
4. **Longitudinal driver profile** — underneath all three. A coach-maintained,
   evolving model of the driver (tendencies, recurring weaknesses, what they're
   working on, progress, advice already given) that persists across sessions and
   is fed into every conversation, so each mode feels like *the same coach who
   knows you*. The static `DriverProfile` in Settings is the seed. *Vision.*

## Cross-cutting enabler: voice

The driver mostly races in **VR**, where leaving the headset to read a desktop
chat is cumbersome and risks crashing the game on mode-switch. So **voice I/O**
(speech-in, speech-out, push-to-talk) is not a nice-to-have — it's what makes
the in-race experience usable at all. It is a new *client* over the same core,
not a rewrite.

## The rig workflow this serves

Register → practice server → event (brief practice → qualy → race), all as **one
session** `(track, car, online)`. Before going out: ask for a setup
recommendation. During: race questions over push-to-talk (no in-ear spotter). After:
the arc and struggles are already known; deep-dive to plan improvements. The
driver profile carries the learnings into the next session. Rinse and repeat.

## Course-correct (2026-09-14, issue #99)

An outside review of the backlog found the pattern: **the architecture is right,
but cycles were going into a track-map curation pipeline that never improves a
lap time, while the deterministic analysis — the real edge over a generic LLM
notebook — used a fraction of the telemetry captured.** Adopted, with these
decisions:

- **The coach only states what is measured.** Baked corner radius/width were
  stale and reached the model as fact; they no longer render (#99). Elevation
  and banking stay (re-baked, rig-verified). Anything the coach can't trust is
  cut from the prompt, not "fixed later".
- **Code extracts, the model narrates.** Deltas, rankings, thresholds and
  "early/late vs best" are computed; the model is handed conclusions, not asked
  to infer them. `get_time_loss` (ranked delta-t by corner/straight) is the
  template for every remaining #38 tool.
- **Result-log ingest stays.** It is ~97% of stored laps plus race results and
  car names. The problems were seam bugs (dedupe, identity), not the
  architecture; live capture wins on conflict.
- **Parked**, labelled `deprioritized` in Issues: map-geometry curation, the
  editor's save-path guardrails, promote/import, sector auto-generation,
  replay overlay, 3D viewer, live corner callouts. They come back only when a
  coaching feature needs them.
- **Token budget:** one subagent at a time, cheaper models for implementation,
  review done in-session.

## Approved build order (post-v1, revised 2026-09-14)

1. **Tighten** — make every number the coach states honest. First the
   time/distance axis: `lap_distance` is a ~5 Hz staircase (~8.5 m steps,
   ~0.2 s delta-t error) under every brake/throttle/coasting/time-loss fact,
   while `elapsed_time` is captured and unused (#101). Then diff every
   per-corner metric against the reference, not just min speed, so "early or
   late vs best" is computed, not inferred (#102). Then #77 (store the game's
   lap number), #87 (chat-screen save flips a curated direction), #71
   (v15-store residual), #59 (delete the terminal REPL). Done: #67, #33
   (rig-verify still owed), `track_edge` cut from the prompt until validated.
2. **Deepen** — the rest of #38, cheapest first: P1.5 consistency → P1.3
   handling → P1.4 traction circle → P1.6 tire trend → P1.7 gear/lockup; then
   Phase 0 setup-channel capture → Phase 2 setup diagnostics.
3. **Voice loop** — a thin speech-in → core → speech-out path over the existing
   agentic coach. Unblocks the VR reality; highest daily-use leverage.
4. **Race tools for the coach (PTT)** — race state and gap trend, fuel/energy
   and pit window, tire life, battle comparison (#152; spec
   `docs/superpowers/specs/2026-09-22-coach-absorbs-pit-wall-design.md` §3).
   The Pit Wall screen is retired (#151).
5. **Deep-dive Analyst mode** — leans on the longitudinal profile, which
   shipped 2026-09-08 (summary injection, driver memories, session setups).

Not pursuing: proactive *push* spotter alerts ("box this lap, fuel's short") —
race answers are PTT only (decided 2026-09-22). Fast-follow after voice: make
building a track map less of a curation loop (#100).

## Where v1 stands

The v1 auto-context foundation is essentially complete — live capture, per-corner
analysis, live sector times (guarded), phase-grouped laps, race narrative (finish,
incidents, and the live position arc), setup panel, trend chart, settings + info
tips, and cross-turn chat continuity. This is the raw material every future mode
stands on. Remaining v1 gaps need the rig or the driver: populating the racing/
setup **knowledge base** (`simcoach/knowledge/*.md`), and live verification of the
coach prompt behavior, sector convention, and `mPlace` on the rig.
