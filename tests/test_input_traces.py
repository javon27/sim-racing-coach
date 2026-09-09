"""Driver-input traces resampled onto a uniform lap_distance grid (issue #21).

The mismatch this pins: `lap_distance` comes from the SCORING struct at ~5 Hz while
throttle/brake/steering come from the TELEMETRY struct at ~48.6 Hz, so the stored
lap_distance channel is a STAIRCASE (≈10 identical values, then a jump). Sampling
the inputs at those repeated distances collapses ~10 samples onto one x — a
stair-stepped plot that hides the very modulation the graph exists to show.
"""

import numpy as np
import pytest

from simcoach.analysis import input_traces
from simcoach.model_types import LapRecord


def _staircase_lap(n=486, hz=48.6, scoring_hz=5.0, length=5000.0):
    """A synthetic lap shaped like real capture: inputs at `hz`, lap_distance held
    at `scoring_hz` (repeated values), constant speed."""
    t = np.arange(n) / hz
    true_dist = length * t / t[-1]
    step = 1.0 / scoring_hz
    # Held value: the distance as of the most recent scoring tick.
    ticks = np.floor(t / step) * step
    held = np.interp(ticks, t, true_dist)
    throttle = np.clip(np.sin(2 * np.pi * t / t[-1] * 4) * 0.5 + 0.5, 0, 1)
    brake = np.clip(-np.sin(2 * np.pi * t / t[-1] * 4), 0, 1)
    steering = np.sin(2 * np.pi * t / t[-1] * 3) * 0.4
    return LapRecord(
        sim="lmu", track="test_track", car="test_car", lap_time=t[-1], valid=True,
        timestamp=0.0,
        channels={"lap_distance": held, "throttle": throttle, "brake": brake,
                  "steering": steering, "speed": np.full(n, 100.0)},
    )


def test_grid_is_uniform_and_strictly_increasing():
    tr = input_traces(_staircase_lap(), bins=64)
    d = np.asarray(tr["dist"])
    assert len(d) == 64
    assert np.all(np.diff(d) > 0)
    steps = np.diff(d)
    assert np.allclose(steps, steps[0], atol=0.11)  # emitted distances round to 0.1 m


def test_sub_scoring_tick_input_survives():
    """The load-bearing consequence of the rate mismatch: ~10 input samples share
    one lap_distance, so anything the driver does INSIDE one 5 Hz scoring tick (a
    stab, a quick lift) is invisible to any method that keys on the distinct stored
    distances -- it only ever sees one sample per tick. De-staircasing gives every
    48.6 Hz sample its own distance, so the lift survives."""
    rec = _staircase_lap()
    stored = np.asarray(rec.channels["lap_distance"])
    # Precondition: the fixture really is a staircase (else the test proves nothing).
    assert len(np.unique(stored)) < len(stored) / 5
    tick = np.flatnonzero(np.r_[True, np.diff(stored) != 0])

    # A ~0.1 s lift entirely inside one scoring tick, starting after its first sample.
    lo = tick[20] + 2
    rec.channels["throttle"][:] = 1.0
    rec.channels["throttle"][lo:lo + 5] = 0.0
    assert lo + 5 < tick[21], "lift must sit inside a single scoring tick"

    # What a distinct-distance keyed resample can see: one sample per tick.
    per_tick = rec.channels["throttle"][tick]
    assert per_tick.min() == 1.0, "the lift is invisible at the scoring rate"

    tr = input_traces(rec, bins=200)
    assert min(tr["throttle"]) < 0.5


def test_channels_track_the_true_signal():
    rec = _staircase_lap()
    tr = input_traces(rec, bins=200)
    d = np.asarray(tr["dist"])
    # Reference: the true (un-held) distance the fixture was built from.
    n = len(rec.channels["throttle"])
    t = np.arange(n) / 48.6
    true_dist = 5000.0 * t / t[-1]
    expect = np.interp(d, true_dist, rec.channels["throttle"])
    assert np.max(np.abs(np.asarray(tr["throttle"]) - expect)) < 0.05
    # De-staircasing recovers the lap length to ~0.2% (the final partial run is
    # extrapolated at the last observed rate, not measured).
    assert tr["length"] == pytest.approx(5000.0, rel=5e-3)


def test_values_are_rounded_and_json_safe():
    tr = input_traces(_staircase_lap(), bins=32)
    for key in ("dist", "throttle", "brake", "steering"):
        assert all(isinstance(v, float) for v in tr[key])
        assert all(np.isfinite(v) for v in tr[key])


def test_missing_channel_returns_empty():
    rec = _staircase_lap()
    del rec.channels["steering"]
    assert input_traces(rec) == {}


def test_too_short_returns_empty():
    rec = LapRecord(sim="lmu", track="t", car="c", lap_time=1.0, valid=True, timestamp=0.0,
                    channels={"lap_distance": np.array([0.0]), "throttle": np.array([0.0]),
                              "brake": np.array([0.0]), "steering": np.array([0.0])})
    assert input_traces(rec) == {}


def test_non_finite_samples_are_dropped():
    rec = _staircase_lap()
    rec.channels["throttle"][5] = np.nan
    rec.channels["lap_distance"][7] = np.inf
    tr = input_traces(rec, bins=64)
    assert all(np.isfinite(v) for v in tr["throttle"])
