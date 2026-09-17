import dataclasses

import numpy as np
import pytest

from simcoach.analysis import analyze_lap, lap_events, stint_wear_baseline
from simcoach.corners import CornerMap
from simcoach.model_types import LapRecord


def test_analyze_lap_metrics(synthetic_lap):
    from simcoach.analysis import analyze_lap
    from simcoach.corners import CornerMap
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    a = analyze_lap(synthetic_lap, cmap)
    assert a.has_reference is False
    c1, c2 = a.corners
    assert c1.corner_no == 1 and c1.direction == "left"
    assert c1.apex_dist == 1000
    # The HAIRPIN's minimum (50 km/h), not the sweeper's (94). Both bounds matter:
    # the fixture's two corners used to share one speed dip, so "min_spd == 30" was
    # true of whichever corner the code happened to read (#70).
    assert abs(c1.min_spd - 50.42) < 0.01
    assert abs(c2.min_spd - 93.82) < 0.01
    assert 800 <= c1.brake_point <= 1200
    assert c1.brake_point < c1.apex_dist          # braking precedes the apex
    assert c1.trailbrake_overlap > 0.0
    # Trail braking is a per-corner reading, not a constant: the hairpin is braked
    # to its minimum, the sweeper released before turn-in.
    assert c1.trailbrake_overlap > c2.trailbrake_overlap
    # Magnitudes of SIGNED channels, one corner of each hand (#70 red-team). lat_g is
    # + in the left hairpin and - in the right sweeper, so a signed max(lat_g) reads
    # 1.25 / 0.00 and max(-lat_g) 0.00 / 1.53 -- zeroing every corner of one hand.
    assert abs(c1.peak_lat_g - 1.25) < 0.01 and abs(c2.peak_lat_g - 1.53) < 0.01
    # track_edge and path_lateral carry the car's side, so the signed differences
    # (edge - path_lat: -4.96 / -6.48; edge - |path_lat|: -9.20 / -11.44) would put
    # every corner "past the edge". Only |edge| - |path_lat| gives the real gap.
    assert abs(c1.track_edge_min - 0.31) < 0.01 and abs(c2.track_edge_min - 1.56) < 0.01


def test_brake_point_found_when_braking_is_in_the_approach(synthetic_lap):
    # Braking that happens BEFORE the corner window (the approach to a fast corner
    # like Parabolica) must still be detected, not fall back to the "no brake"
    # sentinel — a window-only search regressed here.
    import numpy as np

    from simcoach.analysis import analyze_lap
    from simcoach.corners import CornerMap
    from simcoach.model_types import LapRecord
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    c = cmap.corners[0]
    ch = dict(synthetic_lap.channels)
    dist = ch["lap_distance"]
    brake = np.zeros_like(dist)
    brake[(dist >= c.start_dist - 120) & (dist < c.start_dist)] = 0.8  # approach only
    lap = LapRecord(**{**synthetic_lap.__dict__, "channels": {**ch, "brake": brake}})
    c1 = analyze_lap(lap, cmap).corners[0]
    assert c1.brake_point is not None              # a point was actually found
    assert c1.brake_point < c.start_dist           # captured in the approach
    assert c1.brake_point < c1.apex_dist           # sane "before apex" readout


def test_no_brake_clause_when_the_driver_never_brakes(synthetic_lap):
    """A corner taken flat has no brake point. The old sentinel was a distance at/after
    the apex, which only stayed silent while it landed on that side -- the same shape
    that made throttle_point render track geometry as a driver input. None cannot."""
    import dataclasses

    import numpy as np

    from simcoach.analysis import analyze_lap
    from simcoach.corners import CornerMap
    from simcoach.format import format_corner_line

    ch = dict(synthetic_lap.channels)
    ch["brake"] = np.zeros_like(ch["lap_distance"])   # flat all lap
    lap = dataclasses.replace(synthetic_lap, channels=ch)
    c1 = analyze_lap(lap, CornerMap.load("simcoach/tracks/test_track.json")).corners[0]

    assert c1.brake_point is None
    line = format_corner_line(c1, "metric")
    assert "before apex" not in line and "no heavy braking" in line


def test_tire_summary_and_prompt(synthetic_lap):
    from simcoach.analysis import analyze_lap
    from simcoach.corners import CornerMap
    from simcoach.format import format_lap_facts
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    a = analyze_lap(synthetic_lap, cmap)
    # synthetic tires: 80°C base, +5 under braking (in corners) -> mean between 80 and 85
    assert set(a.tire_means) == {"fl", "fr", "rl", "rr"}
    assert all(80.0 <= v <= 85.0 for v in a.tire_means.values())
    assert a.tire_warmup is not None

    user = format_lap_facts(a, units="metric")
    assert "Tire temps" in user and "FL" in user and "warm-up" in user.lower()


def test_wheel_report(synthetic_lap):
    from simcoach.analysis import analyze_lap
    from simcoach.corners import CornerMap
    from simcoach.format import format_wheel_report
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    a = analyze_lap(synthetic_lap, cmap)
    # lap-level wheel report: brake temps, pressures, wear
    rep = format_wheel_report(a, "metric")
    assert "Brake temps" in rep and "wear" in rep.lower()


def test_min_grip_is_not_a_field_or_output(synthetic_lap):
    # Issue #43: mGripFract is never populated by LMU (rig-confirmed flat 0.0
    # always, 157/157 stored laps), so "min grip" was a fabricated reading on
    # every corner ever analysed. There is no grip signal to recover -- the
    # field and its output line must both be gone, not re-derived.
    from simcoach.analysis import CornerMetrics, analyze_lap
    from simcoach.corners import CornerMap
    from simcoach.format import format_lap_facts
    assert not hasattr(CornerMetrics, "min_grip")
    assert "min_grip" not in CornerMetrics.__dataclass_fields__
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    a = analyze_lap(synthetic_lap, cmap)
    # #38 P1.4 added a legitimate "grip envelope" reading from lat_g/long_g (a
    # real signal, unlike dead mGripFract) -- the word "grip" now appears
    # honestly. What must still never appear is the old fabricated wording.
    assert "min grip" not in format_lap_facts(a).lower()


def test_corner_metrics_fields_vary_or_are_allowlisted(synthetic_lap):
    # #43's min_grip reached coach output as a real per-corner measurement
    # because the only guard was isnan, and LMU's channel (always flat 0.0)
    # is non-nan. This test catches the general SHAPE of that: a per-corner
    # derivation whose result doesn't actually depend on its per-corner input.
    # It does NOT prove a source channel is live or dead at the game level --
    # that needs real capture (scripts/dead_channel_sweep.py reads what LMU
    # actually wrote). This test only walks every numeric CornerMetrics field
    # by reflection and demands two corners driven with distinctly different
    # inputs produce distinctly different values. A field that comes back
    # identical either ignores its input (the bug) or this fixture doesn't
    # exercise it differently (yet) -- fix the fixture, or allowlist it with
    # a reason. It can't pass silently either way.
    # This test used to build its own lap, because the shared synthetic_lap's two
    # corners were mirror images and tied on 13 of 17 fields (#70). The shared fixture
    # is now built to differ in every driver input, so this runs on the lap every
    # other test uses -- which also means a fixture edit that re-introduces a tie is
    # caught HERE, rather than silently re-vacating the tests that lean on it.
    import dataclasses
    import math

    import numpy as np

    from simcoach.analysis import CornerMetrics, analyze_lap
    from simcoach.corners import CornerMap

    cmap = CornerMap.load("simcoach/tracks/test_track.json")  # corner1 800-1200, corner2 2000-2400
    # #43 specifically: the fixture carries mGripFract the way LMU writes it -- present,
    # flat 0.0 -- and no CornerMetrics field may turn that into a per-corner reading.
    assert all(np.all(synthetic_lap.channels[f"grip_{w}"] == 0.0) for w in ("fl", "fr", "rl", "rr"))

    corners = analyze_lap(synthetic_lap, cmap).corners
    assert len(corners) == 2

    # Fields that are legitimately the same regardless of driving belong here,
    # WITH A REASON, instead of being silently skipped. min_spd_delta is None
    # unless a reference lap is supplied (not exercised here) and is already
    # filtered out below as non-numeric, so nothing needs listing today --
    # this set exists for the next field that's a genuine tie, not a bug.
    # #38 P1.8: brake_reversals and steer_reversals are 1 for BOTH corners
    # here, and that is structural, not a tie the fixture failed to exercise:
    # a single clean, non-pumped brake application (rise to peak, then
    # release) or a single clean lock-and-unwind of the wheel always has
    # exactly one direction change, regardless of the corner's speed,
    # pressure, lock angle or distance. This synthetic fixture deliberately
    # never pumps the brake or saws the wheel (see conftest's physics-based
    # profile), so both corners read the same baseline "1" -- which is also
    # why format.py's report floors are 2 (brake) and 3 (steer), not 1:
    # pumping/sawing is what should ever surface, not the ordinary single
    # input this fixture models. throttle_reversals ties at 0 for the same
    # reason -- a single clean throttle pickup that ramps up and holds is
    # monotonic (no stabbing) in both corners.
    ALLOWLISTED: set[str] = {"brake_reversals", "throttle_reversals", "steer_reversals"}

    checked = 0
    for f in dataclasses.fields(CornerMetrics):
        v1, v2 = getattr(corners[0], f.name), getattr(corners[1], f.name)
        if isinstance(v1, bool) or not isinstance(v1, (int, float)):
            continue  # identity/label/list fields, not numeric measurements
        if f.name in ALLOWLISTED:
            continue
        checked += 1
        same = v1 == v2 or (isinstance(v1, float) and isinstance(v2, float)
                             and math.isnan(v1) and math.isnan(v2))
        assert not same, (
            f"CornerMetrics.{f.name} is identical ({v1!r}) across two corners "
            "driven with deliberately different inputs. Two explanations, not "
            "one: either its derivation ignores its per-corner input (the bug "
            "this test hunts for), or this fixture just doesn't exercise it "
            "differently yet. This test does NOT prove the source channel "
            "itself is live -- for that see scripts/dead_channel_sweep.py, "
            "which reads what the game actually wrote. Fix the fixture to "
            "exercise the field if it should vary, or add it to ALLOWLISTED "
            "with a reason if it's genuinely corner-invariant."
        )
    # Every numeric field, not "most": a None brake_point/throttle_point on either
    # corner is skipped by the isinstance filter above, and a floor of 10 let that
    # skip pass silently. 17 is today's full count; a new numeric field raises it.
    assert checked >= 17, f"only {checked} numeric fields compared"


