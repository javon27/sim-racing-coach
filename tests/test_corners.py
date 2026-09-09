import json

import numpy as np
import pytest

from simcoach.model_types import LapRecord


def test_computed_direction_matches_geometry(synthetic_lap):
    from simcoach.corners import compute_direction
    assert compute_direction(synthetic_lap, 800, 1200) == "left"
    assert compute_direction(synthetic_lap, 2000, 2400) == "right"


def test_map_directions_consistent(synthetic_lap):
    from simcoach.corners import CornerMap, verify_map_directions
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    assert verify_map_directions(synthetic_lap, cmap) == []


def test_verify_map_directions_detects_wrong_direction(synthetic_lap):
    """Detection test: verify_map_directions catches a flipped direction in the map."""
    from simcoach.corners import Corner, CornerMap, verify_map_directions

    # Load correct map, mutate one corner's direction
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    # Flip corner 1 "Left Hairpin" to "right" (wrong)
    cmap.corners[0] = Corner(
        number=1, name="Left Hairpin", direction="right",
        start_dist=800, apex_dist=1000, end_dist=1200
    )

    # Should detect mismatch: declared "right", computed "left"
    mismatches = verify_map_directions(synthetic_lap, cmap)
    assert (1, "right", "left") in mismatches


def test_compute_direction_raises_on_empty_window(synthetic_lap):
    """Empty window (no telemetry in range) raises ValueError."""
    from simcoach.corners import compute_direction

    # Window entirely outside lap range (0-3000)
    with pytest.raises(ValueError, match="no telemetry in window"):
        compute_direction(synthetic_lap, 3100, 3200)


def test_compute_direction_raises_on_all_nan_window(synthetic_lap):
    """All-NaN window raises ValueError."""
    from simcoach.corners import compute_direction

    # Create lap with steering all NaN in a specific range
    n = 1000
    dist = np.linspace(0, 3000, n)
    channels = dict(synthetic_lap.channels)
    steering = channels["steering"].copy().astype(float)
    # Set steering to NaN in a region
    nan_mask = (dist > 800) & (dist < 1200)
    steering[nan_mask] = np.nan
    channels["steering"] = steering

    lap_with_nan = LapRecord(
        sim=synthetic_lap.sim,
        track=synthetic_lap.track,
        car=synthetic_lap.car,
        lap_time=synthetic_lap.lap_time,
        valid=synthetic_lap.valid,
        timestamp=synthetic_lap.timestamp,
        channels=channels,
    )

    # Query the all-NaN region raises ValueError
    with pytest.raises(ValueError, match="no telemetry in window"):
        compute_direction(lap_with_nan, 800, 1200)


def test_compute_direction_tolerates_partial_nan_via_nanmean(synthetic_lap):
    """Partial NaN samples are skipped; nanmean computes from remaining valid samples."""
    from simcoach.corners import compute_direction

    # Create lap with steering partially NaN (but steering sign still clear in valid samples)
    n = 1000
    dist = np.linspace(0, 3000, n)
    channels = dict(synthetic_lap.channels)
    steering = channels["steering"].copy().astype(float)
    # Set half the samples in the left corner region to NaN
    nan_mask = (dist > 800) & (dist < 1000)
    steering[nan_mask] = np.nan
    channels["steering"] = steering

    lap_with_partial_nan = LapRecord(
        sim=synthetic_lap.sim,
        track=synthetic_lap.track,
        car=synthetic_lap.car,
        lap_time=synthetic_lap.lap_time,
        valid=synthetic_lap.valid,
        timestamp=synthetic_lap.timestamp,
        channels=channels,
    )

    # Should still compute "left" from the remaining negative steering samples
    result = compute_direction(lap_with_partial_nan, 800, 1200)
    assert result == "left"


def test_corner_at_returns_correct_corner():
    """corner_at returns the corner containing a given distance."""
    from simcoach.corners import CornerMap

    cmap = CornerMap.load("simcoach/tracks/test_track.json")

    # Inside corner 1 (800-1200)
    corner = cmap.corner_at(1000)
    assert corner is not None
    assert corner.number == 1
    assert corner.name == "Left Hairpin"

    # Inside corner 2 (2000-2400)
    corner = cmap.corner_at(2200)
    assert corner is not None
    assert corner.number == 2
    assert corner.name == "Right Sweeper"


def test_corner_at_inclusive_boundaries():
    """corner_at includes start and end boundaries."""
    from simcoach.corners import CornerMap

    cmap = CornerMap.load("simcoach/tracks/test_track.json")

    # Start boundary of corner 1
    corner = cmap.corner_at(800)
    assert corner is not None
    assert corner.number == 1

    # End boundary of corner 1
    corner = cmap.corner_at(1200)
    assert corner is not None
    assert corner.number == 1


