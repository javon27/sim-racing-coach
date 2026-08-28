import pytest


def test_computed_direction_matches_geometry(synthetic_lap):
    from simcoach.corners import compute_direction
    assert compute_direction(synthetic_lap, 800, 1200) == "left"
    assert compute_direction(synthetic_lap, 2000, 2400) == "right"


def test_map_directions_consistent(synthetic_lap):
    from simcoach.corners import CornerMap, verify_map_directions
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    assert verify_map_directions(synthetic_lap, cmap) == []