def test_synthetic_lap_uses_lmu_sign_conventions(synthetic_lap):
    # Pins the shared fixture to the signs LMU actually writes, measured on real stored
    # laps (#70). analysis.py takes magnitudes of all four channels, so no per-corner
    # metric test can notice a flipped sign -- the fixture had lat_g and long_g
    # backwards and stayed green. Consumers that DO read a sign (raceline._calibrate:
    # long_g; ribbon.multi_signal_direction: lat_g) inherit whatever this encodes.
    import numpy as np

    ch = synthetic_lap.channels
    d, steer, lat = ch["lap_distance"], ch["steering"], ch["lat_g"]
    left, right = (d > 800) & (d < 1200), (d > 2000) & (d < 2400)

    # long_g: + under braking, - accelerating out of a corner. Every braking sample is
    # positive; the MEDIAN carries the magnitude (np.gradient smears the first/last
    # sample of a braking zone, so a per-sample floor would test the smoothing).
    braking = ch["long_g"][ch["brake"] > 0.5]
    assert np.all(braking > 0) and np.median(braking) > 0.9
    # On throttle: never positive, and the strongest pull out of a corner is negative
    # (a median would sit on the 0.0 of the speed-cap plateau).
    accel = ch["long_g"][(ch["throttle"] > 0.5) & (left | right)]
    assert np.all(accel <= 1e-9) and np.min(accel) < -0.3
    # lat_g: + in the LEFT corner, - in the right; steering has the opposite sign.
    turning = np.abs(steer) > 0.1
    assert np.all(lat[left & turning] > 0) and np.all(steer[left & turning] < 0)
    assert np.all(lat[right & turning] < 0) and np.all(steer[right & turning] > 0)
    # path_lateral (+ = right): the left hairpin is out wide right, apexes left, out right.
    pl = ch["path_lateral"]

    def at(x):
        return pl[int(np.argmin(np.abs(d - x)))]

    assert at(940) > 3 and at(1015) < -3 and at(1120) > 3
    assert at(2120) < -3 and at(2185) > 3 and at(2320) < -3
    # track_edge is the edge on the car's own side: same sign as path_lateral.
    wide = np.abs(pl) > 1
    assert np.all(np.sign(ch["track_edge"][wide]) == np.sign(pl[wide]))


def test_extended_corner_inputs(synthetic_lap):
    # Longitudinal-g (split by pedal), steering, ABS/TC, and track-width usage
    # reach the per-corner metrics and the coach's corner line.
    import numpy as np

    from simcoach.analysis import analyze_lap
    from simcoach.corners import CornerMap
    from simcoach.format import format_corner_line
    from simcoach.model_types import LapRecord
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    ch = dict(synthetic_lap.channels)
    brake = ch["brake"]
    dist = ch["lap_distance"]
    left = (dist > 800) & (dist < 1200)   # the first corner (braking, close to edge)
    # LMU sign: +1.4 g under braking, -0.3 g on throttle (#70). The metrics are
    # magnitudes split by pedal, so the sign must not change what they report.
    ch["long_g"] = np.where(brake > 0.1, 1.4, -0.3)
    ch["abs_active"] = (brake > 0.1).astype(float)    # ABS on under braking
    ch["tc_active"] = np.zeros_like(brake)
    abs_expect = float(np.mean((brake > 0.1)[left]))  # the fraction ABS is actually on
    # mTrackEdge is the track half-width at that point (0.3m in T1), not the car's
    # distance to it -- that distance is |track_edge| - |path_lateral|. The car sits
    # 0.1m off the centre path here, so it's 0.2m from the edge, not 0.3m.
    ch["track_edge"] = np.where(left, 0.3, 5.0)       # track half-width: ~0.3m in T1
    ch["path_lateral"] = np.where(left, 0.1, 0.0)     # car's offset from centre path
    lap = LapRecord(**{**synthetic_lap.__dict__, "channels": ch})

    c1 = analyze_lap(lap, cmap).corners[0]
    assert abs(c1.peak_brake_g - 1.4) < 1e-6
    # Same long_g channel, split by which pedal was down: the corner's exit is on
    # throttle, so the accel side reads 0.3g. (This used to assert nan, because the
    # shared fixture held a flat 0.1 throttle through the whole corner and the accel
    # branch was never reached -- the assertion documented the fixture, not the split.)
    assert abs(c1.peak_accel_g - 0.3) < 1e-6
    assert abs(c1.peak_steer - 0.62) < 1e-4   # peak lock, to within a sample spacing
    assert abs(c1.abs_pct - abs_expect) < 0.01
    assert abs(c1.track_edge_min - 0.2) < 1e-6

    line = format_corner_line(c1, "metric")
    assert "brake 1.4g" in line and "accel 0.3g" in line
    assert "ABS" in line and "edge" not in line   # track_edge computed, not rendered (#99)
    assert "TC" not in line                  # tc_active is flat 0 here


def test_track_edge_distance_uses_path_lateral(synthetic_lap):
    # Issue #45: mTrackEdge is the track EDGE's offset from the centre path
    # (~half the track width there), not the car's distance to it. True
    # distance-to-edge = |track_edge| - |path_lateral|. Holding track_edge
    # constant and varying path_lateral must change the reported distance --
    # the old implementation (nanmin(|track_edge|) alone) ignored path_lateral
    # entirely and fails this test.
    import math

    import numpy as np

    from simcoach.analysis import analyze_lap
    from simcoach.corners import CornerMap
    from simcoach.model_types import LapRecord
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    ch = dict(synthetic_lap.channels)
    dist = ch["lap_distance"]
    in_corner = (dist > 800) & (dist < 1200)   # corner 1's window
    half_width = 4.0   # constant track half-width (mTrackEdge) through the corner

    def edge_min_for(path_lateral_in_corner):
        c = dict(ch)
        c["track_edge"] = np.where(in_corner, half_width, 10.0)
        c["path_lateral"] = np.where(in_corner, path_lateral_in_corner, 0.0)
        lap = LapRecord(**{**synthetic_lap.__dict__, "channels": c})
        return analyze_lap(lap, cmap).corners[0].track_edge_min

    # Car mid-track (path_lateral ~= 0): distance-to-edge ~= the full half-width.
    mid_track = edge_min_for(0.0)
    assert abs(mid_track - half_width) < 1e-6

    # Car running right at the edge (path_lateral == half_width): distance ~= 0.
    assert abs(edge_min_for(half_width) - 0.0) < 1e-6

    # Same track_edge, different path_lateral -> different reported distance.
    near_edge = edge_min_for(half_width - 0.5)
    assert not math.isclose(near_edge, mid_track)
    assert abs(near_edge - 0.5) < 1e-6

    # A car beyond the white line (|path_lateral| > |track_edge|) reports how far
    # PAST it went, as a negative number. Clamping that to 0.0 flattened 168 of
    # 468 real corners (36%) -- true minima with a median of -0.38m, worst -9.75m,
    # at a median 153 km/h -- and no other signal reports them: the lap-level
    # `valid` flag (mLapInvalidated) fired for none of the 168.
    assert abs(edge_min_for(half_width + 1.0) - -1.0) < 1e-6


def test_track_edge_past_edge_wording(synthetic_lap):
    # Metric stays computed (sign-aware) for when it is rig-verified; see below.
    # A negative distance-to-edge must read as "past edge", not as a nonsense
    # "-0.4m from edge". Both wordings name the car's CENTRE, because mPathLateral
    # is the centre and we hold no per-car width -- a ~2m GT car at "centre 0.2m
    # from edge" already has its outer wheels ~0.8m over the line.
    import numpy as np

    from simcoach.analysis import analyze_lap
    from simcoach.corners import CornerMap
    from simcoach.format import format_corner_line
    from simcoach.model_types import LapRecord
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    ch = dict(synthetic_lap.channels)
    in_corner = (ch["lap_distance"] > 800) & (ch["lap_distance"] < 1200)

    def line_for(path_lateral_in_corner):
        c = dict(ch)
        c["track_edge"] = np.where(in_corner, 4.0, 10.0)
        c["path_lateral"] = np.where(in_corner, path_lateral_in_corner, 0.0)
        lap = LapRecord(**{**synthetic_lap.__dict__, "channels": c})
        return format_corner_line(analyze_lap(lap, cmap).corners[0], "metric")

    # Cut from the prompt until rig-verified (#99): neither wording renders,
    # and no raw negative number leaks either.
    past = line_for(4.4)            # centre 0.4m beyond the white line
    assert "edge" not in past and "-0.4" not in past
    inside = line_for(3.8)          # centre still 0.2m inside the line
    assert "edge" not in inside


def test_extended_inputs_absent_without_channels(synthetic_lap):
    # A lap recorded before long_g/abs/tc/track_edge existed leaves the new metrics
    # nan (not 0), so the coach line simply omits them. The channels are DROPPED
    # explicitly here: this used to rely on the shared fixture simply not having them,
    # which meant the test stopped covering anything the moment the fixture grew.
    import dataclasses
    import math
    import re

    from simcoach.analysis import analyze_lap
    from simcoach.corners import CornerMap
    from simcoach.format import format_corner_line
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    dropped = ("long_g", "abs_active", "tc_active", "track_edge", "path_lateral")
    assert all(k in synthetic_lap.channels for k in dropped)   # there is something to drop
    ch = {k: v for k, v in synthetic_lap.channels.items() if k not in dropped}
    c1 = analyze_lap(dataclasses.replace(synthetic_lap, channels=ch), cmap).corners[0]
    assert math.isnan(c1.peak_brake_g) and math.isnan(c1.peak_accel_g)
    assert math.isnan(c1.abs_pct) and math.isnan(c1.tc_pct) and math.isnan(c1.track_edge_min)
    line = format_corner_line(c1, "metric")
    for omitted in ("from edge", "past edge", "ABS", "TC", "accel"):
        assert omitted not in line
    # The brake-g clause specifically ("brake 1.3g"), not the brake POINT clause
    # ("brake 18m before apex"), which a lap without long_g still has.
    assert not re.search(r"brake \d+\.\dg", line) and "before apex" in line


def test_tire_summary_absent_without_channels(synthetic_lap):

    from simcoach.analysis import analyze_lap
    from simcoach.corners import CornerMap
    from simcoach.model_types import LapRecord
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    ch = {k: v for k, v in synthetic_lap.channels.items() if not k.startswith("tire_temp")}
    lap = LapRecord(**{**synthetic_lap.__dict__, "channels": ch})
    a = analyze_lap(lap, cmap)
    assert a.tire_means is None and a.tire_warmup is None


def test_no_throttle_clause_when_the_driver_never_gets_past_the_gate(synthetic_lap):
    """A corner the driver never takes past 80% throttle has NO throttle point. The
    old sentinel was corner.end_dist -- after the apex by construction -- so
    format_corner_line rendered (end_dist - apex_dist) as a driver input: track
    geometry stated to the coach as fact. Wet running, traffic, a fuel-save stint
    or a short-shifted slow corner all reach this."""
    import dataclasses

    import numpy as np

    from simcoach.analysis import analyze_lap
    from simcoach.corners import CornerMap
    from simcoach.format import format_corner_line

    ch = dict(synthetic_lap.channels)
    ch["throttle"] = np.full(len(ch["lap_distance"]), 0.6)   # never above the 0.8 gate
    lap = dataclasses.replace(synthetic_lap, channels=ch)
    c1 = analyze_lap(lap, CornerMap.load("simcoach/tracks/test_track.json")).corners[0]

    assert c1.throttle_point is None
    assert "back to throttle" not in format_corner_line(c1, "metric")


