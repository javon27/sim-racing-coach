"""One-shot rig diagnostic: print RAW shared-memory values beside what we report.

Run this at the rig instead of guessing from a captured .npz. It answers six
open questions in a single lap, all of which are currently unresolvable offline
because the conflict is between LMU's header and our own code:

  #43  mGripFract  -- is 1.0 full grip or full slip? Our "min grip" line may be
                     INVERTED. Fails silently: both readings look plausible.
  #46  mWear       -- the header says fraction WORN, our code says remaining.
                     Also fails silently.
  #44  mBrakeTemp  -- Kelvin or Celsius? mTemperature is documented Kelvin and
                     mBrakeTemp is not documented at all.
  #47  mVirtualEnergy / mSoC -- 0-1 fraction, or already a percentage? The
                     energy report multiplies by 100.
  #38  mElapsedTime -- genuinely per-frame, or does it step at the 5 Hz scoring
                     rate like mLapDist does? Per-frame unblocks delta-t.
  #33  the 16 aid/adjust indices -- do they carry your LIVE on-track value, or
                     only the garage value? If live, the coach can stop hedging.

Usage, from the repo root with LMU running and you in the car:

    .venv/Scripts/python.exe scripts/rig_probe.py

It prints a live table, then a verdict block on exit (Ctrl+C). Nothing is
written anywhere and no game state is touched -- this only reads.

WHAT TO DO WITH IT
  1. Sit in the garage. Start the probe. Check the GARAGE block matches your
     setup screen (ABS/TC/engine map).
  2. Drive out. Do one hot lap with at least one hard braking zone and one
     corner where you slide a little.
  3. While on track, change ABS one step with your wheel button. Watch whether
     the abs row moves. That is #33.
  4. Come back to the garage and stop the probe with Ctrl+C.
  5. Paste the verdict block back to Claude.
"""

from __future__ import annotations

import math
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from simcoach.lmu import smm
from simcoach.lmu.rf2structs import rF2Scoring, rF2Telemetry

WHEELS = ("FL", "FR", "RL", "RR")
POLL_HZ = 20.0


def _player(tele):
    """The player's vehicle telemetry, or None if the MMF has no vehicles yet."""
    return tele.mVehicles[0] if tele.mNumVehicles else None


