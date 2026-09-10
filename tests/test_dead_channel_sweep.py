"""The sweep's own check: a synthetic lap set where we know which channels are flat.

The sweep in scripts/dead_channel_sweep.py is the #43 guard, and it is only
worth anything if its classification logic cannot silently break. It cannot be
tested against real captures -- those are the driver's and are not in the repo
-- so this builds laps whose answer we decide, and asserts each of the four
buckets the sweep has to keep apart.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import numpy as np

_SWEEP = Path(__file__).resolve().parent.parent / "scripts" / "dead_channel_sweep.py"


def _load_sweep():
    """Import the script by path -- it lives in scripts/, not the package."""
    spec = importlib.util.spec_from_file_location("dead_channel_sweep", _SWEEP)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


sweep_mod = _load_sweep()


def _lap(path: Path, varying: bool, extra: dict[str, np.ndarray] | None = None) -> str:
    """One synthetic lap. speed always varies and flat_everywhere never does, so
    they are the two fixed controls; only flat_sometimes follows `varying`."""
    channels = {
        "speed": np.linspace(40.0, 70.0, 50),
        "flat_everywhere": np.zeros(50),
        "flat_sometimes": np.linspace(0.0, 1.0, 50) if varying else np.zeros(50),
    }
    channels.update(extra or {})
    np.savez_compressed(path, **channels)
    return str(path.with_suffix(".npz"))


def _stats(tmp_path: Path) -> dict[str, sweep_mod.ChannelStat]:
    """Two cars: one lap varying, one lap with several channels pinned flat."""
    laps = [
        ("Car A", _lap(tmp_path / "a", varying=True, extra={"nan_chan": np.full(50, np.nan)})),
        ("Car B", _lap(tmp_path / "b", varying=False, extra={"nan_chan": np.full(50, np.nan)})),
    ]
    stats, read, missing = sweep_mod.sweep(laps)
    assert (read, missing) == (2, 0)
    return stats


def test_flat_in_every_lap_across_two_cars_is_a_finding(tmp_path):
    st = _stats(tmp_path)["flat_everywhere"]
    assert st.flat_everywhere and not st.inconclusive
    assert (st.constant_laps, st.laps) == (2, 2)
    assert st.value_str() == "0"


def test_flat_in_only_some_laps_is_not_a_finding(tmp_path):
    st = _stats(tmp_path)["flat_sometimes"]
    assert st.constant_laps == 1 and st.laps == 2
    assert not st.flat_everywhere


def test_channel_that_varies_everywhere_is_never_flagged(tmp_path):
    st = _stats(tmp_path)["speed"]
    assert st.constant_laps == 0 and not st.flat_everywhere


def test_all_nan_channel_counts_as_dead_not_as_varying(tmp_path):
    st = _stats(tmp_path)["nan_chan"]
    assert st.flat_everywhere and st.all_nan_laps == 2
    assert st.value_str() == "nan"


def test_one_car_only_is_inconclusive_not_a_finding(tmp_path):
    """The cry-wolf guard: a garage setting flat across a single car proves nothing."""
    laps = [
        ("Car A", _lap(tmp_path / "a", varying=True)),
        ("Car A", _lap(tmp_path / "b", varying=True)),
    ]
    st = sweep_mod.sweep(laps)[0]["flat_everywhere"]
    assert st.flat_everywhere, "flat in both laps"
    assert st.inconclusive, "but only one car, so it must not be reported as dead"


def test_different_constant_per_car_is_alive_not_dead(tmp_path):
    """A garage setting nobody moved mid-lap: constant in every lap, two cars, two
    values. Constancy alone would call this DEAD and exit 2 on a live channel --
    and it is the shape every INCONCLUSIVE channel turns into the moment a second
    car is captured, so the false finding would fire exactly when the driver does
    the thing that was meant to resolve the list."""
    laps = []
    for name, car, val in (("a", "Car A", 3.0), ("b", "Car B", 5.0)):
        p = tmp_path / f"{name}.npz"
        np.savez_compressed(p, speed=np.linspace(1.0, 9.0, 50), motor_map=np.full(50, val))
        laps.append((car, str(p)))
    stats, read, missing = sweep_mod.sweep(laps)
    st = stats["motor_map"]

    assert (st.constant_laps, st.laps, len(st.cars)) == (2, 2, 2)
    assert not st.flat_everywhere, "two distinct values is proof the game fills it"
    assert not st.inconclusive
    assert sweep_mod.report(stats, read, missing) == 0


def test_value_str_distinguishes_one_constant_from_several(tmp_path):
    """The only thing that lets a human spot a multi-valued channel in the table."""
    one, several = sweep_mod.ChannelStat(), sweep_mod.ChannelStat()
    one.constant_values.add(3.0)
    several.constant_values.update({3.0, 5.0})

    assert one.value_str() == "3"
    assert several.value_str() != one.value_str()
    assert "2 distinct" in several.value_str() and "3" in several.value_str()


def _one_car_flat(tmp_path) -> tuple[dict, int, int]:
    """Two laps, ONE car, a channel flat in both -- the deferred case."""
    laps = []
    for name in ("a", "b"):
        p = tmp_path / f"{name}.npz"
        np.savez_compressed(p, speed=np.linspace(1.0, 9.0, 50), motor_map_max=np.full(50, 11.0))
        laps.append(("Car A", str(p)))
    return sweep_mod.sweep(laps)


def test_inconclusive_block_is_printed_with_the_deferred_channel(tmp_path, capsys):
    """The bucket carrying the sweep's honest open question must actually print."""
    stats, read, missing = _one_car_flat(tmp_path)
    assert stats["motor_map_max"].inconclusive
    code = sweep_mod.report(stats, read, missing)
    out = capsys.readouterr().out

    assert code == 0, "a deferral is not a finding"
    assert "INCONCLUSIVE" in out
    assert "motor_map_max" in out.split("INCONCLUSIVE", 1)[1], "named in its own block"
    assert "second car" in out, "says what would resolve it"
    assert "NEW FINDING" not in out


