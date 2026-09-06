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


def test_reconstruct_surface_stitches_sparse_seam():
    # Two edge passes that hug ±5 in the middle but CONVERGE to ~0 at S/F (the
    # transitional crossing) -> the seam bins are narrow. Closed-loop stitching
    # must refill them from neighbours so the ribbon stays continuous.
    def edge(sign, n=800):
        x = np.linspace(0.0, 400.0, n)
        z = np.where((x >= 12) & (x <= 388), sign * 5.0, 0.0)  # ~3% crossing at S/F
        return {"pos_x": x, "pos_z": z, "lap_distance": x.copy()}

    r = reconstruct_surface([edge(1), edge(-1)], bins=50)
    assert abs(r["width"][25] - 10.0) < 1.0   # mid-lap full width
    assert r["width"][0] > 5.0                # seam bin stitched, not the ~0 crossing
    assert r["width"][-1] > 5.0


def test_reconstruct_surface_keeps_narrow_midlap_corner():
    # Passes spread on the straights but converge to one line in a mid-lap band
    # (a tight corner). That bin is narrow but WELL-SAMPLED and NOT at the seam,
    # so it must stay narrow -- not stitched/widened (that would flatten the apex).
    def pass_(off, n=400):
        x = np.linspace(0.0, 400.0, n)
        z = np.where((x >= 180) & (x <= 220), 0.0, off)  # everyone takes the same corner line
        return {"pos_x": x, "pos_z": z, "lap_distance": x.copy()}

    r = reconstruct_surface([pass_(o) for o in (-4.0, -1.0, 2.0, 5.0)], bins=50)
    # Stitch stays seam-only: the mid narrow bin is NOT widened to the ~9 median
    # (smoothing softens the sharp dip, but it's still clearly narrower).
    assert r["width"][25] < 0.75 * r["width"][10]
    assert r["width"][10] > 5.0   # a straight bin keeps the full weave spread


def _turns_deg(path):
    # Signed per-vertex turn angles (interior only; no S/F wrap pair).
    p = np.array(path, dtype=float)
    v = np.diff(p, axis=0)
    nxt = v[1:]
    cur = v[:-1]
    cross = cur[:, 0] * nxt[:, 1] - cur[:, 1] * nxt[:, 0]
    dot = (cur * nxt).sum(1)
    return np.degrees(np.arctan2(cross, dot))


def test_reconstruct_surface_smooths_weave_straight():
    # Two straight edges pin the envelope at ±5; a lap weaving edge-to-edge between
    # them would zigzag a per-bin-MEAN center, but the midpoint center + smoothing
    # keeps it straight. (This is the squiggle the user hit.)
    # Realistic scale (~1 km straight, 100 bins) so the distance-based smoothing
    # half-window (~40 m) is a small fraction of the loop, as on a real track.
    x = np.linspace(0.0, 1000.0, 400)
    edge_l = {"pos_x": x, "pos_z": np.full(400, 5.0), "lap_distance": x.copy()}
    edge_r = {"pos_x": x, "pos_z": np.full(400, -5.0), "lap_distance": x.copy()}
    # A weaving lap that DRIFTS across the width -- this pulls a per-bin MEAN center
    # off to a fake curve, but the envelope MIDPOINT (from the edges) stays straight.
    weave = {"pos_x": x, "pos_z": np.linspace(-4.0, 4.0, 400), "lap_distance": x.copy()}
    r = reconstruct_surface([edge_l, edge_r, weave], bins=100)
    cz = np.array([p[1] for p in r["center"]])[10:-10]  # interior (wrap ends excluded)
    # Center sits on the TRUE midline (z~0), NOT pulled toward the drifting weave
    # (a per-bin MEAN center would reach ~±1.33 here) -- the midpoint fix.
    assert np.abs(cz).max() < 0.6
    assert np.abs(_turns_deg(r["center"])[10:-10]).mean() < 1.0   # straight stays straight
    assert abs(r["width"][50] - 10.0) < 0.5                       # width = the true spread


def test_reconstruct_surface_edges_from_bound_laps():
    # left-hug + right-hug + a weave that SPIKES past the left bound mid-lap. Edges
    # must come from the BOUND laps (left stays ~-5), not the all-sample envelope
    # (which would grab the -8 spike).
    x = np.linspace(0.0, 400.0, 400)
    left = {"pos_x": x, "pos_z": np.full(400, -5.0), "lap_distance": x.copy()}
    right = {"pos_x": x, "pos_z": np.full(400, 5.0), "lap_distance": x.copy()}
    wz = np.zeros(400); wz[(x >= 200) & (x <= 210)] = -8.0  # ran wide once
    weave = {"pos_x": x, "pos_z": wz, "lap_distance": x.copy()}
    r = reconstruct_surface([left, weave, right], bins=100)
    lz = np.array([p[1] for p in r["left"]])[10:-10]
    assert lz.min() > -6.0                       # bound lap, not the -8 weave spike
    assert abs(r["width"][50] - 10.0) < 1.0      # edges = the two bounds (~10 apart)
    cz = np.array([p[1] for p in r["center"]])[10:-10]
    assert np.abs(cz).max() < 0.6                # center = midpoint of the bounds


def test_reconstruct_surface_preserves_arc_curvature():
    # A constant-radius CLOSED circle must keep its ~360 deg of turning, not be
    # smoothed flat (a real track is a closed loop).
    th = np.linspace(0.0, 2 * np.pi, 400, endpoint=False)
    d = np.linspace(0.0, 2 * np.pi * 100.0, 400, endpoint=False)  # shared arc-length axis
    laps = [{"pos_x": rr * np.cos(th), "pos_z": rr * np.sin(th), "lap_distance": d}
            for rr in (98.0, 100.0, 102.0)]
    r = reconstruct_surface(laps, bins=100)
    total = abs(_turns_deg(r["center"]).sum())
    assert 300.0 < total < 420.0   # curvature preserved (would be ~0 if flattened)


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
    # Convention: positive = RIGHT higher. Here LEFT is higher -> negative.
    expected = np.arctan2(-2.0, 10.0)
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


def test_smooth_curve_preserves_corners_and_cleans_straights():
    # The adaptive smoother must keep a sharp corner sharp (unlike a fixed boxcar,
    # which rounds it) while removing noise on the straights.
    from simcoach.ribbon import _menger, _smooth, _smooth_curve

    rng = np.random.default_rng(0)
    t = np.linspace(0, 1, 60, endpoint=False)[:, None]
    side = lambda a, b: np.array(a) + (np.array(b) - np.array(a)) * t
    pts = np.vstack([side([0, 0], [10, 0]), side([10, 0], [10, 10]),
                     side([10, 10], [0, 10]), side([0, 10], [0, 0])])  # square: 4 sharp 90° corners
    x, z = pts[:, 0].copy(), pts[:, 1].copy()
    xn, zn = x + rng.normal(0, 0.15, x.shape), z + rng.normal(0, 0.15, z.shape)

    sx, sz = _smooth_curve(xn, zn, base_w=8)
    bx, bz = _smooth(xn, 8), _smooth(zn, 8)  # fixed boxcar, for comparison

    assert np.std(sx - x) < np.std(xn - x)               # straights denoised
    assert _menger(sx, sz).max() > _menger(bx, bz).max()  # corners kept sharper than boxcar
