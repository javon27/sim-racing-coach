"""S/F seam-wrapping corners: start_dist > end_dist spans [start, len] u [0, end]."""
import json

import numpy as np
import pytest

from simcoach.analysis import analyze_lap
from simcoach.corners import Corner, CornerMap, compute_direction, in_window
from simcoach.format import format_corner_line
from simcoach.model_types import LapRecord

# A corner sitting on the S/F line: window wraps 4800 -> 100, apex just before S/F.
_WRAP = Corner(1, "Start Kink", "left", start_dist=4800.0, apex_dist=4950.0, end_dist=100.0)


def test_corner_at_wraps_seam():
    cm = CornerMap([_WRAP])
    assert cm.corner_at(4850.0) is _WRAP   # just before S/F
    assert cm.corner_at(50.0) is _WRAP     # just after S/F
    assert cm.corner_at(4950.0) is _WRAP   # the apex
    assert cm.corner_at(2000.0) is None    # mid-lap, outside


def test_compute_direction_over_wrapped_window():
    rec = LapRecord("lmu", "T", "C", 90.0, True, 0.0, {
        "lap_distance": np.array([50.0, 2500.0, 4900.0]),
        "steering": np.array([-0.5, 0.0, -0.5]),   # both in-window samples steer left
    })
    assert compute_direction(rec, 4800.0, 100.0) == "left"


def test_analyze_lap_metrics_for_seam_corner():
    dist = np.array([10.0, 60.0, 90.0, 2000.0, 4850.0, 4950.0, 4990.0])
    speed = np.array([100.0, 40.0, 100.0, 200.0, 100.0, 30.0, 100.0])  # min 30 @ apex 4950
    rec = LapRecord("lmu", "T", "C", 90.0, True, 0.0, {
        "lap_distance": dist, "speed": speed,
        "brake": np.zeros(7), "throttle": np.ones(7), "steering": np.full(7, -0.4),
    })
    a = analyze_lap(rec, CornerMap([_WRAP]))
    cm = a.corners[0]
    assert cm.min_spd == 30.0            # min taken across BOTH sides of the seam
    assert not np.isnan(cm.entry_spd)    # window wasn't empty (would be nan on a bad mask)
    assert cm.direction == "left"


def test_brake_point_reaches_back_across_the_seam():
    """Issue #7: apex just AFTER S/F, braking just BEFORE it. The approach look-back
    has to wrap, and the brake point has to come back on the apex's side of the seam
    so every consumer's `apex_dist - brake_point` stays a positive metres-before-apex."""
    corner = Corner(1, "Seam", "right", start_dist=4900.0, apex_dist=60.0, end_dist=200.0)
    dist = np.arange(0.0, 5000.0, 10.0)
    brake = np.where((dist >= 4750.0) & (dist <= 4950.0), 1.0, 0.0)  # released before S/F
    speed = np.where(brake > 0, 80.0, 200.0)
    rec = LapRecord("lmu", "T", "C", 90.0, True, 0.0, {
        "lap_distance": dist, "speed": speed, "brake": brake,
        "throttle": 1.0 - brake, "steering": np.full(dist.size, 0.4),
    })
    cm = analyze_lap(rec, CornerMap([corner], length=5000.0)).corners[0]
    assert cm.brake_point == 4750.0 - 5000.0        # unwrapped onto the apex's side
    assert cm.apex_dist - cm.brake_point == 310.0   # a real "310m before apex" figure
    assert "brake 310m before apex" in format_corner_line(cm)


def test_approach_look_back_crosses_the_seam_for_a_corner_starting_at_zero():
    """The same defect's live half: a window that does NOT wrap but starts at S/F
    (Imola's T1 is [0.0, 200.9]). Its braking happens before the line, so the 250 m
    approach look-back has to wrap too instead of clamping at lap_distance 0."""
    corner = Corner(1, "Tamburello", "right", start_dist=0.0, apex_dist=60.0, end_dist=200.0)
    dist = np.arange(0.0, 5000.0, 10.0)
    brake = np.where((dist >= 4800.0) & (dist <= 4950.0), 1.0, 0.0)  # all before S/F
    speed = np.where(brake > 0, 80.0, 200.0)
    rec = LapRecord("lmu", "T", "C", 90.0, True, 0.0, {
        "lap_distance": dist, "speed": speed, "brake": brake,
        "throttle": 1.0 - brake, "steering": np.full(dist.size, 0.4),
    })
    cm = analyze_lap(rec, CornerMap([corner], length=5000.0)).corners[0]
    assert cm.brake_point == 4800.0 - 5000.0
    assert cm.apex_dist - cm.brake_point == 260.0


def _write(tmp_path, corners):
    p = tmp_path / "t.json"
    p.write_text(json.dumps({"corners": corners, "length": 5000}))
    return str(p)


def test_load_accepts_wrap_rejects_degenerate_and_bad_apex(tmp_path):
    ok = _write(tmp_path, [{"number": 1, "name": "K", "direction": "left",
                            "start_dist": 4800, "apex_dist": 4950, "end_dist": 100}])
    assert CornerMap.load(ok).corners[0].apex_dist == 4950  # wrap loads

    degen = _write(tmp_path, [{"number": 1, "name": "K", "direction": "left",
                               "start_dist": 500, "apex_dist": 500, "end_dist": 500}])
    with pytest.raises(ValueError):
        CornerMap.load(degen)

    bad_apex = _write(tmp_path, [{"number": 1, "name": "K", "direction": "left",
                                  "start_dist": 4800, "apex_dist": 2000, "end_dist": 100}])
    with pytest.raises(ValueError):
        CornerMap.load(bad_apex)  # 2000 is in neither [4800,len] nor [0,100]


def test_in_window_scalar_and_array():
    assert in_window(4850.0, 4800.0, 100.0)
    assert in_window(50.0, 4800.0, 100.0)
    assert not in_window(2000.0, 4800.0, 100.0)
    m = in_window(np.array([50.0, 2000.0, 4900.0]), 4800.0, 100.0)
    assert list(m) == [True, False, True]
    # normal (non-wrap) window unchanged
    assert in_window(150.0, 100.0, 200.0) and not in_window(50.0, 100.0, 200.0)