def test_analyze_lap_delta(synthetic_lap):
    # A DISTINCT reference: the best lap carried 5 km/h more through corner 1 and drove
    # corner 2 identically. Using synthetic_lap as its own reference made every delta
    # exactly 0, so a wrong-corner read -- applied to both laps, as a real bug would be
    # -- still printed 0 and passed (#70 review). Here the right deltas are -5 at
    # corner 1 and 0 at corner 2; reading the other corner swaps them.
    import dataclasses

    from simcoach.analysis import analyze_lap
    from simcoach.corners import CornerMap
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    d = synthetic_lap.channels["lap_distance"]
    faster_t1 = synthetic_lap.channels["speed"] + 5.0 * ((d >= 800) & (d <= 1200))
    best = dataclasses.replace(synthetic_lap, channels={**synthetic_lap.channels,
                                                        "speed": faster_t1})
    a = analyze_lap(synthetic_lap, cmap, reference=best)
    assert a.has_reference is True
    c1, c2 = a.corners
    assert abs(c1.min_spd_delta - -5.0) < 1e-9
    assert abs(c2.min_spd_delta) < 1e-9


def _lap_with_speed(synthetic_lap, speed):
    import dataclasses
    channels = dict(synthetic_lap.channels)
    channels["speed"] = speed
    return dataclasses.replace(synthetic_lap, channels=channels)


def test_analyze_lap_metrics_tolerates_partial_nan_speed(synthetic_lap):
    """A single NaN sample in corner 1's speed window must not poison min_spd."""
    import numpy as np

    from simcoach.analysis import analyze_lap
    from simcoach.corners import CornerMap

    dist = synthetic_lap.channels["lap_distance"]
    speed = synthetic_lap.channels["speed"].copy()
    corner1_idx = np.where((dist >= 800) & (dist <= 1200))[0]
    speed[corner1_idx[0]] = np.nan  # NaN at the window's first sample
    lap = _lap_with_speed(synthetic_lap, speed)

    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    clean = analyze_lap(synthetic_lap, cmap).corners[0]
    a = analyze_lap(lap, cmap)
    c1 = a.corners[0]

    # Every reading must survive the hole, and entry_spd must fall through to the next
    # valid sample rather than coming back NaN. Comparing against the CLEAN lap rather
    # than against literals is what makes this a tolerance test instead of a
    # restatement of the fixture's numbers.
    assert abs(c1.min_spd - clean.min_spd) < 1e-9
    assert abs(c1.apex_spd - clean.apex_spd) < 1e-9
    assert abs(c1.exit_spd - clean.exit_spd) < 1e-9
    assert c1.brake_point == clean.brake_point
    assert clean.throttle_point is not None
    assert c1.throttle_point == clean.throttle_point
    assert not np.isnan(c1.entry_spd)


def test_analyze_lap_metrics_all_nan_speed_window_is_degenerate(synthetic_lap):
    """An entirely-NaN speed window falls to the degenerate path instead of crashing."""
    import numpy as np

    from simcoach.analysis import analyze_lap
    from simcoach.corners import CornerMap

    dist = synthetic_lap.channels["lap_distance"]
    speed = synthetic_lap.channels["speed"].copy()
    corner1_idx = np.where((dist >= 800) & (dist <= 1200))[0]
    speed[corner1_idx] = np.nan
    lap = _lap_with_speed(synthetic_lap, speed)

    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    a = analyze_lap(lap, cmap)
    c1 = a.corners[0]

    assert np.isnan(c1.min_spd)
    # Both used to be corner 1's end_dist (1200.0) -- the corner's own geometry, not a
    # reading. No usable samples means no point was found, in either channel.
    assert c1.brake_point is None
    assert c1.throttle_point is None


def test_analyze_lap_delta_is_none_when_min_spd_is_nan(synthetic_lap):
    """min_spd_delta must be None (not NaN) when a corner's min_spd is undefined."""
    import numpy as np

    from simcoach.analysis import analyze_lap
    from simcoach.corners import CornerMap

    dist = synthetic_lap.channels["lap_distance"]
    speed = synthetic_lap.channels["speed"].copy()
    corner1_idx = np.where((dist >= 800) & (dist <= 1200))[0]
    speed[corner1_idx] = np.nan
    lap = _lap_with_speed(synthetic_lap, speed)

    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    a = analyze_lap(lap, cmap, reference=synthetic_lap)
    assert a.corners[0].min_spd_delta is None


# --- #102: diff every comparable metric vs the reference, not just min speed -----

def test_analyze_lap_diffs_brake_and_throttle_points_vs_reference(synthetic_lap):
    # Reference brakes 10 m EARLIER and picks up throttle 15 m EARLIER than the
    # current (synthetic_lap) lap in corner 1: shift the whole brake/throttle
    # profile earlier along distance so the on/off edges both move together.
    import dataclasses

    import numpy as np

    from simcoach.analysis import analyze_lap
    from simcoach.corners import CornerMap

    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    dist = synthetic_lap.channels["lap_distance"]
    ref_brake = np.interp(dist + 10.0, dist, synthetic_lap.channels["brake"])
    ref_throttle = np.interp(dist + 15.0, dist, synthetic_lap.channels["throttle"])
    reference = dataclasses.replace(synthetic_lap, channels={
        **synthetic_lap.channels, "brake": ref_brake, "throttle": ref_throttle,
    })

    a = analyze_lap(synthetic_lap, cmap, reference=reference)
    c1 = a.corners[0]
    assert c1.brake_point is not None and c1.brake_point_delta is not None
    assert abs(c1.brake_point_delta - 10.0) < 5.0    # current brakes LATER -> positive
    assert c1.throttle_point is not None and c1.throttle_point_delta is not None
    assert abs(c1.throttle_point_delta - 15.0) < 5.0


def test_analyze_lap_delta_none_when_reference_never_brakes(synthetic_lap):
    # A corner the reference never braked in has no brake point to diff against --
    # the delta must be None, never computed off a "not found" sentinel.
    import dataclasses

    import numpy as np

    from simcoach.analysis import analyze_lap
    from simcoach.corners import CornerMap

    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    ref_brake = np.zeros_like(synthetic_lap.channels["lap_distance"])
    reference = dataclasses.replace(synthetic_lap, channels={
        **synthetic_lap.channels, "brake": ref_brake,
    })

    a = analyze_lap(synthetic_lap, cmap, reference=reference)
    c1 = a.corners[0]
    assert c1.brake_point is not None       # the current lap still braked
    assert c1.brake_point_delta is None     # but the reference never did


def test_analyze_lap_diffs_extended_corner_metrics_vs_reference(synthetic_lap):
    # A reference with more trailbrake overlap and more ABS in corner 1 -- both
    # extended (delta) fields must move, not just min_spd_delta.
    import dataclasses

    import numpy as np

    from simcoach.analysis import analyze_lap
    from simcoach.corners import CornerMap

    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    dist = synthetic_lap.channels["lap_distance"]
    corner1 = (dist >= 800) & (dist <= 1200)
    # Widen the reference's braking to cover its whole steering-lock zone, so it
    # trail-brakes MORE of the corner than the current lap does.
    steer_mask = np.abs(synthetic_lap.channels["steering"]) > 0.1
    ref_brake = synthetic_lap.channels["brake"].copy()
    ref_brake[corner1 & steer_mask] = 0.5
    ref_abs = synthetic_lap.channels["abs_active"].copy()
    ref_abs[corner1] = 1.0
    reference = dataclasses.replace(synthetic_lap, channels={
        **synthetic_lap.channels, "brake": ref_brake, "abs_active": ref_abs,
    })

    a = analyze_lap(synthetic_lap, cmap, reference=reference)
    c1 = a.corners[0]
    assert c1.trailbrake_overlap_delta is not None and c1.trailbrake_overlap_delta < 0
    assert c1.abs_pct_delta is not None and c1.abs_pct_delta < 0


def test_analyze_lap_direction_is_computed_not_read_from_map(synthetic_lap):
    """Live analysis must COMPUTE direction from telemetry, not just echo the map's
    stated direction. Use a map with a deliberately WRONG direction and assert the
    analysis reports the telemetry-computed direction instead."""
    from simcoach.analysis import analyze_lap
    from simcoach.corners import Corner, CornerMap

    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    # Corner 1 is actually "left" per telemetry; flip the map to say "right".
    cmap.corners[0] = Corner(
        number=1, name="Left Hairpin", direction="right",
        start_dist=800, apex_dist=1000, end_dist=1200,
    )

    a = analyze_lap(synthetic_lap, cmap)
    assert a.corners[0].direction == "left"  # telemetry-computed, not the map's "right"
    assert a.corners[1].direction == "right"


def test_analyze_lap_rejects_reference_from_different_track(synthetic_lap):
    import dataclasses

    import pytest

    from simcoach.analysis import analyze_lap
    from simcoach.corners import CornerMap

    other_track_lap = dataclasses.replace(synthetic_lap, track="other_track")
    cmap = CornerMap.load("simcoach/tracks/test_track.json")

    with pytest.raises(ValueError, match="other_track"):
        analyze_lap(synthetic_lap, cmap, reference=other_track_lap)


# --- distance-keyed time axis + two-lap alignment (issue #38 slice 1) ---------

def _flat_lap(speed_kmh, n=500, length=1000.0, **extra):
    """A constant-speed lap on a clean distance axis."""
    import numpy as np

    from simcoach.model_types import LapRecord
    dist = np.linspace(0.0, length, n)
    ch = {"lap_distance": dist, "speed": np.full(n, float(speed_kmh)),
          "throttle": np.ones(n), "brake": np.zeros(n), "steering": np.zeros(n)}
    ch.update(extra)
    return LapRecord("lmu", "test_track", "test_car", 0.0, True, 0.0, ch)


def test_lap_time_axis_constant_speed_is_distance_over_speed():
    from simcoach.analysis import lap_time_axis
    d, t = lap_time_axis(_flat_lap(72.0, length=1000.0))   # 72 km/h = 20 m/s
    assert len(d) == len(t)
    assert t[0] == 0.0
    assert abs(t[-1] - 50.0) < 0.05        # 1000 m / 20 m/s = 50 s  (km/h NOT m/s)


def test_speed_channel_is_km_per_hour():
    # The `speed` channel is km/h (smm.py: |mLocalVel| * 3.6). This constant is the
    # ONE place that conversion lives; a wrong value scales every delta-t by 3.6.
    from simcoach.analysis import _SPEED_KMH_TO_MS
    assert _SPEED_KMH_TO_MS == 1.0 / 3.6


