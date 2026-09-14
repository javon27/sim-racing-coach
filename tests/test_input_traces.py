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


def test_sub_scoring_tick_input_survives_at_every_phase():
    """The load-bearing consequence of the rate mismatch: ~10 input samples share
    one lap_distance, so anything the driver does INSIDE one 5 Hz scoring tick (a
    stab, a quick lift) is invisible to any method that keys on the distinct stored
    distances -- it only ever sees one sample per tick.

    De-staircasing gives every 48.6 Hz sample its own distance, but that alone is
    not enough: at 500 bins one bin spans 180-400 ms, i.e. AT OR COARSER than the
    scoring tick, so point-sampling the grid drops the lift whenever it happens to
    land between two grid points. The per-bin min/max envelope is what makes the
    lift survive, so the phase is SWEPT across a whole tick — passing at one lucky
    offset proves nothing.

    Le Mans scale (13.6 km, 3:20) is the hard case: the widest bins.
    """
    base = _staircase_lap(n=9720, length=13600.0)
    stored = np.asarray(base.channels["lap_distance"])
    # Precondition: the fixture really is a staircase (else the test proves nothing).
    assert len(np.unique(stored)) < len(stored) / 5
    tick = np.flatnonzero(np.r_[True, np.diff(stored) != 0])
    span = tick[21] - tick[20]  # samples in one scoring tick (~9-10)
    assert span >= 5

    misses, hidden = [], 0
    for phase in range(span):
        rec = _staircase_lap(n=9720, length=13600.0)
        lo = tick[20] + phase
        rec.channels["throttle"][:] = 1.0
        rec.channels["throttle"][lo:lo + 5] = 0.0  # ~0.1 s lift
        # Phases where the lift lands entirely between two scoring updates are
        # invisible to anything keyed on the distinct stored distances.
        if phase > 0 and lo + 5 <= tick[21]:
            assert rec.channels["throttle"][tick].min() == 1.0
            hidden += 1
        if min(input_traces(rec, bins=500)["throttle"]["lo"]) >= 0.5:
            misses.append(phase)
    assert hidden >= 3, "fixture must exercise phases the scoring rate cannot see"
    assert not misses, f"lift lost at phases {misses} of {span}"


def test_envelope_brackets_the_true_signal():
    rec = _staircase_lap()
    tr = input_traces(rec, bins=200)
    d = np.asarray(tr["dist"])
    # Reference: the true (un-held) distance the fixture was built from.
    n = len(rec.channels["throttle"])
    t = np.arange(n) / 48.6
    true_dist = 5000.0 * t / t[-1]
    expect = np.interp(d, true_dist, rec.channels["throttle"])
    lo = np.asarray(tr["throttle"]["lo"])
    hi = np.asarray(tr["throttle"]["hi"])
    assert np.all(lo <= hi)
    # The band contains the signal, and is tight on a smooth trace.
    assert np.all(lo <= expect + 0.05) and np.all(hi >= expect - 0.05)
    assert np.max(hi - lo) < 0.1
    # De-staircasing recovers the lap length to ~0.2% (the final partial run is
    # extrapolated at the last observed rate, not measured).
    assert tr["length"] == pytest.approx(5000.0, rel=5e-3)


def test_bins_with_no_sample_still_get_a_value():
    # More bins than raw samples: every bin must still carry a finite lo/hi, else
    # the chart draws a hole.
    tr = input_traces(_staircase_lap(n=60), bins=500)
    for k in ("throttle", "brake", "steering"):
        assert len(tr[k]["lo"]) == len(tr[k]["hi"]) == 500
        assert all(np.isfinite(v) for v in tr[k]["lo"] + tr[k]["hi"])


def test_values_are_rounded_and_json_safe():
    tr = input_traces(_staircase_lap(), bins=32)
    assert all(isinstance(v, float) and np.isfinite(v) for v in tr["dist"])
    for key in ("throttle", "brake", "steering"):
        for edge in ("lo", "hi"):
            vals = tr[key][edge]
            assert all(isinstance(v, float) and np.isfinite(v) for v in vals)


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
    assert all(np.isfinite(v) for v in tr["throttle"]["lo"] + tr["throttle"]["hi"])


def test_backward_distance_step_cannot_break_monotonicity():
    """A lap_distance dip (a wrap, a scoring glitch) must not leave the resampled
    axis non-monotonic. Comparing each knot only to its IMMEDIATE predecessor is
    not enough: after a dip, the next knot passes as long as it beats the dipped
    value, so the axis stays out of order — and np.interp then returns garbage on
    an unsorted xp WITHOUT raising."""
    rec = _staircase_lap()
    d = rec.channels["lap_distance"]
    ticks = np.flatnonzero(np.r_[True, np.diff(d) != 0])
    # Dip well below the running maximum, then resume from there. The knot after
    # the dip is > the dip but still < the pre-dip maximum.
    peak = d[ticks[20]]
    d[ticks[20]:ticks[21]] = peak - 900.0
    d[ticks[21]:ticks[22]] = peak - 800.0
    d[ticks[22]:ticks[23]] = peak - 700.0

    tr = input_traces(rec, bins=200)
    assert tr, "a recoverable distance glitch should not blank the trace"
    axis = np.asarray(tr["dist"])
    assert np.all(np.diff(axis) > 0)
    for k in ("throttle", "brake", "steering"):
        assert all(np.isfinite(v) for v in tr[k]["lo"] + tr[k]["hi"])
