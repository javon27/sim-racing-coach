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


def test_reconstruct_surface_bank_from_cross_slope():
    # A cross-slope across the width -> non-zero bank from the fit. Magnitude only:
    # these are straight passes, so there is no inside/outside to call it banked or
    # off-camber, and the +offset side is the driver's LEFT (#58), not an "outer" edge.
    laps = [_pass(-4.0, 0.0), _pass(0.0, 0.5), _pass(4.0, 1.0)]  # y rises with +offset
    r = reconstruct_surface(laps, bins=50)
    mid = len(r["bank"]) // 2
    assert abs(r["bank"][mid]) > 0.05      # detects the cross-slope


def _sep(r):
    # Drawn edge separation per bin -- the corridor, independent of the width GATE.
    return np.hypot(*(np.array(r["left"]) - np.array(r["right"])).T)


def test_reconstruct_surface_single_pass_no_crash():
    r = reconstruct_surface([_pass(0.0, 0.0)], bins=50)
    # One line spans no width: UNKNOWN, not a measured 0 m (#69). No crash either.
    assert r["center"] and all(w is None for w in r["width"])
    assert all(b is None for b in r["bank"])


def test_same_line_laps_give_unknown_width_and_bank():
    # #69: real racing laps sit within ~1 m of one line and their pos_y differs by a few
    # cm lap to lap. The old >1e-6 m test fitted a slope across that 1 m -- tens of
    # degrees of "banking" -- and wrote the 1 m envelope as the track width.
    laps = [_pass(z, y) for z, y in ((-0.4, 3.00), (0.1, 3.06), (0.5, 2.95), (-0.2, 3.04))]
    r = reconstruct_surface(laps, bins=50)
    assert all(w is None for w in r["width"])
    assert all(b is None for b in r["bank"])
    assert r["center"] and r["left"] and r["right"]     # the corridor is still drawn


_TH = np.linspace(0.0, 2 * np.pi, 1200, endpoint=False)   # a CLOSED 2.5 km loop, like a
_R = 400.0                                                # real track: a straight wraps its
                                                          # ends into a false seam, and half
                                                          # a lap must exceed the 1 km a
                                                          # spread has to be sustained over


def _ring_lap(off, y=None):
    # One lap of a radius-400 circle at lateral offset `off` (scalar or per-sample).
    r = _R + np.broadcast_to(np.asarray(off, dtype=float), _TH.shape)
    ch = {"pos_x": r * np.cos(_TH), "pos_z": r * np.sin(_TH), "lap_distance": _TH * _R}
    if y is not None:
        ch["pos_y"] = np.broadcast_to(np.asarray(y, dtype=float), _TH.shape)
    return ch


def test_edge_lap_bins_estimate_racing_line_bins_do_not():
    # One cloud, both regimes -- the shape of the Spa edge-lap session: edge laps hug
    # +-5 m over the first half, then all three laps share one line (jittering ~0.3 m,
    # pos_y a few cm apart) over the second. Cross-slope 0.05 m/m. Magnitude only: the
    # sign convention is pinned by the #58 tests below.
    first = _TH < np.pi

    def lap(edge, jitter, dy):
        off = np.where(first, edge, jitter)
        return _ring_lap(off, 0.05 * off + np.where(first, 0.0, dy))

    r = reconstruct_surface([lap(5.0, 0.3, 0.04), lap(-5.0, -0.3, -0.03), lap(0.0, 0.0, 0.0)],
                            bins=120)
    for i in range(15, 45):                              # well inside the edge-lap half
        assert abs(r["width"][i] - 10.0) < 1.0
        assert abs(abs(r["bank"][i]) - np.arctan(0.05)) < 0.01
    for i in range(75, 105):                             # well inside the one-line half
        assert r["width"][i] is None and r["bank"][i] is None


