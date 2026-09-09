"""mapbuild detects corners and emits a valid, direction-verified map from telemetry."""
import itertools
import json
from types import SimpleNamespace

import numpy as np

from simcoach import mapbuild
from simcoach.corners import CornerMap, verify_map_directions
from simcoach.mapbuild import build_map, detect_corners


def _synthetic_lap():
    """A 1000 m lap with a LEFT at ~300 m and a RIGHT at ~700 m (Gaussian corners)."""
    dist = np.linspace(0, 1000, 600)
    left = np.exp(-(((dist - 300) / 40) ** 2))
    right = np.exp(-(((dist - 700) / 40) ** 2))
    steer = -0.5 * left + 0.5 * right          # RIGHT_IS_POSITIVE=True
    speed = 200 - 110 * left - 130 * right      # dips at each corner
    return {"lap_distance": dist, "speed": speed, "steering": steer}


def test_detect_finds_both_corners_with_direction():
    corners = detect_corners(_synthetic_lap())
    assert len(corners) == 2
    a, b = corners
    assert 260 < a["apex_dist"] < 340 and a["direction"] == "left"
    assert 660 < b["apex_dist"] < 740 and b["direction"] == "right"
    assert a["min_speed"] < 120  # speed dip detected at the apex


def _close_corners_lap():
    """A 1000 m lap with a LEFT at ~300 m and a RIGHT at ~430 m -- far enough apart to
    detect as two corners (> _MIN_GAP_M) but close enough that a wide half-width
    window would overlap."""
    dist = np.linspace(0, 1000, 600)
    left = np.exp(-(((dist - 300) / 20) ** 2))
    right = np.exp(-(((dist - 430) / 20) ** 2))
    steer = -0.5 * left + 0.5 * right          # RIGHT_IS_POSITIVE=True
    speed = 200 - 110 * left - 130 * right
    return {"lap_distance": dist, "speed": speed, "steering": steer}


def test_build_map_clamps_windows_so_the_file_matches_what_load_verifies(tmp_path, monkeypatch):
    """#30: build_map wrote a fixed half-width window around each apex with no
    neighbour clamp, while the self-verify step ran verify_map_directions on
    CornerMap.load() of the file -- i.e. on CLAMPED windows. So the map QA never
    looked at the windows that actually landed on disk. build_map now applies the
    same clamp before serialising, making the two agree by construction.

    _WIN_M is widened past half the minimum corner gap so the unclamped windows
    genuinely overlap: with the shipped knobs (_MIN_GAP_M 120 >= 2 * _WIN_M 55) they
    happen not to, which is exactly the accident this removes."""
    monkeypatch.setattr(mapbuild, "_WIN_M", 80.0)
    ch = _close_corners_lap()
    obj = build_map("close", ch, length_m=1000)
    assert len(obj["corners"]) == 2

    by_apex = sorted(obj["corners"], key=lambda c: c["apex_dist"])
    for prev, cur in itertools.pairwise(by_apex):
        assert prev["end_dist"] <= cur["start_dist"], (
            f"written window {prev['end_dist']} overlaps {cur['start_dist']}"
        )

    p = tmp_path / "close.json"
    p.write_text(json.dumps(obj))
    loaded = CornerMap.load(str(p))
    assert [(c.start_dist, c.end_dist) for c in loaded.corners] == \
           [(c["start_dist"], c["end_dist"]) for c in obj["corners"]], \
           "the load-time clamp still moved the windows: the file is not what gets verified"


def test_build_map_is_valid_and_direction_verified(tmp_path):
    ch = _synthetic_lap()
    obj = build_map("synthetic", ch, length_m=1000)
    p = tmp_path / "synthetic.json"
    p.write_text(json.dumps(obj))

    m = CornerMap.load(str(p))          # raises on a bad window
    assert len(m.corners) == 2
    rec = SimpleNamespace(channels=ch)
    assert verify_map_directions(rec, m) == []   # directions match telemetry