def test_corner_at_returns_none_for_gap():
    """corner_at returns None when distance falls in a gap between corners."""
    from simcoach.corners import CornerMap

    cmap = CornerMap.load("simcoach/tracks/test_track.json")

    # Gap between corner 1 (ends 1200) and corner 2 (starts 2000)
    corner = cmap.corner_at(1500)
    assert corner is None


def _write_track_json(tmp_path, corners):
    path = tmp_path / "bad_track.json"
    path.write_text(json.dumps({"track": "bad_track", "corners": corners}))
    return str(path)


def test_load_rejects_apex_outside_window(tmp_path):
    """CornerMap.load raises ValueError when apex_dist is outside [start_dist, end_dist]."""
    from simcoach.corners import CornerMap

    corners = [
        {"number": 1, "name": "Bad Apex", "direction": "left",
         "start_dist": 800, "apex_dist": 1300, "end_dist": 1200},
    ]
    path = _write_track_json(tmp_path, corners)

    with pytest.raises(ValueError, match="apex"):
        CornerMap.load(path)


def test_load_accepts_wrap_rejects_degenerate(tmp_path):
    """start_dist > end_dist is a valid S/F-seam WRAP; only start == end is degenerate."""
    from simcoach.corners import CornerMap

    wrap = _write_track_json(tmp_path, [
        {"number": 1, "name": "Seam", "direction": "left",
         "start_dist": 1200, "apex_dist": 1300, "end_dist": 800},  # wraps; apex >= start
    ])
    assert CornerMap.load(wrap).corners[0].start_dist == 1200  # loads fine

    degen = _write_track_json(tmp_path, [
        {"number": 1, "name": "Degen", "direction": "left",
         "start_dist": 900, "apex_dist": 900, "end_dist": 900},
    ])
    with pytest.raises(ValueError, match="degenerate"):
        CornerMap.load(degen)


def test_verify_map_directions_flags_undetermined_corner(synthetic_lap):
    """verify_map_directions marks out-of-range corners as undetermined."""
    from simcoach.corners import Corner, CornerMap, verify_map_directions

    # Create a map with an out-of-range corner
    out_of_range_corner = Corner(
        number=3, name="Out of Range", direction="left",
        start_dist=5000, apex_dist=5100, end_dist=5200
    )
    corners = list(CornerMap.load("simcoach/tracks/test_track.json").corners)
    corners.append(out_of_range_corner)
    cmap = CornerMap(corners)

    mismatches = verify_map_directions(synthetic_lap, cmap)
    # Corner 3 declared "left" but computed "undetermined"
    assert (3, "left", "undetermined") in mismatches


def test_corner_map_carries_notes():
    from simcoach.corners import CornerMap
    data = {
        "corners": [{"number": 1, "name": "T1", "direction": "left",
                     "apex_dist": 50.0, "start_dist": 10.0, "end_dist": 90.0}],
        "notes": {"overview": "rhythm track", "corners": {"1": "brake earlier than it looks"},
                  "source": "LMU wiki"},
    }
    cm = CornerMap.from_data(data)
    assert cm.notes["overview"] == "rhythm track"
    assert cm.notes["corners"]["1"].startswith("brake")


def test_corner_map_notes_default_empty():
    from simcoach.corners import CornerMap
    cm = CornerMap.from_data({"corners": []})
    assert cm.notes == {}


def test_corner_map_carries_baked_geometry():
    # Baked geometry metrics (radius_m/width_m/banking_deg/elev_delta_m) live in
    # the map JSON alongside corner-identity fields but aren't Corner fields --
    # from_data must keep them reachable (issue #35), keyed by corner label.
    from simcoach.corners import CornerMap
    data = {
        "corners": [
            {"number": 1, "name": "T1", "direction": "left", "apex_dist": 50.0,
             "start_dist": 10.0, "end_dist": 90.0,
             "radius_m": 66.0, "width_m": 12.0, "banking_deg": 2.0, "elev_delta_m": -11.0},
            {"number": 2, "name": "T2", "direction": "right", "apex_dist": 150.0,
             "start_dist": 110.0, "end_dist": 190.0},  # no geometry for this one
        ],
    }
    cm = CornerMap.from_data(data)
    assert cm.geometry["1"] == {"radius_m": 66.0, "width_m": 12.0,
                                 "banking_deg": 2.0, "elev_delta_m": -11.0}
    assert "2" not in cm.geometry


def test_corner_map_geometry_default_empty():
    from simcoach.corners import CornerMap
    cm = CornerMap.from_data({"corners": []})
    assert cm.geometry == {}
