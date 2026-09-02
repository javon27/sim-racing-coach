# Silverstone (Grand Prix layout) notes

5.891 km, 18 turns, WEC layout (the corner-map JSON `simcoach/tracks/
silverstone.json` already exists and was not touched per project
instructions -- this note adds the wiki's driving detail alongside it).
Britain's high-speed corner sequences (Maggotts-Becketts-Chapel) are the
defining feature; the guide frames the whole lap around rhythm through
those flowing sections plus clean chicane exits (Vale).

## General
- [LMU] Maggotts-Becketts-Chapel is repeatedly called out as the section
  where overdriving costs the most time in both classes.
- [LMU] Hypercar generally carries noticeably higher entry speed through
  Becketts and can brake later into Stowe/Village/Brooklands, but is more
  sensitive to abrupt steering corrections destabilizing the aero platform.
- [LMU] Setup baseline, GT3: medium-to-high rear wing, slightly soft
  suspension, conservative front tyre pressure, higher differential preload
  for the slow corners, brake bias slightly forward.
- [LMU] Setup baseline, Hypercar: medium rear wing (balance), slightly
  stiffer/compliant suspension, stable ride height under braking, moderate
  differential preload, brake bias slightly rearward.

## Corner-by-corner
- **T1 Copse (right):** First corner after the start/finish, high-speed,
  light braking or a lift depending on fuel, neutral balance, early apex
  without pinching the entry -- sets up Maggotts.
- **T2 Maggotts (left):** High-speed left, slight lift before the apex,
  stay fully left, flat and minimal steering through the corner.
- **T3-5 Becketts (right-left-right):** Commit fully on entry, lift only if
  needed for stability, use the road width (inside kerbs can unsettle the
  rear), balance over aggression -- rhythm is everything.
- **T6 Chapel (left):** Fast, flat out, marks the end of Becketts, use the
  whole track for minimum drag while accelerating -- exit speed is critical
  for Hangar Straight.
- **T7 Stowe (right):** Fast/medium at the end of Hangar Straight, brake in
  a straight line on the left, late turn-in and apex, use the whole track
  on exit, progressive throttle -- early steering input causes GT3
  understeer.
- **T8-9 Vale:** Chicane after Stowe, hard braking about 2 seconds after
  the corner comes into view, use the kerb on exit, sets up Club.
- **T10 Club (right):** Leads onto Wellington Straight, late apex, smooth
  throttle, avoid sliding (LMU simulates real rear traction loss), a stable
  exit sets up top speed on Wellington.
- **T11 Abbey (right):** High-speed, a light lift preserves the fronts.
- **T12 Farm Curve (left):** Slight kink, keep the car flat with minimal
  steering for less drag -- patience pays, over-rotation costs front grip.
- **T13 Village (right):** Leads into Loop, hard braking at the start of
  the left kerb, late apex, position right for Loop.
- **T14 Loop (left):** Tight, light braking with engine braking to turn,
  avoid aggressive kerb use (LMU penalizes over-rotation).
- **T15 Aintree (left):** Kink, minimal lift or flat out depending on
  fuel/tyres.
- **T16 Brooklands:** Slight kink at the start of Brooklands, brake at the
  flag-light on the right, trail-brake to rotate, smooth line into Luffield.
- **T17 Luffield (right):** Slow, brake in a straight line with a little
  trail-braking, rotate the car first then progressive throttle, careful
  kerb use (too aggressive causes rear instability) -- the exit dictates
  speed down to Woodcote.
- **T18-19 Woodcote (right):** Flat-out closing bend, use the full width.

## Common mistakes
Overdriving the Maggotts-Becketts-Chapel sequence, early throttle at
Luffield, excessive kerb use at Chapel or Club, aggressive rotation
overheating the fronts, poor rhythm through Abbey-Farm-Village. Hypercar
adds: too-aggressive trail-braking at Stowe/Brooklands, excessive kerb use
in Vale, destabilizing the aero platform with abrupt steering.

Source: LMU wiki (lemansultimate.wiki.gg), CC-BY-SA, summarized 2026-08-29.
