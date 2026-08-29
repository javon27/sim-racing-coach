"""mapbuild detects corners and emits a valid, direction-verified map from telemetry."""
from types import SimpleNamespace

import numpy as np

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


def test_build_map_is_valid_and_direction_verified(tmp_path):
    ch = _synthetic_lap()
    obj = build_map("synthetic", ch, length_m=1000)
    p = tmp_path / "synthetic.json"
    import json
    p.write_text(json.dumps(obj))

    m = CornerMap.load(str(p))          # raises on a bad window
    assert len(m.corners) == 2
    rec = SimpleNamespace(channels=ch)
    assert verify_map_directions(rec, m) == []   # directions match telemetry