def test_width_needs_the_edges_to_span_it_not_just_one_stray_lap():
    # Real Algarve racing laps: the two BOUND laps (edges) sat ~2 m apart, and a third lap
    # running 7 m wide over a stretch lent those bins a spread the edges never had --
    # 97 bins passed on spread alone with a median width of 1.8 m.
    wide = (_TH > 1.5) & (_TH < 1.9)
    r = reconstruct_surface([_ring_lap(1.0), _ring_lap(-1.0), _ring_lap(np.where(wide, 7.0, 0.0))],
                            bins=120)
    assert np.all(_sep(r) < 3.0)                         # the bound-lap corridor is ~2 m
    assert all(w is None for w in r["width"])            # and never written as a width


def test_one_lap_straying_wide_is_not_a_width_even_over_hundreds_of_metres():
    # Real Fuji Speedway, 17 racing laps and no edge laps: every stretch the per-bin gate
    # let through was LOCAL. The longest ran 236 m, where one qualifying lap (7254) sat
    # 24.8 m off the pack, and the save baked "width 26m, 2 deg banked" into two corners.
    # Deliberate edge laps and mapping captures span the width for the whole lap instead
    # (Spa, Laguna, Algarve: runs of 2311 m and up). Fixture: that exact excursion.
    wide = (_TH > 1.0) & (_TH < 1.0 + 236.0 / _R)
    laps = [_ring_lap(o, 3.0 + dy) for o, dy in ((-0.4, 0.0), (0.1, 0.06), (0.5, -0.05), (-0.2, 0.04))]
    laps.append(_ring_lap(np.where(wide, 24.8, 0.0), np.where(wide, 2.0, 3.0)))
    r = reconstruct_surface(laps, bins=120)
    assert all(w is None for w in r["width"])
    assert all(b is None for b in r["bank"])


def test_sustained_spread_run_is_measured_around_the_closed_loop():
    # A run through the start/finish line is ONE run: an edge-lap stretch straddling the
    # line must not be split into two short pieces and thrown away.
    from simcoach.ribbon import _sustained
    seam = np.zeros(100, dtype=bool)
    seam[:30] = True
    seam[-30:] = True                                    # 60 bins, through S/F
    assert _sustained(seam, 50).tolist() == seam.tolist()
    mid = np.zeros(100, dtype=bool)
    mid[40:70] = True                                    # 30 bins, mid-lap
    assert not _sustained(mid, 50).any()
    assert _sustained(np.ones(100, dtype=bool), 500).all()   # a whole lap always counts


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
    assert abs(_sep(r)[25] - 10.0) < 1.0      # mid-lap full corridor (a 400 m loop is too
                                              # short to sustain a WIDTH; the edges hold)
    # The stitch is of the EDGES (the seam's samples really converge, so its width is
    # gated to unknown). Smoothing alone already lifts the pinch to ~6.5 m on this
    # fixture, so the bound has to sit above that: stitched 8.3, unstitched 6.48. The old
    # "> 5.0" held either way and never tested the stitch.
    sep = _sep(r)
    assert sep[0] > 7.5
    assert sep[-1] > 7.5


