import numpy as np
import pytest

from simcoach.corners import Corner, CornerMap
from simcoach.trackmap import (
    average_racing_line,
    build_track_map,
    direction_from_path,
)


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
    assert m["corners"][0] == {"number": 1, "name": "T1", "direction": "right",
                               "x": 1000.0, "y": 1000.0,
                               "apex_dist": 100.0, "start_dist": 50.0, "end_dist": 150.0}
    assert m["corners"][1] == {"number": 2, "name": "T2", "direction": "left",
                               "x": 1000.0, "y": 0.0,
                               "apex_dist": 200.0, "start_dist": 150.0, "end_dist": 250.0}


def test_dist_parallel_to_path():
    m = build_track_map(_square_channels(), CornerMap([]), width=1000)
    assert len(m["dist"]) == len(m["path"])
    assert m["dist"][0] == 0.0
    assert m["dist"][-1] == 400.0


def test_sectors_passthrough():
    m = build_track_map(_square_channels(), CornerMap([]), width=1000, sectors=[100.0, 250.0])
    assert m["sectors"] == [100.0, 250.0]
    # No sectors -> empty list, not missing key.
    assert build_track_map(_square_channels(), CornerMap([]))["sectors"] == []


def test_average_racing_line_offset_laps_to_midline():
    # Two laps identical but offset by a constant +10 in x -> midline is +5.
    base_x = np.array([0.0, 10.0, 20.0, 30.0])
    z = np.array([0.0, 5.0, 5.0, 0.0])
    d = np.array([0.0, 100.0, 200.0, 300.0])
    lap_a = {"pos_x": base_x, "pos_z": z, "lap_distance": d}
    lap_b = {"pos_x": base_x + 10.0, "pos_z": z, "lap_distance": d}
    path, dist = average_racing_line([lap_a, lap_b], bins=4)
    xs = [p[0] for p in path]
    assert xs == pytest.approx([5.0, 15.0, 25.0, 35.0])
    assert dist[0] == 0.0 and dist[-1] == 300.0


def test_average_racing_line_one_lap_is_resample():
    d = np.array([0.0, 100.0, 200.0])
    lap = {"pos_x": np.array([0.0, 2.0, 4.0]), "pos_z": np.array([0.0, 1.0, 2.0]),
           "lap_distance": d}
    path, dist = average_racing_line([lap], bins=3)
    assert [p[0] for p in path] == pytest.approx([0.0, 2.0, 4.0])
    assert [p[1] for p in path] == pytest.approx([0.0, 1.0, 2.0])


def test_average_racing_line_skips_bad_laps():
    good = {"pos_x": np.array([0.0, 1.0]), "pos_z": np.array([0.0, 1.0]),
            "lap_distance": np.array([0.0, 100.0])}
    no_pos = {"lap_distance": np.array([0.0, 100.0])}
    assert average_racing_line([no_pos]) == ([], [])
    path, _ = average_racing_line([good, no_pos])
    assert path  # the good lap alone still averages


def test_direction_synthetic_left_right():
    # Pins the world-handedness constant. Path goes +x then turns toward +z:
    # counter-clockwise = LEFT; the mirror turns RIGHT.
    dist = [0.0, 10.0, 20.0, 30.0, 40.0]
    left = [[0.0, 0.0], [10.0, 0.0], [20.0, 0.0], [20.0, 10.0], [20.0, 20.0]]
    right = [[0.0, 0.0], [10.0, 0.0], [20.0, 0.0], [20.0, -10.0], [20.0, -20.0]]
    assert direction_from_path(left, dist, apex_dist=20.0, k=2) == "left"
    assert direction_from_path(right, dist, apex_dist=20.0, k=2) == "right"


def test_direction_too_short_raises():
    with pytest.raises(ValueError):
        direction_from_path([[0.0, 0.0], [1.0, 1.0]], [0.0, 1.0], apex_dist=0.5)


def test_direction_monza_real():
    """Calibrate direction_from_path against Monza's 10 telemetry-verified
    directions. SKIPS until a captured Monza lap carries pos_x/pos_z -- no stored
    lap in this repo does yet (position capture via smm.py is a parallel lane;
    the DB + scratchpad captures hold only speed/steering/lat_g/lap_distance).
    When a real Monza pos lap exists, this pins _LEFT_TURN_IS_CROSS_POSITIVE to
    LMU's actual world frame; if it fails, flip that constant."""
    import glob
    import json
    from pathlib import Path

    candidates = glob.glob("scratchpad_live/**/*monza*naz*.npz", recursive=True) + \
        glob.glob("scratchpad_live/**/autodromo_nazionale_monza*.npz", recursive=True)
    lap = None
    for f in candidates:
        z = np.load(f)
        if "pos_x" in z.files and "pos_z" in z.files:
            lap = {"pos_x": z["pos_x"], "pos_z": z["pos_z"], "lap_distance": z["lap_distance"]}
            break
    if lap is None:
        pytest.skip("no captured Monza lap with pos_x/pos_z yet -- direction "
                    "constant unconfirmed against real LMU world coords")

    path, dist = average_racing_line([lap])
    corners = json.loads(Path("simcoach/tracks/autodromo_nazionale_monza.json").read_text())["corners"]
    mismatches = [
        (c["number"], c["direction"], direction_from_path(path, dist, c["apex_dist"]))
        for c in corners
        if direction_from_path(path, dist, c["apex_dist"]) != c["direction"]
    ]
    assert not mismatches, f"direction mismatches (corner, expected, got): {mismatches}"


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
