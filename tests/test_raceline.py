import numpy as np

from simcoach.raceline import (
    CarParams,
    min_curvature_line,
    speed_profile,
    solve_corners,
)


# --- synthetic corridors (world coords, closed loop, paired by station) ---

def _straight(n=40, length=400.0, halfwidth=5.0):
    x = np.linspace(0.0, length, n, endpoint=False)
    left = np.column_stack([x, np.full(n, halfwidth)])
    right = np.column_stack([x, np.full(n, -halfwidth)])
    return left, right


def _annulus(n=180, r_in=90.0, r_out=110.0):
    th = np.linspace(0.0, 2 * np.pi, n, endpoint=False)
    inner = np.column_stack([r_in * np.cos(th), r_in * np.sin(th)])
    outer = np.column_stack([r_out * np.cos(th), r_out * np.sin(th)])
    return inner, outer  # left = inner edge, right = outer edge


def _stadium_edges(L=200.0, R=50.0, half_w=6.0, nb=40, na=40):
    """Closed stadium corridor: two straights joined by two semicircle ends."""
    xb = np.linspace(0.0, L, nb, endpoint=False)
    bottom = np.column_stack([xb, np.full(nb, -R)])
    thr = np.linspace(-np.pi / 2, np.pi / 2, na, endpoint=False)
    right = np.column_stack([L + R * np.cos(thr), R * np.sin(thr)])
    xt = np.linspace(L, 0.0, nb, endpoint=False)
    top = np.column_stack([xt, np.full(nb, R)])
    thl = np.linspace(np.pi / 2, 3 * np.pi / 2, na, endpoint=False)
    left_arc = np.column_stack([R * np.cos(thl), R * np.sin(thl)])
    center = np.vstack([bottom, right, top, left_arc])

    t = np.roll(center, -1, axis=0) - np.roll(center, 1, axis=0)
    t /= np.linalg.norm(t, axis=1, keepdims=True)
    normal = np.column_stack([-t[:, 1], t[:, 0]])  # left-hand unit normal
    return center + half_w * normal, center - half_w * normal, center


# --- tests ---

def test_straight_corridor_centered_and_uncapped():
    left, right = _straight()
    alpha, line = min_curvature_line(left, right)
    # interior (away from the closed-loop seam) stays on the centerline
    assert np.allclose(alpha[10:30], 0.5, atol=0.05)

    _, kappa = speed_profile(line, 1.5, 9.81, 14.0, 9.0)
    assert np.all(kappa[10:30] < 1e-6)
    # cornering cap is effectively unlimited where the line is straight
    v_cap = np.sqrt(1.5 * 9.81 / np.maximum(kappa, 1e-9))
    assert v_cap[20] > 1e3


def test_arc_hugs_inside_with_constant_corner_speed():
    left, right = _annulus()  # left = inner edge
    alpha, line = min_curvature_line(left, right, iterations=2000)
    # curve-shortening pulls the band toward the inside (left, alpha->0)
    assert alpha.mean() < 0.4

    v, kappa = speed_profile(line, 1.5, 9.81, 14.0, 9.0)
    # a full constant-radius loop -> ~constant corner speed = sqrt(mu*g*r)
    assert np.std(v) / np.mean(v) < 0.05
    r_line = 1.0 / np.mean(kappa)
    v_expected = np.sqrt(1.5 * 9.81 * r_line)
    assert abs(np.mean(v) - v_expected) < 0.1 * v_expected


def test_solve_corners_finds_the_two_arcs():
    left, right, _ = _stadium_edges()
    corners = solve_corners(left, right)
    assert len(corners) == 2

    car = CarParams()
    v_ref = np.sqrt(car.mu * car.g * 50.0)  # centerline arc speed, m/s
    total = 0.0  # sanity: distinct, in-range windows
    for c in corners:
        assert 0.5 * v_ref < c["min_speed"] < 1.7 * v_ref
        assert c["start_dist"] != c["end_dist"]
        assert c["min_speed"] > 0.0


def test_telem_calibration_raises_corner_speed_cap():
    left, right, _ = _stadium_edges()
    base = solve_corners(left, right)  # defaults: mu = 1.5
    # telemetry showing more grip than the GT3 default
    telem = {
        "lat_g": np.array([2.2, -2.2, 0.4, np.nan]),
        "long_g": np.array([1.5, -1.8, 0.0]),
        "speed": np.array([200.0, 100.0, 150.0]),
    }
    hot = solve_corners(left, right, telem=telem)

    assert min(c["min_speed"] for c in hot) > min(c["min_speed"] for c in base)
