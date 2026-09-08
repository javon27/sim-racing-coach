# tests/test_corner_geometry.py
import numpy as np

from simcoach.analysis import corner_geometry, geometry_clause


def _circle(R, n=360):
    # A full circle of radius R: curvature is 1/R everywhere; dist = arc length.
    t = np.linspace(0.0, 2 * np.pi, n, endpoint=False)
    path = np.stack([R * np.cos(t), R * np.sin(t)], axis=1).tolist()
    dist = (R * t).tolist()
    return path, dist


def test_radius_from_path_curvature():
    path, dist = _circle(50.0)
    corners = [{"label": "1", "start_dist": 10.0, "apex_dist": 20.0, "end_dist": 40.0}]
    g = corner_geometry(path, dist, corners)
    assert abs(g["1"]["radius_m"] - 50.0) < 2.0     # recovers the circle's radius
    assert "width_m" not in g["1"]                  # no source -> omitted, never faked
    assert "banking_deg" not in g["1"]
    assert "elev_delta_m" not in g["1"]


def test_width_bank_elevation_sampled():
    path, dist = _circle(50.0)
    n = len(path)
    width = [12.0] * n
    bank = [np.radians(3.0)] * n                    # ribbon bank is radians
    elev = [d * 0.01 for d in dist]                 # +1 cm per m -> gentle uphill
    corners = [{"label": "1", "start_dist": 0.0, "apex_dist": 50.0, "end_dist": 100.0}]
    g = corner_geometry(path, dist, corners, width=width, bank=bank, elev=elev)
    assert abs(g["1"]["width_m"] - 12.0) < 1e-6
    assert abs(g["1"]["banking_deg"] - 3.0) < 0.1   # converted to degrees
    assert abs(g["1"]["elev_delta_m"] - 1.0) < 0.1  # ~+1 m over the 100 m window (uphill)


def test_downhill_is_negative_delta():
    path, dist = _circle(50.0)
    elev = [-d * 0.02 for d in dist]                # drops 2 cm per m
    corners = [{"label": "3", "start_dist": 0.0, "apex_dist": 50.0, "end_dist": 100.0}]
    g = corner_geometry(path, dist, corners, elev=elev)
    assert g["3"]["elev_delta_m"] < 0              # downhill


def test_straight_has_no_meaningful_radius():
    path = [[float(i), 0.0] for i in range(200)]
    dist = [float(i) for i in range(200)]
    corners = [{"label": "1", "start_dist": 10.0, "apex_dist": 50.0, "end_dist": 90.0}]
    g = corner_geometry(path, dist, corners)
    assert "radius_m" not in g["1"]                 # ~zero curvature -> not a corner radius


def test_wraparound_window():
    # A corner whose window straddles the start/finish seam (start_dist > end_dist).
    path, dist = _circle(40.0)
    total = dist[-1] + (dist[1] - dist[0])
    corners = [{"label": "1", "start_dist": total - 20.0, "apex_dist": 0.0, "end_dist": 20.0}]
    g = corner_geometry(path, dist, corners)
    assert abs(g["1"]["radius_m"] - 40.0) < 3.0     # still recovers the radius across the seam


def test_geometry_clause_renders_present_metrics_only():
    c = geometry_clause({"radius_m": 35.0, "elev_delta_m": -6.0, "width_m": 12.0, "banking_deg": 2.0})
    assert "radius ~35m" in c
    assert "downhill 6m" in c
    assert "width 12m" in c
    assert "banking" in c
    assert geometry_clause({}) == ""                # nothing supplied -> empty
    up = geometry_clause({"elev_delta_m": 4.0, "banking_deg": 0.4})
    assert "uphill 4m" in up and "banking" not in up  # sign + sub-threshold dropped