def test_table_flags_a_deferred_channel_as_one_car_only(tmp_path, capsys):
    """The table's flag column must not go silent on a deferred channel."""
    stats, read, missing = _one_car_flat(tmp_path)
    sweep_mod.report(stats, read, missing)
    out = capsys.readouterr().out

    row = next(ln for ln in out.splitlines() if ln.startswith("motor_map_max"))
    assert "1 car only" in row, f"flag column blank on a deferred channel: {row!r}"
    speed_row = next(ln for ln in out.splitlines() if ln.startswith("speed"))
    assert "1 car only" not in speed_row, "a varying channel must carry no flag"


def test_missing_telemetry_files_are_skipped_not_fatal(tmp_path):
    laps = [
        ("Car A", _lap(tmp_path / "a", varying=True)),
        ("Car B", str(tmp_path / "gone.npz")),
    ]
    stats, read, missing = sweep_mod.sweep(laps)
    assert (read, missing) == (1, 1)
    assert stats["speed"].laps == 1


def test_report_buckets_findings_expected_and_inconclusive(tmp_path, capsys):
    """End to end: the verdict must name a real finding and excuse a known one."""
    laps = [
        ("Car A", _lap(tmp_path / "a", varying=True, extra={"grip_fl": np.zeros(50)})),
        ("Car B", _lap(tmp_path / "b", varying=True, extra={"grip_fl": np.zeros(50)})),
    ]
    stats, read, missing = sweep_mod.sweep(laps)
    code = sweep_mod.report(stats, read, missing)
    out = capsys.readouterr().out

    assert code == 2, "a new dead channel must be a non-zero exit"
    assert "NEW FINDING" in out
    assert "flat_everywhere" in out
    # grip_fl is flat across both cars too, but it is known-dead, not a new find.
    assert "EXPECTED-CONSTANT" in out
    assert "mGripFract" in out


def test_report_is_clean_when_only_known_dead_channels_are_flat(tmp_path, capsys):
    laps = [
        ("Car A", str(tmp_path / "a.npz")),
        ("Car B", str(tmp_path / "b.npz")),
    ]
    for _, p in laps:
        np.savez_compressed(p, speed=np.linspace(1.0, 9.0, 50), grip_fl=np.zeros(50))
    stats, read, missing = sweep_mod.sweep(laps)
    code = sweep_mod.report(stats, read, missing)
    out = capsys.readouterr().out

    assert code == 0
    assert "NEW FINDING" not in out
    assert "no dead channels beyond the known-dead ones" in out


def test_stored_laps_reads_only_rows_that_have_telemetry(tmp_path):
    """The ~7k result-log backfill rows carry no telemetry and must be filtered out."""
    db = tmp_path / "t.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE laps (id INTEGER PRIMARY KEY, car TEXT, telemetry_path TEXT)")
    con.executemany(
        "INSERT INTO laps (car, telemetry_path) VALUES (?, ?)",
        [("Car A", "a.npz"), ("Car B", None), ("Car C", "c.npz")],
    )
    con.commit()
    con.close()
    assert sweep_mod.stored_laps(db) == [("Car A", "a.npz"), ("Car C", "c.npz")]