def test_reconstruct_surface_keeps_narrow_midlap_corner():
    # Passes spread on the straights but converge to one line in a mid-lap band
    # (a tight corner). That bin is narrow but WELL-SAMPLED and NOT at the seam,
    # so it must stay narrow -- not stitched/widened (that would flatten the apex).
    def pass_(off, n=400):
        x = np.linspace(0.0, 400.0, n)
        z = np.where((x >= 180) & (x <= 220), 0.0, off)  # everyone takes the same corner line
        return {"pos_x": x, "pos_z": z, "lap_distance": x.copy()}

    r = reconstruct_surface([pass_(o) for o in (-4.0, -1.0, 2.0, 5.0)], bins=50)
    # Stitch stays seam-only: the mid narrow bin is NOT widened to the ~9 median.
    # Measured: 0.06x the straight here, 0.70x if the stitch runs everywhere -- so the
    # old "< 0.75x" held either way; 0.25x separates them.
    sep = _sep(r)
    assert sep[25] < 0.25 * sep[10]
    assert sep[10] > 5.0          # a straight bin keeps the full weave spread
    assert r["width"][25] is None  # ...and one line through the corner measures no width


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
    # Two edge-hugging passes at z = +-5 plus a weave that SPIKES past the z=-5
    # bound mid-lap. Edges must come from the BOUND laps (the -5 edge stays ~-5),
    # not the all-sample envelope (which would grab the -8 spike).
    # On a pass along +x, +lateral is +z (#58), so z=-5 is the driver's RIGHT edge
    # and that is the key the spike would leak into.
    x = np.linspace(0.0, 400.0, 400)
    neg = {"pos_x": x, "pos_z": np.full(400, -5.0), "lap_distance": x.copy()}
    pos = {"pos_x": x, "pos_z": np.full(400, 5.0), "lap_distance": x.copy()}
    wz = np.zeros(400); wz[(x >= 200) & (x <= 210)] = -8.0  # ran wide once
    weave = {"pos_x": x, "pos_z": wz, "lap_distance": x.copy()}
    r = reconstruct_surface([neg, weave, pos], bins=100)
    rz = np.array([p[1] for p in r["right"]])[10:-10]
    lz = np.array([p[1] for p in r["left"]])[10:-10]
    # Bound two-sided, so each array has to be the edge it is NAMED for: a
    # one-sided "rz.min() > -6" would also hold for the +5 pass, i.e. it would
    # still pass with the keys swapped. These go red if they are.
    assert -6.0 < rz.min() and rz.max() < -4.0   # ~-5 throughout: bound lap, no -8 spike
    assert 4.0 < lz.min() and lz.max() < 6.0     # ~+5 throughout
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
    # Convention (#58, matching reconstruct_surface): positive = the LEFT edge is
    # higher = banked for a right-hander. Here LEFT is higher -> positive.
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


# --- #58: ribbon handedness --------------------------------------------------
# `_lateral` rotates the tangent +90 as (-tz, tx) and never consults
# RIGHT_IS_POSITIVE, so "+lateral = the right edge" was a naming assumption with
# no test behind it. These pin which physical side it actually lands on, anchored
# to the repo's OWN owned-geometry direction convention (trackmap's
# _LEFT_TURN_IS_CROSS_POSITIVE, via direction_from_path) rather than to a guess.

def _ccw_circle(r=100.0, n=240):
    """A closed circular track traversed counter-clockwise in (x, z) -- a
    continuous LEFT-hander under the repo's direction convention. Returns
    (x, z, dist, theta); the centre is the origin, so the INSIDE of the turn at
    any station is exactly the -radial direction, by construction."""
    th = np.linspace(0.0, 2 * np.pi, n, endpoint=False)
    return r * np.cos(th), r * np.sin(th), th * r, th


