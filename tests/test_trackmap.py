import numpy as np

from simcoach.corners import Corner, CornerMap
from simcoach.trackmap import build_track_map


def _square_channels():
    # A closed square loop, world coords, with a lap-distance axis.
    x = np.array([0.0, 100.0, 100.0, 0.0, 0.0])
    z = np.array([0.0, 0.0, 100.0, 100.0, 0.0])
    dist = np.array([0.0, 100.0, 200.0, 300.0, 400.0])
    return {"pos_x": x, "pos_z": z, "lap_distance": dist}


def test_square_path_normalized_into_box():
    cmap = CornerMap([
        Corner(1, "T1", "right", 50, 100, 150),   # apex nearest dist=100 -> world (100,0)
        Corner(2, "T2", "left", 150, 200, 250),   # apex nearest dist=200 -> world (100,100)
    ])
    m = build_track_map(_square_channels(), cmap, width=1000)

    assert m["width"] == 1000
    assert m["height"] == 1000  # square -> square box
    # Every path point sits inside the box.
    for px, py in m["path"]:
        assert 0 <= px <= m["width"]
        assert 0 <= py <= m["height"]
    # Vertical flip: world (100,0) -> bottom-right; world (100,100) -> top-right.
    assert m["corners"][0] == {"number": 1, "name": "T1", "direction": "right", "x": 1000.0, "y": 1000.0}
    assert m["corners"][1] == {"number": 2, "name": "T2", "direction": "left", "x": 1000.0, "y": 0.0}


def test_empty_corner_map_yields_no_corners():
    m = build_track_map(_square_channels(), CornerMap([]), width=1000)
    assert m["corners"] == []
    assert len(m["path"]) == 5


def test_none_corner_map_still_draws_path():
    m = build_track_map(_square_channels(), None, width=1000)
    assert m["corners"] == []
    assert m["path"]


def test_missing_position_returns_empty():
    assert build_track_map({"lap_distance": np.arange(10)}, CornerMap([])) == {}


def test_degenerate_path_returns_empty():
    ch = {"pos_x": np.zeros(5), "pos_z": np.zeros(5), "lap_distance": np.arange(5)}
    assert build_track_map(ch, CornerMap([])) == {}


def test_too_short_returns_empty():
    ch = {"pos_x": np.array([1.0]), "pos_z": np.array([2.0]), "lap_distance": np.array([0.0])}
    assert build_track_map(ch, CornerMap([])) == {}


def test_downsample_caps_path_length():
    n = 5000
    ang = np.linspace(0, 2 * np.pi, n)
    ch = {"pos_x": 100 * np.cos(ang), "pos_z": 100 * np.sin(ang),
          "lap_distance": np.linspace(0, 1000, n)}
    m = build_track_map(ch, CornerMap([]), width=1000)
    assert len(m["path"]) <= 300
