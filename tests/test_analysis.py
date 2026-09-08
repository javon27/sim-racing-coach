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

    _, user = Coach(FakeModel(), "kb").build_prompt(a, "car", "how were my tires?")
    assert "Tire temps" in user and "FL" in user and "warm-up" in user.lower()


def test_wheel_report_and_min_grip(synthetic_lap):
    from simcoach.analysis import analyze_lap
    from simcoach.coach import Coach
    from simcoach.corners import CornerMap
    from simcoach.format import format_wheel_report
    from simcoach.model.fake import FakeModel
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    a = analyze_lap(synthetic_lap, cmap)
    # min grip dips to 0.9 in the braking corners
    assert abs(a.corners[0].min_grip - 0.9) < 1e-9
    # lap-level wheel report: brake temps, pressures, wear
    rep = format_wheel_report(a, "metric")
    assert "Brake temps" in rep and "wear" in rep.lower()
    # surfaced in the prompt corner line
    _, user = Coach(FakeModel(), "kb").build_prompt(a, "car", "grip?")
    assert "min grip" in user


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
    ch["track_edge"] = np.where(left, 0.3, 5.0)       # ran to ~0.3m of the edge in T1
    lap = LapRecord(**{**synthetic_lap.__dict__, "channels": ch})

    c1 = analyze_lap(lap, cmap).corners[0]
    assert abs(c1.peak_brake_g - 1.4) < 1e-6
    assert np.isnan(c1.peak_accel_g)          # no throttle-on phase inside the corner
    assert abs(c1.peak_steer - 0.5) < 1e-6
    assert c1.abs_pct > 0.9
    assert abs(c1.track_edge_min - 0.3) < 1e-6

    line = format_corner_line(c1, "metric")
    assert "brake 1.4g" in line and "ABS" in line and "from edge" in line
    assert "accel" not in line               # nan accel is omitted


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
