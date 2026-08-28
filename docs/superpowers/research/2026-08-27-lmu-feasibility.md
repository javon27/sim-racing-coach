# LMU Data-Access Feasibility Spike — Findings

Date: 2026-08-27 (probed live on the user's machine, LMU running, GT3 on track)
Status: **PASS** — all three design pillars confirmed readable live.
Task: Task 2 of the core plan (rig-side feasibility probe).

## TL;DR

Le Mans Ultimate exposes **two** independent data paths, both live-verified:

1. **Local REST API on `http://localhost:6397`** (the in-game UI backend) —
   returns setup, live standings/timing, lap distance, fuel, weather. Trivially
   parseable JSON. Covers the setup pillar, the lap-boundary pillar, the
   distance axis, and a surprising amount of live telemetry-adjacent data.
2. **Shared memory (memory-mapped files)** — the classic Iron Wolf
   `$rFactor2SMMP_*$` MMFs (plugin **enabled**) and LMU's **first-party**
   `LMU_Data` SDK MMF. Reserved for the 50 Hz driver inputs and per-tire
   channels the REST API does not carry.

Nothing about the design needs to change. The capture component (deferred to the
follow-up plan) has confirmed interfaces to build against.

## Environment observed

- LMU install: `S:\SteamLibrary\steamapps\common\Le Mans Ultimate`
- Ports open on localhost: **6397** and 6398 (REST API).
- Plugins present: `rFactor2SharedMemoryMapPlugin64.dll` (**`" Enabled": 1`** in
  `Plugins/CustomPluginVariables.JSON`), `LMU.3.8.SharedMemoryMapPlugin.dll`,
  `TelemetryToolPluginForLMU.dll`, `ChatTransceiver.dll`.
- First-party SDK header: `Support/SharedMemoryInterface/SharedMemoryInterface.hpp`
  (Studio 397 / MSG, 2025) — documents the native MMF.

## Pillar 1 — Live telemetry: CONFIRMED

**Shared memory (raw byte probe, `mmap` from Python, user driving):**
- `$rFactor2SMMP_Telemetry$` — OPEN, live. Header decoded:
  `mVersionUpdateBegin == mVersionUpdateEnd == 14237` (double-buffer **snapshot is
  consistent** — read when begin==end for a torn-free frame), `mNumVehicles = 6`.
- `$rFactor2SMMP_Scoring$` — OPEN, live.
- `LMU_Data` (native SDK) — OPEN, live: `gameVersion = 14130`, `FFBTorque = 0.639`
  (non-zero steering load = actively driving). Layout = `SharedMemoryObjectOut`
  { generic, paths, scoring(`ScoringInfoV01` + `VehicleScoringInfoV01[104]`),
  telemetry(`TelemInfoV01[104]`) }, guarded by a spinlock
  (`LMU_SharedMemoryLockData` / `LMU_Data_Event`), structs from
  `InternalsPlugin.hpp`.

**REST also carries live motion** (`/rest/watch/standings`, player entry):
`carVelocity` (vector; |v| ≈ 59.3 m/s ≈ 213 km/h), `carAcceleration` (vector),
`carPosition` (world x/y/z), `pathLateral`.

**Decision for the follow-up:** use the **classic rF2 SMMP** MMFs for the 50 Hz
driver inputs (`mUnfilteredThrottle/Brake/Steering`), engine, and per-wheel
tire temps — it is the best-documented path (struct defs in
TonyWhitley/pyRfactor2SharedMemory `rF2data.py`, and in the local
`InternalsPlugin.hpp`). `pyRfactor2SharedMemory` is **not on PyPI** — vendor the
ctypes struct module or read via the documented offsets. Use the begin==end
version check for torn-free reads. LMU's native `LMU_Data` is a viable
alternative (richer, first-party) but needs the spinlock protocol.

## Pillar 2 — Active setup: CONFIRMED

`GET http://localhost:6397/rest/garage/getPlayerGarageData` → **172 setting keys**,
each `{ key, stringValue, lastSavedStringValue, minValue, maxValue, isFreeSetting,
... }`. Examples live: `VM_ANTILOCKBRAKESYSTEMMAP = "9 (Understeer)"`,
`VM_TRACTIONCONTROLMAP = "8"`.

**Tier signal found:** `isFreeSetting` marks which params are adjustable in the
current session — **170 of 172 free** in this practice session. In a Bronze race
only TC / brake-bias / ABS would report `free=True`. This is exactly the input
the tier-aware coach needs; **read tier-legality from `isFreeSetting`, do not
hardcode a tier table.** (Confirms the deferred tier-awareness follow-up is
buildable off real data.)

## Pillar 3 — Lap boundaries + distance axis: CONFIRMED

`GET /rest/watch/standings` (player entry) returns, live:
- `lapDistance = 6126.70` — **the lap-distance alignment axis**, straight from the API.
- `lapStartET`, `timeIntoLap`, `estimatedLapTime`, `sector` (=SECTOR3),
  `lapsCompleted`, `lastLapTime`, `bestLapTime`, `bestLapSectorTime{1,2}`.
- Context: `driverName`, `carClass=GT3`, `carNumber=34`, `vehicleName` (WEC #34),
  `position`, `flag`, `pitState`/`pitLapDistance`/`pitting`, `penalties`.
- **`fuelFraction = 0.545`** — live fuel level, feeds the fuel calculator directly.

Lap completion is detectable from `lapsCompleted` incrementing and/or
`lastLapTime` becoming valid; scoring MMF also carries `mLapNumber` /
`mLapStartET` / `mLapDist`.

## Deferred to the follow-up (LMU-capture) plan

1. **Exact channel decode + units + the `RIGHT_IS_POSITIVE` sign convention.**
   Not guessed here on purpose — needs vetted struct offsets tested against a
   recorded lap. `mLocalVel` is vehicle-local (x lateral / z longitudinal);
   confirm steering sign and lat-accel sign together, on a wheel lap (the probe
   session was on a **controller** — fine for presence/sign but not for input
   *magnitude* calibration).
2. **Sampling rate of REST vs shared memory.** REST is UI-rate (unmeasured,
   likely 5–10 Hz); shared memory is engine-rate (~50–90 Hz). Use SMM for
   high-rate input traces, REST for setup/timing/fuel/context. Consider a hybrid
   capture.
3. **Which MMF to standardize on** (classic rF2 SMMP vs native `LMU_Data`) —
   lean rF2 SMMP for documentation/tooling maturity.

## Probe artifacts (throwaway)

- `scratchpad/lmu_smm_probe.py` — the raw MMF-open probe (not kept in repo).
- REST probed with `Invoke-RestMethod` against `localhost:6397`.