def test_lateral_points_left_of_travel_not_right():
    """+lateral is the LEFT side of travel, contradicting the old docstring.

    Construction, so left/right is not a matter of opinion: on a corner that
    direction_from_path calls a LEFT-hander, the inside of the turn is the
    driver's left. The circle's inside is the -radial direction, so dotting
    `_lateral` with -radial settles it. The opposite rotation, (tz, -tx), flips
    every one of these dots negative -- this is not a round-trip.
    """
    from simcoach.ribbon import _lateral
    from simcoach.trackmap import direction_from_path

    x, z, dist, th = _ccw_circle()
    path = [[a, b] for a, b in zip(x, z)]
    # Anchor: this traverse IS a left-hander under the repo's convention. If this
    # assert ever fires, the direction convention moved and the rest is void.
    for apex in (0.0, dist[len(dist) // 3], dist[2 * len(dist) // 3]):
        assert direction_from_path(path, dist, apex) == "left"

    lat_x, lat_z = _lateral(x, z)
    inward_x, inward_z = -np.cos(th), -np.sin(th)      # toward the centre = inside = LEFT
    dots = lat_x * inward_x + lat_z * inward_z
    assert np.all(dots > 0.99)                          # +lateral == inside == LEFT
    assert not np.any(dots < 0)                         # ...and never the right edge


def test_left_key_is_the_inside_of_a_cambered_left_hander():
    """The "left" array holds the driver's LEFT edge -- anchored to the
    RIG-VALIDATED banking sign, not to a guess, so the keys and that sign can
    never drift apart again.

    Geometry: a circular track traversed counter-clockwise in (x, z) with the
    surface CAMBERED -- elevation rises with radius, so the physically OUTER edge
    is higher. Two facts then pin the labels with no round-trip:

      1. bank comes out NEGATIVE here. Per 9756b65 (rig-validated on Laguna's
         Corkscrew: T8 left -> -3.9, T9 right -> +4.1) negative means banked FOR
         A LEFT-HANDER. "Banked" means the outer edge is higher, which is exactly
         how this surface is built -- so this traverse is a left-hander.
      2. The inside of a left-hander is the driver's LEFT, and the inside here is
         the smaller radius, by construction.

    => out["left"] must be the INNER edge. This is a two-sided tripwire: under
    the labelling that shipped before #58 the inner edge comes back keyed "right"
    and the radius asserts go red, while flipping _lateral's rotation to
    (tz, -tx) instead makes bank positive and the first assert go red. Neither
    convention can be changed alone.
    """
    r_in, r_out = 95.0, 105.0
    _, _, dist, th = _ccw_circle()
    slope = 0.10  # elevation per metre of radius -> outer edge ~1 m higher

    def lap(r):
        return {"pos_x": r * np.cos(th), "pos_z": r * np.sin(th),
                "pos_y": np.full(len(th), slope * r), "lap_distance": dist}

    out = reconstruct_surface([lap(r_in), lap(r_out)], bins=120)
    mid = len(out["left"]) // 2

    def rad(p):
        return float(np.hypot(p[0], p[1]))

    # 1. Outer-higher camber on this traverse reads as a banked LEFT-hander.
    assert out["bank"][mid] < -0.05
    # 2. ...so the inside (smaller radius) is the driver's left.
    assert rad(out["left"][mid]) < rad(out["right"][mid])
    assert abs(rad(out["left"][mid]) - r_in) < 2.0
    assert abs(rad(out["right"][mid]) - r_out) < 2.0


def test_left_key_is_the_inside_in_the_envelope_fallback():
    """Same relabel, the OTHER branch. Two passes that each spend half the lap on
    each side have equal mean offset, so they can't be classified into bound laps
    and the edges come from the lateral ENVELOPE instead. +lateral is the driver's
    left, so the most-POSITIVE offset per bin is the LEFT edge -- the inside of
    this counter-clockwise (left-hand) circle, i.e. the smaller radius. Sampled
    well away from the two radius steps, which are the only ambiguous stations."""
    r_in, r_out = 95.0, 105.0
    th = np.linspace(0.0, 2 * np.pi, 600, endpoint=False)
    dist = th * 100.0
    first_half = th < np.pi

    def lap(inner_first):
        r = np.where(first_half == inner_first, r_in, r_out)
        return {"pos_x": r * np.cos(th), "pos_z": r * np.sin(th), "lap_distance": dist}

    out = reconstruct_surface([lap(True), lap(False)], bins=120)

    def rad(p):
        return float(np.hypot(p[0], p[1]))

    for i in (30, 90):  # quarter and three-quarter distance; steps sit at 0 and 60
        assert rad(out["left"][i]) < rad(out["right"][i])
        assert abs(rad(out["left"][i]) - r_in) < 2.0
        assert abs(rad(out["right"][i]) - r_out) < 2.0