def test_lap_time_axis_ignores_non_increasing_distance():
    import numpy as np

    from simcoach.analysis import lap_time_axis
    lap = _flat_lap(72.0, n=100, length=1000.0)
    lap.channels["lap_distance"][50] = 10.0          # a backwards jitter sample
    d, t = lap_time_axis(lap)
    assert np.all(np.diff(d) > 0)                     # filtered out
    assert abs(t[-1] - 50.0) < 0.5


def test_lap_time_axis_survives_a_stopped_sample():
    import math

    from simcoach.analysis import lap_time_axis
    lap = _flat_lap(72.0)
    lap.channels["speed"][10] = 0.0
    _, t = lap_time_axis(lap)
    assert math.isfinite(t[-1])                        # no div-by-zero / inf


# --- #101: de-staircased lap_distance + measured elapsed_time axis -----------

def _stair_hold(dist, hz=48.6, scoring_hz=5.0):
    """Hold `dist` (already sampled at `hz`) at the ~5 Hz scoring rate, same
    staircase shape as test_input_traces._staircase_lap: ~10 identical values,
    then a jump."""
    import numpy as np
    n = len(dist)
    t = np.arange(n) / hz
    step = 1.0 / scoring_hz
    ticks = np.floor(t / step) * step
    return np.interp(ticks, t, dist)


def test_brake_point_resolves_to_within_1m_despite_the_staircase():
    """A ~48.6 Hz lap (~0.33 m/sample -- real capture density) that brakes at a
    known, deliberately off-grid distance. Holding lap_distance at the ~5 Hz
    scoring rate (#101's staircase, ~8-10 m steps) makes that true brake point
    land mid-step; de-staircasing must recover it to within ~1 m."""
    import dataclasses

    import numpy as np

    from simcoach.analysis import analyze_lap
    from simcoach.corners import CornerMap
    from simcoach.model_types import LapRecord

    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    true_brake_at = 950.37   # inside T1 (800-1200, apex 1000), off any 8-10m grid line

    n, hz, length = 9000, 48.6, 3000.0
    t = np.arange(n) / hz
    dist = length * t / t[-1]
    ch = {"lap_distance": dist, "speed": np.full(n, 200.0),
          "brake": np.where(dist >= true_brake_at, 0.8, 0.0),
          "throttle": np.zeros(n), "steering": np.zeros(n)}
    smooth_lap = LapRecord(sim="lmu", track="test_track", car="test_car",
                           lap_time=float(t[-1]), valid=True, timestamp=0.0, channels=ch)
    smooth_bp = analyze_lap(smooth_lap, cmap).corners[0].brake_point
    assert smooth_bp is not None and abs(smooth_bp - true_brake_at) < 0.5  # sanity

    held = _stair_hold(dist, hz=hz)
    assert len(np.unique(held)) < len(held) / 5    # precondition: really a staircase

    # The raw staircase channel itself disagrees with the truth near the brake
    # point by more than the ~1 m tolerance the fix must hit -- reading it
    # directly (the pre-fix behaviour) could not have resolved it that tightly.
    i = int(np.argmin(np.abs(dist - true_brake_at)))
    assert abs(held[i] - dist[i]) > 1.0

    staircase_lap = dataclasses.replace(smooth_lap, channels={**ch, "lap_distance": held})
    fixed_bp = analyze_lap(staircase_lap, cmap).corners[0].brake_point
    assert fixed_bp is not None
    assert abs(fixed_bp - true_brake_at) < 1.0


def test_lap_time_axis_uses_measured_elapsed_time_when_present():
    """A lap carrying elapsed_time/lap_start_et must produce a time axis matching
    it exactly, not the ds/v estimate (deliberately given a different total so the
    two paths can't be confused)."""
    import numpy as np

    from simcoach.analysis import _uses_exact_time_axis, lap_time_axis
    n = 50
    et = np.linspace(10.0, 22.345, n)          # 12.345 s elapsed, NOT 1000/20=50s
    lap = _flat_lap(72.0, n=n, length=1000.0,
                    elapsed_time=et, lap_start_et=np.full(n, 10.0))
    assert _uses_exact_time_axis(lap) is True
    d, t = lap_time_axis(lap)
    assert np.allclose(t, et - 10.0)
    assert np.allclose(d, lap.channels["lap_distance"])


def test_uses_exact_time_axis_false_without_elapsed_time_channels():
    """Older captures (no elapsed_time/lap_start_et) fall back to ds/v -- the
    existing lap_time_axis tests above exercise that path already; this pins the
    predicate `format_time_loss`'s noise floor depends on."""
    from simcoach.analysis import _uses_exact_time_axis
    assert _uses_exact_time_axis(_flat_lap(72.0, length=1000.0)) is False


def test_exact_time_axis_drops_the_seam_sample():
    """A sample carrying the NEXT lap's lap_start_et shows elapsed_time -
    lap_start_et dropping back toward zero (smm.py) -- it must be filtered out
    like a backwards lap_distance jitter, not integrated as a huge negative dt."""
    import numpy as np

    from simcoach.analysis import lap_time_axis
    n = 50
    et = np.linspace(10.0, 22.0, n)
    lap_start = np.full(n, 10.0)
    lap_start[-1] = 22.0                        # next lap's lap_start_et at the seam
    lap = _flat_lap(72.0, n=n, length=1000.0, elapsed_time=et, lap_start_et=lap_start)
    d, t = lap_time_axis(lap)
    assert np.all(np.diff(t) > 0)
    assert len(d) == n - 1                      # the seam sample dropped, not kept


def test_input_traces_unaffected_by_lap_distance_interpolation_cache():
    """analyze_lap caches an interpolated lap_distance on the record (#101, for its
    own repeat corner-metric reads); input_traces reads record.channels directly
    and must be unaffected regardless of whether that cache ran first."""
    import numpy as np

    from simcoach.analysis import analyze_lap, input_traces
    from simcoach.corners import CornerMap
    from simcoach.model_types import LapRecord

    n = 486
    t = np.arange(n) / 48.6
    dist = 3000.0 * t / t[-1]
    step = 1.0 / 5.0
    held = np.interp(np.floor(t / step) * step, t, dist)
    lap = LapRecord(sim="lmu", track="test_track", car="test_car", lap_time=t[-1],
                    valid=True, timestamp=0.0,
                    channels={"lap_distance": held, "throttle": np.ones(n),
                              "brake": np.zeros(n), "steering": np.zeros(n),
                              "speed": np.full(n, 100.0)})
    before = input_traces(lap, bins=50)
    analyze_lap(lap, CornerMap.load("simcoach/tracks/test_track.json"))
    after = input_traces(lap, bins=50)
    assert before.keys() == after.keys()
    for k, v in before.items():
        assert after[k] == v


def test_input_traces_mean_and_speed(synthetic_lap):
    """#95: each channel gets a `mean` band alongside `lo`/`hi`, and a `speed`
    channel (km/h) is added on the same bins."""
    from simcoach.analysis import input_traces

    tr = input_traces(synthetic_lap, bins=50)
    assert "speed" in tr
    for ch in ("throttle", "brake", "steering", "speed"):
        band = tr[ch]
        assert len(band["mean"]) == len(band["lo"]) == len(band["hi"]) == 50
        for lo, mean, hi in zip(band["lo"], band["mean"], band["hi"]):
            assert lo <= mean <= hi


def test_time_loss_buckets_flag_exact_when_both_laps_carry_elapsed_time():
    import numpy as np

    from simcoach.analysis import time_loss_buckets
    cur, ref = _slow_in_t1()
    for lap in (cur, ref):
        n = len(lap.channels["lap_distance"])
        lap.channels["elapsed_time"] = np.arange(n, dtype=float) * 0.01
        lap.channels["lap_start_et"] = np.zeros(n)
    b = time_loss_buckets(cur, ref, _test_cmap())
    assert b and all(x["exact"] for x in b)


def test_time_loss_buckets_flag_not_exact_when_either_lap_lacks_elapsed_time():
    from simcoach.analysis import time_loss_buckets
    cur, ref = _slow_in_t1()
    b = time_loss_buckets(cur, ref, _test_cmap())
    assert b and all(not x["exact"] for x in b)


def test_align_time_axes_injected_loss_shows_as_delta():
    from simcoach.analysis import align_time_axes
    ref = _flat_lap(72.0, length=1000.0)
    cur = _flat_lap(72.0, length=1000.0)
    # slow to 36 km/h (10 m/s) between 400-600 m: 200 m costs 20 s instead of 10 s
    m = (cur.channels["lap_distance"] >= 400) & (cur.channels["lap_distance"] <= 600)
    cur.channels["speed"][m] = 36.0
    d, delta = align_time_axes(cur, ref)
    assert abs(delta[-1] - 10.0) < 0.3                # +10 s lost, net
    assert abs(delta[d < 400][-1]) < 0.1              # nothing lost before the slow bit
    assert abs(delta[d > 600][0] - 10.0) < 0.3        # all of it inside the window


def test_align_time_axes_rejects_insufficient_overlap():
    from simcoach.analysis import align_time_axes
    assert align_time_axes(_flat_lap(72.0, length=300.0), _flat_lap(72.0, length=3000.0)) is None


# --- whole-lap time-loss buckets ---------------------------------------------

def _test_cmap():
    from simcoach.corners import CornerMap
    return CornerMap.load("simcoach/tracks/test_track.json")


def _slow_in_t1():
    """(cur, ref) on test_track geometry: identical 144 km/h laps except cur drops
    to 72 km/h between 900-1100 m — inside T1's 800-1200 m window only."""
    ref = _flat_lap(144.0, n=1500, length=3000.0)
    cur = _flat_lap(144.0, n=1500, length=3000.0)
    d = cur.channels["lap_distance"]
    cur.channels["speed"][(d >= 900) & (d <= 1100)] = 72.0
    return cur, ref


def test_time_loss_buckets_attribute_the_loss_to_the_right_corner():
    from simcoach.analysis import time_loss_buckets
    cur, ref = _slow_in_t1()
    b = time_loss_buckets(cur, ref, _test_cmap())
    t1 = next(x for x in b if x["kind"] == "corner" and x["label"].startswith("T1"))
    assert t1["delta"] > 0.5                                        # the loss landed in T1
    assert all(abs(x["delta"]) < 0.05 for x in b if x is not t1)    # and nowhere else


def test_time_loss_buckets_sum_to_the_net_delta():
    from simcoach.analysis import align_time_axes, time_loss_buckets
    cur, ref = _slow_in_t1()
    b = time_loss_buckets(cur, ref, _test_cmap())
    _, delta = align_time_axes(cur, ref)
    assert abs(sum(x["delta"] for x in b) - float(delta[-1])) < 0.02


