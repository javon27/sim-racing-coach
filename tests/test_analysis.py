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
    from simcoach.model_types import LapRecord
    from simcoach.format import format_corner_line
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    ch = dict(synthetic_lap.channels)
    brake, throttle = ch["brake"], ch["throttle"]
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
    assert "ABS" in line and "from edge" in line
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
    import numpy as np
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
