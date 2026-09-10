"""Offline sweep for DEAD capture channels: ones the game never actually fills.

This is the follow-up artifact for #43, where we shipped a fabricated
"min grip 0.00" on every corner of every lap because LMU never populates
mGripFract -- it is a flat 0.0 forever. Nothing in the unit suite could fail on
that, and the reflection test #43 added cannot either: it asserts a derivation
varies with a SYNTHETIC input, so it catches "the feature ignores its input",
not "the source channel is dead". The next person adding a field will feed a
varying synthetic channel for their own feature, and a dead real source passes
clean again.

The only evidence that separates "dead source" from "not exercised by this
fixture" is what the game actually wrote. So this reads the driver's own stored
.npz laps and flags channels that are entirely constant in EVERY lap they
appear in. Those are the ones reaching analysis and the coach as measurements.

Usage, from the repo root -- no rig, no sim, nothing written:

    .venv/Scripts/python.exe scripts/dead_channel_sweep.py

THREE WAYS TO BE WRONG, AND WHAT GUARDS EACH
  Constant in only SOME laps is normal, so it is never a finding: a non-hybrid
  car leaves soc at 0.0, and abs_active is flat on a lap where ABS never fired.

  Constant within every lap but holding a DIFFERENT value per lap or per car is
  alive -- a garage setting nobody moved mid-lap looks exactly like that. So a
  finding needs one single value everywhere, not just per-lap constancy.

  One value in EVERY lap but across only ONE car is still not evidence. The aid
  and adjustment indices (motor_map, front_antisway, every *_max ceiling) are
  car properties or garage settings -- flat across one car's laps is exactly
  what a working channel looks like. Those are reported INCONCLUSIVE, not dead,
  until a lap in a second car can disagree.

Paste the VERDICT BLOCK back to Claude.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from simcoach.paths import db_path, telemetry_dir

# Channels we have a concrete reason to expect flat across the whole store, with
# the condition that would retire the entry. Anything flat everywhere across
# more than one car and NOT listed here is a new finding.
_GRIP = "#43: LMU never populates mWheel.mGripFract. Captured raw, reported nowhere."
_LOAD = "#43: LMU never populates mWheel.mTireLoad. Same dead-source shape as grip."
_HYBRID = (
    "No hybrid system on any car captured so far (all LMGT3/LMP entries), so 0.0 is "
    "the true value, not a dead channel. virtual_energy varies in these same laps, "
    "which is what proves the energy capture itself works. Retire this entry the "
    "first time a Hypercar lap lands and these stay flat."
)
EXPECTED_CONSTANT = {
    "grip_fl": _GRIP,
    "grip_fr": _GRIP,
    "grip_rl": _GRIP,
    "grip_rr": _GRIP,
    "tire_load_fl": _LOAD,
    "tire_load_fr": _LOAD,
    "tire_load_rl": _LOAD,
    "tire_load_rr": _LOAD,
    "soc": _HYBRID,
    "battery_charge": _HYBRID,
}


class ChannelStat:
    """How one channel behaved across every lap it appears in."""

    def __init__(self) -> None:
        self.laps = 0
        self.constant_laps = 0
        self.constant_values: set[float] = set()
        self.all_nan_laps = 0
        self.cars: set[str] = set()

    @property
    def flat_everywhere(self) -> bool:
        """Pinned to ONE value in every lap it appears in -- the suspicious shape.

        Constant within each lap is not enough. A channel holding 3.0 on one car
        and 5.0 on another is constant in both laps across two cars, yet it is
        plainly alive -- that is a garage setting nobody touched mid-lap. Taking
        a second value anywhere is proof the game fills it, so the distinct-value
        count is what decides, not the per-lap constancy alone.
        """
        return (
            self.laps > 0
            and self.constant_laps == self.laps
            and len(self.constant_values) <= 1
        )

    @property
    def inconclusive(self) -> bool:
        """Flat, but only one car sampled, so nothing could have disagreed yet."""
        return self.flat_everywhere and len(self.cars) < 2

    def value_str(self) -> str:
        if not self.constant_values:
            return "" if not self.all_nan_laps else "nan"
        vals = sorted(self.constant_values)
        if len(vals) == 1:
            return f"{vals[0]:.6g}"
        return f"{vals[0]:.6g}..{vals[-1]:.6g} ({len(vals)} distinct)"


def constant_value(a: np.ndarray) -> float | None:
    """The single value a channel holds for a whole lap, or None if it varies.

    NaN-aware: a channel the game left entirely as NaN is just as dead as one
    pinned at 0.0, so it reports as constant with a NaN value.
    """
    finite = a[np.isfinite(a)]
    if finite.size == 0:
        return float("nan")
    lo = float(finite.min())
    return lo if lo == float(finite.max()) else None


def stored_laps(db: Path) -> list[tuple[str, str]]:
    """(car, telemetry_path) for stored laps, oldest first. Strictly read-only.

    Most rows in `laps` are result-log backfill with no telemetry at all, so
    filter on telemetry_path rather than walking the whole table.
    """
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT car, telemetry_path FROM laps WHERE telemetry_path IS NOT NULL "
            "ORDER BY id"
        ).fetchall()
    finally:
        con.close()
    return [(car or "?", path) for car, path in rows]


def sweep(laps: list[tuple[str, str]]) -> tuple[dict[str, ChannelStat], int, int]:
    """Fold every lap file into per-channel stats. Returns (stats, read, missing)."""
    stats: dict[str, ChannelStat] = {}
    read = missing = 0
    for car, path in laps:
        if not os.path.exists(path):
            missing += 1
            continue
        with np.load(path) as npz:
            for name in npz.files:
                arr = np.asarray(npz[name], dtype=float).ravel()
                st = stats.setdefault(name, ChannelStat())
                st.laps += 1
                st.cars.add(car)
                val = constant_value(arr)
                if val is not None:
                    st.constant_laps += 1
                    if np.isnan(val):
                        st.all_nan_laps += 1
                    else:
                        st.constant_values.add(val)
        read += 1
    return stats, read, missing


def report(stats: dict[str, ChannelStat], read: int, missing: int) -> int:
    """Print the table and the verdict block. Returns a process exit code."""
    print("=" * 78)
    print(f"DEAD CHANNEL SWEEP  --  {read} laps with telemetry read"
          f"{f', {missing} files missing' if missing else ''}")
    print(f"store: {db_path()}")
    print(f"       {telemetry_dir()}")
    print(f"sweep: {Path(__file__).resolve()}")
    print("=" * 78)

    if not read:
        print("\nNo stored laps with telemetry. Nothing to sweep.")
        return 1

    expected, findings, unclear = [], [], []
    for name in sorted(stats):
        if not stats[name].flat_everywhere:
            continue
        if name in EXPECTED_CONSTANT:
            expected.append(name)
        elif stats[name].inconclusive:
            unclear.append(name)
        else:
            findings.append(name)

    print(f"\n{'channel':<20} {'laps':>5} {'const':>6} {'cars':>5}  "
          f"{'value':<16} flat?")
    print("-" * 78)
    for name in sorted(stats):
        st = stats[name]
        if name in findings:
            flag = "DEAD"
        elif name in expected:
            flag = "flat (expected)"
        elif name in unclear:
            flag = "flat (1 car only)"
        else:
            flag = ""
        print(f"{name:<20} {st.laps:>5} {st.constant_laps:>6} {len(st.cars):>5}  "
              f"{st.value_str():<16} {flag}")

    print("\n" + "=" * 78)
    print("VERDICT BLOCK")
    print("=" * 78)
    print("\nA finding needs ONE value in EVERY lap across two or more cars. Constant")
    print("in only some laps is normal. Holding a different value per car is alive --")
    print("that is a garage setting nobody moved mid-lap. And flat across a single car")
    print("is not evidence either, because nothing could have disagreed yet.")

    if expected:
        print("\nEXPECTED-CONSTANT -- flat, and we know why. Not dead by surprise:")
        for n in expected:
            st = stats[n]
            print(f"  {n:<16} {st.constant_laps}/{st.laps} laps, "
                  f"{len(st.cars)} car(s), at {st.value_str()}")
        for reason in dict.fromkeys(EXPECTED_CONSTANT[n] for n in expected):
            who = ", ".join(n for n in expected if EXPECTED_CONSTANT[n] == reason)
            print(f"\n  {who}:\n    {reason}")

    if unclear:
        print("\nINCONCLUSIVE -- flat in every lap, but only one car has been")
        print("captured with them. Car properties and garage settings look exactly")
        print("like this when working. Re-run after a lap in a second car:")
        for n in unclear:
            st = stats[n]
            print(f"  {n:<20} {st.constant_laps}/{st.laps} laps, 1 car, "
                  f"at {st.value_str()}")

    if findings:
        print("\n*** NEW FINDING -- flat in every lap across MULTIPLE cars, and not ***")
        print("*** a known-dead channel. These reach analysis and the coach as     ***")
        print("*** measurements today. Check each against the LMU struct.          ***")
        for n in findings:
            st = stats[n]
            nan_note = f" ({st.all_nan_laps} laps all-NaN)" if st.all_nan_laps else ""
            print(f"  {n:<20} {st.constant_laps}/{st.laps} laps, "
                  f"{len(st.cars)} cars, at {st.value_str()}{nan_note}")
        print("\nVERDICT: NEW DEAD CHANNELS FOUND -- see above.")
    else:
        print("\nVERDICT: no dead channels beyond the known-dead ones. Every other")
        print("         channel varies in at least one real lap.")

    print("\n" + "=" * 78)
    print("Paste everything from VERDICT BLOCK down back to Claude.")
    return 2 if findings else 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv:
        print(__doc__)
        return 1
    stats, read, missing = sweep(stored_laps(db_path()))
    return report(stats, read, missing)


if __name__ == "__main__":
    raise SystemExit(main())