def test_time_loss_buckets_cover_corners_and_straights():
    from simcoach.analysis import time_loss_buckets
    cur, ref = _slow_in_t1()
    cmap = _test_cmap()
    import itertools
    b = time_loss_buckets(cur, ref, cmap)
    assert {x["kind"] for x in b} == {"corner", "straight"}
    assert len([x for x in b if x["kind"] == "corner"]) == len(cmap.corners)
    # contiguous tiling in track order, so the deltas can't double-count or gap
    for prev, nxt in itertools.pairwise(b):
        assert abs(prev["end_dist"] - nxt["start_dist"]) < 1e-6
    # straights are named from the corners they sit between, never invented
    names = [x["label"] for x in b if x["kind"] == "straight"]
    assert any("T1" in n and "T2" in n for n in names)


def test_time_loss_buckets_none_without_usable_reference():
    from simcoach.analysis import time_loss_buckets
    short, long_ref = _flat_lap(72.0, length=300.0), _flat_lap(72.0, length=3000.0)
    assert time_loss_buckets(short, long_ref, _test_cmap()) is None


# --- coasting / dead time -----------------------------------------------------

def _coasting_lap():
    """A lap at a constant 36 km/h (10 m/s) that brakes 800-900 m, coasts with
    NEITHER pedal 900-1000 m, then picks the throttle back up — inside T1's
    800-1200 m window. 100 m of coasting at 10 m/s is exactly 10 s."""
    lap = _flat_lap(36.0, n=1500, length=3000.0)
    d = lap.channels["lap_distance"]
    lap.channels["brake"][(d >= 800) & (d < 900)] = 0.8
    lap.channels["throttle"][(d >= 800) & (d < 1000)] = 0.0
    lap.channels["steering"][(d >= 800) & (d <= 1200)] = -0.5
    return lap


def test_corner_coasting_measures_the_dead_zone():
    from simcoach.analysis import analyze_lap
    c1 = analyze_lap(_coasting_lap(), _test_cmap()).corners[0]
    assert abs(c1.coast_dist - 100.0) < 4.0           # ~100 m with neither pedal
    assert abs(c1.coast_time - 10.0) < 0.5            # at 10 m/s, ~10 s of dead time
    assert abs(c1.brake_to_throttle_dist - 100.0) < 5.0


def test_corner_without_coasting_reports_zero(synthetic_lap):
    from simcoach.analysis import analyze_lap
    from simcoach.format import format_corner_line
    c1 = analyze_lap(synthetic_lap, _test_cmap()).corners[0]
    assert c1.coast_time == 0.0 and c1.coast_dist == 0.0
    assert "coasting" not in format_corner_line(c1)    # a clean corner carries no line


def test_lap_coast_total_and_lap_facts_line():
    from simcoach.analysis import analyze_lap
    from simcoach.format import format_lap_facts
    a = analyze_lap(_coasting_lap(), _test_cmap())
    secs, meters = a.coast_total
    assert abs(secs - 10.0) < 0.5 and abs(meters - 100.0) < 4.0
    assert "Coasting this lap" in format_lap_facts(a)


def test_lap_coast_total_is_none_without_pedal_channels():
    import numpy as np

    from simcoach.analysis import lap_coast_total
    from simcoach.model_types import LapRecord
    n = 10
    bare = LapRecord("lmu", "t", "c", 0.0, True, 0.0,
                     {"lap_distance": np.linspace(0.0, 100.0, n),
                      "speed": np.full(n, 72.0)})
    assert lap_coast_total(bare) is None


def test_time_loss_buckets_handle_a_corner_wrapping_the_start_finish_seam():
    # A window with start_dist > end_dist spans the S/F seam; its two distance runs
    # must fold into ONE bucket and the tiling must still sum to the net delta.
    from simcoach.analysis import align_time_axes, time_loss_buckets
    from simcoach.corners import Corner, CornerMap
    cur, ref = _slow_in_t1()
    cmap = CornerMap([Corner(1, "Seam", "left", 2800.0, 2900.0, 200.0),
                      Corner(2, "Left Hairpin", "left", 800.0, 1000.0, 1200.0)])
    b = time_loss_buckets(cur, ref, cmap)
    assert len([x for x in b if x["kind"] == "corner"]) == 2
    _, delta = align_time_axes(cur, ref)
    assert abs(sum(x["delta"] for x in b) - float(delta[-1])) < 0.02


# --- consistency (#38) ---------------------------------------------------------

def _consistency_stint(synthetic_lap, scales):
    """One `stint_consistency` input: analyses/lap_times for len(scales) clones of
    `synthetic_lap`, each with T1's min-speed window (900-1100, its minimum sits at
    1015 -- see conftest) scaled by that lap's factor. T2 (2000-2400) is untouched,
    so only T1 should show up as inconsistent."""
    import dataclasses

    from simcoach.analysis import analyze_lap
    from simcoach.corners import CornerMap
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    analyses, lap_times = [], []
    for i, scale in enumerate(scales):
        ch = {k: v.copy() for k, v in synthetic_lap.channels.items()}
        d = ch["lap_distance"]
        win = (d >= 900) & (d <= 1100)
        ch["speed"][win] = ch["speed"][win] * scale
        lap = dataclasses.replace(synthetic_lap, channels=ch, lap_time=90.0 + i)
        analyses.append(analyze_lap(lap, cmap))
        lap_times.append(lap.lap_time)
    return analyses, lap_times


def test_stint_consistency_ranks_the_noisy_corner_first(synthetic_lap):
    import numpy as np

    from simcoach.analysis import stint_consistency
    analyses, lap_times = _consistency_stint(
        synthetic_lap, [1.0, 0.95, 1.05, 0.90, 1.10])

    result = stint_consistency(analyses, lap_times)
    assert result["ok"] is True
    assert result["n"] == 5
    assert result["ranking"][0] == "T1 Left Hairpin"

    min_spds = [c.min_spd for a in analyses for c in a.corners if c.corner_no == 1]
    expected_sigma = float(np.std(min_spds))
    assert abs(result["corners"]["T1 Left Hairpin"]["min_spd"]["sigma"] - expected_sigma) < 1e-6
    assert result["corners"]["T1 Left Hairpin"]["min_spd"]["n"] == 5
    assert abs(result["lap_time_sigma"] - float(np.std(lap_times))) < 1e-9


def test_stint_consistency_honest_with_fewer_than_three_laps(synthetic_lap):
    from simcoach.analysis import analyze_lap, stint_consistency
    from simcoach.corners import CornerMap
    a = analyze_lap(synthetic_lap, CornerMap.load("simcoach/tracks/test_track.json"))
    result = stint_consistency([a, a], [90.0, 91.0])
    assert result == {"ok": False, "n": 2}


def test_stint_consistency_skips_none_and_nan_fields_instead_of_zeroing(synthetic_lap):
    """One of three laps never brakes at all (brake_point -> None for every
    corner). That lap must be SKIPPED for brake_point's sigma/n, not counted as a
    zero brake_point -- a zero would read as "brakes right at the apex", the
    opposite of what actually happened."""
    import dataclasses

    import numpy as np

    from simcoach.analysis import analyze_lap, stint_consistency
    from simcoach.corners import CornerMap
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    analyses, lap_times = [], []
    for i in range(3):
        ch = dict(synthetic_lap.channels)
        if i == 0:
            ch = {**ch, "brake": np.zeros_like(ch["lap_distance"])}  # flat -> no brake_point
        lap = dataclasses.replace(synthetic_lap, channels=ch, lap_time=90.0 + i)
        analyses.append(analyze_lap(lap, cmap))
        lap_times.append(lap.lap_time)

    result = stint_consistency(analyses, lap_times)
    bp = result["corners"]["T1 Left Hairpin"]["brake_point"]
    assert bp["n"] == 2   # only the two laps that actually braked here count


# --- tire-deg trend (#38 P1.6) --------------------------------------------------

def _trend_stint(n=5, temp_rise=2.0, wear_fall=0.02, pace_rise=0.1, missing=frozenset()):
    """n synthetic LapAnalysis objects built directly (no fixture needed): RL
    heats up `temp_rise` C/lap and wears `wear_fall`/lap (remaining fraction),
    every other wheel flat; lap time rises `pace_rise` s/lap. Laps whose index
    is in `missing` carry no tire_means/wheels at all (older-capture lap)."""
    from simcoach.analysis import LapAnalysis
    analyses, lap_times = [], []
    for i in range(n):
        t = 90.0 + pace_rise * i
        tire_means = wheels = None
        if i not in missing:
            tire_means = {"fl": 80.0, "fr": 80.0, "rl": 80.0 + temp_rise * i, "rr": 80.0}
            wheels = {
                "tire_press_mean": {"fl": 27.0, "fr": 27.0, "rl": 27.0, "rr": 27.0},
                "tire_wear_end": {"fl": 0.95, "fr": 0.95, "rl": 1.0 - wear_fall * i, "rr": 0.95},
            }
        analyses.append(LapAnalysis(lap_time=t, corners=[], has_reference=False,
                                    tire_means=tire_means, wheels=wheels))
        lap_times.append(t)
    return analyses, lap_times


def test_stint_tire_trend_reads_rl_wear_temp_and_pace():
    from simcoach.analysis import stint_tire_trend
    analyses, lap_times = _trend_stint()
    result = stint_tire_trend(analyses, lap_times)
    assert result["ok"] is True
    assert result["n"] == 5
    assert result["hottest"] == "rl"
    assert result["most_worn"] == "rl"
    assert abs(result["temp_slope"]["rl"] - 2.0) < 1e-6
    assert abs(result["wear_pct_per_lap"]["rl"] - 2.0) < 1e-6   # -100 * -0.02
    assert abs(result["pace_slope_s"] - 0.1) < 1e-6
    # Untouched wheels read ~flat (present, near-zero slope), not skipped.
    assert abs(result["wear_pct_per_lap"]["fl"]) < 1e-6
    assert all(abs(v) < 1e-6 for v in result["press_slope"].values())


def test_stint_tire_trend_honest_with_two_laps():
    from simcoach.analysis import stint_tire_trend
    analyses, lap_times = _trend_stint(n=2)
    result = stint_tire_trend(analyses, lap_times)
    assert result == {"ok": False, "n": 2}


def test_stint_tire_trend_skips_missing_wheels_instead_of_zeroing():
    from simcoach.analysis import stint_tire_trend
    # 2 of 5 laps missing wheels/tire_means -- 3 remain, right at the floor.
    ok = stint_tire_trend(*_trend_stint(missing=frozenset({0, 1})))
    assert ok["ok"] is True
    assert "rl" in ok["wear_pct_per_lap"] and "rl" in ok["temp_slope"]

    # 3 of 5 missing -- only 2 remain, below the floor -> field absent, not zero.
    short = stint_tire_trend(*_trend_stint(missing=frozenset({0, 1, 2})))
    assert "rl" not in short["wear_pct_per_lap"]
    assert "rl" not in short["temp_slope"]
    assert short["most_worn"] is None
    assert short["hottest"] is None
    # Pace never depends on wheels/tire_means -- still readable.
    assert abs(short["pace_slope_s"] - 0.1) < 1e-6


# --- handling_read (#38) ---------------------------------------------------

