import glob
import json

import numpy as np

from simcoach.analysis import analyze_lap
from simcoach.corners import Corner, CornerMap
from simcoach.model_types import LapRecord


def _write(tmp_path, data):
    p = tmp_path / "t.json"
    p.write_text(json.dumps(data))
    return str(p)


def test_label_defaults_to_number_and_round_trips_8a(tmp_path):
    path = _write(tmp_path, {"corners": [
        {"number": 8, "name": "Corkscrew", "direction": "left",
         "start_dist": 100, "apex_dist": 120, "end_dist": 160},  # no label -> "8"
        {"number": 9, "label": "8A", "name": "Corkscrew 2", "direction": "right",
         "start_dist": 160, "apex_dist": 180, "end_dist": 220},
    ]})
    cm = CornerMap.load(path)
    assert cm.corners[0].label == "8"      # defaulted from number
    assert cm.corners[1].label == "8A"     # explicit sub-letter survives


def test_apexes_defaults_and_round_trips_two(tmp_path):
    path = _write(tmp_path, {"corners": [
        {"number": 1, "name": "T1", "direction": "left",
         "start_dist": 0, "apex_dist": 50, "end_dist": 100},                       # -> [50]
        {"number": 2, "name": "Andretti", "direction": "left", "apexes": [210, 260],
         "start_dist": 200, "apex_dist": 210, "end_dist": 300},
    ]})
    cm = CornerMap.load(path)
    assert cm.corners[0].apexes == [50.0]
    assert cm.corners[1].apexes == [210, 260]


def test_sections_load_and_validate(tmp_path):
    good = _write(tmp_path, {"corners": [], "length": 1000,
                             "sections": [{"name": "The Corkscrew", "kind": "complex",
                                           "start_dist": 100, "end_dist": 260}]})
    cm = CornerMap.load(good)
    assert cm.sections[0]["name"] == "The Corkscrew" and cm.sections[0]["kind"] == "complex"

    bad = _write(tmp_path, {"corners": [], "length": 1000,
                            "sections": [{"name": "x", "start_dist": 500, "end_dist": 400}]})
    try:
        CornerMap.load(bad)
        assert False, "expected ValueError for start>=end"
    except ValueError:
        pass


def test_all_shipped_track_files_still_load():
    files = glob.glob("simcoach/tracks/*.json")
    assert len(files) >= 16
    for f in files:
        CornerMap.load(f)  # must not raise (back-compat)


def test_analysis_multi_apex_per_apex_min_speeds():
    # Speed dips at ~210 and ~260 (two apexes), rises between them.
    dist = np.linspace(0, 400, 401)
    speed = np.full(401, 200.0)
    speed[(dist > 190) & (dist < 230)] = 90.0    # first apex ~210
    speed[(dist > 245) & (dist < 275)] = 70.0    # second apex ~260 (slower)
    rec = LapRecord("lmu", "T", "C", 90.0, True, 0.0, {
        "lap_distance": dist, "speed": speed, "brake": np.zeros(401),
        "throttle": np.ones(401), "steering": np.full(401, -0.3),
    })
    cm = CornerMap([Corner(2, "Andretti", "left", 180, 210, 300, apexes=[210, 260])],
                   sections=[{"name": "Infield", "kind": "complex",
                              "start_dist": 180, "end_dist": 300}])
    a = analyze_lap(rec, cm)
    corner = a.corners[0]
    assert corner.apexes == [210, 260]
    assert len(corner.apex_min_speeds) == 2
    assert abs(corner.apex_min_speeds[0] - 90.0) < 1e-6
    assert abs(corner.apex_min_speeds[1] - 70.0) < 1e-6
    assert corner.section == "Infield"
