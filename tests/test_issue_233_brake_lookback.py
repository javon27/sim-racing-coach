"""#233: the brake-point look-back stops at the previous corner's end. Bahrain Outer
T2 (apex 823, ~85 m after T1's end) read "brake 243m before apex @ 94% peak" -- T1's
braking -- and "brake 150 m earlier than best" off it."""
import numpy as np

from simcoach.analysis import analyze_lap
from simcoach.corners import Corner, CornerMap
from simcoach.format import format_corner_line
from simcoach.model_types import LapRecord

L = 5000.0


def _lap(brake_on):
    dist = np.arange(0.0, L, 5.0)
    brake = np.where(brake_on(dist), 1.0, 0.0)
    return LapRecord("lmu", "T", "C", 90.0, True, 0.0, {
        "lap_distance": dist, "speed": np.where(brake > 0, 80.0, 200.0), "brake": brake,
        "throttle": 1.0 - brake, "steering": np.full(dist.size, 0.4),
    })


# T1 -> T2 laid out like Bahrain Outer T1/T2: T2 starts ~50 m after T1 ends.
T1 = Corner(1, "T1", "right", start_dist=700.0, apex_dist=730.0, end_dist=740.0)
T2 = Corner(2, "T2", "left", start_dist=790.0, apex_dist=820.0, end_dist=860.0)
T3 = Corner(3, "T3", "right", start_dist=2000.0, apex_dist=2050.0, end_dist=2100.0)
CMAP = CornerMap([T1, T2, T3], length=L)


def test_corner_with_no_braking_of_its_own_has_no_brake_point():
    rec = _lap(lambda d: (d >= 580.0) & (d <= 700.0))     # T1's braking only
    t1, t2, _ = analyze_lap(rec, CMAP).corners
    assert t1.brake_point == 580.0                        # T1 keeps its own
    assert t2.brake_point is None                         # not T1's, 240 m back
    line = format_corner_line(t2)
    assert "before apex" not in line and "no heavy braking" in line


def test_no_brake_delta_against_a_reference_that_did_brake_for_it():
    best = _lap(lambda d: ((d >= 580.0) & (d <= 700.0)) | ((d >= 760.0) & (d <= 790.0)))
    cur = _lap(lambda d: (d >= 580.0) & (d <= 700.0))
    t2 = analyze_lap(cur, CMAP, reference=best).corners[1]
    assert t2.brake_point is None and t2.brake_point_delta is None


def test_own_braking_after_the_previous_corner_is_kept():
    rec = _lap(lambda d: ((d >= 580.0) & (d <= 700.0)) | ((d >= 760.0) & (d <= 790.0)))
    t2 = analyze_lap(rec, CMAP).corners[1]
    assert t2.brake_point == 760.0
    assert "brake 60m before apex" in format_corner_line(t2)


def test_run_still_on_at_the_previous_corners_end_is_not_this_corners():
    """One continuous application from T1's approach through to T2: the driver
    started braking for T1. Clipping it at T1's end would state a brake point
    (740 m) the driver never chose -- so it is None, not a clipped point."""
    rec = _lap(lambda d: (d >= 580.0) & (d <= 800.0))
    t1, t2, _ = analyze_lap(rec, CMAP).corners
    assert t1.brake_point == 580.0
    assert t2.brake_point is None


def test_ordinary_heavy_braking_corner_is_unaffected():
    rec = _lap(lambda d: (d >= 1800.0) & (d <= 2030.0))  # 1000+ m after T2's end
    t3 = analyze_lap(rec, CMAP).corners[2]
    assert t3.brake_point == 1800.0
    assert "brake 250m before apex" in format_corner_line(t3)


def test_bound_wraps_across_the_seam():
    """The previous corner ends just before S/F, this one's apex just after it."""
    a = Corner(1, "A", "right", start_dist=4850.0, apex_dist=4900.0, end_dist=4950.0)
    b = Corner(2, "B", "left", start_dist=10.0, apex_dist=40.0, end_dist=100.0)
    mid = Corner(3, "M", "left", start_dist=2000.0, apex_dist=2050.0, end_dist=2100.0)
    cmap = CornerMap([a, b, mid], length=L)
    # Only A's braking (4700-4850): B's look-back crosses S/F into it.
    ca, cb, _ = analyze_lap(_lap(lambda d: (d >= 4700.0) & (d <= 4850.0)), cmap).corners
    assert ca.brake_point == 4700.0
    assert cb.brake_point is None
    # B's own braking straddling S/F, after A's end: kept, unwrapped before the line.
    cb = analyze_lap(_lap(lambda d: (d >= 4970.0) | (d <= 20.0)), cmap).corners[1]
    assert cb.brake_point == 4970.0 - L
    assert cb.apex_dist - cb.brake_point == 70.0