def test_handling_read_understeer():
    # Steering keeps winding (0.31 -> 0.6 over the mid phase) while lat_g sits
    # flat at 1.5 -- the car is adding lock and getting nothing back.
    import numpy as np

    from simcoach.analysis import handling_read

    n = 20
    steer = np.linspace(0.1, 0.6, n)     # peak 0.6, at the min-speed sample
    lat_g = np.full(n, 1.5)
    speed = np.linspace(150.0, 50.0, n)  # min speed at the last sample
    brake = np.zeros(n)
    throttle = np.zeros(n)

    result = handling_read(steer, lat_g, speed, brake, throttle, None)
    assert result["handling"] == "understeer"
    assert "lock" in result["handling_why"] and "lat g" in result["handling_why"]
    assert result["lockup"] is None and result["wheelspin"] is None


def test_handling_read_oversteer_countersteer():
    # Steering normally sits opposite sign to lat_g (LMU convention, #70); 8
    # consecutive samples flip to lat_g's OWN sign mid-corner -- countersteer.
    import numpy as np

    from simcoach.analysis import handling_read

    n = 20
    steer = np.full(n, -0.3)
    steer[6:14] = 0.3   # 8-sample countersteer against the corner's direction
    lat_g = np.full(n, 1.2)
    speed = np.linspace(150.0, 50.0, n)
    brake = np.zeros(n)
    throttle = np.zeros(n)

    result = handling_read(steer, lat_g, speed, brake, throttle, None)
    assert result["handling"] == "oversteer"
    assert "countersteer" in result["handling_why"]


def test_handling_read_neutral_on_a_clean_trace():
    # Steering and lat_g rise and fall together (no plateau, no reversal, no
    # exit drop) -- a clean, proportional corner.
    import numpy as np

    from simcoach.analysis import handling_read

    n = 20
    steer = np.concatenate([np.linspace(0.0, -0.15, 10), np.linspace(-0.15, 0.0, 10)])
    lat_g = np.concatenate([np.linspace(0.0, 0.8, 10), np.linspace(0.8, 0.0, 10)])
    speed = np.concatenate([np.linspace(150.0, 80.0, 10), np.linspace(80.0, 150.0, 10)])
    brake = np.zeros(n)
    throttle = np.zeros(n)

    result = handling_read(steer, lat_g, speed, brake, throttle, None)
    assert result["handling"] == "neutral"
    assert result["handling_why"] is None


def test_handling_read_too_few_samples_is_none():
    import numpy as np

    from simcoach.analysis import handling_read

    n = 5
    result = handling_read(np.zeros(n), np.zeros(n), np.full(n, 100.0),
                           np.zeros(n), np.zeros(n), None)
    assert result["handling"] is None and result["handling_why"] is None


def test_handling_read_lockup():
    import numpy as np

    from simcoach.analysis import handling_read

    n = 15
    steer, lat_g = np.zeros(n), np.zeros(n)
    speed = np.full(n, 120.0)
    brake = np.full(n, 0.6)
    throttle = np.zeros(n)
    wheel_rot = {w: np.full(n, 30.0) for w in ("fl", "fr", "rl", "rr")}
    wheel_rot["fl"][7] = 0.05   # one wheel stopped while the car moves at 120 km/h

    result = handling_read(steer, lat_g, speed, brake, throttle, wheel_rot)
    assert result["lockup"] is True
    assert result["wheelspin"] is False


def test_handling_read_wheelspin():
    import numpy as np

    from simcoach.analysis import handling_read

    n = 15
    steer, lat_g = np.zeros(n), np.zeros(n)
    speed = np.full(n, 100.0)
    brake = np.zeros(n)
    throttle = np.full(n, 0.8)
    wheel_rot = {w: np.full(n, 30.0) for w in ("fl", "fr", "rl", "rr")}
    wheel_rot["rl"][7] = 40.0   # rear-left outpaces the front pair under power

    result = handling_read(steer, lat_g, speed, brake, throttle, wheel_rot)
    assert result["wheelspin"] is True
    assert result["lockup"] is False


def test_handling_read_none_when_wheel_rot_missing():
    import numpy as np

    from simcoach.analysis import handling_read

    n = 15
    result = handling_read(np.zeros(n), np.zeros(n), np.full(n, 100.0),
                           np.full(n, 0.6), np.full(n, 0.8), None)
    assert result["lockup"] is None and result["wheelspin"] is None


def test_analyze_lap_carries_handling_fields_neutral_on_synthetic_lap(synthetic_lap):
    # No wheel_rot channels in the fixture (#38 needs a fresh capture for
    # lockup/wheelspin) and both corners are clean, proportional cornering --
    # so the new fields must be present but inert, and the corner line unchanged.
    from simcoach.analysis import analyze_lap
    from simcoach.corners import CornerMap
    from simcoach.format import format_corner_line

    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    a = analyze_lap(synthetic_lap, cmap)
    for cm in a.corners:
        assert cm.handling == "neutral"
        assert cm.lockup is None and cm.wheelspin is None
        line = format_corner_line(cm, "metric")
        assert "understeer" not in line and "oversteer" not in line
        assert "lockup" not in line and "wheelspin" not in line


def test_wheelspin_and_lockup_read_magnitude_not_sign():
    # Real captures carry mRotation NEGATIVE rolling forward (~ -176 rad/s at speed).
    # A rear wheel turning SLOWER than the fronts must not read as wheelspin, and a
    # faster one must.
    import numpy as np

    from simcoach.analysis import handling_read
    n = 40
    steer = np.full(n, -0.3); lat_g = np.full(n, 1.0); speed = np.full(n, 120.0)
    brake = np.zeros(n); throttle = np.full(n, 0.8)
    front = np.full(n, -176.0)
    slow_rear = {"fl": front, "fr": front, "rl": np.full(n, -150.0), "rr": np.full(n, -150.0)}
    assert handling_read(steer, lat_g, speed, brake, throttle, slow_rear)["wheelspin"] is False
    fast_rear = {"fl": front, "fr": front, "rl": np.full(n, -230.0), "rr": front}
    assert handling_read(steer, lat_g, speed, brake, throttle, fast_rear)["wheelspin"] is True
    # Lockup: a wheel at ~0 rad/s (either sign) under braking at speed.
    braking = np.full(n, 0.8)
    locked = {"fl": np.full(n, -0.2), "fr": front, "rl": front, "rr": front}
    assert handling_read(steer, lat_g, speed, braking, np.zeros(n), locked)["lockup"] is True


# --- traction-circle (G-G) utilization (#38 P1.4) ---------------------------

def test_grip_envelope_p98_of_usable_samples(synthetic_lap):
    import dataclasses

    import numpy as np

    from simcoach.analysis import grip_envelope

    n = 1000
    lat_g = np.linspace(0.31, 1.8, n)   # a spread of loaded samples, all past the gate
    long_g = np.zeros(n)
    lap = dataclasses.replace(synthetic_lap, channels={
        **synthetic_lap.channels, "lat_g": lat_g, "long_g": long_g})
    env = grip_envelope([lap])
    assert env is not None
    assert abs(env - float(np.percentile(lat_g, 98.0))) < 1e-6


def test_grip_envelope_ignores_a_single_crash_spike(synthetic_lap):
    # #78: a raw max of long_g was set by one 40g crash-spike sample. p98
    # must barely move when that single sample is injected.
    import dataclasses

    import numpy as np

    from simcoach.analysis import grip_envelope

    n = 1000
    lat_g = np.linspace(0.31, 1.8, n)
    long_g = np.zeros(n)
    baseline = dataclasses.replace(synthetic_lap, channels={
        **synthetic_lap.channels, "lat_g": lat_g, "long_g": long_g})
    env_before = grip_envelope([baseline])

    spiked_long = long_g.copy()
    spiked_long[500] = 40.0
    spiked = dataclasses.replace(baseline, channels={**baseline.channels, "long_g": spiked_long})
    env_after = grip_envelope([spiked])

    assert env_before is not None and env_after is not None
    assert abs(env_after - env_before) / env_before < 0.05


def test_grip_envelope_none_without_load_channels(synthetic_lap):
    import dataclasses

    from simcoach.analysis import grip_envelope

    ch = {k: v for k, v in synthetic_lap.channels.items() if k not in ("lat_g", "long_g")}
    lap = dataclasses.replace(synthetic_lap, channels=ch)
    assert grip_envelope([lap]) is None
    assert grip_envelope([]) is None


def test_analyze_lap_gg_peak_is_p95_of_this_laps_own_cornering_phase(synthetic_lap):
    # #108 item 2: gg_peak must read intelligibly (raw g, not a ratio) before an
    # envelope has formed -- computed from the SAME cornering-phase samples
    # gg_used uses (|lat_g| > the load gate), independent of the envelope value.
    from simcoach.analysis import (
        _GG_LOAD_GATE,
        _GG_USED_PCTL,
        analyze_lap,
        combined_g,
    )
    from simcoach.corners import CornerMap

    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    analysis = analyze_lap(synthetic_lap, cmap, envelope=1.5, envelope_laps=2)

    lat_g = synthetic_lap.channels["lat_g"]
    long_g = synthetic_lap.channels["long_g"]
    cg = combined_g(lat_g, long_g)
    phase = np.abs(lat_g) > _GG_LOAD_GATE
    expected_peak = float(np.percentile(cg[phase], _GG_USED_PCTL))

    assert analysis.gg_peak is not None
    assert abs(analysis.gg_peak - expected_peak) < 1e-6
    assert analysis.gg_envelope_laps == 2


def test_analyze_lap_gg_envelope_laps_defaults_to_none(synthetic_lap):
    from simcoach.analysis import analyze_lap
    from simcoach.corners import CornerMap

    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    analysis = analyze_lap(synthetic_lap, cmap)
    assert analysis.gg_envelope_laps is None


def test_gg_used_and_dead_middle_per_corner(synthetic_lap):
    import dataclasses

    import numpy as np

    from simcoach.analysis import analyze_lap
    from simcoach.corners import CornerMap

    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    dist = synthetic_lap.channels["lap_distance"]
    env = 2.0
    lat_g = np.zeros_like(dist)
    long_g = np.zeros_like(dist)

    # Corner 1 (800-1200): flat at 95% of a known envelope throughout.
    m1 = (dist >= 800) & (dist <= 1200)
    lat_g[m1] = 0.95 * env

    # Corner 2 (2000-2400): the first 60% of the window coasts under half the
    # envelope (weak trail-brake blend), the rest is loaded.
    idx2 = np.where((dist >= 2000) & (dist <= 2400))[0]
    split = int(len(idx2) * 0.6)
    lat_g[idx2[:split]] = 0.4          # combined 0.4g < 0.5 * env (1.0) -- dead
    lat_g[idx2[split:]] = 0.95 * env

    lap = dataclasses.replace(synthetic_lap, channels={
        **synthetic_lap.channels, "lat_g": lat_g, "long_g": long_g})
    a = analyze_lap(lap, cmap, envelope=env)
    c1, c2 = a.corners

    assert abs(c1.gg_used - 0.95) < 0.02
    assert c1.gg_dead_middle == 0.0
    assert abs(c2.gg_dead_middle - 0.6) < 0.05
    assert a.gg_envelope == env
    assert a.gg_used is not None


