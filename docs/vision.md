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
2. **Pit Wall** — live, in-race. A race-engineer persona for pit timing, fuel/
   energy-to-finish, gaps and deltas, relative pace. Agentic: it has the tools
   to answer whatever comes up, not a fixed command set. Aims to replace
   CrewChief. *Vision.*
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
recommendation. During: hands-free race-engineer questions (Pit Wall). After:
the arc and struggles are already known; deep-dive to plan improvements. The
driver profile carries the learnings into the next session. Rinse and repeat.

## Approved build order (post-v1)

1. **Voice loop** — a thin speech-in → core → speech-out path over the existing
   agentic coach. Unblocks the VR reality; highest daily-use leverage.
2. **Pit Wall persona + live-race tools** — standings/gaps, fuel/energy, pit
   window, relative pace (most data already reachable via `simcoach/lmu/rest.py`
   plus the per-lap position capture already in place).
3. **Longitudinal driver profile** — once there's a body of sessions worth
   distilling; a stored, evolving profile injected into every conversation and
   updated after deep-dives.
4. **Deep-dive Analyst mode** — leans on the profile.

Later/optional: proactive *push* spotter alerts ("box this lap, fuel's short"),
distinct from the agentic *pull* model above.

## Where v1 stands

The v1 auto-context foundation is essentially complete — live capture, per-corner
analysis, live sector times (guarded), phase-grouped laps, race narrative (finish,
incidents, and the live position arc), setup panel, trend chart, settings + info
tips, and cross-turn chat continuity. This is the raw material every future mode
stands on. Remaining v1 gaps need the rig or the driver: populating the racing/
setup **knowledge base** (`simcoach/knowledge/*.md`), and live verification of the
coach prompt behavior, sector convention, and `mPlace` on the rig.
