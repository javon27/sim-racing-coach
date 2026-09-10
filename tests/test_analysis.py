def test_analyze_lap_metrics(synthetic_lap):
    from simcoach.analysis import analyze_lap
    from simcoach.corners import CornerMap
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    a = analyze_lap(synthetic_lap, cmap)
    assert a.has_reference is False
    c1 = a.corners[0]
    assert c1.corner_no == 1 and c1.direction == "left"
    assert c1.apex_dist == 1000
    assert abs(c1.min_spd - 30.0) < 1e-6
    assert 800 <= c1.brake_point <= 1200
    assert c1.trailbrake_overlap > 0.0


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
    assert c1.brake_point < c.start_dist           # captured in the approach
    assert c1.brake_point < c1.apex_dist           # sane "before apex" readout
    assert c1.brake_point != c.end_dist            # not the no-brake sentinel


def test_tire_summary_and_prompt(synthetic_lap):
    from simcoach.analysis import analyze_lap
    from simcoach.coach import Coach
    from simcoach.corners import CornerMap
    from simcoach.model.fake import FakeModel
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    a = analyze_lap(synthetic_lap, cmap)
    # synthetic tires: 80°C base, +5 under braking (in corners) -> mean between 80 and 85
    assert set(a.tire_means) == {"fl", "fr", "rl", "rr"}
    assert all(80.0 <= v <= 85.0 for v in a.tire_means.values())
    assert a.tire_warmup is not None

    _, user = Coach(FakeModel()).build_prompt(a, "car", "how were my tires?")
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
    assert "grip" not in format_lap_facts(a).lower()


def test_corner_metrics_fields_vary_or_are_allowlisted():
    # General regression for #43's bug CLASS, not just the one field: a raw
    # channel that never varies (LMU's mGripFract, always flat 0.0) reached
    # coach output as a real per-corner measurement because the only guard was
    # isnan, and a dead-but-valid 0.0 sails through that guard. This test
    # doesn't know the name "grip" -- it walks every numeric CornerMetrics
    # field by reflection and demands that two corners driven with distinctly
    # different inputs produce distinctly different values for it. A future
    # field that comes back identical across both either isn't really wired to
    # per-corner telemetry (the #43 shape) or is legitimately corner-invariant
    # and belongs on ALLOWLISTED with a reason -- it can't pass silently.
    import dataclasses
    import math

    import numpy as np

    from simcoach.analysis import CornerMetrics, analyze_lap
    from simcoach.corners import CornerMap
    from simcoach.model_types import LapRecord

    cmap = CornerMap.load("simcoach/tracks/test_track.json")  # corner1 800-1200, corner2 2000-2400
    n = 1000
    dist = np.linspace(0, 3000, n)
    in1 = (dist > 800) & (dist < 1200)
    in2 = (dist > 2000) & (dist < 2400)
    after1 = (dist > 1000) & (dist < 1200)   # back-on-throttle half of corner1
    after2 = (dist > 2200) & (dist < 2400)   # back-on-throttle half of corner2

    speed = np.full(n, 80.0)
    speed[in1], speed[in2] = 30.0, 50.0                    # different apex speeds
    brake = np.zeros(n)
    brake[in1], brake[in2] = 0.8, 0.5                      # different peak brake
    throttle = np.zeros(n) + 1.0
    throttle[in1 | in2] = 0.0
    throttle[after1 | after2] = 1.0                        # both corners exit on throttle
    steer = np.zeros(n)
    steer[in1] = -0.5                                      # corner1: steering held all the way through
    steer[in2 & ~after2] = 0.8                             # corner2: steering only the first half
    lat_g = np.where(in1, -1.0, np.where(in2, 1.6, 0.1))   # independent, different peak per corner
    long_g = np.zeros(n)
    long_g[in1] = -1.4
    long_g[after1] = 0.3                                   # overwrites part of corner1 -> accel phase
    long_g[in2] = -0.9
    long_g[after2] = 0.6                                   # overwrites part of corner2 -> accel phase
    abs_active = np.zeros(n)
    abs_active[in1] = 1.0                                  # ABS only in corner1
    tc_active = np.zeros(n)
    tc_active[after2] = 1.0                                # TC only in corner2's exit
    track_edge = np.where(in1, 4.0, np.where(in2, 5.0, 10.0))
    path_lateral = np.where(in1, 3.8, np.where(in2, 4.0, 0.0))
    # A raw channel shaped exactly like the real #43 bug: flat 0.0 everywhere,
    # never responding to the very different braking/steering above. Captured
    # (real capture keeps sampling it), but must never surface as a measurement.
    dead = np.zeros(n)

    ch = {
        "lap_distance": dist, "speed": speed, "brake": brake, "throttle": throttle,
        "steering": steer, "lat_g": lat_g, "long_g": long_g,
        "abs_active": abs_active, "tc_active": tc_active,
        "track_edge": track_edge, "path_lateral": path_lateral,
    }
    for w in ("fl", "fr", "rl", "rr"):
        ch[f"grip_{w}"] = dead
    lap = LapRecord("lmu", "test_track", "test_car", 92.5, True, 0.0, ch)

    corners = analyze_lap(lap, cmap).corners
    assert len(corners) == 2

    # Bookkeeping fields that are legitimately the same shape regardless of
    # driving: min_spd_delta is None unless a reference lap is supplied (not
    # exercised here), so it's filtered out below as non-numeric already --
    # listed for documentation, not because the filter needs it.
    ALLOWLISTED: set[str] = set()

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
            "driven with deliberately different inputs -- looks like a "
            "dead/unwired channel reaching coach output as a fake measurement "
            "(the #43 bug shape), or it belongs on ALLOWLISTED with a reason."
        )
    assert checked >= 10  # sanity: this actually exercised most numeric fields


