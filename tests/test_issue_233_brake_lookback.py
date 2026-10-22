"""#233: a corner's brake point is never the previous corner's braking. Bahrain Outer
T2 (apex 823, ~85 m after T1's end) read "brake 243m before apex @ 94% peak" -- T1's
braking -- and "brake 150 m earlier than best" off it. The bound is the previous
corner's last apex; a run straddling it is split by where the speed is shed."""
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


def test_run_begun_before_the_previous_apex_is_not_this_corners():
    """One continuous application from T1's approach through to T2: the driver
    started braking for T1 (before its apex). Clipping it at the bound would state a
    brake point the driver never chose -- so it is None, and so is the #169 zone:
    the two must never disagree ("no heavy braking" vs "braking from 80 m")."""
    rec = _lap(lambda d: (d >= 580.0) & (d <= 820.0))    # on through T2's apex
    t1, t2, _ = analyze_lap(rec, CMAP).corners
    assert t1.brake_point == 580.0
    assert t2.brake_point is None
    assert t2.brake_zone_start is None and t2.brake_zone_peak is None


def test_stop_starting_before_turn_in_on_a_tiled_map_is_kept():
    """Imola T17 Rivazza shape: maps tile corners (previous end == this start) and
    the real stop begins ~30 m before turn-in, after the previous apex. An end bound
    threw it away ("no heavy braking" on a 234->89 km/h stop)."""
    p = Corner(1, "P", "left", start_dist=3900.0, apex_dist=3950.0, end_dist=4034.0)
    r = Corner(2, "Rivazza", "left", start_dist=4034.0, apex_dist=4100.0, end_dist=4200.0)
    far = Corner(3, "F", "right", start_dist=1000.0, apex_dist=1050.0, end_dist=1100.0)
    cm = analyze_lap(_lap(lambda d: (d >= 4005.0) & (d <= 4100.0)),
                     CornerMap([p, r, far], length=L)).corners[1]
    assert cm.brake_point == 4005.0
    assert cm.brake_zone_start == cm.brake_point       # zone and point agree
    assert "brake 95m before apex" in format_corner_line(cm)


def test_release_and_reapply_after_the_previous_apex_is_kept():
    """Monza T2 shape: brake for T1, release to 0.09 (not off the brake, not on the
    throttle) after T1's apex, re-apply for T2 -- before T1's mapped END (740). The
    re-application is T2's own; an end bound threw it away."""
    dist = np.arange(0.0, L, 5.0)
    brake = np.select([(dist >= 580.0) & (dist <= 700.0), (dist > 700.0) & (dist < 735.0),
                       (dist >= 735.0) & (dist <= 820.0)], [1.0, 0.09, 0.9], 0.0)
    rec = LapRecord("lmu", "T", "C", 90.0, True, 0.0, {
        "lap_distance": dist, "speed": np.where(brake > 0, 80.0, 200.0), "brake": brake,
        "throttle": np.where(brake > 0, 0.0, 1.0), "steering": np.full(dist.size, 0.4),
    })
    t2 = analyze_lap(rec, CMAP).corners[1]
    assert t2.brake_point == 735.0
    assert t2.brake_zone_start == 735.0                # T1's run (580-700) not claimed


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


def _shaped(brake_pts, speed_pts, throttle_pts=((0.0, 1.0), (L, 1.0))):
    """Channels from (distance, value) breakpoints, linearly interpolated."""
    dist = np.arange(0.0, L, 5.0)
    def f(pts):
        return np.interp(dist, [d for d, _ in pts], [v for _, v in pts])
    return LapRecord("lmu", "T", "C", 90.0, True, 0.0, {
        "lap_distance": dist, "speed": f(speed_pts), "brake": f(brake_pts),
        "throttle": f(throttle_pts), "steering": np.full(dist.size, 0.4),
    })


KINK = Corner(1, "", "right", start_dist=560.0, apex_dist=600.0, end_dist=620.0)
HAIR = Corner(2, "", "right", start_dist=620.0, apex_dist=780.0, end_dist=820.0)
FAR = Corner(3, "", "left", start_dist=2000.0, apex_dist=2050.0, end_dist=2100.0)
KMAP = CornerMap([KINK, HAIR, FAR], length=L)
_OFF_THR = ((0.0, 1.0), (585.0, 1.0), (590.0, 0.0), (780.0, 0.0), (790.0, 1.0), (L, 1.0))