def test_analyze_lap_gg_fields_none_without_envelope_or_load_channels(synthetic_lap):
    import dataclasses

    from simcoach.analysis import analyze_lap
    from simcoach.corners import CornerMap

    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    ch = {k: v for k, v in synthetic_lap.channels.items() if k not in ("lat_g", "long_g")}
    lap = dataclasses.replace(synthetic_lap, channels=ch)
    a = analyze_lap(lap, cmap)
    assert a.gg_envelope is None and a.gg_used is None
    assert all(cm.gg_used is None and cm.gg_dead_middle is None for cm in a.corners)


def test_analyze_lap_falls_back_to_self_referenced_envelope(synthetic_lap):
    # Real lat_g/long_g on the fixture (~1.25g/1.53g peaks) give both corners a
    # usable, if optimistic, envelope read when no history-based envelope is passed.
    from simcoach.analysis import analyze_lap
    from simcoach.corners import CornerMap

    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    a = analyze_lap(synthetic_lap, cmap)
    assert a.gg_envelope is not None
    assert a.gg_used is not None


# --- gear/RPM sanity (#38 P1.7) ----------------------------------------------

def test_gear_read_gear_min_at_min_speed_sample():
    import numpy as np

    from simcoach.analysis import gear_read

    n = 10
    gear = np.array([3.0] * 5 + [2.0] + [3.0] * 4)   # 2nd at the min-speed sample
    rpm = np.full(n, 6000.0)
    speed = np.concatenate([np.linspace(100.0, 40.0, 6), np.linspace(50.0, 90.0, 4)])
    throttle = np.zeros(n)
    result = gear_read(gear, rpm, throttle, speed, min_i=5, rpm_max_lap=7000.0)
    assert result["gear_min"] == 2


def test_gear_read_bog_true_when_exit_rpm_is_low():
    import numpy as np

    from simcoach.analysis import gear_read

    n = 10
    gear = np.full(n, 2.0)
    speed = np.concatenate([np.linspace(100.0, 40.0, 6), np.linspace(45.0, 90.0, 4)])
    throttle = np.zeros(n)
    throttle[6:] = 1.0   # first pickup at index 6, one past min_i
    rpm_max_lap = 7000.0
    rpm = np.full(n, rpm_max_lap)
    rpm[6] = 0.4 * rpm_max_lap   # 40% of peak -- below the 55% bog floor
    result = gear_read(gear, rpm, throttle, speed, min_i=5, rpm_max_lap=rpm_max_lap)
    assert result["bog"] is True


def test_gear_read_bog_false_when_exit_rpm_clears_the_band():
    import numpy as np

    from simcoach.analysis import gear_read

    n = 10
    gear = np.full(n, 2.0)
    speed = np.concatenate([np.linspace(100.0, 40.0, 6), np.linspace(45.0, 90.0, 4)])
    throttle = np.zeros(n)
    throttle[6:] = 1.0
    rpm_max_lap = 7000.0
    rpm = np.full(n, rpm_max_lap)
    rpm[6] = 0.7 * rpm_max_lap   # 70% of peak -- clears the 55% bog floor
    result = gear_read(gear, rpm, throttle, speed, min_i=5, rpm_max_lap=rpm_max_lap)
    assert result["bog"] is False


def test_gear_read_short_shift_true_at_low_rpm():
    import numpy as np

    from simcoach.analysis import gear_read

    n = 10
    gear = np.array([1.0] * 4 + [2.0] * 6)   # upshift at index 3 -> 4
    rpm_max_lap = 7000.0
    rpm = np.full(n, rpm_max_lap)
    rpm[3] = 0.6 * rpm_max_lap   # shifted at 60% of peak -- below the 80% floor
    speed = np.linspace(100.0, 150.0, n)
    throttle = np.full(n, 0.8)
    result = gear_read(gear, rpm, throttle, speed, min_i=0, rpm_max_lap=rpm_max_lap)
    assert result["short_shift"] is True


def test_gear_read_short_shift_false_near_redline():
    import numpy as np

    from simcoach.analysis import gear_read

    n = 10
    gear = np.array([1.0] * 4 + [2.0] * 6)
    rpm_max_lap = 7000.0
    rpm = np.full(n, rpm_max_lap)
    rpm[3] = 0.9 * rpm_max_lap   # shifted at 90% of peak -- clears the 80% floor
    speed = np.linspace(100.0, 150.0, n)
    throttle = np.full(n, 0.8)
    result = gear_read(gear, rpm, throttle, speed, min_i=0, rpm_max_lap=rpm_max_lap)
    assert result["short_shift"] is False


def test_gear_read_none_when_channels_absent():
    import numpy as np

    from simcoach.analysis import gear_read

    n = 5
    result = gear_read(None, None, np.zeros(n), np.full(n, 100.0), min_i=2, rpm_max_lap=None)
    assert result == {"gear_min": None, "bog": None, "short_shift": None}


def test_analyze_lap_gear_fields_none_without_rpm_channel(synthetic_lap):
    # synthetic_lap carries "gear" but no "rpm" channel (conftest) -- gear_read
    # needs both, so every corner's new fields stay None and the corner line is
    # silent, same shape as the #38 handling fields on this fixture.
    from simcoach.analysis import analyze_lap
    from simcoach.corners import CornerMap
    from simcoach.format import format_corner_line

    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    a = analyze_lap(synthetic_lap, cmap)
    for cm in a.corners:
        assert cm.gear_min is None and cm.bog is None and cm.short_shift is None
        line = format_corner_line(cm, "metric")
        assert "gear at min speed" not in line
        assert "bogged" not in line and "short-shifted" not in line


# --- input-shape facts (#38 P1.8) --------------------------------------------

def test_input_shape_linear_release_measures_distance_and_steep_frac():
    import numpy as np
    import pytest

    from simcoach.analysis import input_shape

    dist = np.array([0.0, 8.0, 16.0, 24.0, 32.0, 40.0])
    brake = np.array([1.0, 0.8, 0.6, 0.4, 0.2, 0.0])
    n = brake.size
    result = input_shape(dist, brake, np.zeros(n), np.zeros(n))
    assert result["brake_release_m"] == pytest.approx(40.0)
    assert result["brake_release_steep_frac"] == pytest.approx(0.2, abs=0.01)


def test_input_shape_dumped_release_has_high_steep_frac():
    import numpy as np

    from simcoach.analysis import input_shape

    dist = np.array([0.0, 8.0, 16.0, 24.0])
    brake = np.array([1.0, 0.97, 0.03, 0.0])
    n = brake.size
    result = input_shape(dist, brake, np.zeros(n), np.zeros(n))
    assert result["brake_release_steep_frac"] > 0.8


def test_input_shape_no_braking_phase_release_is_none():
    import numpy as np

    from simcoach.analysis import input_shape

    n = 6
    result = input_shape(np.linspace(0, 40, n), np.zeros(n), np.zeros(n), np.zeros(n))
    assert result["brake_release_m"] is None
    assert result["brake_release_steep_frac"] is None


def test_input_shape_no_release_inside_window_is_none():
    import numpy as np

    from simcoach.analysis import input_shape

    n = 6
    brake = np.linspace(0.5, 1.0, n)   # rising the whole window, never releases
    result = input_shape(np.linspace(0, 40, n), brake, np.zeros(n), np.zeros(n))
    assert result["brake_release_m"] is None


def test_input_shape_pumped_brake_counts_three_reversals():
    import numpy as np

    from simcoach.analysis import input_shape

    brake = np.array([0.9, 0.7, 0.9, 0.7, 0.9, 0.7, 0.9])   # 3 re-applications (pumps) of 0.2
    n = brake.size
    result = input_shape(np.linspace(0, 40, n), brake, np.zeros(n), np.zeros(n))
    assert result["brake_reversals"] == 3


def test_input_shape_smooth_brake_has_no_reversals():
    import numpy as np

    from simcoach.analysis import input_shape

    brake = np.array([0.9, 0.8, 0.7, 0.6, 0.5])   # monotonic release, no pumping
    n = brake.size
    result = input_shape(np.linspace(0, 40, n), brake, np.zeros(n), np.zeros(n))
    assert result["brake_reversals"] == 0


def test_input_shape_hysteresis_ignores_small_wiggles():
    import numpy as np

    from simcoach.analysis import input_shape

    # 0.02 wiggle, under the 0.05 hysteresis.
    brake = np.array([0.9, 0.88, 0.9, 0.88, 0.9])
    n = brake.size
    result = input_shape(np.linspace(0, 40, n), brake, np.zeros(n), np.zeros(n))
    assert result["brake_reversals"] == 0


def test_input_shape_throttle_stabs_counted():
    import numpy as np

    from simcoach.analysis import input_shape

    throttle = np.array([0.9, 0.6, 0.9, 0.6, 0.9, 0.6, 0.9])   # 3 stabs, mirrors brake pumping
    n = throttle.size
    result = input_shape(np.linspace(0, 40, n), np.zeros(n), throttle, np.zeros(n))
    assert result["throttle_reversals"] == 3


def test_input_shape_steering_sawing_counted():
    import numpy as np

    from simcoach.analysis import input_shape

    steer = np.array([0.0, 0.3, -0.3, 0.3, -0.3, 0.3])   # lock, then 3 saws beyond it
    n = steer.size
    result = input_shape(np.linspace(0, 40, n), np.zeros(n), np.zeros(n), steer)
    assert result["steer_reversals"] == 3


def test_input_shape_none_when_channel_absent():
    import numpy as np

    from simcoach.analysis import input_shape

    n = 5
    result = input_shape(np.linspace(0, 40, n), None, np.zeros(n), None)
    assert result["brake_release_m"] is None
    assert result["brake_release_steep_frac"] is None
    assert result["brake_reversals"] is None
    assert result["steer_reversals"] is None
    assert result["throttle_reversals"] == 0   # present but flat -- a real 0, not absent


def test_analyze_lap_input_shape_fields_are_sane(synthetic_lap):
    # synthetic_lap through analyze_lap: every corner gets a read, and it's
    # internally consistent (no nonsense ranges), whatever the exact values are.
    from simcoach.analysis import analyze_lap
    from simcoach.corners import CornerMap

    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    a = analyze_lap(synthetic_lap, cmap)
    for cm in a.corners:
        assert cm.brake_reversals is not None and cm.brake_reversals >= 0
        assert cm.throttle_reversals is not None and cm.throttle_reversals >= 0
        assert cm.steer_reversals is not None and cm.steer_reversals >= 0
        assert cm.brake_release_m is not None and cm.brake_release_m >= 0
        assert cm.brake_release_steep_frac is not None
        assert 0.0 <= cm.brake_release_steep_frac <= 1.0