def test_extended_corner_inputs(synthetic_lap):
    # Longitudinal-g (split by pedal), steering, ABS/TC, and track-width usage
    # reach the per-corner metrics and the coach's corner line.
    import numpy as np
    from simcoach.analysis import analyze_lap
    from simcoach.corners import CornerMap
    from simcoach.model_types import LapRecord
    from simcoach.format import format_corner_line
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    ch = dict(synthetic_lap.channels)
    brake, throttle = ch["brake"], ch["throttle"]
    dist = ch["lap_distance"]
    left = (dist > 800) & (dist < 1200)   # the first corner (braking, close to edge)
    ch["long_g"] = np.where(brake > 0, -1.4, 0.3)     # 1.4g decel under braking
    ch["abs_active"] = (brake > 0).astype(float)      # ABS on under braking
    ch["tc_active"] = np.zeros_like(brake)
    # mTrackEdge is the track half-width at that point (0.3m in T1), not the car's
    # distance to it -- that distance is |track_edge| - |path_lateral|. The car sits
    # 0.1m off the centre path here, so it's 0.2m from the edge, not 0.3m.
    ch["track_edge"] = np.where(left, 0.3, 5.0)       # track half-width: ~0.3m in T1
    ch["path_lateral"] = np.where(left, 0.1, 0.0)     # car's offset from centre path
    lap = LapRecord(**{**synthetic_lap.__dict__, "channels": ch})

    c1 = analyze_lap(lap, cmap).corners[0]
    assert abs(c1.peak_brake_g - 1.4) < 1e-6
    assert np.isnan(c1.peak_accel_g)          # no throttle-on phase inside the corner
    assert abs(c1.peak_steer - 0.5) < 1e-6
    assert c1.abs_pct > 0.9
    assert abs(c1.track_edge_min - 0.2) < 1e-6

    line = format_corner_line(c1, "metric")
    assert "brake 1.4g" in line and "ABS" in line and "from edge" in line
    assert "accel" not in line               # nan accel is omitted


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

    past = line_for(4.4)            # centre 0.4m beyond the white line
    assert "centre 0.4m past edge" in past
    assert "-0.4" not in past and "from edge" not in past

    inside = line_for(3.8)          # centre still 0.2m inside the line
    assert "centre 0.2m from edge" in inside
    assert "past edge" not in inside


def test_extended_inputs_absent_without_channels(synthetic_lap):
    # A lap without long_g/abs/tc/track_edge leaves the new metrics nan (not 0),
    # so the coach line simply omits them.
    import math
    from simcoach.analysis import analyze_lap
    from simcoach.corners import CornerMap
    from simcoach.format import format_corner_line
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    c1 = analyze_lap(synthetic_lap, cmap).corners[0]
    assert math.isnan(c1.peak_brake_g) and math.isnan(c1.abs_pct) and math.isnan(c1.track_edge_min)
    line = format_corner_line(c1, "metric")
    assert "from edge" not in line and "ABS" not in line


def test_tire_summary_absent_without_channels(synthetic_lap):
    import numpy as np
    from simcoach.analysis import analyze_lap
    from simcoach.corners import CornerMap
    from simcoach.model_types import LapRecord
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    ch = {k: v for k, v in synthetic_lap.channels.items() if not k.startswith("tire_temp")}
    lap = LapRecord(**{**synthetic_lap.__dict__, "channels": ch})
    a = analyze_lap(lap, cmap)
    assert a.tire_means is None and a.tire_warmup is None


def test_analyze_lap_delta(synthetic_lap):
    from simcoach.analysis import analyze_lap
    from simcoach.corners import CornerMap
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    a = analyze_lap(synthetic_lap, cmap, reference=synthetic_lap)
    assert a.has_reference is True
    assert abs(a.corners[0].min_spd_delta) < 1e-6


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
    a = analyze_lap(lap, cmap)
    c1 = a.corners[0]

    assert abs(c1.min_spd - 30.0) < 1e-6
    assert 800 <= c1.throttle_point <= 1200


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
    assert c1.brake_point == 1200.0
    assert c1.throttle_point == 1200.0


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


def test_analyze_lap_direction_is_computed_not_read_from_map(synthetic_lap):
    """Live analysis must COMPUTE direction from telemetry, not just echo the map's
    stated direction. Use a map with a deliberately WRONG direction and assert the
    analysis reports the telemetry-computed direction instead."""
    from simcoach.analysis import analyze_lap
    from simcoach.corners import CornerMap, Corner

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