def test_stop_starting_just_before_a_kinks_apex_is_the_hairpins():
    """Mulsanne shape: braking from 590, kink apex 600, hairpin apex 780; ~11 of the
    205 km/h shed is before the kink apex. The run is the hairpin's, from its real
    start -- rejecting it printed "no heavy braking" on a 270->65 km/h stop."""
    rec = _shaped(
        ((0.0, 0.0), (585.0, 0.0), (590.0, 1.0), (780.0, 1.0), (785.0, 0.0), (L, 0.0)),
        ((0.0, 270.0), (590.0, 270.0), (780.0, 65.0), (850.0, 120.0), (L, 270.0)),
        _OFF_THR)
    hair = analyze_lap(rec, KMAP).corners[1]
    assert hair.brake_point == 590.0                    # real start, not clipped at 600
    assert hair.brake_zone_start == 590.0
    assert hair.brake_carried_from is None
    assert "brake 190m before apex" in format_corner_line(hair)


def test_run_shedding_its_speed_before_the_previous_apex_is_carried():
    """The negative: most of the loss happens before the kink's apex, then the car
    holds speed into the next corner on a trailing brake -- that run is the kink's.
    Rendered as carried, never as "no heavy braking"."""
    rec = _shaped(
        ((0.0, 0.0), (495.0, 0.0), (500.0, 1.0), (600.0, 0.3), (780.0, 0.3), (785.0, 0.0),
         (L, 0.0)),
        ((0.0, 250.0), (500.0, 250.0), (600.0, 100.0), (780.0, 95.0), (850.0, 150.0),
         (L, 250.0)),
        ((0.0, 1.0), (495.0, 1.0), (500.0, 0.0), (780.0, 0.0), (790.0, 1.0), (L, 1.0)))
    hair = analyze_lap(rec, KMAP).corners[1]
    assert hair.brake_point is None
    assert hair.brake_zone_start is None and hair.brake_zone_peak is None
    assert hair.brake_carried_from == "T1"
    line = format_corner_line(hair)
    assert "braking carried through from T1" in line
    assert "no heavy braking" not in line and "before apex" not in line


def test_zone_stops_at_a_reacceleration_after_the_previous_corner():
    """A dab on the previous corner's exit (520-530, 150->140), speed back up to 170
    on part throttle (never the 0.8 full-throttle cut), then this corner's own stop
    from 620. The dab is not this corner's braking zone."""
    a = Corner(1, "", "left", start_dist=400.0, apex_dist=450.0, end_dist=500.0)
    b = Corner(2, "", "right", start_dist=560.0, apex_dist=700.0, end_dist=760.0)
    rec = _shaped(
        ((0.0, 0.0), (515.0, 0.0), (520.0, 0.3), (530.0, 0.3), (535.0, 0.0), (615.0, 0.0),
         (620.0, 0.9), (700.0, 0.9), (705.0, 0.0), (L, 0.0)),
        ((0.0, 150.0), (520.0, 150.0), (535.0, 140.0), (615.0, 170.0), (700.0, 80.0),
         (800.0, 150.0), (L, 150.0)),
        ((0.0, 1.0), (450.0, 1.0), (455.0, 0.5), (615.0, 0.5), (620.0, 0.0), (700.0, 0.0),
         (710.0, 1.0), (L, 1.0)))
    cm = analyze_lap(rec, CornerMap([a, b, FAR], length=L)).corners[1]
    assert cm.brake_point == 620.0
    assert cm.brake_zone_start == 620.0                 # not the dab at 520


def test_carried_label_needs_the_run_to_reach_this_corners_look_back():
    """The previous corner trail-brakes through its apex (a carried-style run), but
    the next corner is ~400 m on, beyond its 250 m look-back, with no braking: there
    is nothing carried INTO it, so no "carried through" label."""
    a = Corner(1, "", "left", start_dist=400.0, apex_dist=450.0, end_dist=500.0)
    b = Corner(2, "", "right", start_dist=800.0, apex_dist=850.0, end_dist=900.0)
    rec = _shaped(
        ((0.0, 0.0), (345.0, 0.0), (350.0, 1.0), (450.0, 0.4), (470.0, 0.4), (475.0, 0.0),
         (L, 0.0)),
        ((0.0, 200.0), (350.0, 200.0), (450.0, 90.0), (470.0, 88.0), (600.0, 200.0),
         (L, 200.0)),
        ((0.0, 1.0), (345.0, 1.0), (350.0, 0.0), (475.0, 0.0), (480.0, 1.0), (L, 1.0)))
    cm = analyze_lap(rec, CornerMap([a, b, FAR], length=L)).corners[1]
    assert cm.brake_point is None
    assert cm.brake_carried_from is None
    assert "no heavy braking" in format_corner_line(cm)