class Track:
    """Min/max/first/last for one scalar, so a single lap settles direction."""

    def __init__(self) -> None:
        self.lo = float("inf")
        self.hi = float("-inf")
        self.first: float | None = None
        self.last = float("nan")
        self.n = 0

    def add(self, v: float) -> None:
        if math.isnan(v):
            return
        if self.first is None:
            self.first = v
        self.last = v
        self.lo = min(self.lo, v)
        self.hi = max(self.hi, v)
        self.n += 1

    def __str__(self) -> str:
        if not self.n:
            return "no samples"
        return f"first={self.first:<10.4f} last={self.last:<10.4f} min={self.lo:<10.4f} max={self.hi:.4f}"


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    limit = None
    if argv and argv[0] == "--seconds" and len(argv) > 1:
        limit = float(argv[1])           # so you never have to find Ctrl+C mid-stint
    seen: dict[str, Track] = {}
    et_deltas: list[float] = []
    dist_deltas: list[float] = []
    prev_et = prev_dist = None
    aid_changes: dict[str, set[int]] = {}
    garage: dict[str, str] = {}
    scoring_errors: Counter[str] = Counter()   # surfaced in the verdict, not swallowed
    frames = 0
    t0 = time.monotonic()

    def tr(key: str) -> Track:
        return seen.setdefault(key, Track())

    print(__doc__.split("WHAT TO DO")[0])
    print(f"polling {POLL_HZ:.0f} Hz -- Ctrl+C to stop and print the verdict\n")

    try:
        while limit is None or time.monotonic() - t0 < limit:
            time.sleep(1.0 / POLL_HZ)
            try:
                tele = smm.read_consistent(smm.TELEMETRY_CANDIDATES[0], rF2Telemetry)
            except OSError as exc:                        # MMF not up yet
                print(f"\rwaiting for LMU shared memory ({exc})   ", end="")
                continue
            v = _player(tele)
            if v is None:
                print("\rLMU is up but reports no vehicles -- are you in the car?   ", end="")
                continue
            frames += 1

            # --- #38: is mElapsedTime per-frame, or 5 Hz like lap distance? ---
            # mLapDist lives on the SCORING struct, not telemetry -- which is the
            # whole point of #38: scoring ticks ~5 Hz, telemetry ~48.6 Hz, so the
            # control has to come from the other MMF.
            sv = None
            try:
                scoring = smm.read_consistent(smm.SCORING_CANDIDATES[0], rF2Scoring)
                sv = smm._find_player(scoring)
            except (OSError, ValueError) as exc:
                scoring_errors[type(exc).__name__] += 1
            if prev_et is not None and v.mElapsedTime != prev_et:
                et_deltas.append(v.mElapsedTime - prev_et)
            if sv is not None:
                if prev_dist is not None and sv.mLapDist != prev_dist:
                    dist_deltas.append(sv.mLapDist - prev_dist)
                prev_dist = sv.mLapDist
            prev_et = v.mElapsedTime
            lap_dist = sv.mLapDist if sv is not None else float("nan")

            # --- #43 / #44 / #46: per-wheel, raw ---
            for i, w in enumerate(WHEELS):
                wh = v.mWheel[i]
                tr(f"grip_{w}").add(wh.mGripFract)
                tr(f"wear_{w}").add(wh.mWear)
                tr(f"braketemp_{w}").add(wh.mBrakeTemp)
                tr(f"tiretemp_{w}_center").add(wh.mTemperature[1])

            # --- #47: energy scale ---
            tr("mVirtualEnergy").add(v.mVirtualEnergy)
            tr("mSoC").add(v.mSoC)
            tr("mBatteryChargeFraction").add(v.mBatteryChargeFraction)

            # --- #33: do the aid indices move while on track? ---
            for name in ("mABS", "mTC", "mTCSlip", "mTCCut", "mMotorMap",
                         "mMigration", "mFrontAntiSway", "mRearAntiSway"):
                val = int(getattr(v, name))
                aid_changes.setdefault(name, set()).add(val)
                tr(name).add(val)
                tr(name + "Max").add(int(getattr(v, name + "Max")))
            tr("mRearBrakeBias").add(v.mRearBrakeBias)

            if frames == 1:
                for name in ("mABS", "mTC", "mMotorMap"):
                    garage[name] = f"{int(getattr(v, name))} of 0-{int(getattr(v, name + 'Max'))}"

            if frames % 10 == 0:
                print(
                    f"\rlap {v.mLapNumber}  dist {lap_dist:7.1f}m  "
                    f"grip {min(v.mWheel[i].mGripFract for i in range(4)):.3f}  "
                    f"wear FL {v.mWheel[0].mWear:.5f}  "
                    f"bt FL {v.mWheel[0].mBrakeTemp:7.1f}  "
                    f"energy {v.mVirtualEnergy:.4f}  "
                    f"ABS {int(v.mABS)}/{int(v.mABSMax)}   ",
                    end="", flush=True)
    except KeyboardInterrupt:
        pass

    dur = time.monotonic() - t0
    print("\n\n" + "=" * 78)
    print(f"VERDICT BLOCK  --  {frames} frames over {dur:.1f}s "
          f"({frames / dur if dur else 0:.1f} Hz effective)")
    print("=" * 78)
    if not frames:
        print("No frames read. LMU was not running, or you were not in a car.")
        return 1

    print("\nGARAGE AT START (compare against your setup screen):")
    for k, val in garage.items():
        print(f"  {k:<14} {val}")

    print("\n--- #43  mGripFract: which end is grip? ---")
    for w in WHEELS:
        print(f"  grip_{w:<3} {seen[f'grip_{w}']}")
    g = [seen[f"grip_{w}"] for w in WHEELS if seen[f"grip_{w}"].n]
    if g:
        lo, hi = min(x.lo for x in g), max(x.hi for x in g)
        print(f"  range across all wheels: {lo:.4f} .. {hi:.4f}")
        print("  READ: if this sits near 1.0 and DIPS when you slid, 1.0 = full grip")
        print("        and our 'min grip' line is correct. If it sits near 0.0 and")
        print("        RISES when you slid, it is a slip fraction and we are INVERTED.")

    print("\n--- #46  mWear: worn, or remaining? ---")
    for w in WHEELS:
        print(f"  wear_{w:<3} {seen[f'wear_{w}']}")
    wf = seen["wear_FL"]
    if wf.n and wf.first is not None:
        direction = "ROSE" if wf.last > wf.first else "FELL" if wf.last < wf.first else "did not move"
        print(f"  FL {direction} over the run ({wf.first:.6f} -> {wf.last:.6f})")
        print("  READ: started ~0.0 and ROSE  => fraction WORN; our 'remaining' is WRONG.")
        print("        started ~1.0 and FELL   => fraction REMAINING; our label is right.")

    print("\n--- #44  mBrakeTemp: Kelvin or Celsius? ---")
    for w in WHEELS:
        print(f"  braketemp_{w:<3} {seen[f'braketemp_{w}']}")
    print(f"  tire center FL (documented KELVIN, for scale): {seen['tiretemp_FL_center']}")
    bt = seen["braketemp_FL"]
    if bt.n:
        print(f"  READ: peak {bt.hi:.1f}. A hot brake is ~400-700 C = ~670-970 K.")
        print("        If that looks ~273 too HIGH it is Kelvin and we must subtract;")
        print(f"        as Celsius we would be reporting {bt.hi - 273.15:.1f}.")

    print("\n--- #47  energy scale ---")
    for k in ("mVirtualEnergy", "mSoC", "mBatteryChargeFraction"):
        print(f"  {k:<24} {seen[k]}")
    print("  READ: max <= 1.0 => 0-1 fraction, the x100 in the energy report is right.")
    print("        max up near 100 => already a percentage and we multiply twice.")

    print("\n--- #38  is mElapsedTime per-frame? ---")
    if et_deltas:
        uniq = sorted({round(d, 4) for d in et_deltas})[:6]
        print(f"  {len(et_deltas)} changes in {frames} frames "
              f"({100 * len(et_deltas) / frames:.1f}% of frames)")
        print(f"  smallest step {min(et_deltas):.5f}s  median-ish steps {uniq}")
    else:
        print("  mElapsedTime NEVER changed -- that would be a blocker for #38.")
    if dist_deltas:
        print(f"  mLapDist changed on {100 * len(dist_deltas) / frames:.1f}% of frames "
              f"(the known-5Hz control)")
    print("  READ: if mElapsedTime changes on a far higher % of frames than mLapDist,")
    print("        it is per-frame and #38's delta-t can be integrated on it.")

    print("\n--- #33  did the aid indices move while you were on track? ---")
    for name, vals in sorted(aid_changes.items()):
        s = sorted(vals)
        moved = "MOVED" if len(s) > 1 else "static"
        print(f"  {name:<16} {moved:<7} values seen: {s[:8]}")
    print(f"  mRearBrakeBias   {seen['mRearBrakeBias']}")
    print("  READ: if you changed ABS on track and its row says MOVED, these carry the")
    print("        LIVE value and the coach can report it instead of hedging about the")
    print("        garage setup. If static, the garage caveat has to stay.")
    print("  NOTE: mRearBrakeBias scale is also unverified -- compare its value above")
    print("        against the brake-bias number on your dash.")
    print("\n" + "=" * 78)
    print("Paste everything from VERDICT BLOCK down back to Claude.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
