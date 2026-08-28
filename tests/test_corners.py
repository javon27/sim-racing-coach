import pytest
import json
import tempfile
from pathlib import Path
import numpy as np
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
    from simcoach.corners import CornerMap, verify_map_directions, Corner

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

    with pytest.raises(ValueError, match="apex_dist"):
        CornerMap.load(path)


def test_load_rejects_start_ge_end(tmp_path):
    """CornerMap.load raises ValueError when start_dist >= end_dist (wrapped/degenerate window)."""
    from simcoach.corners import CornerMap

    corners = [
        {"number": 1, "name": "Wrapped", "direction": "left",
         "start_dist": 1200, "apex_dist": 1000, "end_dist": 800},
    ]
    path = _write_track_json(tmp_path, corners)

    with pytest.raises(ValueError, match="start_dist"):
        CornerMap.load(path)


def test_verify_map_directions_flags_undetermined_corner(synthetic_lap):
    """verify_map_directions marks out-of-range corners as undetermined."""
    from simcoach.corners import CornerMap, verify_map_directions, Corner

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