def test_analyze_lap_diffs_input_shape_metrics_vs_reference(synthetic_lap):
    # A reference that pumps the brake 3 times in corner 1's braking zone (the
    # current lap brakes smoothly) must move brake_reversals_delta.
    import dataclasses

    import numpy as np

    from simcoach.analysis import analyze_lap
    from simcoach.corners import CornerMap

    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    dist = synthetic_lap.channels["lap_distance"]
    braking = synthetic_lap.channels["brake"] > 0.1
    corner1 = (dist >= 800) & (dist <= 1200) & braking
    ref_brake = synthetic_lap.channels["brake"].copy()
    idx = np.where(corner1)[0]
    ref_brake[idx] = 0.9 + 0.1 * (np.arange(idx.size) % 2)   # oscillate 0.9/1.0
    reference = dataclasses.replace(synthetic_lap, channels={
        **synthetic_lap.channels, "brake": ref_brake,
    })

    a = analyze_lap(synthetic_lap, cmap, reference=reference)
    c1 = a.corners[0]
    assert c1.brake_reversals_delta is not None and c1.brake_reversals_delta < 0


# --- per-lap event log (#108 items 3+4) ------------------------------------

def _path_lap(segments, hz=50.0, track="test_track"):
    """A LapRecord whose PATH (pos_x/pos_z) turns by `turn_deg` over each segment
    while speed ramps v_start -> v_end. segments: (duration_s, turn_deg,
    v_start_kmh, v_end_kmh). Everything else (distance, time) is integrated from
    that speed trace, so the time axis and the path agree."""
    from simcoach.analysis import _SPEED_KMH_TO_MS
    v_parts, h_parts = [], []
    heading = 0.0
    for dur, turn, v0, v1 in segments:
        k = max(2, round(dur * hz))
        v_parts.append(np.linspace(v0, v1, k))
        h_parts.append(heading + np.linspace(0.0, turn, k))
        heading += turn
    v = np.concatenate(v_parts)
    h = np.radians(np.concatenate(h_parts))
    n = v.size
    dt = 1.0 / hz
    t = np.arange(n) * dt
    ds = np.r_[0.0, v[1:] * _SPEED_KMH_TO_MS * dt]
    ch = {
        "lap_distance": np.cumsum(ds),
        "time": t,
        "speed": v,
        "pos_x": np.cumsum(ds * np.cos(h)),
        "pos_z": np.cumsum(ds * np.sin(h)),
        "throttle": np.zeros(n),
        "brake": np.zeros(n),
        "steering": np.zeros(n),
    }
    return LapRecord("lmu", track, "test_car", float(t[-1]), True, 0.0, ch)


def test_lap_events_flags_a_spin_on_the_straight():
    # 200 deg of rotation in 2 s while speed falls 180 -> 30: a spin.
    rec = _path_lap([(2.0, 0.0, 180.0, 180.0),
                     (2.0, 200.0, 180.0, 30.0),
                     (2.0, 0.0, 30.0, 30.0)])
    cmap = CornerMap([])
    events = lap_events(rec, cmap, analyze_lap(rec, cmap))
    spins = [e for e in events if e["kind"] == "spin"]
    assert len(spins) == 1
    e = spins[0]
    assert e["corner_no"] is None          # no mapped corner here
    assert e["detail"]["speed_from_kmh"] == pytest.approx(180.0, abs=2.0)
    assert e["detail"]["speed_min_kmh"] == pytest.approx(30.0, abs=2.0)
    assert e["detail"]["turned_deg"] == pytest.approx(200.0, abs=5.0)


def test_lap_events_does_not_flag_a_170_degree_hairpin():
    # The discriminator is the speed COLLAPSE, not the angle: brake 250 -> 65 on
    # the straight (no rotation), then turn 170 deg at a steady 65 km/h. Every
    # 3 s window has a big angle or a big speed drop, never both.
    rec = _path_lap([(3.0, 0.0, 250.0, 65.0),
                     (2.5, 170.0, 65.0, 65.0),
                     (3.0, 0.0, 65.0, 120.0)])
    cmap = CornerMap([])
    events = lap_events(rec, cmap, analyze_lap(rec, cmap))
    assert [e for e in events if e["kind"] == "spin"] == []


def test_lap_events_skips_spins_without_position_channels(synthetic_lap):
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    events = lap_events(synthetic_lap, cmap, analyze_lap(synthetic_lap, cmap))
    assert [e for e in events if e["kind"] == "spin"] == []


def test_lap_events_off_track_only_past_the_knob(synthetic_lap):
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    # Corner 1's closest approach is 0.3 m INSIDE the edge -> not an off.
    events = lap_events(synthetic_lap, cmap, analyze_lap(synthetic_lap, cmap))
    assert [e for e in events if e["kind"] == "off"] == []
    # Push the car 1.5 m past its own edge through corner 1.
    ch = dict(synthetic_lap.channels)
    d = np.asarray(ch["lap_distance"], float)
    edge = np.asarray(ch["track_edge"], float).copy()
    lat = np.asarray(ch["path_lateral"], float).copy()
    seg = (d >= 980) & (d <= 1020)
    lat[seg] = np.copysign(np.abs(edge[seg]) + 1.5, lat[seg])
    ch["path_lateral"] = lat
    rec = dataclasses.replace(synthetic_lap, channels=ch)
    offs = [e for e in lap_events(rec, cmap, analyze_lap(rec, cmap)) if e["kind"] == "off"]
    assert len(offs) == 1
    assert offs[0]["corner_no"] == 1
    assert offs[0]["detail"]["past_edge_m"] == pytest.approx(1.5, abs=0.2)


def test_lap_events_passes_lockup_and_wheelspin_through(synthetic_lap):
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    ch = dict(synthetic_lap.channels)
    d = np.asarray(ch["lap_distance"], float)
    n = d.size
    speed = np.asarray(ch["speed"], float)
    brake = np.asarray(ch["brake"], float)
    # Front wheels stopped through corner 1's brake zone -> lockup; a rear wheel
    # spinning 30% faster than the fronts on corner 2's exit -> wheelspin.
    rot = {w: speed / 3.6 / 0.33 for w in ("fl", "fr", "rl", "rr")}
    locked = (d >= 900) & (d <= 1000) & (brake > 0.5)
    for w in ("fl", "fr"):
        rot[w] = np.where(locked, 0.0, rot[w])
    spin = (d >= 2200) & (d <= 2300)
    rot["rl"] = np.where(spin, rot["rl"] * 1.3, rot["rl"])
    for w, v in rot.items():
        ch[f"wheel_rot_{w}"] = v
    assert n == d.size
    rec = dataclasses.replace(synthetic_lap, channels=ch)
    events = lap_events(rec, cmap, analyze_lap(rec, cmap))
    assert {(e["kind"], e["corner_no"]) for e in events} >= {("lockup", 1), ("wheelspin", 2)}


def _wear_lap(base, wheel="fr", drop=0.09, at_dist=1000.0):
    ch = dict(base.channels)
    d = np.asarray(ch["lap_distance"], float)
    wear = np.asarray(ch[f"tire_wear_{wheel}"], float).copy()
    i = np.argmin(np.abs(d - at_dist))
    wear[i:] -= drop
    ch[f"tire_wear_{wheel}"] = wear
    return dataclasses.replace(base, channels=ch), i


def test_lap_events_wear_step_flags_the_wheel_and_places_it(synthetic_lap):
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    rec, i = _wear_lap(synthetic_lap)          # FR: 0.03/lap baseline -> 0.12 lap
    baseline = {w: 0.03 for w in ("fl", "fr", "rl", "rr")}
    steps = [e for e in lap_events(rec, cmap, analyze_lap(rec, cmap), baseline)
             if e["kind"] == "wear_step"]
    assert len(steps) == 1
    e = steps[0]
    assert e["detail"]["wheel"] == "fr"
    assert e["corner_no"] == 1
    assert e["lap_distance"] == pytest.approx(float(np.asarray(
        rec.channels["lap_distance"], float)[i]), abs=1.0)
    assert e["detail"]["step_pct"] == pytest.approx(12.0, abs=0.5)
    assert e["detail"]["baseline_pct"] == pytest.approx(3.0, abs=0.01)


def test_lap_events_no_wear_events_without_a_baseline(synthetic_lap):
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    rec, _ = _wear_lap(synthetic_lap)
    events = lap_events(rec, cmap, analyze_lap(rec, cmap))
    assert [e for e in events if e["kind"] == "wear_step"] == []


def test_stint_wear_baseline_is_the_per_wheel_median(synthetic_lap):
    recs = []
    for drop in (0.00, 0.02, 0.09):
        ch = dict(synthetic_lap.channels)
        wear = np.asarray(ch["tire_wear_fr"], float).copy()
        wear[len(wear) // 2:] -= drop
        ch["tire_wear_fr"] = wear
        recs.append(dataclasses.replace(synthetic_lap, channels=ch))
    base = stint_wear_baseline(recs)
    assert base["fr"] == pytest.approx(0.05, abs=1e-6)   # median of .03/.05/.12
    assert base["fl"] == pytest.approx(0.03, abs=1e-6)


def test_stint_wear_baseline_skips_wheels_without_the_channel(synthetic_lap):
    recs = []
    for _ in range(3):
        ch = {k: v for k, v in synthetic_lap.channels.items() if k != "tire_wear_rr"}
        recs.append(dataclasses.replace(synthetic_lap, channels=ch))
    base = stint_wear_baseline(recs)
    assert "rr" not in base and "fl" in base


def test_stint_wear_baseline_needs_two_laps(synthetic_lap):
    assert stint_wear_baseline([synthetic_lap]) == {}
    assert stint_wear_baseline([]) == {}


def test_analyze_lap_on_a_partial_fragment_is_nan_where_the_lap_never_went(synthetic_lap):
    """#77: a stored out/in/spin lap covers only part of the track and carries no lap
    time. analyze_lap must not raise on either -- it reports NaN for the corners the
    fragment never reached, and real numbers for the ones it did, which is what lets
    the coach screen keep inputs + corner analysis on a spin lap."""
    import numpy as np

    from simcoach.analysis import analyze_lap
    from simcoach.corners import CornerMap
    from simcoach.model_types import LapRecord

    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    c1, c2 = cmap.corners[0], cmap.corners[1]
    d = synthetic_lap.channels["lap_distance"]
    # Keep only the second half of the lap: corner 1's window is never entered.
    keep = d >= (c1.end_dist + c2.start_dist) / 2
    assert keep.sum() > 10 and not keep[0]
    frag = LapRecord(**{**synthetic_lap.__dict__,
                        "lap_time": None, "valid": False, "partial": "partial",
                        "channels": {k: v[keep] for k, v in synthetic_lap.channels.items()}})

    a = analyze_lap(frag, cmap)
    assert a.lap_time is None
    by_no = {cm.corner_no: cm for cm in a.corners}
    assert np.isnan(by_no[c1.number].min_spd)
    assert np.isfinite(by_no[c2.number].min_spd)
