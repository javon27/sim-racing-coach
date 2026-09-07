# Patreon Launch Plan — Fund the Roadmap

> **Le Mans Ultimate · AI Coach** — Monetization & go-to-market · Draft v1 (2026-09-07)
>
> Visual version (artifact): https://claude.ai/code/artifact/17059215-573e-4f8b-a799-a96937649d07

Patrons aren't buying a locked door. Because the app is **bring-your-own-key**,
your cost per user is ~zero — so Patreon sells **early access, the modes people
want built next, and a seat on the pit wall**, not compute. From a standing
start, that's the most honest pitch you have.

---

## 01 · The model

Three surfaces, one funnel. The free app is the demo; Patreon funds the build
and hands patrons the new modes first.

- **Free, public.** The app as it stands today — live capture and the
  between-laps conversational coach, one car / one track, track maps. This is
  the top of the funnel and your single best marketing asset.
- **Patreon.** Early-access builds, the advanced modes as they ship, premium
  driver-verified corner maps, a patron Discord, and a vote on what gets built next.
- **BYO-key throughout.** Users bring their own Anthropic key, so inference is
  on them. No backend, no per-user cost, no metering — you run this solo without
  financial risk, and every tier is pure margin.

---

## 02 · Tiers

Keep it to two paid tiers at launch. Don't build a $25+ tier until patrons ask
for one — it's a door with nothing behind it yet.

| Tier | Price | What they get |
|---|---|---|
| **Free** | $0 | Between-laps conversational coach · one car/track · track maps · BYO Anthropic key |
| **Pit Crew** *(recommended entry)* | $5/mo | Everything in Free · advanced modes as they ship · early-access builds · premium driver-verified corner maps · patron Discord |
| **Race Engineer** | $12/mo | Everything in Pit Crew · roadmap voting · feature-request priority · credits listing · changelog deep-dives |

---

## 03 · Free vs paid

The line is drawn by **mode**, not by throttling the basics. Near term, paid =
early access to modes still being built — so as each one ships, the paid tier
gets objectively fatter. Good compounding story; tell it plainly.

| Capability | Free | Patreon |
|---|---|---|
| Live capture + between-laps coach | ✅ Included | ✅ Included |
| Deep-dive post-session analysis | — | ✅ On ship |
| Longitudinal driver profile | — | ✅ On ship |
| Live pit-wall / voice engineer | — | ✅ On ship |
| Premium driver-verified maps | — | ✅ Included |
| Early-access builds & roadmap vote | — | ✅ Included |

---

## 04 · Tracking

Honor-system distribution — the full build lives in a patron-only post, no
in-app enforcement — but you still want to *see* the funnel.

- **Anonymous, opt-out analytics.** Events for install, sim detected,
  sessions/laps analyzed, which features get used, and a free-vs-patron build
  flag. Enough to measure reach, engagement, and conversion without collecting
  anything personal.
- **Simplest path:** a hosted event tool (PostHog or Plausible free tier) plus a
  build-variant flag baked into the patron build (`patron=true`). No backend of
  your own, no OAuth, no login flow. Conversion cohorts for free.
- **Add later, only if needed:** a one-time patron code entered on first launch,
  to tie a build to a cohort. Skip it until you actually need per-cohort numbers
  — it's not enforcement and shouldn't pretend to be.

---

## 05 · The wedge

You're entering a category with a free incumbent. Lead every message with what
makes this different — never with "it's a spotter."

- **vs. CrewChief.** CrewChief is a scripted spotter — fixed phrases, no *why*.
  This is an AI engineer that reads your telemetry and **explains the lap in
  conversation**: where you braked early, which corner cost the tenths, what to change.
- **vs. the manual workflow.** It replaces the tedious copy-telemetry-into-a-
  chatbot routine you built this to kill. The context — setup, fastest lap,
  corner map, your line — is already loaded, so you just ask.

---

## 06 · Campaign — from zero

No audience yet, so the plan builds one before it converts one. The phases run
in order; don't skip to the ask before the demo exists.

1. **Cut the anchor demo.** One tight 2–3 minute video: a real lap where the
   coach catches a concrete mistake — braking too early into the Mulsanne
   chicane — and the lap time drops. Everything points here. Without it, nothing
   downstream converts. *(YouTube + embeddable clip.)*
2. **Seed value, don't sell.** Post the free build + demo as "I built this," not
   an ad, where LMU drivers already gather. Answer questions, take the roasting,
   ship fixes in public. *(r/LeMansUltimate · r/simracing · LMU Discord ·
   OverTake / RaceDepartment.)*
3. **Weekly corner-clips.** 30–60s clips of the coach nailing one specific
   corner tip. Sim racers eat concrete, corner-level advice — this is your
   organic engine and it costs only editing time. *(1×/week · shorts + reddit +
   discord.)*
4. **Seed the creators.** Give micro sim-racing streamers and YouTubers the full
   build free and let them use it live on stream. Authentic use in front of
   their audience beats any ad you could buy for this crowd. *(5–10 micro-
   creators to start.)*
5. **Convert.** An in-app "unlock advanced modes / fund the roadmap" prompt and a
   public roadmap patrons vote on. Free users convert when they can see exactly
   what their $5 builds next.

---

## ⚠ Before you take money — legal check, do this first

Confirm the **Le Mans Ultimate / Studio 397 EULA** permits a commercial tool
that reads their shared-memory telemetry, and keep their trademarks out of your
branding and store copy. This gates everything below — resolve it before the
first pledge, not after.

---

## First moves — next two weeks, in order of leverage

- [ ] **Clear the EULA question.** Read the shared-memory + commercial-use terms; note what's allowed in writing.
- [ ] **Record the anchor demo.** Pick the corner, capture the before/after lap, keep it under 3 minutes.
- [ ] **Wire opt-out analytics + the patron build flag.** Hosted tool, no backend — small, scoped implementation task.
- [ ] **Stand up the Patreon page** with the two tiers and a public roadmap.
- [ ] **Ship the free build** to one community and listen before posting anywhere else.
