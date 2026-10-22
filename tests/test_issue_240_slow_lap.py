"""#240: a clean race lap far off the stint's pace gets a measured `slow lap` event."""
import dataclasses

import numpy as np

from simcoach.coach_tools import CoachContext
from simcoach.corners import CornerMap
from simcoach.store import LapStore


def _slowed(base):
    """`base` driven slowly through T1: half speed, 20% throttle, 40 m with no pedal."""
    ch = {k: np.array(v, float) for k, v in base.channels.items()}
    d = ch["lap_distance"]
    seg = (d >= 790) & (d <= 1210)
    ch["speed"][seg] *= 0.5
    ch["brake"][seg] = 0.0
    ch["throttle"][seg] = 0.2
    ch["throttle"][(d >= 1000) & (d <= 1040)] = 0.0
    return dataclasses.replace(base, channels=ch)


def _off(base):
    ch = {k: np.array(v, float) for k, v in base.channels.items()}
    d = ch["lap_distance"]
    surf = {w: np.zeros(d.size) for w in ("fl", "fr", "rl", "rr")}
    i = int(np.searchsorted(d, 990.0))
    surf["fr"][:5] = 5
    surf["fl"][i:i + 10] = surf["rl"][i:i + 10] = 2
    ch.update({f"surface_{w}": v for w, v in surf.items()})
    return dataclasses.replace(base, channels=ch)


def _stint(tmp_path, base, special, times=None):
    """Race laps 1..8; lap 5 is `special` (a record), the rest are `base`."""
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    for no in range(1, 9):
        lap = special if no == 5 else base
        t = (times or {}).get(no, 90.0 + 0.1 * no)
        store.save(dataclasses.replace(lap, session_id="s1--online", phase="race",
                                       lap_no=no, lap_time=t, timestamp=100.0 + no))
    return CoachContext(store, cmap=CornerMap.load("simcoach/tracks/test_track.json"),
                        track=base.track, car=base.car, session_id="s1--online",
                        on_track=True)


def _lap_block(out, n):
    lines = out.splitlines()
    i = next(i for i, ln in enumerate(lines) if ln.startswith(f"Lap: race lap {n} "))
    j = next((k for k in range(i + 1, len(lines)) if lines[k].startswith("Lap:")), len(lines))
    return "\n".join(lines[i:j])


def test_slow_clean_lap_fires_with_buckets_and_inputs(tmp_path, synthetic_lap):
    ctx = _stint(tmp_path, synthetic_lap, _slowed(synthetic_lap), {5: 96.0})
    block = _lap_block(ctx.dispatch("get_stint_events", {}), 5)
    assert "slow lap: +5.4 s vs the stint median" in block
    assert "T1 Left Hairpin (" in block and "max throttle 20%" in block and "36 m with neither pedal" in block
    assert "traffic" not in block and "mistake" not in block
    assert block.count("slow lap") == 1
    others = "\n".join(_lap_block(ctx.dispatch("get_stint_events", {}), n) for n in (2, 3, 4))
    assert "slow lap" not in others


def test_lap_with_an_event_does_not_fire(tmp_path, synthetic_lap):
    ctx = _stint(tmp_path, synthetic_lap, _off(_slowed(synthetic_lap)), {5: 96.0})
    block = _lap_block(ctx.dispatch("get_stint_events", {}), 5)
    assert "off track" in block and "slow lap" not in block


def test_first_race_lap_does_not_fire(tmp_path, synthetic_lap):
    ctx = _stint(tmp_path, synthetic_lap, synthetic_lap, {1: 99.0})
    assert "slow lap" not in ctx.dispatch("get_stint_events", {})


def test_pit_lap_does_not_fire(tmp_path, synthetic_lap):
    ctx = _stint(tmp_path, synthetic_lap,
                 dataclasses.replace(_slowed(synthetic_lap), pit=True), {5: 96.0})
    assert "slow lap" not in ctx.dispatch("get_stint_events", {})


def test_lap_just_under_the_threshold_does_not_fire(tmp_path, synthetic_lap):
    # median 90.5; the bar is 2.5% = 2.26 s -> 92.6 is under, 92.9 over
    under = _stint(tmp_path, synthetic_lap, _slowed(synthetic_lap), {5: 92.6})
    assert "slow lap" not in under.dispatch("get_stint_events", {})


def test_lap_just_over_the_threshold_fires(tmp_path, synthetic_lap):
    over = _stint(tmp_path, synthetic_lap, _slowed(synthetic_lap), {5: 92.9})
    assert "slow lap: +2.3 s" in over.dispatch("get_stint_events", {})
