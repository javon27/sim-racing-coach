import numpy as np
import pytest

from simcoach.ribbon import (
    multi_signal_direction,
    reconstruct_ribbon,
    reconstruct_surface,
    ribbon_to_screen,
)


def _pass(zoff, yconst=None, n=200):
    # A straight free-driving pass along +x at lateral offset zoff (+ optional height).
    x = np.linspace(0.0, 400.0, n)
    ch = {"pos_x": x, "pos_z": np.full(n, zoff), "lap_distance": x.copy()}
    if yconst is not None:
        ch["pos_y"] = np.full(n, yconst)
    return ch


def test_reconstruct_surface_straight_envelope():
    # Several passes spread across a straight -> flat center, width = spread, bank~0.
    laps = [_pass(z, 0.0) for z in (-4.0, -1.0, 2.0, 5.0)]
    r = reconstruct_surface(laps, bins=50)
    # centerline ~ mean offset ((-4-1+2+5)/4 = 0.5); width ~ spread (5 - -4 = 9).
    mid = r["width"][len(r["width"]) // 2]
    assert abs(mid - 9.0) < 0.5
    assert all(abs(b) < 1e-6 for b in r["bank"])          # flat -> no bank
    assert len(r["left"]) == len(r["right"]) == len(r["center"]) == 50


def test_reconstruct_surface_bank_from_higher_outer_edge():
    # Outer edge (positive lateral offset) is higher -> non-zero bank from the fit.
    laps = [_pass(-4.0, 0.0), _pass(0.0, 0.5), _pass(4.0, 1.0)]  # y rises with +offset
    r = reconstruct_surface(laps, bins=50)
    mid = len(r["bank"]) // 2
    assert abs(r["bank"][mid]) > 0.05      # detects the cross-slope


def test_reconstruct_surface_single_pass_no_crash():
    r = reconstruct_surface([_pass(0.0, 0.0)], bins=50)
    assert r["center"] and all(w == 0.0 for w in r["width"])  # degenerate width, no crash


def test_reconstruct_surface_empty_is_empty():
    assert reconstruct_surface([{"lap_distance": np.arange(3.0)}]) == {}


def _edge(zconst, dist_max):
    # A straight edge along +x at constant z, over its own lap_distance range.
    return {
        "pos_x": np.array([0.0, 50.0, 100.0]),
        "pos_z": np.array([zconst, zconst, zconst]),
        "lap_distance": np.array([0.0, dist_max / 2, dist_max]),
    }


def test_reconstruct_ribbon_straight_center_constant_width():
    ribbon = reconstruct_ribbon(_edge(5.0, 100.0), _edge(-5.0, 100.0), bins=10)
    assert [round(p[1], 3) for p in ribbon["center"]] == [0.0] * 10  # midline z=0
    assert all(abs(w - 10.0) < 1e-6 for w in ribbon["width"])       # constant 10m
    assert ribbon["dist"][0] == 0.0
    assert abs(ribbon["dist"][-1] - 100.0) < 1e-6                    # arc length


def test_reconstruct_ribbon_pairs_by_progress_not_distance():
    # Same geometry, but the right edge's lap is TWICE as long (slower lap):
    # progress-pairing must still center it, raw-distance pairing would not.
    ribbon = reconstruct_ribbon(_edge(5.0, 100.0), _edge(-5.0, 200.0), bins=10)
    assert [round(p[1], 3) for p in ribbon["center"]] == [0.0] * 10
    assert all(abs(w - 10.0) < 1e-6 for w in ribbon["width"])


def test_reconstruct_ribbon_bank_from_elevation():
    # Left edge 2m higher than right, 10m apart -> bank atan(2/10) at each station.
    left = {**_edge(5.0, 100.0), "pos_y": np.full(3, 2.0)}
    right = {**_edge(-5.0, 100.0), "pos_y": np.zeros(3)}
    ribbon = reconstruct_ribbon(left, right, bins=5)
    expected = np.arctan2(2.0, 10.0)
    assert all(abs(b - expected) < 1e-3 for b in ribbon["bank"])  # bank stored at 4 dp


def test_reconstruct_ribbon_flat_when_no_elevation():
    ribbon = reconstruct_ribbon(_edge(5.0, 100.0), _edge(-5.0, 100.0), bins=5)
    assert ribbon["bank"] == [0.0] * 5  # no pos_y -> flat


def test_reconstruct_ribbon_nearest_point_pairing():
    # Two parallel straight edges 2m apart (true width 2). The right edge's
    # lap_distance is DISTORTED (a huge last segment) so equal-progress pairing
    # would cut a diagonal and inflate width; nearest-point pairing gives ~2.
    left = {
        "pos_x": np.linspace(0.0, 10.0, 11), "pos_z": np.zeros(11),
        "lap_distance": np.linspace(0.0, 10.0, 11),
    }
    right = {
        "pos_x": np.linspace(0.0, 10.0, 11), "pos_z": np.full(11, 2.0),
        "lap_distance": np.array([0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 100.0]),
    }
    ribbon = reconstruct_ribbon(left, right)  # default bins: dense enough to pair
    assert all(abs(w - 2.0) < 0.2 for w in ribbon["width"])            # ~true width
    assert all(abs(p[1] - 1.0) < 0.2 for p in ribbon["center"])       # midline z~1


def test_reconstruct_ribbon_missing_pos_is_empty():
    assert reconstruct_ribbon({"lap_distance": np.arange(3.0)}, _edge(-5.0, 100.0)) == {}


# A CCW quarter-circle centerline = a LEFT turn, dense enough that the default
# k=3 curvature window stays local (a tiny path would wrap the closed loop).
_ANG = np.linspace(0.0, np.pi / 2, 40)
_LEFT_CENTER = [[float(20 * np.sin(a)), float(20 * (1 - np.cos(a)))] for a in _ANG]
_DIST = list(np.linspace(0.0, 40.0, 40))
_N = len(_DIST)


def _edge_signals(steer_sign):
    return {
        "lap_distance": np.array(_DIST),
        "steering": np.full(_N, steer_sign * 0.4),  # >0 right, <0 left (RIGHT_IS_POSITIVE)
        "lat_g": np.full(_N, 1.0),                   # >0 -> left
    }


def test_multi_signal_direction_agreement_is_high():
    # Curvature says left; steering (neg = left) + lat_g (pos = left) agree.
    d, conf = multi_signal_direction(_LEFT_CENTER, _DIST, 20.0,
                                     _edge_signals(-1), lap_length=40.0, half=0.3)
    assert d == "left" and conf == "high"


def test_multi_signal_direction_disagreement_is_low():
    # Curvature still left, but steering now positive (=right) -> flag low.
    d, conf = multi_signal_direction(_LEFT_CENTER, _DIST, 20.0,
                                     _edge_signals(+1), lap_length=40.0, half=0.3)
    assert d == "left" and conf == "low"


def test_multi_signal_direction_no_edge_signals_is_high():
    d, conf = multi_signal_direction(_LEFT_CENTER, _DIST, 20.0)
    assert d == "left" and conf == "high"


def test_ribbon_to_screen_shape_and_corner_placement():
    ribbon = reconstruct_ribbon(_edge(5.0, 100.0), _edge(-5.0, 100.0), bins=10)
    corners = [{"number": 1, "name": "T1", "direction": "left",
                "apex_dist": 50.0, "start_dist": 40.0, "end_dist": 60.0}]
    screen = ribbon_to_screen(ribbon, corners, sectors=[33.0])
    for k in ("width", "height", "left", "right", "center", "corners", "sectors"):
        assert k in screen
    assert len(screen["center"]) == len(ribbon["center"])
    assert "x" in screen["corners"][0] and "y" in screen["corners"][0]
    assert screen["sectors"] == [33.0]


def test_ribbon_to_screen_rotation_swaps_bounds():
    # Elongated ribbon along +x; 90 deg rotation swaps the fitted box and stays
    # aligned/clip-free.
    ribbon = reconstruct_ribbon(_edge(5.0, 100.0), _edge(-5.0, 100.0))
    flat = ribbon_to_screen(ribbon, [])
    rot = ribbon_to_screen(ribbon, [], rotation=90)
    assert flat["rotation"] == 0.0 and rot["rotation"] == 90.0
    assert flat["width"] > flat["height"]        # wide unrotated
    assert rot["height"] > rot["width"]          # tall after 90 deg
    for x, y in rot["center"]:
        assert 0 <= x <= rot["width"] and 0 <= y <= rot["height"]


def test_ribbon_to_screen_empty_ribbon():
    assert ribbon_to_screen({}, []) == {}
