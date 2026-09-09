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
    assert a["min_speed_kmh"] < 120  # speed dip detected at the apex


def test_detect_corners_min_speed_is_kmh_not_ms():
    """#55: the captured `speed` channel is ALREADY km/h (lmu/smm.py stores
    |mLocalVel| * 3.6), so detect_corners must pass it through unconverted.

    _synthetic_lap dips to 90 and 70 km/h. Band picked so only km/h fits:
    a m/s reading of the same lap would be 25.0/19.4 (below LO), and a spurious
    *3.6 would be 324/252 (above HI).
    """
    LO, HI = 50.0, 150.0
    lap = _synthetic_lap()
    trough = float(np.min(lap["speed"]))               # 70.0 km/h, by construction
    assert trough / 3.6 < LO and trough * 3.6 > HI     # both wrong readings miss the band

    for c in detect_corners(lap):
        assert LO < c["min_speed_kmh"] < HI
        assert "min_speed" not in c                    # unitless key retired


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

    _WIN_M is widened past half the minimum corner gap so two INTERIOR windows
    overlap, which the shipped knobs prevent (_MIN_GAP_M 120 >= 2 * _WIN_M = 110,
    so interior windows always sit >= 10 m apart). The guard is not merely
    defensive, though: detect_corners merges peaks by LINEAR lap distance while
    the clamp is CIRCULAR, so the first and last apexes of a lap are never
    gap-checked against each other and the clamp does fire at the S/F seam with
    the shipped knobs. Lap-end clipping keeps that from ever being an overlap on
    disk -- it was a divergence between the file and CornerMap.load(), which is
    what the self-verify step was reading."""
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


def _seam_corners_lap():
    """A 3000 m lap with a LEFT at ~70 m and a RIGHT at ~2970 m -- 2900 m apart
    LINEARLY (detect_corners' merge check never fuses them) but only 100 m apart
    CIRCULARLY across the S/F seam, closer than _MIN_GAP_M (120 m) even with
    unmodified shipped knobs."""
    dist = np.linspace(0, 3000, 3001)
    left = np.exp(-(((dist - 70) / 15) ** 2))
    right = np.exp(-(((dist - 2970) / 15) ** 2))
    steer = -0.5 * left + 0.5 * right          # RIGHT_IS_POSITIVE=True
    speed = 200 - 110 * left - 130 * right
    return {"lap_distance": dist, "speed": speed, "steering": steer}


def test_build_map_clamp_fires_at_the_seam_with_shipped_knobs():
    """#30 follow-up: the write-time clamp is not a no-op with the shipped knobs
    (_MIN_GAP_M=120, _WIN_M=55). detect_corners merges by LINEAR lap distance
    while clamp_corner_windows is CIRCULAR, so the first and last apexes of a lap
    are never gap-checked against each other by detect_corners -- and here they
    sit only 100 m apart across the seam, under _MIN_GAP_M, so the clamp trims
    the first corner's start_dist inward even with unmodified constants."""
    ch = _seam_corners_lap()
    detected = detect_corners(ch)
    assert len(detected) == 2  # 2900 m linear gap: nowhere near merged

    obj = build_map("seam", ch, length_m=3000)
    by_apex = sorted(obj["corners"], key=lambda c: c["apex_dist"])
    first = by_apex[0]

    # what build_map would have written with NO clamp (pre-#30-fix behaviour)
    unclamped_start = round(max(0.0, first["apex_dist"] - mapbuild._WIN_M), 1)
    assert first["start_dist"] != unclamped_start, (
        "clamp did not fire at the seam -- with unmodified shipped knobs it should"
    )
    assert (first["apex_dist"], first["start_dist"], first["end_dist"]) == (70.0, 20.0, 125.0)


def test_build_map_is_valid_and_direction_verified(tmp_path):
    ch = _synthetic_lap()
    obj = build_map("synthetic", ch, length_m=1000)
    p = tmp_path / "synthetic.json"
    p.write_text(json.dumps(obj))

    m = CornerMap.load(str(p))          # raises on a bad window
    assert len(m.corners) == 2
    rec = SimpleNamespace(channels=ch)
    assert verify_map_directions(rec, m) == []   # directions match telemetry
